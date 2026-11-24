"""Deal detection, duplicate prevention and the manual-approval publishing workflow.

pending  --approve-->  approved  --publish-->  published
   \\--reject--> rejected               \\--(webhook error)--> failed --publish--> ...

* A deal alert is created only for verified, in-stock offers.
* Duplicates are prevented by a unique fingerprint (offer + kind + price) and a
  cooldown: the same offer is not alerted again within ``cooldown_days`` unless
  the price dropped further by ``min_further_drop_pct``.
* Nothing is published without approval unless you explicitly pass
  ``auto_approve=True`` (``--auto-approve``).
* A published alert can never be published again.
"""

from __future__ import annotations

import json
import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .content import T, build_content, short_date
from .db import Database
from .history import (check_discount_claim, detect_price_change, detect_price_drop, is_lowest_in_90d, lowest_since,
                      stable_previous_price, stats_for_offer)
from .models import now_iso
from .webhook import PublishError, send_event

log = logging.getLogger(__name__)


@dataclass
class DealRules:
    min_deal_score: float = 70.0
    min_affiliate_score: Optional[float] = None
    price_drop_pct: float = 10.0
    cooldown_days: float = 7.0
    min_further_drop_pct: float = 3.0
    seen_within_hours: float = 36.0
    language: str = "he"
    disclosure: Optional[str] = None
    # A deal must save at least this much vs the store's own earlier price or vs other stores.
    # A high Deal Score alone (e.g. a gift card at face value) is not a deal.
    min_savings_pct: float = 5.0
    skip_vague_titles: bool = True
    # products never published, whatever the price (owner's rule: no 18+, nothing illegal, no tobacco or drugs).
    # Extra words from automation.json "deals": {"exclude_keywords": [...]} are added to these.
    exclude_keywords: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "DealRules":
        d = d or {}
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


_GONE = ("OutOfStock", "SoldOut", "Discontinued")


def store_prices(db: Database, group_id: Optional[str], currency: str, min_confidence: float = 0.95) -> list[dict]:
    """The cheapest verified, in-stock offer of each STORE (domain) in a match group, cheapest first.

    Several pages of one store (colours, bundles, duplicate listings) count once: comparing a page
    with another page of the same store is not a comparison between stores. Uncertain matches
    are left out so a different product never becomes a "more expensive store".
    """
    if not group_id:
        return []
    rows = db.query("SELECT id, store, domain, price, currency, shipping_cost, url, affiliate_url, last_fetched_at, "
                    "availability, match_confidence FROM offers WHERE group_id=? AND price IS NOT NULL "
                    "AND price_verified=1 AND currency=?", (group_id, currency))
    best: dict[str, dict] = {}
    for r in rows:
        r = dict(r)
        if (r["availability"] or "") in _GONE or (r["match_confidence"] or 1.0) < min_confidence:
            continue
        key = r["domain"] or r["store"] or r["url"]
        if key not in best or r["price"] < best[key]["price"]:
            best[key] = r
    return sorted(best.values(), key=lambda r: r["price"])


def _comparison_for(db: Database, offer: dict) -> Optional[dict]:
    """A light comparison across DIFFERENT stores for the offer's match group (None with fewer than 2)."""
    stores = store_prices(db, offer.get("group_id"), offer["currency"])
    if len(stores) < 2:
        return None
    prices = [r["price"] for r in stores]
    mid = len(prices) // 2
    median = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
    others = [r["price"] for r in stores if r["domain"] != offer.get("domain")]
    return {"n_compared": len(prices), "median_price": round(median, 2), "uncertain": False,
            "partial_reasons": "", "cheapest_other": min(others) if others else None}


# whole words / phrases (Hebrew prefixes ה ו ב ל מ ש כ allowed), so "בירה" does not hit "מבירה" but "הבירה" does
EXCLUDED_DEFAULT = (
    # tobacco, vaping, hookah
    "סיגריה", "סיגריות", "סיגריה אלקטרונית", "טבק", "וייפ", "וייפים", "ווייפ", "vape", "vapes", "e-cigarette",
    "cigarette", "cigarettes", "tobacco", "נרגילה", "נרגילות", "hookah", "shisha", "iqos", "אייקוס",
    # drugs and cannabis
    "קנאביס", "מריחואנה", "cannabis", "marijuana", "thc", "cbd", "גראס", "grinder for weed",
    # alcohol
    "אלכוהול", "וודקה", "ויסקי", "וויסקי", "יין", "יינות", "בירה", "בירות", "ערק", "ליקר", "vodka", "whisky",
    "whiskey", "wine", "beer", "liquor", "alcohol",
    # adult
    "18+", "ויברטור", "סקס", "אירוטי", "אירוטית", "צעצוע מין", "vibrator", "sex toy", "erotic", "adult toy",
    # weapons that are not tools
    "אקדח אוויר", "רובה אוויר", "airsoft gun", "taser", "טייזר", "אגרופן",
)


def _excluded_re(extra) -> re.Pattern:
    words = sorted({w.strip().lower() for w in (*EXCLUDED_DEFAULT, *(extra or ())) if str(w).strip()},
                   key=len, reverse=True)
    alt = "|".join(re.escape(w) for w in words)
    return re.compile(rf"(?<![^\W\d_])[הובלמשכ]?(?:{alt})(?![^\W\d_])", re.I | re.U)


def excluded_reason(title: str, extra=None) -> Optional[str]:
    """The forbidden word found in the title, or None."""
    m = _excluded_re(extra).search((title or "").lower())
    return m.group(0) if m else None


_WORD = re.compile(r"[^\W_]{2,}", re.UNICODE)


def is_vague_title(title: Optional[str]) -> bool:
    """True for titles too generic to identify a product (e.g. "כרטיס זכרון").

    A title is specific enough if it has a number (capacity, model, size), a Latin word
    (usually a brand or model), or at least three words.
    """
    words = _WORD.findall(title or "")
    if not words:
        return True
    if re.search(r"\d", title or "") or any(re.search(r"[A-Za-z]", w) for w in words):
        return False
    return len(words) < 3


def savings_evidence(st, price: float, comparison: Optional[dict]) -> Optional[dict]:
    """Best verified saving: vs the store's own earlier price, or vs the median of other stores."""
    best = None
    ref = (st.prior_median_90d or st.previous_price) if st.sufficient else stable_previous_price(st)
    if ref:
        if ref > price:
            best = {"basis": "history", "reference": ref, "pct": round((ref - price) / ref * 100, 1)}
    # vs the cheapest OTHER store - a conservative figure, never vs another page of the same store
    other = (comparison or {}).get("cheapest_other")
    if other and not comparison.get("uncertain") and other > price:
        pct = round((other - price) / other * 100, 1)
        if best is None or pct > best["pct"]:
            best = {"basis": "market", "reference": other, "pct": pct}
    return best


def detect_deals(db: Database, rules: DealRules, now: Optional[datetime] = None) -> list[dict]:
    """Create pending alerts for qualifying offers. Returns the newly created alerts."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=rules.seen_within_hours)).replace(microsecond=0).isoformat()
    offers = [dict(r) for r in db.query(
        "SELECT * FROM offers WHERE last_seen >= ? AND price_verified=1 AND price IS NOT NULL "
        "AND availability IN ('InStock','LimitedAvailability','OnlineOnly','InStoreOnly')", (since,))]
    created = []
    for o in offers:
        st = stats_for_offer(db, o["id"], now=now)
        drop = detect_price_drop(st, rules.price_drop_pct)
        lowest = is_lowest_in_90d(st)
        kind = None
        if not drop:
            drop = detect_price_change(st, rules.price_drop_pct)     # short history, stable previous price
        if drop:
            kind = drop.get("kind", "price_drop")
        elif lowest:
            kind = "lowest_90d"
        elif o["deal_score"] is not None and o["deal_score"] >= rules.min_deal_score:
            kind = "top_deal"
        if kind is None:
            continue
        if rules.min_affiliate_score is not None and (o["affiliate_score"] or 0) < rules.min_affiliate_score:
            continue
        if rules.skip_vague_titles and is_vague_title(o["title"]):
            continue
        if excluded_reason(o["title"], rules.exclude_keywords) or excluded_reason(o.get("category") or "",
                                                                                rules.exclude_keywords):
            continue
        comparison = _comparison_for(db, o)
        saving = savings_evidence(st, o["price"], comparison)
        if saving is None or saving["pct"] < rules.min_savings_pct:
            continue
        if _is_duplicate(db, o, kind, rules, now):
            continue
        disc = check_discount_claim(st, o["price"], o["list_price"])
        content = build_content(o, {"lowest_90d": lowest, "lowest_since": lowest_since(st)}, comparison, drop, disc,
                                lang=rules.language, disclosure=rules.disclosure)
        fp = f"{o['id']}:{kind}:{o['price']:.2f}:{o['currency']}"
        with db.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO deal_alerts(offer_id, kind, fingerprint, price, currency, deal_score, "
                            "affiliate_score, title, content_json, status, created_at, savings_pct) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (o["id"], kind, fp, o["price"], o["currency"], o["deal_score"], o["affiliate_score"],
                             o["title"], json.dumps(content, ensure_ascii=False), "pending",
                             now.replace(microsecond=0).isoformat(), saving["pct"]))
            if cur.rowcount:
                created.append({"id": cur.lastrowid, "offer_id": o["id"], "kind": kind, "title": o["title"],
                                "price": o["price"], "currency": o["currency"], "deal_score": o["deal_score"],
                                "affiliate_score": o["affiliate_score"], "url": o["url"]})
    return created


def _colorless(title: str) -> str:
    """The title without colour words: 'Sonos Beam (Gen 2) שחור' and '... לבן' are one product for the channel."""
    from .attributes import _color_re
    return re.sub(r"[\s\-|,/()]+", " ", _color_re.sub(" ", (title or "").lower())).strip()


def _sibling_offer_ids(db: Database, offer: dict) -> list[int]:
    """The same product at the same store on another page (another colour, or a second link to it)."""
    ids = {offer["id"]}
    if offer.get("group_id"):
        ids |= {r["id"] for r in db.query("SELECT id FROM offers WHERE group_id=? AND domain=?",
                                         (offer["group_id"], offer.get("domain")))}
    key = _colorless(offer.get("title") or "")
    if key:
        ids |= {r["id"] for r in db.query("SELECT id, title FROM offers WHERE domain=? AND id<>?",
                                         (offer.get("domain"), offer["id"])) if _colorless(r["title"]) == key}
    return sorted(ids)


def _is_duplicate(db: Database, offer: dict, kind: str, rules: DealRules, now: datetime) -> bool:
    since = (now - timedelta(days=rules.cooldown_days)).replace(microsecond=0).isoformat()
    ids = _sibling_offer_ids(db, offer)
    marks = ",".join("?" * len(ids))
    rows = db.query(f"SELECT price FROM deal_alerts WHERE offer_id IN ({marks}) AND currency=? AND created_at >= ? "
                    "AND status <> 'rejected'", (*ids, offer.get("currency"), since))
    if not rows:
        rejected_same = db.query("SELECT 1 FROM deal_alerts WHERE offer_id=? AND status='rejected' AND price<=?",
                                 (offer["id"], offer["price"]))
        return bool(rejected_same)  # you already said no at this price or lower
    best_previous = min(r["price"] for r in rows)
    return offer["price"] > best_previous * (1 - rules.min_further_drop_pct / 100)


def list_alerts(db: Database, status: Optional[str] = None, limit: int = 100) -> list[dict]:
    sql = ("SELECT a.*, o.url, o.affiliate_url, o.store FROM deal_alerts a JOIN offers o ON o.id=a.offer_id")
    params: list = []
    if status:
        sql += " WHERE a.status=?"
        params.append(status)
    rows = db.query(sql + " ORDER BY a.id DESC LIMIT ?", (*params, limit))
    return [dict(r) for r in rows]


def decide(db: Database, alert_ids: list[int], approve: bool) -> int:
    new = "approved" if approve else "rejected"
    n = 0
    with db.tx() as c:
        for aid in alert_ids:
            cur = c.execute("UPDATE deal_alerts SET status=?, decided_at=? WHERE id=? AND status IN ('pending','failed')",
                            (new, now_iso(), aid))
            n += cur.rowcount
    return n


def _sender(target, session=None):
    """``target``: webhook URL (str) or a callable(event, data) such as TelegramPublisher.send_event."""
    if callable(target):
        return target
    return lambda event, data: send_event(target, event, data, session=session)


def publish_approved(db: Database, url, session=None, auto_approve: bool = False, limit: int = 20,
                     page_url_for=None) -> dict:
    """Send approved alerts as ``deal.publish`` events to a webhook URL or a publisher
    (e.g. Telegram). Each alert is published at most once."""
    send = _sender(url, session)
    if auto_approve:
        with db.tx() as c:
            c.execute("UPDATE deal_alerts SET status='approved', decided_at=? WHERE status='pending'", (now_iso(),))
    rows = db.query("SELECT a.*, o.url, o.affiliate_url, o.store, o.image FROM deal_alerts a JOIN offers o "
                    "ON o.id=a.offer_id WHERE a.status IN ('approved','failed') AND a.decided_at IS NOT NULL "
                    "ORDER BY a.id LIMIT ?", (limit,))
    sent, failed = 0, 0
    published_ids: list[int] = []
    for r in rows:
        # claim the row first so a concurrent run cannot publish it twice
        with db.tx() as c:
            claimed = c.execute("UPDATE deal_alerts SET status='publishing' WHERE id=? AND status IN ('approved','failed')",
                                (r["id"],)).rowcount
        if not claimed:
            continue
        payload = {"alert_id": r["id"], "kind": r["kind"], "title": r["title"], "price": r["price"],
                   "currency": r["currency"], "deal_score": r["deal_score"], "affiliate_score": r["affiliate_score"],
                   "store": r["store"], "url": r["url"], "affiliate_url": r["affiliate_url"], "image": r["image"],
                   "content": _recheck_claims(db, r["offer_id"], json.loads(r["content_json"] or "{}")),
                   "page_url": page_url_for(r["id"]) if page_url_for else ""}
        try:
            ref = send("deal.publish", payload)
            with db.tx() as c:
                c.execute("UPDATE deal_alerts SET status='published', published_at=?, error=NULL, published_ref=? "
                          "WHERE id=?", (now_iso(), json.dumps(ref) if isinstance(ref, dict) else None, r["id"]))
            sent += 1
            published_ids.append(r["id"])
        except PublishError as exc:
            with db.tx() as c:
                c.execute("UPDATE deal_alerts SET status='failed', error=? WHERE id=?", (str(exc)[:300], r["id"]))
            failed += 1
    return {"published": sent, "failed": failed, "ids": published_ids}


def posts_to_fix(db: Database, page_url_for=None) -> list[dict]:
    """Published deals whose channel post says "lowest in 90 days" without a full 90-day history (posted before
    that rule was fixed): [{id, title, ref, payload}] with the corrected content, for TelegramPublisher.edit_deal."""
    out = []
    for r in db.query("SELECT a.*, o.url, o.affiliate_url, o.store, o.image FROM deal_alerts a JOIN offers o "
                      "ON o.id=a.offer_id WHERE a.status='published' AND a.published_ref IS NOT NULL ORDER BY a.id"):
        content = json.loads(r["content_json"] or "{}")
        fixed = _recheck_claims(db, r["offer_id"], content)
        if fixed == content:
            continue
        tg_ref = (json.loads(r["published_ref"] or "null") or {}).get("telegram") or {}
        if not tg_ref.get("message_id"):
            continue
        out.append({"id": r["id"], "title": r["title"], "ref": tg_ref, "content": fixed,
                    "payload": {"title": r["title"], "url": r["url"], "affiliate_url": r["affiliate_url"],
                                "image": r["image"], "content": fixed,
                                "page_url": page_url_for(r["id"]) if page_url_for else ""}})
    return out


def _changed(a: float, b: float) -> bool:
    """A real price change: at least 1% and at least ₪1 (no post rewritten for agorot)."""
    return abs(a - b) >= max(1.0, abs(b) * 0.01)


def post_updates(db: Database, page_url_for=None, seen_within_hours: float = 36.0,
                 now: Optional[datetime] = None) -> list[dict]:
    """Published channel posts whose product changed since the post said it: the price went up (the deal is over),
    went down, the product is out of stock - or it is back at the posted price (the note goes). Only from a price
    we checked lately (verified, fetched within ``seen_within_hours``); a store we could not read changes nothing.
    -> [{id, title, ref, state, price, note, payload}] for TelegramPublisher.edit_deal; each post once per change
    (shown_price / shown_state remember what the post says now)."""
    from .content import israel_time, short_money
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=seen_within_hours)).isoformat()
    out = []
    for r in db.query("SELECT a.*, o.url, o.affiliate_url, o.store, o.image, o.price AS now_price, "
                      "o.currency AS now_currency, o.price_verified AS now_verified, o.availability, o.last_fetched_at "
                      "FROM deal_alerts a JOIN offers o ON o.id=a.offer_id "
                      "WHERE a.status='published' AND a.published_ref IS NOT NULL ORDER BY a.id"):
        tg_ref = (json.loads(r["published_ref"] or "null") or {}).get("telegram") or {}
        if not tg_ref.get("message_id"):
            continue
        if not (r["now_verified"] and r["now_price"] is not None and r["now_currency"] == r["currency"]
                and (r["last_fetched_at"] or "") >= since):
            continue                                   # not checked lately: say nothing new
        posted, current = float(r["price"]), float(r["now_price"])
        if r["availability"] in _GONE:
            state = "gone"
        elif current > posted and _changed(current, posted):
            state = "up"
        elif current < posted and _changed(current, posted):
            state = "down"
        else:
            state = "ok"
        shown_state = r["shown_state"] or "ok"
        shown = float(r["shown_price"]) if r["shown_price"] is not None else posted
        if state == shown_state and (state in ("ok", "gone") or not _changed(current, shown)):
            continue                                   # the post already says this
        when = israel_time(r["last_fetched_at"])
        money = short_money(current, r["currency"])
        note = {"up": f"⚠️ עדכון {when}: המחיר עלה ל-{money}, כלומר המבצע הסתיים.",
                "down": f"🔄 עדכון {when}: המחיר ירד עוד, ל-{money}.",
                "gone": f"⚠️ עדכון {when}: המוצר אזל במלאי בחנות.",
                "ok": None}[state]
        content = {**json.loads(r["content_json"] or "{}"), "update": note}
        if not note:
            content.pop("update")
        out.append({"id": r["id"], "title": r["title"], "ref": tg_ref, "state": state, "price": current, "note": note,
                    "payload": {"title": r["title"], "url": r["url"], "affiliate_url": r["affiliate_url"],
                                "image": r["image"], "content": content,
                                "page_url": page_url_for(r["id"]) if page_url_for else ""}})
    return out


def mark_post_updated(db: Database, alert_id: int, state: str, price: float, now: Optional[datetime] = None) -> None:
    with db.tx() as c:
        c.execute("UPDATE deal_alerts SET shown_state=?, shown_price=?, post_updated_at=? WHERE id=?",
                  (state, price, (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat(), alert_id))


def _recheck_claims(db: Database, offer_id: int, content: dict) -> dict:
    """A deal approved before the 90-day rule was fixed may still carry "lowest in 90 days": check it again
    against the history at publish time, and say "lowest since <first check>" instead when it is shorter."""
    claims = content.get("claims") or []
    if not any(c.get("basis") == "price_history_90d" for c in claims):
        return content
    st = stats_for_offer(db, offer_id)
    if is_lowest_in_90d(st):
        return content
    since = lowest_since(st)
    lang_he = any("\u0590" <= ch <= "\u05ff" for c in claims for ch in c.get("text", ""))
    fixed = []
    for c in claims:
        if c.get("basis") != "price_history_90d":
            fixed.append(c)
        elif since:
            fixed.append({"text": T["he" if lang_he else "en"]["lowest_since"].format(since=short_date(since)),
                          "basis": "price_history_since"})
    out = {**content, "claims": fixed}
    if content.get("facts"):
        out["facts"] = {**content["facts"], "lowest90": False, "since": since}
    return out


def smart_approve(db: Database, min_savings_pct: float = 15.0, per_store_per_day: int = 2,
                  now: Optional[datetime] = None) -> list[int]:
    """'smart' approval: a pending deal with a verified saving of at least ``min_savings_pct`` is approved
    automatically - the biggest savings first, at most ``per_store_per_day`` automatic ones per store in 24 hours.
    Everything else stays pending for the owner (approval buttons in Telegram). Returns the approved ids."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).replace(microsecond=0).isoformat()
    used: dict[str, int] = {}
    for r in db.query("SELECT o.domain, COUNT(*) n FROM deal_alerts a JOIN offers o ON o.id=a.offer_id "
                      "WHERE a.auto_approved=1 AND a.decided_at >= ? GROUP BY o.domain", (since,)):
        used[r["domain"]] = r["n"]
    rows = db.query("SELECT a.id, o.domain, a.savings_pct FROM deal_alerts a JOIN offers o ON o.id=a.offer_id "
                    "WHERE a.status='pending' AND a.savings_pct >= ? ORDER BY a.savings_pct DESC, a.id",
                    (min_savings_pct,))
    chosen = []
    for r in rows:
        if used.get(r["domain"], 0) >= per_store_per_day:
            continue
        used[r["domain"]] = used.get(r["domain"], 0) + 1
        chosen.append(r["id"])
    if chosen:
        stamp = now.replace(microsecond=0).isoformat()
        with db.tx() as c:
            c.executemany("UPDATE deal_alerts SET status='approved', decided_at=?, auto_approved=1 WHERE id=? "
                          "AND status='pending'", [(stamp, i) for i in chosen])
    return chosen


def notify_pending(db: Database, url, session=None) -> int:
    """Internal notification to YOU (webhook or private Telegram chat): alerts awaiting approval, once each."""
    rows = db.query("SELECT a.id, a.kind, a.title, a.price, a.currency, a.deal_score, a.affiliate_score, o.url "
                    "FROM deal_alerts a JOIN offers o ON o.id=a.offer_id WHERE a.status='pending' AND a.notified_at IS NULL")
    if not rows:
        return 0
    _sender(url, session)("deals.pending_approval", {"count": len(rows), "alerts": [dict(r) for r in rows],
                                                     "approve_with": "py main.py approve <id> | dashboard"})
    with db.tx() as c:
        c.executemany("UPDATE deal_alerts SET notified_at=? WHERE id=?", [(now_iso(), r["id"]) for r in rows])
    return len(rows)


def withdraw(db: Database, alert_ids: list[int], delete_telegram=None) -> list[dict]:
    """Take deals back: they disappear from the website on its next build and, when ``delete_telegram``
    (callable(chat_id, message_id)) is given, the channel post is deleted. Works for any status.
    Returns one result per id: {"id", "ok", "telegram": "deleted" | "not posted" | "<error>"}."""
    results = []
    for aid in alert_ids:
        row = db.query("SELECT id, status, published_ref FROM deal_alerts WHERE id=?", (aid,))
        if not row:
            results.append({"id": aid, "ok": False, "telegram": "no such deal"})
            continue
        r = row[0]
        tg_note = "not posted"
        ref = json.loads(r["published_ref"] or "null") or {}
        t = ref.get("telegram") if isinstance(ref, dict) else None
        if t and delete_telegram:
            try:
                delete_telegram(t["chat_id"], t["message_id"])
                tg_note = "deleted"
            except PublishError as exc:
                tg_note = f"could not delete ({exc}) - delete it in the channel by hand"
        elif t:
            tg_note = "still in the channel (Telegram not configured here) - delete it by hand"
        with db.tx() as c:
            c.execute("UPDATE deal_alerts SET status='withdrawn', decided_at=?, error=? WHERE id=?",
                      (now_iso(), None if tg_note in ("deleted", "not posted") else tg_note[:300], aid))
        results.append({"id": aid, "ok": True, "telegram": tg_note, "was": r["status"]})
    return results

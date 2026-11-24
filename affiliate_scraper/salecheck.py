"""'We checked this sale': a public note when a store's discount does not hold up against the facts.

Two sources, never mixed:
* ``offer``   - a store we check ourselves: it shows "₪P instead of ₪L", and OUR checks of the product page show
  the price was already ₪P (or less) lately - after a rise, or with ₪L never seen at all.
* ``channel`` - a store's own Telegram channel: today "₪P instead of ₪W", but its own earlier posts show ₪P (or
  less), then a higher price, then this "sale". The evidence is the store's posts (links), not our checks.

Defamation is the risk, so the note states facts only - dates, prices, the source - with no word about the
store's intent ("manipulation", "fake", "cheat" never appear). The reader draws the conclusion.
Limits: ``per_day`` notes a day, one store at most once in ``store_days``, one product once in ``item_days``.
Never while the agent manager runs. The owner is told about every note (to delete it if it does not fit).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import Database
from .validation import _parse_iso

DEFAULTS = {"enabled": True, "auto": True, "per_day": 2, "store_days": 7, "item_days": 30,
            "min_claim_pct": 15.0, "min_rise_pct": 15.0, "min_days": 14, "min_obs": 4, "fresh_hours": 36,
            "lookback_days": 60}
SAME = 0.005                    # "the same price": within 0.5%
_FORBIDDEN_WORDS = ("מניפולצ", "רמא", "מזויף", "מזויפ", "הונא", "עוקץ", "תרמית", "fake", "scam")

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_warnings (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,              -- offer | channel
    ref_id INTEGER NOT NULL,           -- offers.id or channel_claims.id
    store TEXT, item TEXT,             -- for the limits: one store a week, one product a month
    text TEXT, link TEXT,
    created_at TEXT, published_at TEXT, published_ref TEXT,
    UNIQUE(source, ref_id)
);
"""


def rules_from(cfg: Optional[dict]) -> dict:
    return {**DEFAULTS, **(cfg or {})}


def ensure_schema(db: Database) -> None:
    db.conn.executescript(SCHEMA)
    db.conn.commit()


def _d(iso: str) -> str:
    """'2026-11-18T...' -> '18.11' (Israel date, summer time too)."""
    from .content import israel_time
    return israel_time(iso).split(",")[0]


def _money(v: float) -> str:
    from .content import short_money
    return short_money(v, "ILS")


def _pct(hi: float, lo: float) -> int:
    return round((hi - lo) / hi * 100) if hi else 0


def _rise_then_back(points: list[tuple[str, float]], price: float, min_rise_pct: float) -> Optional[dict]:
    """In time order: a point at about ``price`` or less, LATER a point at least min_rise_pct higher.
    -> {low_at, low, high_at, high}: the last low before a rise and the FIRST higher price after it, or None."""
    best = None
    for i, (t1, p1) in enumerate(points):
        if p1 > price * (1 + SAME):
            continue
        for t2, p2 in points[i + 1:]:
            if p2 >= price * (1 + min_rise_pct / 100):
                best = {"low_at": t1, "low": p1, "high_at": t2, "high": p2}
                break
    return best


# ------------------------------------------------------------------ source 1: stores we check ourselves
def offer_finding(db: Database, o: dict, rules: Optional[dict] = None,
                  now: Optional[datetime] = None) -> Optional[dict]:
    """One offer: does our own history contradict its shown discount ("₪P instead of ₪L", L = list_price)?
    -> the finding, or None. Also used by detect_deals: a sale we would warn about is never posted as a deal."""
    rules = rules or rules_from(None)
    if not o.get("price_verified") or not o.get("price") or not o.get("list_price") or o["list_price"] <= o["price"]:
        return None
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=float(rules["lookback_days"]))).isoformat()
    price, listed = float(o["price"]), float(o["list_price"])
    claimed = _pct(listed, price)
    if claimed < float(rules["min_claim_pct"]):
        return None
    rows = [r for r in db.history(o["id"], since=since, verified_only=True) if r["currency"] == o["currency"]]
    if len(rows) < int(rules["min_obs"]):
        return None
    first, last = _parse_iso(rows[0]["observed_at"]), _parse_iso(rows[-1]["observed_at"])
    if not first or not last or (last - first).days < int(rules["min_days"]):
        return None
    # the current price period (the trailing checks at about this price) is not its own evidence
    k = len(rows) - 1
    while k > 0 and abs(rows[k - 1]["price"] - price) <= SAME * price:
        k -= 1
    before = [(r["observed_at"], float(r["price"])) for r in rows[:k]]
    pattern = _rise_then_back(before, price, float(rules["min_rise_pct"])) if before else None
    highest = max([p for _, p in before] + [price])
    never_listed = listed > highest * 1.05
    low_before = [(t, p) for t, p in before if p <= price * (1 + SAME)]
    flat = not before                  # the same price in every check: then it is its own evidence
    if pattern:
        low_at, low = pattern["low_at"], pattern["low"]
    elif never_listed and (low_before or flat):
        low_at, low = low_before[-1] if low_before else (rows[0]["observed_at"], float(rows[0]["price"]))
    else:
        return None                    # the price IS lower than before, or nothing contradicts the "was"
    return {"source": "offer", "ref_id": o["id"], "store": o.get("store") or o.get("domain") or "",
            "item": str(o["id"]), "title": o["title"], "price": price, "claimed_was": listed,
            "claimed_pct": claimed, "pattern": pattern, "low_at": low_at, "low": low, "flat": flat,
            "never_listed": never_listed, "highest": highest, "n_checks": len(rows),
            "since": rows[0]["observed_at"], "link": o["url"]}


def _posted_as_deal(db: Database, o: dict, now: datetime) -> bool:
    """We already posted (or approved) this product at this price lately - never contradict our own post."""
    since = (now - timedelta(days=14)).isoformat()
    return bool(db.query("SELECT 1 FROM deal_alerts WHERE offer_id=? AND status IN ('approved','published') "
                         "AND created_at >= ? AND ABS(price - ?) <= ?",
                         (o["id"], since, o["price"], SAME * float(o["price"]))))


def offer_findings(db: Database, rules: dict, now: Optional[datetime] = None) -> list[dict]:
    """The offers (checked in the last ``fresh_hours``) whose shown discount our own checks contradict."""
    from .alerts import _GONE, excluded_reason, is_vague_title
    now = now or datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=float(rules["fresh_hours"]))).isoformat()
    out = []
    for o in db.query("SELECT * FROM offers WHERE price_verified=1 AND price > 0 AND list_price > price "
                      "AND last_fetched_at >= ?", (fresh,)):
        o = dict(o)
        if o.get("availability") in _GONE or excluded_reason(o["title"] or "") or is_vague_title(o["title"]):
            continue
        f = offer_finding(db, o, rules, now)
        if f and not _posted_as_deal(db, o, now):
            out.append(f)
    return out


def offer_text(f: dict) -> str:
    lines = [f"🔍 בדקנו את המבצע: {f['title'][:110]}", "",
             f"{f['store']} מציגה עכשיו {_money(f['price'])} \"במקום {_money(f['claimed_was'])}\" "
             f"({f['claimed_pct']}% הנחה).",
             f"מה ראינו בבדיקות שלנו בדף המוצר ({f['n_checks']} בדיקות מאז {_d(f['since'])}):"]
    if f["pattern"]:
        p = f["pattern"]
        lines.append(f"• ב-{_d(p['low_at'])} המחיר שם היה {_money(p['low'])}, וב-{_d(p['high_at'])} הוא עלה "
                     f"ל-{_money(p['high'])}.")
    elif f.get("flat"):
        lines.append(f"• מאז {_d(f['low_at'])} המחיר שם היה {_money(f['low'])} בכל הבדיקות.")
    else:
        lines.append(f"• ב-{_d(f['low_at'])} המחיר שם כבר היה {_money(f['low'])}.")
    if f["never_listed"]:
        lines.append(f"• את {_money(f['claimed_was'])} לא ראינו שם אף פעם. הכי גבוה שראינו: {_money(f['highest'])}.")
    same = abs(f["price"] - f["low"]) <= SAME * f["price"]
    lines += ["", f"כלומר המחיר היום {'זהה למחיר' if same else 'לא נמוך מהמחיר'} שראינו שם ב-{_d(f['low_at'])}.",
              "אלה העובדות מהבדיקות שלנו, בלי פרשנות."]
    return "\n".join(lines)


# ------------------------------------------------------------------ source 2: the stores' Telegram channels
def channel_findings(db: Database, rules: dict, now: Optional[datetime] = None) -> list[dict]:
    """A store's channel post today: "₪P instead of ₪W" - while its own earlier posts show ₪P or less, then
    a price at least min_rise_pct higher."""
    from .alerts import excluded_reason
    from .channels import ensure_schema as channel_schema, same_product, store_name
    channel_schema(db)
    now = now or datetime.now(timezone.utc)
    recent = (now - timedelta(hours=48)).isoformat()
    out = []
    for c in db.query("SELECT * FROM channel_claims WHERE posted_at >= ? AND was_price > price AND price > 0 "
                      "ORDER BY posted_at", (recent,)):
        c = dict(c)
        if excluded_reason(c["title"] or ""):
            continue
        price, was = float(c["price"]), float(c["was_price"])
        if _pct(was, price) < float(rules["min_claim_pct"]):
            continue
        posted = _parse_iso(c["posted_at"])
        if not posted:
            continue
        start = (posted - timedelta(days=float(rules["lookback_days"]))).isoformat()
        earlier = [dict(r) for r in db.query(
            "SELECT id, price, posted_at, title, post_url FROM channel_claims WHERE channel=? AND posted_at < ? "
            "AND posted_at >= ? AND price > 0 ORDER BY posted_at", (c["channel"], c["posted_at"], start))]
        earlier = [r for r in earlier if same_product(r["title"], c["title"])]
        pattern = _rise_then_back([(r["posted_at"], float(r["price"])) for r in earlier], price,
                                  float(rules["min_rise_pct"]))
        if not pattern:
            continue
        urls = {r["posted_at"]: r["post_url"] for r in earlier}
        out.append({"source": "channel", "ref_id": c["id"], "store": store_name(c["channel"]),
                    "item": c.get("item_key") or c["title"], "title": c["title"], "price": price, "claimed_was": was,
                    "claimed_pct": _pct(was, price), "pattern": pattern, "low_at": pattern["low_at"],
                    "low": pattern["low"], "link": c.get("post_url") or "",
                    "low_url": urls.get(pattern["low_at"], ""), "high_url": urls.get(pattern["high_at"], "")})
    return out


def channel_text(f: dict) -> str:
    p = f["pattern"]
    same = abs(f["price"] - p["low"]) <= SAME * f["price"]
    return "\n".join([
        f"🔍 בדקנו את המבצע (לפי הפוסטים של {f['store']} בטלגרם): {f['title'][:100]}", "",
        f"היום הם מפרסמים {_money(f['price'])} \"במקום {_money(f['claimed_was'])}\" ({f['claimed_pct']}% הנחה).",
        f"• ב-{_d(p['low_at'])} הם פרסמו אותו ב-{_money(p['low'])}.",
        f"• ב-{_d(p['high_at'])} הם פרסמו אותו ב-{_money(p['high'])}.", "",
        f"כלומר המחיר היום {'זהה למחיר' if same else 'לא נמוך מהמחיר'} שהם פרסמו ב-{_d(p['low_at'])}.",
        "המחירים לפי הפוסטים של החנות עצמה. לא בדקנו אותם בדף המוצר."])


def text_for(f: dict) -> str:
    text = offer_text(f) if f["source"] == "offer" else channel_text(f)
    body = text.replace(f["title"][:110], "").replace(f["title"][:100], "")     # a product may be "fake fur"
    if any(w in body.lower() for w in _FORBIDDEN_WORDS):           # facts only, never a word about intent
        raise ValueError("a sale-check note may not say anything about the store's intent")
    return text


# ------------------------------------------------------------------ choosing and publishing
def candidates(db: Database, rules: dict, now: Optional[datetime] = None) -> list[dict]:
    """The findings that may be published now, within the limits. Verified ones first."""
    ensure_schema(db)
    now = now or datetime.now(timezone.utc)
    day = (now - timedelta(hours=24)).isoformat()
    room = int(rules["per_day"]) - db.query("SELECT COUNT(*) n FROM price_warnings WHERE published_at >= ?",
                                            (day,))[0]["n"]
    out: list[dict] = []
    stores: set[str] = set()
    for f in offer_findings(db, rules, now) + channel_findings(db, rules, now):
        if len(out) >= room:
            break
        store_since = (now - timedelta(days=float(rules["store_days"]))).isoformat()
        item_since = (now - timedelta(days=float(rules["item_days"]))).isoformat()
        if f["store"] in stores or db.query("SELECT 1 FROM price_warnings WHERE source=? AND ref_id=?",
                                            (f["source"], f["ref_id"])):
            continue
        if db.query("SELECT 1 FROM price_warnings WHERE store=? AND published_at >= ?", (f["store"], store_since)):
            continue
        if db.query("SELECT 1 FROM price_warnings WHERE source=? AND item=? AND published_at >= ?",
                    (f["source"], f["item"], item_since)):
            continue
        try:
            f["text"] = text_for(f)
        except ValueError:
            continue
        stores.add(f["store"])
        out.append(f)
    return out


def record(db: Database, f: dict, published: bool, ref: Optional[dict] = None,
           now: Optional[datetime] = None) -> int:
    when = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    with db.tx() as c:
        cur = c.execute("INSERT OR IGNORE INTO price_warnings(source, ref_id, store, item, text, link, created_at, "
                        "published_at, published_ref) VALUES (?,?,?,?,?,?,?,?,?)",
                        (f["source"], f["ref_id"], f["store"], f["item"], f["text"], f.get("link") or "", when,
                         when if published else None, json.dumps(ref) if ref else None))
        return cur.lastrowid or 0


def buttons_for(f: dict) -> list[tuple[str, str]]:
    if f["source"] == "offer":
        return [("לדף המוצר בחנות ›", f["link"])] if f.get("link", "").startswith("https://") else []
    out = [("הפוסט של היום ›", f.get("link") or "")]
    if f.get("low_url"):
        out.append((f"הפוסט מ-{_d(f['pattern']['low_at'])} ›", f["low_url"]))
    if f.get("high_url"):
        out.append((f"הפוסט מ-{_d(f['pattern']['high_at'])} ›", f["high_url"]))
    return [(t, u) for t, u in out if u.startswith("https://")]

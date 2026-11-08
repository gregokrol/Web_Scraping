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
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .content import build_content
from .db import Database
from .history import check_discount_claim, detect_price_drop, is_lowest_in_90d, stats_for_offer
from .models import now_iso
from .webhook import WebhookError, send_event

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

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "DealRules":
        d = d or {}
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


def _comparison_for(db: Database, offer: dict) -> Optional[dict]:
    """Rebuild a light comparison for the offer's match group from the latest stored offers."""
    if not offer.get("group_id"):
        return None
    rows = db.query("SELECT price, currency, domain, price_verified, availability, match_confidence FROM offers "
                    "WHERE group_id=? AND price IS NOT NULL", (offer["group_id"],))
    same = [r for r in rows if r["price_verified"] and r["currency"] == offer["currency"]
            and (r["availability"] or "") not in ("OutOfStock", "SoldOut", "Discontinued")]
    if len({r["domain"] for r in same}) < 2:
        return None
    prices = sorted(r["price"] for r in same)
    mid = len(prices) // 2
    median = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
    conf = min((r["match_confidence"] or 1.0) for r in rows)
    return {"n_compared": len(prices), "median_price": round(median, 2), "uncertain": conf < 0.95,
            "partial_reasons": ""}


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
        if drop:
            kind = "price_drop"
        elif lowest:
            kind = "lowest_90d"
        elif o["deal_score"] is not None and o["deal_score"] >= rules.min_deal_score:
            kind = "top_deal"
        if kind is None:
            continue
        if rules.min_affiliate_score is not None and (o["affiliate_score"] or 0) < rules.min_affiliate_score:
            continue
        if _is_duplicate(db, o, kind, rules, now):
            continue
        disc = check_discount_claim(st, o["price"], o["list_price"])
        content = build_content(o, {"lowest_90d": lowest}, _comparison_for(db, o), drop, disc,
                                lang=rules.language, disclosure=rules.disclosure)
        fp = f"{o['id']}:{kind}:{o['price']:.2f}:{o['currency']}"
        with db.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO deal_alerts(offer_id, kind, fingerprint, price, currency, deal_score, "
                            "affiliate_score, title, content_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (o["id"], kind, fp, o["price"], o["currency"], o["deal_score"], o["affiliate_score"],
                             o["title"], json.dumps(content, ensure_ascii=False), "pending",
                             now.replace(microsecond=0).isoformat()))
            if cur.rowcount:
                created.append({"id": cur.lastrowid, "offer_id": o["id"], "kind": kind, "title": o["title"],
                                "price": o["price"], "currency": o["currency"], "deal_score": o["deal_score"],
                                "affiliate_score": o["affiliate_score"], "url": o["url"]})
    return created


def _is_duplicate(db: Database, offer: dict, kind: str, rules: DealRules, now: datetime) -> bool:
    since = (now - timedelta(days=rules.cooldown_days)).replace(microsecond=0).isoformat()
    rows = db.query("SELECT price FROM deal_alerts WHERE offer_id=? AND created_at >= ? AND status <> 'rejected'",
                    (offer["id"], since))
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


def publish_approved(db: Database, url: str, session=None, auto_approve: bool = False, limit: int = 20) -> dict:
    """Send approved alerts as ``deal.publish`` events. Each alert is published at most once."""
    if auto_approve:
        with db.tx() as c:
            c.execute("UPDATE deal_alerts SET status='approved', decided_at=? WHERE status='pending'", (now_iso(),))
    rows = db.query("SELECT a.*, o.url, o.affiliate_url, o.store FROM deal_alerts a JOIN offers o ON o.id=a.offer_id "
                    "WHERE a.status IN ('approved','failed') AND a.decided_at IS NOT NULL ORDER BY a.id LIMIT ?", (limit,))
    sent, failed = 0, 0
    for r in rows:
        # claim the row first so a concurrent run cannot publish it twice
        with db.tx() as c:
            claimed = c.execute("UPDATE deal_alerts SET status='publishing' WHERE id=? AND status IN ('approved','failed')",
                                (r["id"],)).rowcount
        if not claimed:
            continue
        payload = {"alert_id": r["id"], "kind": r["kind"], "title": r["title"], "price": r["price"],
                   "currency": r["currency"], "deal_score": r["deal_score"], "affiliate_score": r["affiliate_score"],
                   "store": r["store"], "url": r["url"], "affiliate_url": r["affiliate_url"],
                   "content": json.loads(r["content_json"] or "{}")}
        try:
            send_event(url, "deal.publish", payload, session=session)
            with db.tx() as c:
                c.execute("UPDATE deal_alerts SET status='published', published_at=?, error=NULL WHERE id=?",
                          (now_iso(), r["id"]))
            sent += 1
        except WebhookError as exc:
            with db.tx() as c:
                c.execute("UPDATE deal_alerts SET status='failed', error=? WHERE id=?", (str(exc)[:300], r["id"]))
            failed += 1
    return {"published": sent, "failed": failed}


def notify_pending(db: Database, url: str, session=None) -> int:
    """Internal notification: list alerts awaiting approval (sent once per alert)."""
    rows = db.query("SELECT a.id, a.kind, a.title, a.price, a.currency, a.deal_score, a.affiliate_score, o.url "
                    "FROM deal_alerts a JOIN offers o ON o.id=a.offer_id WHERE a.status='pending' AND a.notified_at IS NULL")
    if not rows:
        return 0
    send_event(url, "deals.pending_approval", {"count": len(rows), "alerts": [dict(r) for r in rows],
                                               "approve_with": "py main.py approve <id> | dashboard"}, session=session)
    with db.tx() as c:
        c.executemany("UPDATE deal_alerts SET notified_at=? WHERE id=?", [(now_iso(), r["id"]) for r in rows])
    return len(rows)

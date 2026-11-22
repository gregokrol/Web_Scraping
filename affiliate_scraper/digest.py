"""Daily summary to share: "today's best deals" as plain text for WhatsApp / Facebook groups.

Only deals already public (on the channel / website), still active, with a proven saving - the same
facts as the posts, nothing new is claimed. Links go to the deal page on the website when there is one,
so the affiliate link can be switched on later without the shared text going stale.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import Database
from .validation import _parse_iso

SHARE_DISCLOSURE = "הקישורים עשויים להיות קישורי שותפים. המחירים נבדקו במועד הפרסום ועשויים להשתנות."


def pick_deals(db: Database, site_cfg, now: Optional[datetime] = None, count: int = 5, hours: float = 48) -> list[dict]:
    from .site import best_per_product, collect_deals
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    deals = []
    for d in collect_deals(db, site_cfg, now):
        created = _parse_iso(d.get("created_at") or "")
        if d["ended"] or d["stale"] or not created or created < since:
            continue
        if not (d["savings_pct"] or d["lowest90"]):
            continue                                   # the same rule as the website lists
        deals.append(d)
    deals = best_per_product(deals)
    deals.sort(key=lambda d: (-(d["savings_pct"] or 0), -(_parse_iso(d["created_at"]) or now).timestamp()))
    return deals[:count]


def build_digest(db: Database, site_cfg, now: Optional[datetime] = None, count: int = 5,
                 hours: float = 48) -> Optional[str]:
    """Plain text (WhatsApp shows *bold*), or None when there is nothing worth sharing."""
    from .site import _money, page_url
    now = now or datetime.now(timezone.utc)
    deals = pick_deals(db, site_cfg, now, count, hours)
    if not deals:
        return None
    nums = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    head = "העסקה הכי טובה היום" if len(deals) == 1 else f"{len(deals)} העסקאות הכי טובות היום"
    lines = [f"🔥 *{head}* (מחירים שנבדקו)", ""]
    for i, d in enumerate(deals):
        title = (d["title"] or "").strip()
        title = title if len(title) <= 70 else title[:68].rstrip() + "…"
        price = _money(d["price"], d["currency"])
        facts = []
        if d["savings_pct"]:
            facts.append(f"חיסכון {d['savings_pct']}%")
        if d.get("prev_price"):
            facts.append(f"במקום {_money(d['prev_price'], d['currency'])}")
        if d["lowest90"]:
            facts.append("הנמוך ב-90 יום")
        link = page_url(site_cfg, d["id"]) or d.get("affiliate_url") or d.get("url") or ""
        lines.append(f"{nums[i] if i < len(nums) else f'{i + 1}.'} {title}")
        lines.append(f"💰 {price}" + (f" · {' · '.join(facts)}" if facts else "") + f" · {d['store']}")
        if link:
            lines.append(f"👉 {link}")
        lines.append("")
    if site_cfg.telegram_url:
        lines.append(f"📢 כל העסקאות, ברגע שהן מופיעות: {site_cfg.telegram_url}")
    if site_cfg.base_url:
        lines.append(f"🌐 {site_cfg.base_url}")
    lines.append(SHARE_DISCLOSURE)
    return "\n".join(lines)

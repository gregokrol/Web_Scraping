"""Daily self-check: is the automation running, are the stores answering, are the published deals still true?

Everything here only READS (the database, Telegram's getMe/getChat, the public website). It never
changes or removes anything by itself - it tells you what to look at and which deals to withdraw.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import Database
from .validation import _parse_iso

log = logging.getLogger(__name__)

BLOCK_KINDS = {"http_403", "http_429", "robots", "host_skipped", "blocked"}


def _hours_since(ts: Optional[str], now: datetime) -> Optional[float]:
    dt = _parse_iso(ts or "")
    return None if dt is None else (now - dt).total_seconds() / 3600


def run_health(db: Database, cfg: dict, tg=None, now: Optional[datetime] = None, session=None,
               check_site: bool = True) -> dict:
    """Returns {"ok", "problems", "warnings", "info", "withdraw_ids"} - lists of short Hebrew lines."""
    now = now or datetime.now(timezone.utc)
    hcfg = cfg.get("health") or {}
    max_gap = float(hcfg.get("max_hours_without_run", 26))
    problems: list[str] = []
    warnings: list[str] = []
    info: list[str] = []
    withdraw_ids: list[int] = []

    # 1. did the automation run?
    last = db.query("SELECT MAX(started_at) t FROM crawl_runs")[0]["t"]
    age = _hours_since(last, now)
    if age is None:
        problems.append("האוטומציה עוד לא רצה אף פעם (תפריט 6, ואז תזמון בתפריט 11).")
    elif age > max_gap:
        problems.append(f"האוטומציה לא רצה {age:.0f} שעות. בדקו שהמחשב דלוק ושהתזמון קיים (תפריט 11).")
    else:
        info.append(f"ריצה אחרונה: לפני {age:.1f} שעות.")
    stuck = db.query("SELECT COUNT(*) n FROM crawl_runs WHERE status='running' AND started_at < ?",
                     ((now - timedelta(hours=6)).isoformat(),))[0]["n"]
    if stuck:
        warnings.append(f"{stuck} ריצות נתקעו ולא הסתיימו (כנראה המחשב נכבה באמצע).")

    # 2. what the last 24 hours brought
    since = (now - timedelta(hours=24)).isoformat()
    runs = db.query("SELECT products, errors, errors_json FROM crawl_runs WHERE started_at >= ?", (since,))
    products = sum(r["products"] or 0 for r in runs)
    if runs and products == 0:
        problems.append("ב-24 השעות האחרונות לא נמצא אף מוצר. ייתכן שהחנויות שינו את האתר או חוסמות.")
    elif runs:
        info.append(f"24 שעות אחרונות: {len(runs)} ריצות, {products} מוצרים נבדקו.")
    errs: Counter = Counter()
    for r in runs:
        for e in json.loads(r["errors_json"] or "[]"):
            if isinstance(e, dict):
                dom = (e.get("url") or "").split("/")[2] if "://" in (e.get("url") or "") else "?"
                errs[(dom, e.get("kind") or "error")] += 1
    for (dom, kind), n in errs.most_common(6):
        note = " - החנות מגבילה גישה. לא עוקפים: מורידים קצב או מוותרים על החנות" if kind in BLOCK_KINDS else ""
        warnings.append(f"{dom}: {n} שגיאות {kind}{note}")

    # 3. stores that used to answer and went quiet
    rows = db.query("SELECT domain, MAX(last_seen) t FROM offers WHERE domain IS NOT NULL GROUP BY domain")
    for r in rows:
        h = _hours_since(r["t"], now)
        if h is not None and 48 < h <= 30 * 24:
            warnings.append(f"{r['domain']}: לא נמצאו מוצרים כבר {h / 24:.0f} ימים.")

    # 4. are the published deals still true?
    from .site import SiteConfig, collect_deals
    site_cfg = SiteConfig.from_dict(cfg.get("site"))
    deals = collect_deals(db, site_cfg, now)
    live = [d for d in deals if d["status"] == "published"]
    for d in live:
        title = (d["title"] or "")[:45]
        if d["ended"]:
            withdraw_ids.append(d["id"])
            warnings.append(f"#{d['id']} {title}: המחיר עלה ל-{d['current_price']} - העסקה הסתיימה.")
        elif not (d["savings_pct"] or d["lowest90"]):
            withdraw_ids.append(d["id"])
            warnings.append(f"#{d['id']} {title}: אין חיסכון מוכח מול מחיר קודם או חנות אחרת.")
        elif d["stale"]:
            warnings.append(f"#{d['id']} {title}: המחיר לא נבדק יותר מ-{site_cfg.stale_hours} שעות.")
    info.append(f"עסקאות שפורסמו: {len(live)}.")

    # 5. Telegram
    if tg is not None and getattr(tg, "configured", False):
        try:
            t = tg.check()
            if t.get("bot_is_admin") is False:
                problems.append("הבוט כבר לא מנהל בערוץ, ולכן לא יוכל לפרסם. הוסיפו אותו שוב כמנהל.")
            else:
                info.append(f"טלגרם תקין: {t.get('channel') or ''}")
        except Exception as exc:  # noqa: BLE001 - any failure is the finding
            problems.append(f"בעיה בחיבור לטלגרם: {str(exc)[:150]}")

    # 6. the public website
    base = site_cfg.base_url if (cfg.get("site") or {}).get("enabled") else ""
    if check_site and base.startswith("https://"):
        import requests
        try:
            resp = (session or requests).get(base, timeout=20)
            if resp.status_code == 200:
                info.append("האתר זמין.")
            else:
                problems.append(f"האתר החזיר שגיאה {resp.status_code}: {base}")
        except requests.RequestException as exc:
            problems.append(f"האתר לא זמין: {type(exc).__name__}")

    return {"ok": not problems, "problems": problems, "warnings": warnings, "info": info,
            "withdraw_ids": withdraw_ids}


def format_report(res: dict) -> str:
    n_p, n_w = len(res["problems"]), len(res["warnings"])
    head = ("✅ בדיקה יומית: הכול תקין" if not n_p and not n_w else
            f"🩺 בדיקה יומית: {n_p} בעיות, {n_w} דברים לבדוק")
    lines = [head]
    if res["problems"]:
        lines += ["", "❌ בעיות:", *[f"• {x}" for x in res["problems"]]]
    if res["warnings"]:
        lines += ["", "⚠️ לבדוק:", *[f"• {x}" for x in res["warnings"]]]
    if res["info"]:
        lines += ["", *[f"ℹ️ {x}" for x in res["info"]]]
    if res["withdraw_ids"]:
        ids = " ".join(str(i) for i in res["withdraw_ids"])
        lines += ["", f"להסרה מהערוץ ומהאתר: py main.py withdraw {ids}  (או תפריט 24)"]
    return "\n".join(lines)


def due(db: Database, now: Optional[datetime] = None, every_hours: float = 20) -> bool:
    """True if the daily report was not sent in the last ``every_hours`` (twice-daily runs -> one report)."""
    now = now or datetime.now(timezone.utc)
    row = db.query("SELECT value FROM meta WHERE key='health_sent_at'")
    h = _hours_since(row[0]["value"] if row else None, now)
    return h is None or h >= every_hours


def mark_sent(db: Database, now: Optional[datetime] = None) -> None:
    now = now or datetime.now(timezone.utc)
    with db.tx() as c:
        c.execute("INSERT INTO meta(key, value) VALUES ('health_sent_at', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now.isoformat(),))

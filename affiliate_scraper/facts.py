"""The system's facts for marketing and reports, counted live from the data - so a post never says an old number.

`py main.py facts` (the agents run it before writing any number about the system; `--subscribers` also asks
Telegram how many people the channel has and remembers one count a day, so growth can be measured).
Why: on 25.11.2026 an Instagram draft said "25 sale pages in 12 stores", copied from STATUS.md, while the
system scanned 27 pages in 13 stores. STATUS.md does not keep these numbers any more.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .db import Database

RUN_GAP = timedelta(hours=1)    # scrapes closer than this belong to one run
RUN_MIN_SCRAPES = 5             # a full run reads many pages; a manual check or a single search does not count
SUBSCRIBERS_KEY = "facts_subscribers"   # meta: {"YYYY-MM-DD": count}, one count a day, the last 120 days


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _count(db: Database, sql: str, params: tuple = ()) -> int:
    try:
        return int(db.query(sql, params)[0][0] or 0)
    except Exception:  # noqa: BLE001 - a table that does not exist yet (e.g. price_warnings) is 0
        return 0


def scan_runs(db: Database, since: datetime) -> list[str]:
    """Start times of the full scan runs since ``since``: groups of scrapes with less than RUN_GAP between them,
    at least RUN_MIN_SCRAPES each (the scheduled runs, not a manual check)."""
    starts = [r[0] for r in db.query("SELECT started_at FROM crawl_runs WHERE started_at >= ? ORDER BY started_at",
                                     (_iso(since),))]
    runs, group = [], []
    for s in starts:
        t = datetime.fromisoformat(s)
        if group and t - datetime.fromisoformat(group[-1]) > RUN_GAP:
            if len(group) >= RUN_MIN_SCRAPES:
                runs.append(group[0])
            group = []
        group.append(s)
    if len(group) >= RUN_MIN_SCRAPES:
        runs.append(group[0])
    return runs


def remember_subscribers(db: Database, count: int, now: Optional[datetime] = None) -> dict[str, int]:
    """Save today's channel subscriber count (one a day) and return the saved counts."""
    now = now or datetime.now(timezone.utc)
    row = db.query("SELECT value FROM meta WHERE key=?", (SUBSCRIBERS_KEY,))
    try:
        log = {k: int(v) for k, v in json.loads(row[0]["value"] if row else "{}").items()}
    except (ValueError, TypeError, AttributeError):
        log = {}
    log[now.date().isoformat()] = int(count)
    log = dict(sorted(log.items())[-120:])
    with db.tx() as c:
        c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (SUBSCRIBERS_KEY, json.dumps(log)))
    return log


def collect(cfg: dict, db: Database, now: Optional[datetime] = None, subscribers: Optional[int] = None) -> dict:
    """Everything a post or a report may say about the system, counted now."""
    from .models import store_label
    from .sources import load_stores
    now = now or datetime.now(timezone.utc)
    week, month = _iso(now - timedelta(days=7)), _iso(now - timedelta(days=30))
    src = cfg.get("sources_config") or ""
    stores = [s for s in (load_stores(src) if src and Path(src).exists() else []) if s.enabled]
    with_pages = [s for s in stores if s.start_urls]
    searchable = [s for s in stores if s.search is not False and (s.search_url or s.search_sitemap)]
    cats = {c: len([w for w in ws or [] if str(w).strip()]) for c, ws in (cfg.get("category_searches") or {}).items()}
    runs = scan_runs(db, now - timedelta(days=7))
    saved = db.query("SELECT AVG(savings_pct) a, MAX(savings_pct) m, COUNT(savings_pct) n FROM deal_alerts "
                     "WHERE status='published' AND published_at >= ? AND savings_pct IS NOT NULL", (month,))[0]
    first = db.query("SELECT MIN(observed_at) FROM price_history")[0][0]
    f = {
        "at": _iso(now),
        "stores": sorted(store_label(s.name, s.domain) for s in stores),
        "sale_pages": sum(len(s.start_urls) for s in with_pages),
        "stores_with_sale_pages": len(with_pages),
        "searchable_stores": len(searchable),
        "search_words": len([w for w in cfg.get("searches", []) if str(w).strip()]),
        "category_words": cats,
        "scan_runs_7d": len(runs),
        "last_scan_run": runs[-1] if runs else None,
        "products_tracked": _count(db, "SELECT COUNT(*) FROM offers"),
        "products_seen_7d": _count(db, "SELECT COUNT(*) FROM offers WHERE last_seen >= ?", (week,)),
        "price_checks": _count(db, "SELECT COUNT(*) FROM price_history"),
        "price_checks_since": first,
        "deals_published": _count(db, "SELECT COUNT(*) FROM deal_alerts WHERE status='published'"),
        "deals_published_7d": _count(db, "SELECT COUNT(*) FROM deal_alerts WHERE status='published' "
                                         "AND published_at >= ?", (week,)),
        "deals_withdrawn": _count(db, "SELECT COUNT(*) FROM deal_alerts WHERE status='withdrawn'"),
        "savings_30d": {"deals": int(saved["n"] or 0),
                        "avg_pct": round(saved["a"], 1) if saved["a"] is not None else None,
                        "max_pct": round(saved["m"], 1) if saved["m"] is not None else None},
        "sale_checks_published": _count(db, "SELECT COUNT(*) FROM price_warnings WHERE published_at IS NOT NULL"),
        "subscribers": None,
    }
    if subscribers is not None:
        log = remember_subscribers(db, subscribers, now)
        week_ago = (now - timedelta(days=7)).date().isoformat()
        before = [v for d, v in log.items() if d <= week_ago]
        f["subscribers"] = {"now": int(subscribers), "week_ago": before[-1] if before else None}
    return f


def _date(iso: Optional[str]) -> str:
    from .content import israel_time
    return israel_time(iso or "") or "-"


def text(f: dict) -> str:
    """The facts as a short Hebrew page for the agents and the owner."""
    from .catalog import CAT_BY_SLUG
    n = lambda v: f"{v:,}"   # noqa: E731
    cats = ", ".join(f"{v} {CAT_BY_SLUG[c][1] if c in CAT_BY_SLUG else c}" for c, v in f["category_words"].items())
    s = f["savings_30d"]
    lines = [
        f"# עובדות המערכת, נכון ל-{_date(f['at'])} (שעון ישראל)",
        "נספר עכשיו מהנתונים (`py main.py facts`). כל מספר על המערכת בתוכן שיווקי או בדוח בא מכאן, "
        "ולא מ-STATUS.md או מהזיכרון.",
        "",
        "## סריקה",
        f"- חנויות פעילות: {len(f['stores'])} ({', '.join(f['stores'])})",
        f"- דפי מבצעים שנסרקים: {f['sale_pages']}, ב-{f['stores_with_sale_pages']} חנויות",
        f"- חנויות שמחפשים בהן מוצרים: {f['searchable_stores']}. מילות חיפוש: {f['search_words']} כלליות"
        + (f", ולפי קטגוריה: {cats}" if cats else ""),
        f"- ריצות סריקה מלאות ב-7 הימים האחרונים: {f['scan_runs_7d']}"
        + (f" (האחרונה התחילה ב-{_date(f['last_scan_run'])})" if f["last_scan_run"] else ""),
        "",
        "## נתונים",
        f"- מוצרים במעקב: {n(f['products_tracked'])} (מהם נראו ב-7 הימים האחרונים: {n(f['products_seen_7d'])})",
        f"- בדיקות מחיר שנשמרו: {n(f['price_checks'])}"
        + (f", מאז {_date(f['price_checks_since']).split(',')[0]}" if f["price_checks_since"] else ""),
        "",
        "## ערוץ",
        f"- עסקאות שפורסמו: {f['deals_published']} בסך הכול, {f['deals_published_7d']} ב-7 הימים האחרונים",
        (f"- חיסכון מאומת בעסקאות שפורסמו ב-30 הימים האחרונים ({s['deals']}): ממוצע {s['avg_pct']:g}%, "
         f"הכי גבוה {s['max_pct']:g}%") if s["deals"] else "- חיסכון מאומת: אין עדיין עסקאות עם חיסכון מאומת ב-30 יום",
        f"- עסקאות שהוסרו מהערוץ אחרי פרסום: {f['deals_withdrawn']}",
        f"- \"בדקנו את המבצע\" שפורסמו: {f['sale_checks_published']}",
    ]
    sub = f["subscribers"]
    if sub:
        lines.append(f"- מנויים בערוץ: {n(sub['now'])}"
                     + (f" (לפני 7 ימים: {n(sub['week_ago'])})" if sub["week_ago"] is not None else
                        " (אין עדיין ספירה מלפני שבוע להשוואה)"))
    else:
        lines.append("- מנויים בערוץ: לא נבדק עכשיו (`py main.py facts --subscribers`). לא כותבים מספר מנויים בלי זה")
    return "\n".join(lines)

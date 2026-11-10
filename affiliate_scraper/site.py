"""Static website for the channel ("מחיר אמת") - built from the deals you APPROVED.

Telegram brings readers to the site (each post links to the deal page with its price history)
and the site brings readers to Telegram (every page has a "join the channel" button). Pages
are plain static HTML, so they are fast and Google can index them. Hosting: GitHub Pages, free.

Nothing appears on the site that you didn't approve - the same rule as the channel.

automation.json::

    "site": {
      "enabled": true,
      "base_url": "https://gregokrol.github.io/mechiremet/",
      "telegram_url": "https://t.me/mechiremet",
      "title": "מחיר אמת",
      "out_dir": "site",
      "publish_dir": "C:\\\\Projects\\\\mechiremet",   <- a local git clone of the GitHub Pages repository
      "days": 30
    }
"""

from __future__ import annotations

import html
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from .db import Database
from .validation import _parse_iso

log = logging.getLogger(__name__)

e = html.escape
LOGO = Path(__file__).resolve().parents[1] / "assets" / "logo.png"
PUBLIC_STATUSES = ("approved", "publishing", "published")
DISCLOSURE = ("גילוי נאות: האתר כולל קישורי שותפים. רכישה דרכם עשויה לזכות אותנו בעמלה, ללא עלות נוספת עבורך. "
              "המחירים נבדקו במועד המצוין ועשויים להשתנות - המחיר הקובע הוא המחיר באתר החנות.")


@dataclass
class SiteConfig:
    base_url: str = ""
    telegram_url: str = ""
    title: str = "מחיר אמת"
    out_dir: str = "site"
    publish_dir: str = ""
    days: int = 30
    stale_hours: float = 48.0

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "SiteConfig":
        d = d or {}
        known = set(cls.__dataclass_fields__)
        cfg = cls(**{k: v for k, v in d.items() if k in known})
        if cfg.base_url and not cfg.base_url.endswith("/"):
            cfg.base_url += "/"
        return cfg


def deal_path(alert_id: int) -> str:
    return f"deals/{int(alert_id)}.html"


def page_url(cfg: SiteConfig, alert_id: int) -> str:
    return f"{cfg.base_url}{deal_path(alert_id)}" if cfg.base_url else ""


def _money(v: Optional[float], cur: str) -> str:
    if v is None:
        return ""
    sym = {"ILS": "₪", "USD": "$", "EUR": "€", "GBP": "£"}.get(cur, "")
    return f"{sym}{v:,.2f}" if sym else f"{v:,.2f} {cur}"


def _safe_url(u: str) -> str:
    return u if isinstance(u, str) and u.startswith(("https://", "http://")) else "#"


def _when(ts: str) -> str:
    dt = _parse_iso(ts or "")
    return dt.astimezone(timezone(timedelta(hours=2))).strftime("%d.%m.%Y %H:%M") if dt else ""


def price_chart_svg(points: list[tuple[str, float]], currency: str) -> str:
    """Server-side SVG line chart of verified price observations (no JavaScript, no external library)."""
    pts = [(dt, p) for ts, p in points if p is not None and (dt := _parse_iso(ts))]
    if len(pts) < 2:
        return ""
    pts.sort()
    W, H, P = 640, 220, 44
    t0, t1 = pts[0][0].timestamp(), pts[-1][0].timestamp()
    lo, hi = min(p for _, p in pts), max(p for _, p in pts)
    pad = (hi - lo) * 0.1 or hi * 0.05 or 1
    lo, hi = lo - pad, hi + pad
    X = lambda t: P + (W - 2 * P) * ((t - t0) / (t1 - t0) if t1 > t0 else 0.5)  # noqa: E731
    Y = lambda v: H - P - (H - 2 * P) * ((v - lo) / (hi - lo))  # noqa: E731
    path = " ".join(f"{'M' if i == 0 else 'L'}{X(dt.timestamp()):.1f},{Y(p):.1f}" for i, (dt, p) in enumerate(pts))
    dots = "".join(f'<circle cx="{X(dt.timestamp()):.1f}" cy="{Y(p):.1f}" r="3.5"><title>{e(dt.strftime("%d.%m.%Y"))}: '
                   f'{e(_money(p, currency))}</title></circle>' for dt, p in pts)
    return (f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="היסטוריית מחיר" dir="ltr">'
            f'<line x1="{P}" y1="{H - P}" x2="{W - P}" y2="{H - P}" class="axis"/>'
            f'<text x="6" y="{Y(hi - pad) - 6:.0f}" text-anchor="start">{e(_money(hi - pad, currency))}</text>'
            f'<text x="6" y="{Y(lo + pad) + 18:.0f}" text-anchor="start">{e(_money(lo + pad, currency))}</text>'
            f'<text x="{P}" y="{H - 12}" text-anchor="start">{e(pts[0][0].strftime("%d.%m"))}</text>'
            f'<text x="{W - P}" y="{H - 12}" text-anchor="end">{e(pts[-1][0].strftime("%d.%m"))}</text>'
            f'<path d="{path}" class="line"/>{dots}</svg>')


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#14161a;--muted:#5f6670;--line:#e3e6ea;--accent:#0b63ce;--good:#127a3a;--warn:#a65a00;--tg:#229ed9}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--text:#eceff3;--muted:#a3aab4;--line:#2a2f37;--accent:#5aa2ff;--good:#43c16f;--warn:#e0a040}}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,"Segoe UI",Arial,sans-serif;background:var(--bg);color:var(--text);line-height:1.55}
a{color:var(--accent)}header{background:var(--card);border-bottom:1px solid var(--line)}
.wrap{max-width:1080px;margin:0 auto;padding:16px}
.top{display:flex;align-items:center;gap:12px;justify-content:space-between;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;color:inherit;font-weight:700;font-size:20px}
.brand img{width:44px;height:44px;border-radius:50%}
.tg{display:inline-flex;align-items:center;gap:8px;background:var(--tg);color:#fff;text-decoration:none;padding:9px 14px;border-radius:10px;font-weight:600}
.hero{padding:28px 0 8px}.hero h1{margin:0 0 6px;font-size:28px}.hero p{margin:0;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px;margin:18px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;display:flex;flex-direction:column;text-decoration:none;color:inherit}
.card img{width:100%;aspect-ratio:1;object-fit:contain;background:#fff}
.card .b{padding:12px;display:flex;flex-direction:column;gap:6px;flex:1}
.price{font-size:20px;font-weight:700}.muted{color:var(--muted);font-size:13px}
.pill{display:inline-block;font-size:12px;border-radius:99px;padding:2px 9px;background:color-mix(in srgb,var(--good) 14%,transparent);color:var(--good);font-weight:600}
.old{color:var(--warn);font-size:13px;font-weight:600}
.deal{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.2fr);gap:22px;margin:20px 0}
@media (max-width:760px){.deal{grid-template-columns:1fr}}
.deal img.main{width:100%;max-height:420px;object-fit:contain;background:#fff;border-radius:14px;border:1px solid var(--line)}
.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}
ul.claims{padding-inline-start:18px;margin:8px 0}ul.claims li{margin:4px 0}
.buy{display:block;text-align:center;background:var(--accent);color:#fff;text-decoration:none;padding:13px;border-radius:12px;font-weight:700;font-size:17px;margin:10px 0}
table{width:100%;border-collapse:collapse}th,td{padding:8px;border-bottom:1px solid var(--line);text-align:right}th{color:var(--muted);font-size:13px}
td.n{direction:ltr;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
svg.chart{width:100%;height:auto;direction:ltr}svg.chart .line{fill:none;stroke:var(--accent);stroke-width:2.5}svg.chart circle{fill:var(--accent)}
svg.chart .axis{stroke:var(--line)}svg.chart text{fill:var(--muted);font-size:12px}
footer{color:var(--muted);font-size:13px;border-top:1px solid var(--line);margin-top:30px}
"""


def _layout(cfg: SiteConfig, title: str, description: str, body: str, path: str, depth: int = 0,
            og_image: str = "", json_ld: Optional[dict] = None) -> str:
    root = "../" * depth
    canonical = f"{cfg.base_url}{path}" if cfg.base_url else ""
    tg = (f'<a class="tg" href="{e(cfg.telegram_url)}" target="_blank" rel="noopener">✈️ הצטרפו לערוץ בטלגרם</a>'
          if cfg.telegram_url else "")
    meta = [f'<meta name="description" content="{e(description)}">',
            f'<meta property="og:title" content="{e(title)}">',
            f'<meta property="og:description" content="{e(description)}">',
            '<meta property="og:type" content="website">', '<meta name="twitter:card" content="summary_large_image">']
    if canonical:
        meta += [f'<link rel="canonical" href="{e(canonical)}">', f'<meta property="og:url" content="{e(canonical)}">']
    if og_image:
        meta.append(f'<meta property="og:image" content="{e(og_image)}">')
    ld = ""
    if json_ld:
        safe_json = json.dumps(json_ld, ensure_ascii=False).replace("</", "<\\/")   # no "</script>" inside
        ld = f'<script type="application/ld+json">{safe_json}</script>'
    return f"""<!doctype html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
{chr(10).join(meta)}
<link rel="icon" href="{root}logo.png">
<link rel="alternate" type="application/rss+xml" title="{e(cfg.title)}" href="{root}feed.xml">
<link rel="stylesheet" href="{root}style.css">
{ld}
</head>
<body>
<header><div class="wrap top">
<a class="brand" href="{root}index.html"><img src="{root}logo.png" alt="">{e(cfg.title)}</a>
{tg}
</div></header>
<main class="wrap">
{body}
</main>
<footer><div class="wrap">
<p>{e(DISCLOSURE)}</p>
<p><a href="{root}how-we-check.html">איך אנחנו בודקים מחירים</a> · <a href="{root}feed.xml">RSS</a></p>
</div></footer>
</body>
</html>
"""


def _card(d: dict, cfg: SiteConfig, depth: int = 0) -> str:
    root = "../" * depth
    img = f'<img src="{e(_safe_url(d["image"]))}" alt="{e(d["title"])}" loading="lazy">' if d.get("image") else ""
    badge = {"price_drop": "ירידת מחיר", "lowest_90d": "הנמוך ב-90 יום", "top_deal": "עסקה משתלמת"}.get(d["kind"], "")
    stale = '<span class="old">המחיר עשוי להשתנות</span>' if d["stale"] else ""
    return (f'<a class="card" href="{root}{deal_path(d["id"])}">{img}<div class="b">'
            f'<span class="pill">{e(badge)}</span><strong>{e(d["title"])}</strong>'
            f'<span class="price">{e(_money(d["price"], d["currency"]))}</span>'
            f'<span class="muted">{e(d["store"])} · נבדק {e(_when(d["checked_at"]))}</span>{stale}</div></a>')


def collect_deals(db: Database, cfg: SiteConfig, now: Optional[datetime] = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    rows = db.query(
        "SELECT a.id, a.kind, a.title, a.price, a.currency, a.content_json, a.created_at, a.status, a.offer_id, "
        "o.url, o.affiliate_url, o.image, o.store, o.domain, o.group_id, o.last_fetched_at, o.source "
        f"FROM deal_alerts a JOIN offers o ON o.id=a.offer_id WHERE a.status IN ({','.join('?' * len(PUBLIC_STATUSES))}) "
        "ORDER BY a.id DESC", PUBLIC_STATUSES)
    out = []
    for r in rows:
        d = dict(r)
        checked = _parse_iso(d["last_fetched_at"] or "")
        d["checked_at"] = d["last_fetched_at"] or d["created_at"]
        d["stale"] = not checked or (now - checked) > timedelta(hours=cfg.stale_hours)
        created = _parse_iso(d["created_at"] or "")
        d["recent"] = bool(created and now - created <= timedelta(days=cfg.days))
        d["content"] = json.loads(d.pop("content_json") or "{}")
        out.append(d)
    return out


def _deal_page(db: Database, d: dict, cfg: SiteConfig) -> str:
    content = d["content"]
    link = _safe_url(d["affiliate_url"] or d["url"])
    # the price and its check time are shown above the list already - skip that claim here
    claims = "".join(f"<li>{e(c['text'])}</li>" for c in content.get("claims", []) if c.get("basis") != "verified_price")
    hist = db.query("SELECT observed_at, price FROM price_history WHERE offer_id=? AND price_verified=1 AND currency=? "
                    "ORDER BY observed_at", (d["offer_id"], d["currency"]))
    chart = price_chart_svg([(h["observed_at"], h["price"]) for h in hist], d["currency"])
    chart_html = (f'<div class="panel"><h2>היסטוריית המחיר</h2>{chart}'
                  f'<p class="muted">מבוסס על {len(hist)} בדיקות מחיר מאומתות.</p></div>' if chart else "")
    others = []
    if d["group_id"]:
        others = db.query("SELECT store, domain, price, currency, shipping_cost, url, affiliate_url, last_fetched_at "
                          "FROM offers WHERE group_id=? AND price_verified=1 AND currency=? ORDER BY price",
                          (d["group_id"], d["currency"]))
    compare = ""
    if len(others) >= 2:
        rows = "".join(f'<tr><td>{e(o["store"])}</td><td class="n">{e(_money(o["price"], o["currency"]))}</td>'
                       f'<td class="n">{"חינם" if o["shipping_cost"] == 0 else e(_money(o["shipping_cost"], o["currency"])) or "לא ידוע"}</td>'
                       f'<td class="n muted">{e(_when(o["last_fetched_at"]))}</td></tr>' for o in others)
        compare = (f'<div class="panel"><h2>אותו מוצר בחנויות אחרות</h2><table><tr><th>חנות</th><th>מחיר</th>'
                   f'<th>משלוח</th><th>נבדק</th></tr>{rows}</table></div>')
    stale = ('<p class="old">⚠️ המחיר נבדק לפני יותר מ-48 שעות ועשוי להשתנות. בדקו את המחיר המעודכן באתר החנות.</p>'
             if d["stale"] else "")
    tg = (f'<p><a class="tg" href="{e(cfg.telegram_url)}" target="_blank" rel="noopener">✈️ קבלו עסקאות כאלה '
          f'ראשונים בערוץ הטלגרם</a></p>' if cfg.telegram_url else "")
    img = f'<img class="main" src="{e(_safe_url(d["image"]))}" alt="{e(d["title"])}">' if d.get("image") else ""
    body = (f'<p class="muted"><a href="../index.html">כל העסקאות</a></p>'
            f'<div class="deal"><div>{img}</div><div>'
            f'<h1>{e(d["title"])}</h1>'
            f'<div class="price">{e(_money(d["price"], d["currency"]))}</div>'
            f'<p class="muted">ב-{e(d["store"])} · המחיר נבדק ב-{e(_when(d["checked_at"]))}</p>{stale}'
            f'<div class="panel"><h2>למה זו עסקה</h2><ul class="claims">{claims}</ul>'
            f'<p class="muted">כל טענה מבוססת על בדיקה: מחיר מאומת, היסטוריית מחירים או השוואה בין חנויות.</p></div>'
            f'<a class="buy" href="{e(link)}" target="_blank" rel="sponsored nofollow noopener">לפרטים ורכישה ב-{e(d["store"])}</a>'
            f'{tg}</div></div>{chart_html}{compare}')
    prices = [o["price"] for o in others] or [d["price"]]
    json_ld = {"@context": "https://schema.org", "@type": "Product", "name": d["title"],
               **({"image": d["image"]} if d.get("image") else {}),
               "offers": {"@type": "AggregateOffer", "priceCurrency": d["currency"], "lowPrice": min(prices),
                          "highPrice": max(prices), "offerCount": len(prices)}}
    desc = f'{d["title"]} ב-{_money(d["price"], d["currency"])} - נבדק מול היסטוריית המחיר וחנויות אחרות.'
    return _layout(cfg, f'{d["title"]} | {cfg.title}', desc, body, deal_path(d["id"]), depth=1,
                   og_image=d.get("image") or "", json_ld=json_ld)


HOW_WE_CHECK = """
<div class="hero"><h1>איך אנחנו בודקים מחירים</h1></div>
<div class="panel">
<p>רוב ה"מבצעים" ברשת מעלים קודם את המחיר ואז "מורידים" אותו. לכן כל מוצר נבדק לפני שהוא מתפרסם:</p>
<ul class="claims">
<li><strong>מחיר מאומת</strong>: המחיר והמטבע נקראים ממקור מובנה של החנות, ומחיר ישן מ-24 שעות לא נחשב מאומת.</li>
<li><strong>היסטוריית מחיר</strong>: משווים למחיר באותה חנות ב-30 וב-90 הימים האחרונים. "הנחה" שלא באמת ירדה מסומנת כמטעה ולא מתפרסמת.</li>
<li><strong>השוואה בין חנויות</strong>: אותו מוצר בדיוק, לפי ברקוד או דגם. גדלים, צבעים ומצב (חדש/מחודש) שונים לא מושווים.</li>
<li><strong>אישור ידני</strong>: כל עסקה נבדקת ומאושרת לפני שהיא עולה לאתר ולערוץ.</li>
</ul>
<p>בכל עמוד מופיע מועד הבדיקה. המחיר הקובע הוא תמיד המחיר באתר החנות.</p>
</div>
"""


def _refuse_project_folder(folder: Path, what: str) -> None:
    """The site folders are wiped before writing. Never let that be the project (or a parent of it)."""
    folder = folder.resolve()
    project = Path(__file__).resolve().parents[1]
    if (folder / "main.py").exists() or (folder / "affiliate_scraper").exists() or folder in (project, *project.parents):
        raise RuntimeError(f"{what} ({folder}) is the project folder or contains it - choose a separate folder "
                           "for the website")


def build_site(db: Database, cfg: SiteConfig, now: Optional[datetime] = None) -> dict:
    """Write the whole site into cfg.out_dir. Returns {"pages": n, "deals": n, "out_dir": path}."""
    out = Path(cfg.out_dir)
    _refuse_project_folder(out, "site.out_dir")
    if out.exists():
        for child in out.iterdir():          # rebuild from scratch, but keep a git checkout / CNAME if present
            if child.name in (".git", "CNAME"):
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    (out / "deals").mkdir(parents=True, exist_ok=True)
    (out / "style.css").write_text(CSS.strip() + "\n", encoding="utf-8")
    if LOGO.is_file():
        shutil.copyfile(LOGO, out / "logo.png")
    (out / ".nojekyll").write_text("", encoding="utf-8")   # GitHub Pages: serve files as they are

    deals = collect_deals(db, cfg, now)
    for d in deals:
        (out / deal_path(d["id"])).write_text(_deal_page(db, d, cfg), encoding="utf-8")

    recent = [d for d in deals if d["recent"]]
    cards = "".join(_card(d, cfg) for d in recent) or '<p class="muted">עסקאות חדשות יופיעו כאן בקרוב.</p>'
    older = [d for d in deals if not d["recent"]]
    older_html = ("<h2>עסקאות קודמות</h2><div class=\"grid\">" + "".join(_card(d, cfg) for d in older[:60]) + "</div>"
                  if older else "")
    hero = (f'<div class="hero"><h1>{e(cfg.title)}: רק הנחות אמיתיות</h1>'
            f'<p>כל עסקה נבדקת מול היסטוריית המחיר שלה ומול חנויות אחרות לפני שהיא מתפרסמת.</p></div>')
    (out / "index.html").write_text(_layout(cfg, f"{cfg.title} | עסקאות שנבדקו באמת",
                                            "עסקאות ודילים בישראל שנבדקו מול היסטוריית המחיר ומול חנויות אחרות.",
                                            f'{hero}<div class="grid">{cards}</div>{older_html}', "index.html"),
                                    encoding="utf-8")
    (out / "how-we-check.html").write_text(_layout(cfg, f"איך אנחנו בודקים מחירים | {cfg.title}",
                                                   "השיטה: מחיר מאומת, היסטוריית מחיר, השוואה בין חנויות ואישור ידני.",
                                                   HOW_WE_CHECK, "how-we-check.html"), encoding="utf-8")
    pages = ["index.html", "how-we-check.html", *[deal_path(d["id"]) for d in deals]]
    if cfg.base_url:
        urls = "".join(f"<url><loc>{e(cfg.base_url + p)}</loc></url>" for p in pages)
        (out / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>'
                                         f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
                                         encoding="utf-8")
        (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {cfg.base_url}sitemap.xml\n", encoding="utf-8")
    items = "".join(
        f"<item><title>{e(d['title'])} - {e(_money(d['price'], d['currency']))}</title>"
        f"<link>{e(cfg.base_url + deal_path(d['id']))}</link><guid>{e(cfg.base_url + deal_path(d['id']))}</guid>"
        f"<pubDate>{e(_parse_iso(d['created_at']).strftime('%a, %d %b %Y %H:%M:%S +0000') if _parse_iso(d['created_at']) else '')}</pubDate></item>"
        for d in recent[:50])
    (out / "feed.xml").write_text(f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
                                  f"<title>{e(cfg.title)}</title><link>{e(cfg.base_url)}</link>"
                                  f"<description>עסקאות שנבדקו באמת</description>{items}</channel></rss>",
                                  encoding="utf-8")
    return {"pages": len(pages), "deals": len(deals), "out_dir": str(out)}


def publish_site(cfg: SiteConfig, run=subprocess.run) -> str:
    """Copy the built site into cfg.publish_dir (a git clone of the Pages repository), commit and push."""
    target = Path(cfg.publish_dir)
    if not cfg.publish_dir or not (target / ".git").exists():
        raise RuntimeError("site.publish_dir must be a local git clone of your GitHub Pages repository "
                           "(see RUN_ON_WINDOWS.md, website section)")
    _refuse_project_folder(target, "site.publish_dir")
    src = Path(cfg.out_dir).resolve()
    if src == target.resolve():
        raise RuntimeError("site.out_dir and site.publish_dir must be different folders")
    for child in target.iterdir():
        if child.name in (".git", "CNAME", "README.md"):
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    for item in src.iterdir():
        dest = target / item.name
        shutil.copytree(item, dest) if item.is_dir() else shutil.copyfile(item, dest)

    def git(*args: str) -> subprocess.CompletedProcess:
        return run(["git", "-C", str(target), *args], capture_output=True, text=True, check=False)

    git("add", "-A")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return "no changes"
    done = git("commit", "-m", f"Update site {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    if done.returncode != 0:
        raise RuntimeError(f"git commit failed: {done.stderr.strip()[:300]}")
    pushed = git("push", "-u", "origin", "HEAD")   # also works for the very first push to an empty repository
    if pushed.returncode != 0:
        raise RuntimeError(f"git push failed: {pushed.stderr.strip()[:300]}")
    return "published"

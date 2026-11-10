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


def _ago(ts: str, now: Optional[datetime] = None) -> str:
    dt = _parse_iso(ts or "")
    if not dt:
        return ""
    sec = max(0, ((now or datetime.now(timezone.utc)) - dt).total_seconds())
    if sec < 3600:
        m = max(1, int(sec // 60))
        return "לפני דקה" if m == 1 else ("לפני 2 דקות" if m == 2 else f"לפני {m} דקות")
    if sec < 86400:
        h = int(sec // 3600)
        return "לפני שעה" if h == 1 else ("לפני שעתיים" if h == 2 else f"לפני {h} שעות")
    d = int(sec // 86400)
    return "אתמול" if d == 1 else ("לפני יומיים" if d == 2 else f"לפני {d} ימים")


def price_chart_svg(points: list[tuple[str, float]], currency: str) -> str:
    """Server-side SVG chart of verified price observations (no JavaScript, no external library)."""
    pts = [(dt, p) for ts, p in points if p is not None and (dt := _parse_iso(ts))]
    if len(pts) < 2:
        return ""
    pts.sort()
    W, H, P = 640, 240, 48
    t0, t1 = pts[0][0].timestamp(), pts[-1][0].timestamp()
    lo, hi = min(p for _, p in pts), max(p for _, p in pts)
    pad = (hi - lo) * 0.12 or hi * 0.05 or 1
    lo, hi = lo - pad, hi + pad
    X = lambda t: P + (W - 2 * P) * ((t - t0) / (t1 - t0) if t1 > t0 else 0.5)  # noqa: E731
    Y = lambda v: H - P - (H - 2 * P) * ((v - lo) / (hi - lo))  # noqa: E731
    xy = [(X(dt.timestamp()), Y(p)) for dt, p in pts]
    line = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(xy))
    area = f"{line} L{xy[-1][0]:.1f},{H - P} L{xy[0][0]:.1f},{H - P} Z"
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4"><title>{e(dt.strftime("%d.%m.%Y"))}: '
                   f'{e(_money(p, currency))}</title></circle>' for (x, y), (dt, p) in zip(xy, pts))
    lx, ly = xy[-1]
    return (f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="היסטוריית מחיר" dir="ltr">'
            '<defs><linearGradient id="ga" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#22d3ee" '
            'stop-opacity=".45"/><stop offset="1" stop-color="#22d3ee" stop-opacity="0"/></linearGradient></defs>'
            f'<line x1="{P}" y1="{H - P}" x2="{W - P}" y2="{H - P}" class="axis"/>'
            f'<text x="6" y="{Y(hi - pad) - 6:.0f}" text-anchor="start">{e(_money(hi - pad, currency))}</text>'
            f'<text x="6" y="{Y(lo + pad) + 18:.0f}" text-anchor="start">{e(_money(lo + pad, currency))}</text>'
            f'<text x="{P}" y="{H - 14}" text-anchor="start">{e(pts[0][0].strftime("%d.%m"))}</text>'
            f'<text x="{W - P}" y="{H - 14}" text-anchor="end">{e(pts[-1][0].strftime("%d.%m"))}</text>'
            f'<path d="{area}" class="area"/><path d="{line}" class="line"/>{dots}'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="8" class="now"/></svg>')


CSS = """
:root{color-scheme:dark;--bg:#060b18;--bg2:#0a1430;--card:#0f1b38;--card2:#142449;--text:#f2f6ff;--muted:#9db0d4;
--line:#22345e;--yellow:#ffd21f;--yellow2:#ffb800;--cyan:#22d3ee;--blue:#2f7bff;--red:#ff3d47;--green:#22c55e;--tg:#2aabee;
--glow:0 0 0 1px rgba(34,211,238,.25),0 10px 40px -10px rgba(34,211,238,.45)}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;font-family:"Rubik",system-ui,"Segoe UI",Arial,sans-serif;color:var(--text);line-height:1.55;
background:radial-gradient(900px 500px at 85% -10%,rgba(47,123,255,.28),transparent 60%),
radial-gradient(700px 420px at 0% 10%,rgba(34,211,238,.16),transparent 60%),var(--bg);background-attachment:fixed}
a{color:var(--cyan)}img{max-width:100%}
.wrap{max-width:1160px;margin:0 auto;padding:0 16px}
header{position:sticky;top:0;z-index:20;background:rgba(6,11,24,.78);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;height:68px}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;color:var(--text);font-weight:800;font-size:21px}
.brand img{width:46px;height:46px;border-radius:50%;box-shadow:0 0 18px rgba(34,211,238,.55)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;font:inherit;font-weight:700;text-decoration:none;
border-radius:14px;padding:12px 18px;transition:transform .15s ease,box-shadow .2s ease,filter .2s}
.btn:hover{transform:translateY(-2px);filter:brightness(1.08)}
.btn-tg{background:linear-gradient(135deg,#2aabee,#1c7fd8);color:#fff;box-shadow:0 8px 24px -8px rgba(42,171,238,.8)}
.btn-ghost{border:1px solid var(--line);color:var(--text);background:rgba(255,255,255,.03)}
.btn-buy{position:relative;overflow:hidden;width:100%;font-size:19px;padding:16px;color:#1a1300;
background:linear-gradient(135deg,var(--yellow),var(--yellow2));box-shadow:0 12px 34px -10px rgba(255,200,0,.85)}
.btn-buy::after{content:"";position:absolute;inset:0;transform:translateX(120%);
background:linear-gradient(100deg,transparent 30%,rgba(255,255,255,.55) 50%,transparent 70%);animation:shine 3.2s ease-in-out infinite}
@keyframes shine{0%,60%{transform:translateX(120%)}100%{transform:translateX(-120%)}}
.hero{padding:56px 0 26px;text-align:center}
.hero h1{margin:0 0 10px;font-size:clamp(32px,5.6vw,58px);line-height:1.1;font-weight:800;letter-spacing:-.5px}
.hl{background:linear-gradient(135deg,var(--yellow),#fff3a6 40%,var(--yellow2));-webkit-background-clip:text;background-clip:text;color:transparent;
text-shadow:0 0 30px rgba(255,210,31,.25)}
.hero p{margin:0 auto 22px;max-width:640px;color:var(--muted);font-size:18px}
.cta-row{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}
.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;max-width:720px;margin:30px auto 0}
.stat{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:16px;padding:14px}
.stat b{display:block;font-size:30px;font-weight:800;color:var(--yellow);font-variant-numeric:tabular-nums}
.stat span{color:var(--muted);font-size:14px}
.trust{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin:22px 0 6px}
.trust span{border:1px solid var(--line);background:rgba(34,197,94,.08);color:#bff5d2;border-radius:99px;padding:6px 12px;font-size:14px}
h2.section{font-size:26px;margin:34px 0 14px;font-weight:800}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px;margin:0 0 20px}
.card{position:relative;display:flex;flex-direction:column;text-decoration:none;color:var(--text);border-radius:20px;overflow:hidden;
background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);transition:transform .18s ease,box-shadow .25s ease,border-color .25s}
.card:hover{transform:translateY(-5px);box-shadow:var(--glow);border-color:rgba(34,211,238,.6)}
.thumb{position:relative;background:#fff;aspect-ratio:1/1;display:flex;align-items:center;justify-content:center}
.thumb img{width:100%;height:100%;object-fit:contain;padding:14px}
.ribbon{position:absolute;top:12px;right:12px;background:var(--red);color:#fff;font-weight:700;font-size:13px;border-radius:10px;
padding:5px 10px;box-shadow:0 6px 16px -6px rgba(255,61,71,.9)}
.ribbon.low{background:linear-gradient(135deg,#7c3aed,#2f7bff)}
.save{position:absolute;bottom:-18px;left:14px;width:64px;height:64px;border-radius:50%;display:flex;flex-direction:column;
align-items:center;justify-content:center;background:radial-gradient(circle at 30% 30%,#fff3a6,var(--yellow) 45%,var(--yellow2));
color:#1a1300;font-weight:800;font-size:18px;line-height:1;box-shadow:0 8px 22px -6px rgba(255,200,0,.9);border:3px solid var(--card2)}
.save small{font-size:10px;font-weight:700}
.new{position:absolute;top:12px;left:12px;background:var(--green);color:#03210f;font-weight:800;font-size:12px;border-radius:8px;padding:4px 8px}
.card .b{padding:22px 16px 16px;display:flex;flex-direction:column;gap:6px;flex:1}
.card .t{font-weight:700;font-size:16px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:2.9em}
.pr{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.price{font-size:26px;font-weight:800;color:#fff;font-variant-numeric:tabular-nums}
.was{color:var(--muted);text-decoration:line-through;font-size:15px}
.meta{color:var(--muted);font-size:13px;margin-top:auto}
.ended{opacity:.55}.ended .ribbon{background:#4b5563}
.warn{background:rgba(255,184,0,.1);border:1px solid rgba(255,184,0,.4);color:#ffe39a;border-radius:12px;padding:10px 12px;font-size:14px}
.empty{text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:18px;padding:36px}
.crumbs{margin:20px 0 6px;font-size:14px}.crumbs a{color:var(--muted)}
.deal{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);gap:26px;margin:10px 0 24px;align-items:start}
@media (max-width:820px){.deal{grid-template-columns:1fr}}
.gallery{position:sticky;top:90px;border-radius:24px;overflow:hidden;background:#fff;box-shadow:var(--glow)}
.gallery img{width:100%;max-height:520px;object-fit:contain;padding:22px;display:block}
@media (max-width:820px){.gallery{position:relative;top:0}.gallery img{max-height:280px;padding:14px}}
.kind{display:inline-block;background:rgba(255,61,71,.15);color:#ffb3b8;border:1px solid rgba(255,61,71,.45);border-radius:99px;
padding:4px 12px;font-weight:700;font-size:14px}
.deal h1{font-size:clamp(24px,3.4vw,36px);line-height:1.2;margin:12px 0}
.bigprice{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin:6px 0 4px}
.bigprice .price{font-size:46px}
.chip{background:linear-gradient(135deg,var(--yellow),var(--yellow2));color:#1a1300;font-weight:800;border-radius:12px;padding:6px 12px;font-size:18px}
.panel{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:20px;padding:18px;margin:14px 0}
.panel h2{margin:0 0 10px;font-size:20px}
ul.checks{list-style:none;padding:0;margin:0}
ul.checks li{position:relative;padding:8px 34px 8px 0;border-bottom:1px solid rgba(255,255,255,.05)}
ul.checks li:last-child{border-bottom:0}
ul.checks li::before{content:"✓";position:absolute;right:0;top:7px;width:24px;height:24px;border-radius:50%;display:flex;align-items:center;
justify-content:center;background:rgba(34,197,94,.18);color:var(--green);font-weight:800}
.share{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.share a,.share button{flex:1;min-width:120px;text-align:center;border:1px solid var(--line);background:rgba(255,255,255,.04);color:var(--text);
font:inherit;font-weight:600;border-radius:12px;padding:10px;text-decoration:none;cursor:pointer}
.share a:hover,.share button:hover{border-color:var(--cyan)}
.tgbox{display:flex;align-items:center;gap:14px;background:linear-gradient(135deg,rgba(42,171,238,.2),rgba(47,123,255,.12));
border:1px solid rgba(42,171,238,.5);border-radius:20px;padding:16px;margin:14px 0}
.tgbox img{width:56px;height:56px;border-radius:50%}.tgbox b{display:block;font-size:17px}.tgbox span{color:var(--muted);font-size:14px}
.tgbox .btn{margin-inline-start:auto;white-space:nowrap}
@media (max-width:560px){.tgbox{flex-wrap:wrap}.tgbox .btn{width:100%}}
svg.chart{width:100%;height:auto;direction:ltr}svg.chart .line{fill:none;stroke:var(--cyan);stroke-width:3;filter:drop-shadow(0 0 6px rgba(34,211,238,.7))}
svg.chart .area{fill:url(#ga)}svg.chart circle{fill:var(--cyan)}svg.chart circle.now{fill:var(--yellow);stroke:#fff;stroke-width:2}
svg.chart .axis{stroke:var(--line)}svg.chart text{fill:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse}th,td{padding:10px 8px;border-bottom:1px solid var(--line);text-align:right}
th{color:var(--muted);font-size:13px;font-weight:600}
td.n{direction:ltr;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
tr.best td{background:rgba(34,197,94,.08)}tr.best td:first-child{font-weight:700}
.besttag{background:var(--green);color:#03210f;border-radius:8px;padding:2px 7px;font-size:12px;font-weight:800;margin-inline-start:6px}
.sticky{display:none}
@media (max-width:820px){.sticky{display:flex;position:fixed;bottom:0;left:0;right:0;z-index:30;gap:12px;align-items:center;padding:10px 14px;
background:rgba(6,11,24,.92);backdrop-filter:blur(12px);border-top:1px solid var(--line)}
.sticky .price{font-size:22px}.sticky .btn-buy{width:auto;flex:1;font-size:17px;padding:12px}body.has-sticky{padding-bottom:82px}}
footer{color:var(--muted);font-size:13px;border-top:1px solid var(--line);margin-top:40px;padding:20px 0;background:rgba(0,0,0,.2)}
footer a{color:var(--muted)}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
"""


def _layout(cfg: SiteConfig, title: str, description: str, body: str, path: str, depth: int = 0,
            og_image: str = "", json_ld: Optional[dict] = None, body_class: str = "", tail: str = "") -> str:
    root = "../" * depth
    canonical = f"{cfg.base_url}{path}" if cfg.base_url else ""
    tg = (f'<a class="btn btn-tg" href="{e(cfg.telegram_url)}" target="_blank" rel="noopener">✈️ הצטרפו לערוץ</a>'
          if cfg.telegram_url else "")
    meta = [f'<meta name="description" content="{e(description)}">',
            f'<meta property="og:title" content="{e(title)}">',
            f'<meta property="og:description" content="{e(description)}">',
            '<meta property="og:type" content="website">', '<meta property="og:locale" content="he_IL">',
            '<meta name="twitter:card" content="summary_large_image">', '<meta name="theme-color" content="#060b18">']
    if canonical:
        meta += [f'<link rel="canonical" href="{e(canonical)}">', f'<meta property="og:url" content="{e(canonical)}">']
    meta.append(f'<meta property="og:image" content="{e(og_image or (cfg.base_url + "logo.png" if cfg.base_url else ""))}">')
    ld = ""
    if json_ld:
        safe_json = json.dumps(json_ld, ensure_ascii=False).replace("</", "<\\/")   # no "</script>" inside
        ld = f'<script type="application/ld+json">{safe_json}</script>'
    tg_footer = f' · <a href="{e(cfg.telegram_url)}">הערוץ בטלגרם</a>' if cfg.telegram_url else ""
    return f"""<!doctype html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
{chr(10).join(meta)}
<link rel="icon" href="{root}logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Rubik:wght@400;500;700;800&display=swap" rel="stylesheet">
<link rel="alternate" type="application/rss+xml" title="{e(cfg.title)}" href="{root}feed.xml">
<link rel="stylesheet" href="{root}style.css">
{ld}
</head>
<body class="{e(body_class)}">
<header><div class="wrap top">
<a class="brand" href="{root}index.html"><img src="{root}logo.png" alt="">{e(cfg.title)}</a>
{tg}
</div></header>
<main class="wrap">
{body}
</main>
<footer><div class="wrap">
<p>{e(DISCLOSURE)}</p>
<p><a href="{root}how-we-check.html">איך אנחנו בודקים מחירים</a> · <a href="{root}feed.xml">RSS</a>{tg_footer}</p>
</div></footer>
{tail}
</body>
</html>
"""


KIND_LABEL = {"price_drop": "📉 ירידת מחיר", "lowest_90d": "🏆 הנמוך ב-90 יום", "top_deal": "🔥 עסקה משתלמת"}


def _card(d: dict, cfg: SiteConfig, depth: int = 0) -> str:
    root = "../" * depth
    img = f'<img src="{e(_safe_url(d["image"]))}" alt="{e(d["title"])}" loading="lazy">' if d.get("image") else ""
    label = "העסקה הסתיימה" if d["ended"] else ("🏆 הנמוך ב-90 יום" if d["lowest90"] else KIND_LABEL.get(d["kind"], ""))
    ribbon = f'<span class="ribbon{" low" if d["lowest90"] and not d["ended"] else ""}">{e(label)}</span>' if label else ""
    save = (f'<span class="save"><bdi dir="ltr">-{d["savings_pct"]}%</bdi><small>חיסכון</small></span>'
            if d["savings_pct"] and not d["ended"] else "")
    new = '<span class="new">חדש</span>' if d["is_new"] and not d["ended"] else ""
    was = f'<span class="was">{e(_money(d["prev_price"], d["currency"]))}</span>' if d["prev_price"] and not d["ended"] else ""
    stale = '<span class="warn">המחיר עשוי להשתנות</span>' if d["stale"] and not d["ended"] else ""
    return (f'<a class="card{" ended" if d["ended"] else ""}" href="{root}{deal_path(d["id"])}">'
            f'<div class="thumb">{img}{ribbon}{new}{save}</div><div class="b">'
            f'<span class="t">{e(d["title"])}</span>'
            f'<div class="pr"><span class="price">{e(_money(d["price"], d["currency"]))}</span>{was}</div>'
            f'{stale}<span class="meta">{e(d["store"])} · נבדק {e(d["ago"])}</span></div></a>')


def collect_deals(db: Database, cfg: SiteConfig, now: Optional[datetime] = None) -> list[dict]:
    """Approved deals + the REAL numbers that make them attractive (nothing here is estimated or invented)."""
    from .history import is_lowest_in_90d, stats_for_offer
    now = now or datetime.now(timezone.utc)
    rows = db.query(
        "SELECT a.id, a.kind, a.title, a.price, a.currency, a.content_json, a.created_at, a.status, a.offer_id, "
        "o.url, o.affiliate_url, o.image, o.store, o.domain, o.group_id, o.last_fetched_at, o.source, "
        "o.price AS current_price, o.currency AS current_currency, o.price_verified AS current_verified "
        f"FROM deal_alerts a JOIN offers o ON o.id=a.offer_id WHERE a.status IN ({','.join('?' * len(PUBLIC_STATUSES))}) "
        "ORDER BY a.id DESC", PUBLIC_STATUSES)
    out = []
    for r in rows:
        d = dict(r)
        checked = _parse_iso(d["last_fetched_at"] or "")
        d["checked_at"] = d["last_fetched_at"] or d["created_at"]
        d["ago"] = _ago(d["checked_at"], now)
        d["stale"] = not checked or (now - checked) > timedelta(hours=cfg.stale_hours)
        created = _parse_iso(d["created_at"] or "")
        d["recent"] = bool(created and now - created <= timedelta(days=cfg.days))
        d["is_new"] = bool(created and now - created <= timedelta(hours=24))
        d["content"] = json.loads(d.pop("content_json") or "{}")
        # the price went back up after you approved it -> say so instead of advertising an old deal
        d["ended"] = bool(d["current_verified"] and d["current_price"] is not None and d["current_currency"] == d["currency"]
                          and d["current_price"] > d["price"] * 1.01)
        st = stats_for_offer(db, d["offer_id"], now=now)
        prev = st.previous_price if (st.sufficient and st.previous_price and st.previous_price > d["price"]) else None
        d["prev_price"] = prev
        drop = round((prev - d["price"]) / prev * 100) if prev else None
        d["lowest90"] = is_lowest_in_90d(st) or any(c.get("basis") == "price_history_90d"
                                                    for c in d["content"].get("claims", []))
        vs_market = None
        if d["group_id"]:
            others = [x["price"] for x in db.query("SELECT price FROM offers WHERE group_id=? AND currency=? AND "
                                                   "price_verified=1 AND id<>?", (d["group_id"], d["currency"], d["offer_id"]))]
            if others:
                ref = sorted(others + [d["price"]])[len(others + [d["price"]]) // 2]
                if ref > d["price"]:
                    vs_market = round((ref - d["price"]) / ref * 100)
        best = max([x for x in (drop, vs_market) if x], default=None)
        d["savings_pct"] = best if best and best >= 3 else None
        out.append(d)
    return out


def best_per_product(deals: list[dict]) -> list[dict]:
    """One deal per product for the listings: active before ended, then the cheapest; newest order kept."""
    best: dict[str, dict] = {}
    for d in deals:
        key = d["group_id"] or f"offer:{d['offer_id']}"
        cur = best.get(key)
        if cur is None or (cur["ended"], cur["price"]) > (d["ended"], d["price"]):
            best[key] = d
    keep = {id(v) for v in best.values()}
    return [d for d in deals if id(d) in keep]


def _deal_page(db: Database, d: dict, cfg: SiteConfig) -> str:
    content = d["content"]
    link = _safe_url(d["affiliate_url"] or d["url"])
    url = page_url(cfg, d["id"])
    # the price and its check time are shown above the list already - skip that claim here
    claims = "".join(f"<li>{e(c['text'])}</li>" for c in content.get("claims", []) if c.get("basis") != "verified_price")
    hist = db.query("SELECT observed_at, price FROM price_history WHERE offer_id=? AND price_verified=1 AND currency=? "
                    "ORDER BY observed_at", (d["offer_id"], d["currency"]))
    chart = price_chart_svg([(h["observed_at"], h["price"]) for h in hist], d["currency"])
    chart_html = (f'<div class="panel"><h2>📈 היסטוריית המחיר</h2>{chart}'
                  f'<p class="meta">מבוסס על {len(hist)} בדיקות מחיר מאומתות. הנקודה הצהובה היא המחיר האחרון.</p></div>'
                  if chart else "")
    others = []
    if d["group_id"]:
        others = db.query("SELECT store, domain, price, currency, shipping_cost, url, affiliate_url, last_fetched_at "
                          "FROM offers WHERE group_id=? AND price_verified=1 AND currency=? ORDER BY price",
                          (d["group_id"], d["currency"]))
    compare = ""
    if len(others) >= 2:
        rows = ""
        for i, o in enumerate(others):
            ship = "חינם" if o["shipping_cost"] == 0 else (_money(o["shipping_cost"], o["currency"]) or "לא ידוע")
            tag = '<span class="besttag">הכי זול</span>' if i == 0 else ""
            rows += (f'<tr class="{"best" if i == 0 else ""}"><td>{e(o["store"])}{tag}</td>'
                     f'<td class="n">{e(_money(o["price"], o["currency"]))}</td><td class="n">{e(ship)}</td>'
                     f'<td class="n meta">{e(_when(o["last_fetched_at"]))}</td></tr>')
        compare = (f'<div class="panel"><h2>⚖️ אותו מוצר בחנויות אחרות</h2><table><tr><th>חנות</th><th>מחיר</th>'
                   f'<th>משלוח</th><th>נבדק</th></tr>{rows}</table></div>')
    warn = ""
    if d["ended"]:
        warn = ('<p class="warn">⚠️ המחיר עלה מאז שהעסקה פורסמה, כלומר העסקה הסתיימה. '
                f'המחיר האחרון שנבדק: {e(_money(d["current_price"], d["currency"]))}.</p>')
    elif d["stale"]:
        warn = '<p class="warn">⚠️ המחיר נבדק לפני יותר מ-48 שעות ועשוי להשתנות. בדקו את המחיר המעודכן באתר החנות.</p>'
    img = f'<img src="{e(_safe_url(d["image"]))}" alt="{e(d["title"])}">' if d.get("image") else ""
    label = "העסקה הסתיימה" if d["ended"] else ("🏆 הנמוך ב-90 יום" if d["lowest90"] else KIND_LABEL.get(d["kind"], ""))
    chip = f'<span class="chip">חיסכון {d["savings_pct"]}%</span>' if d["savings_pct"] and not d["ended"] else ""
    was = (f'<p class="meta">מחיר קודם שנבדק באותה חנות: <span class="was">{e(_money(d["prev_price"], d["currency"]))}</span></p>'
           if d["prev_price"] and not d["ended"] else "")
    share = ""
    if url:
        msg = f'{d["title"]} ב-{_money(d["price"], d["currency"])} - נבדק במחיר אמת'
        share = (f'<div class="share"><a href="https://wa.me/?text={quote(msg + " " + url)}" target="_blank" rel="noopener">'
                 f'💬 שתפו בוואטסאפ</a><a href="https://t.me/share/url?url={quote(url)}&text={quote(msg)}" target="_blank" '
                 f'rel="noopener">✈️ שתפו בטלגרם</a><button type="button" data-copy="{e(url)}">🔗 העתיקו קישור</button></div>')
    tgbox = (f'<div class="tgbox"><img src="../logo.png" alt=""><div><b>רוצים לקבל עסקאות כאלה ראשונים?</b>'
             f'<span>כל עסקה נבדקת לפני שהיא מגיעה אליכם.</span></div>'
             f'<a class="btn btn-tg" href="{e(cfg.telegram_url)}" target="_blank" rel="noopener">✈️ הצטרפו לערוץ</a></div>'
             if cfg.telegram_url else "")
    buy_text = f'לרכישה ב-{e(d["store"])} ←'
    body = (f'<p class="crumbs"><a href="../index.html">כל העסקאות</a> › {e(d["store"])}</p>'
            f'<div class="deal"><div class="gallery">{img}</div><div>'
            f'<span class="kind">{e(label)}</span>'
            f'<h1>{e(d["title"])}</h1>'
            f'<div class="bigprice"><span class="price">{e(_money(d["price"], d["currency"]))}</span>{chip}</div>{was}'
            f'<p class="meta">ב-{e(d["store"])} · המחיר נבדק ב-{e(_when(d["checked_at"]))} ({e(d["ago"])})</p>{warn}'
            f'<div class="panel"><h2>למה זו עסקה</h2><ul class="checks">{claims}</ul>'
            f'<p class="meta">כל טענה כאן מבוססת על בדיקה: מחיר מאומת, היסטוריית מחירים או השוואה בין חנויות.</p></div>'
            f'<a class="btn btn-buy" href="{e(link)}" target="_blank" rel="sponsored nofollow noopener">{buy_text}</a>'
            f'{share}{tgbox}</div></div>{chart_html}{compare}')
    sticky = (f'<div class="sticky"><span class="price">{e(_money(d["price"], d["currency"]))}</span>'
              f'<a class="btn btn-buy" href="{e(link)}" target="_blank" rel="sponsored nofollow noopener">{buy_text}</a></div>'
              '<script>document.querySelectorAll("[data-copy]").forEach(function(b){b.addEventListener("click",function(){'
              'navigator.clipboard&&navigator.clipboard.writeText(b.dataset.copy).then(function(){b.textContent="✓ הקישור הועתק"})})})</script>')
    prices = [o["price"] for o in others] or [d["price"]]
    json_ld = {"@context": "https://schema.org", "@type": "Product", "name": d["title"],
               **({"image": d["image"]} if d.get("image") else {}),
               "offers": {"@type": "AggregateOffer", "priceCurrency": d["currency"], "lowPrice": min(prices),
                          "highPrice": max(prices), "offerCount": len(prices)}}
    lead = f'חיסכון של {d["savings_pct"]}% · ' if d["savings_pct"] and not d["ended"] else ""
    desc = f'{lead}{d["title"]} ב-{_money(d["price"], d["currency"])} - נבדק מול היסטוריית המחיר וחנויות אחרות.'
    return _layout(cfg, f'{d["title"]} | {cfg.title}', desc, body, deal_path(d["id"]), depth=1,
                   og_image=d.get("image") or "", json_ld=json_ld, body_class="has-sticky", tail=sticky)


HOW_WE_CHECK = """
<div class="hero"><h1>איך אנחנו <span class="hl">בודקים</span> מחירים</h1>
<p>רוב ה"מבצעים" ברשת מעלים קודם את המחיר ואז "מורידים" אותו. אצלנו כל מוצר עובר 4 בדיקות לפני שהוא מתפרסם.</p></div>
<div class="panel"><ul class="checks">
<li><strong>מחיר מאומת</strong>: המחיר והמטבע נקראים ממקור מובנה של החנות. מחיר ישן מ-24 שעות לא נחשב מאומת.</li>
<li><strong>היסטוריית מחיר</strong>: משווים למחיר באותה חנות ב-30 וב-90 הימים האחרונים. "הנחה" שלא באמת ירדה מסומנת כמטעה ולא מתפרסמת.</li>
<li><strong>השוואה בין חנויות</strong>: אותו מוצר בדיוק, לפי ברקוד או דגם. גדלים, צבעים ומצב (חדש/מחודש) שונים לא מושווים.</li>
<li><strong>אישור ידני</strong>: כל עסקה נבדקת ומאושרת לפני שהיא עולה לאתר ולערוץ.</li>
</ul>
<p class="meta">בכל עמוד מופיע מועד הבדיקה. המחיר הקובע הוא תמיד המחיר באתר החנות.</p></div>
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

    recent = best_per_product([d for d in deals if d["recent"]])
    cards = "".join(_card(d, cfg) for d in recent) or '<p class="empty">עסקאות חדשות יופיעו כאן בקרוב.</p>'
    older = best_per_product([d for d in deals if not d["recent"]])
    older_html = ('<h2 class="section">עסקאות קודמות</h2><div class="grid">' + "".join(_card(d, cfg) for d in older[:60])
                  + "</div>" if older else "")
    # real counters only
    n_stores = db.query("SELECT COUNT(DISTINCT domain) n FROM offers WHERE price_verified=1")[0]["n"]
    n_checks = db.query("SELECT COUNT(*) n FROM price_history WHERE price_verified=1")[0]["n"]
    tg_btn = (f'<a class="btn btn-tg" href="{e(cfg.telegram_url)}" target="_blank" rel="noopener">✈️ הצטרפו לערוץ בטלגרם</a>'
              if cfg.telegram_url else "")
    hero = (f'<section class="hero"><h1><span class="hl">{e(cfg.title)}</span><br>רק הנחות שבדקנו באמת</h1>'
            f'<p>כל עסקה נבדקת מול היסטוריית המחיר שלה ומול חנויות אחרות, ורק אז מגיעה אליכם. בלי מבצעים מנופחים.</p>'
            f'<div class="cta-row">{tg_btn}<a class="btn btn-ghost" href="how-we-check.html">איך אנחנו בודקים?</a></div>'
            f'<div class="stats"><div class="stat"><b>{len(deals):,}</b><span>עסקאות שפורסמו</span></div>'
            f'<div class="stat"><b>{n_stores:,}</b><span>חנויות בהשוואה</span></div>'
            f'<div class="stat"><b>{n_checks:,}</b><span>בדיקות מחיר</span></div></div>'
            f'<div class="trust"><span>✓ נבדק מול 90 יום</span><span>✓ מושווה בין חנויות</span><span>✓ מאושר ידנית</span></div>'
            f'</section>')
    (out / "index.html").write_text(_layout(cfg, f"{cfg.title} | עסקאות שנבדקו באמת",
                                            "עסקאות ודילים בישראל שנבדקו מול היסטוריית המחיר ומול חנויות אחרות.",
                                            f'{hero}<h2 class="section">🔥 העסקאות האחרונות</h2><div class="grid">{cards}</div>'
                                            f'{older_html}', "index.html"), encoding="utf-8")
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

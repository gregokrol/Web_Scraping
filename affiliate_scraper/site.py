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
import re
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from .content import T, short_date
from urllib.parse import quote

from .db import Database
from .validation import _parse_iso

log = logging.getLogger(__name__)

e = html.escape
LOGO = Path(__file__).resolve().parents[1] / "assets" / "logo.png"
PUBLIC_STATUSES = ("approved", "publishing", "published")
DISCLOSURE = ("גילוי נאות: ייתכן שחלק מהקישורים באתר הם קישורי שותפים. רכישה דרכם עשויה לזכות אותנו בעמלה, ללא עלות נוספת עבורך. "
              "המחירים נבדקו במועד המצוין ועשויים להשתנות - המחיר הקובע הוא המחיר באתר החנות.")


@dataclass
class SiteConfig:
    base_url: str = ""
    telegram_url: str = ""
    title: str = "מחיר אמת"
    out_dir: str = "site"
    publish_dir: str = ""
    publish_branch: str = ""        # e.g. "gh-pages": publish into a branch of THIS project's repository instead
    repo_dir: str = "."             # the project's git repository (for publish_branch)
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


def _ago_tag(ts: str, fallback: str, paren: bool = False) -> str:
    """The page is static: "5 minutes ago" written at build time would stay on the page for hours.
    Store the real time; the visitor's browser turns it into "לפני X" (AGO_JS). Without JS: the fallback."""
    dt = _parse_iso(ts or "")
    if not dt:
        return e(fallback)
    iso = dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return f'<time class="ago"{" data-paren" if paren else ""} datetime="{e(iso)}">{e(fallback)}</time>'


AGO_JS = """<script>(function(){function ago(s){if(s<3600){var m=Math.max(1,Math.floor(s/60));
return m==1?"לפני דקה":m==2?"לפני 2 דקות":"לפני "+m+" דקות"}if(s<86400){var h=Math.floor(s/3600);
return h==1?"לפני שעה":h==2?"לפני שעתיים":"לפני "+h+" שעות"}var d=Math.floor(s/86400);
return d==1?"אתמול":d==2?"לפני יומיים":"לפני "+d+" ימים"}
document.querySelectorAll("time.ago").forEach(function(t){var x=Date.parse(t.getAttribute("datetime"));if(!x)return;
var s=Math.max(0,(Date.now()-x)/1000),a=ago(s);t.textContent=t.hasAttribute("data-paren")?" ("+a+")":a})})()</script>"""


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
            '<defs><linearGradient id="ga" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0f1f45" '
            'stop-opacity=".12"/><stop offset="1" stop-color="#0f1f45" stop-opacity="0"/></linearGradient></defs>'
            f'<line x1="{P}" y1="{H - P}" x2="{W - P}" y2="{H - P}" class="axis"/>'
            f'<text x="6" y="{Y(hi - pad) - 6:.0f}" text-anchor="start">{e(_money(hi - pad, currency))}</text>'
            + (f'<text x="6" y="{Y(lo + pad) + 18:.0f}" text-anchor="start">{e(_money(lo + pad, currency))}</text>'
               if hi - lo > 2 * pad + 0.005 else "")   # flat price: one label, not the same number twice
            + f'<text x="{P}" y="{H - 14}" text-anchor="start">{e(pts[0][0].strftime("%d.%m"))}</text>'
            f'<text x="{W - P}" y="{H - 14}" text-anchor="end">{e(pts[-1][0].strftime("%d.%m"))}</text>'
            f'<path d="{area}" class="area"/><path d="{line}" class="line"/>{dots}'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="8" class="now"/></svg>')


CSS = """
:root{color-scheme:light;--bg:#f4f5f7;--bg2:#eceef2;--card:#ffffff;--card2:#ffffff;--text:#0f1b3d;--muted:#5a6377;
--line:#dfe3ea;--navy:#0f1f45;--yellow:#ffcc1a;--yellow2:#ffcc1a;--cyan:#1673c4;--blue:#1673c4;--red:#d92d35;--green:#17773a;
--tg:#1a6fa8;--glow:none}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;font-family:"Rubik",system-ui,"Segoe UI",Arial,sans-serif;color:var(--text);line-height:1.55;background:var(--bg)}
a{color:var(--blue)}img{max-width:100%}
.wrap{max-width:1160px;margin:0 auto;padding:0 16px}
header{position:sticky;top:0;z-index:20;background:var(--navy);color:#fff;border-bottom:4px solid var(--yellow)}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:64px;flex-wrap:wrap}
.hsearch{flex:1 1 220px;max-width:340px}.hsearch input{width:100%;box-sizing:border-box;padding:9px 12px;border-radius:8px;
border:0;background:#fff;color:var(--text);font:inherit;font-size:15px}
.hsearch input:focus{outline:3px solid var(--yellow);outline-offset:0}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;color:#fff;font-weight:800;font-size:21px}
.brand img{width:44px;height:44px;border-radius:50%}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;font:inherit;font-weight:700;text-decoration:none;
border-radius:8px;padding:11px 16px;border:0;cursor:pointer}
.btn:hover{filter:brightness(.95)}
.btn-tg{background:var(--tg);color:#fff}
.btn-ghost{border:1px solid var(--line);color:var(--text);background:#fff}
.btn-buy{width:100%;font-size:19px;padding:15px;color:var(--navy);background:var(--yellow)}
.hero{padding:34px 0 10px}
.hero h1{margin:0 0 8px;font-size:clamp(28px,4.6vw,44px);line-height:1.15;font-weight:800;letter-spacing:-.3px;max-width:760px}
.hl{color:var(--navy);box-shadow:inset 0 -.32em 0 var(--yellow)}
.hero p{margin:0 0 18px;max-width:620px;color:var(--muted);font-size:18px}
.cta-row{display:flex;gap:10px;flex-wrap:wrap}
.stats{display:flex;gap:6px 18px;flex-wrap:wrap;margin:18px 0 0;color:var(--muted);font-size:15px}
.stat{display:inline}.stat b{color:var(--text);font-weight:800;font-variant-numeric:tabular-nums;margin-inline-end:4px}
.stat span{color:var(--muted)}
.trust{display:none}
h2.section{font-size:24px;margin:30px 0 12px;font-weight:800}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px;margin:0 0 20px}
.card{position:relative;display:flex;flex-direction:column;text-decoration:none;color:var(--text);border-radius:12px;overflow:hidden;
background:var(--card);border:1px solid var(--line)}
.card:hover{border-color:var(--navy)}
.thumb{position:relative;background:#fff;aspect-ratio:1/1;display:flex;align-items:center;justify-content:center;border-bottom:1px solid var(--line)}
.thumb img{width:100%;height:100%;object-fit:contain;padding:14px}
.ribbon{position:absolute;top:10px;right:10px;background:var(--navy);color:#fff;font-weight:700;font-size:13px;border-radius:6px;padding:4px 9px}
.ribbon.low{background:var(--navy);color:var(--yellow)}
.save{position:absolute;bottom:10px;left:10px;display:flex;align-items:baseline;gap:4px;background:var(--red);color:#fff;
font-weight:800;font-size:17px;line-height:1;border-radius:6px;padding:6px 8px}
.save small{font-size:12px;font-weight:600}
.new{position:absolute;top:10px;left:10px;background:var(--green);color:#fff;font-weight:700;font-size:12px;border-radius:6px;padding:4px 8px}
.card .b{padding:12px 14px 14px;display:flex;flex-direction:column;gap:4px;flex:1}
.card .t{font-weight:600;font-size:16px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:2.9em}
.pr{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.price{font-size:24px;font-weight:800;color:var(--text);font-variant-numeric:tabular-nums}
.was{color:var(--muted);text-decoration:line-through;font-size:15px}
.eilat{margin:4px 0 0;color:var(--muted);font-size:15px}.eilat b{color:var(--text);font-size:17px}
.meta{color:var(--muted);font-size:13px;margin-top:auto}
.ended{opacity:.6}.ended .ribbon{background:#6b7280;color:#fff}
.warn{background:#fff7d6;border:1px solid #f0d36a;color:#5c4500;border-radius:8px;padding:10px 12px;font-size:14px}
.empty{text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:12px;padding:32px;background:#fff}
.crumbs{margin:18px 0 6px;font-size:14px}.crumbs a{color:var(--muted)}
.deal{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);gap:24px;margin:10px 0 24px;align-items:start}
@media (max-width:820px){.deal{grid-template-columns:1fr}}
.gallery{position:sticky;top:84px;border-radius:12px;overflow:hidden;background:#fff;border:1px solid var(--line)}
.gallery img{width:100%;max-height:520px;object-fit:contain;padding:22px;display:block}
@media (max-width:820px){.gallery{position:relative;top:0}.gallery img{max-height:280px;padding:14px}}
.kind{display:inline-block;background:var(--navy);color:var(--yellow);border-radius:6px;padding:4px 10px;font-weight:700;font-size:14px}
.deal h1{font-size:clamp(22px,3.2vw,32px);line-height:1.25;margin:10px 0}
.bigprice{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:6px 0 4px}
.bigprice .price{font-size:42px}
.chip{background:var(--red);color:#fff;font-weight:800;border-radius:6px;padding:5px 10px;font-size:17px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px;margin:12px 0 20px}
.tile{display:flex;flex-direction:column;gap:4px;padding:14px;border-radius:10px;background:#fff;border:1px solid var(--line);
color:var(--text);text-decoration:none}.tile:hover{border-color:var(--navy)}.tile small{color:var(--muted)}
.prow{display:flex;gap:12px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line);color:var(--text);text-decoration:none}
.prow:last-child{border-bottom:0}.pimg{flex:0 0 110px;height:110px;border-radius:8px;background:#fff;border:1px solid var(--line);overflow:hidden;display:flex;
align-items:center;justify-content:center;font-size:40px}.pimg img{max-width:100%;max-height:100%;object-fit:contain}
.pb{display:flex;flex-direction:column;gap:2px;min-width:0}.pb .t{font-weight:600;overflow:hidden;text-overflow:ellipsis}
@media (max-width:640px){.pimg{flex-basis:90px;height:90px}}
.results-count{color:var(--muted);margin:6px 0 12px}
.pills{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}.pill{padding:6px 12px;border-radius:6px;border:1px solid var(--line);
color:var(--text);text-decoration:none;background:#fff}.crumbs{color:var(--muted);font-size:14px}.crumbs a{color:var(--muted)}
.panel{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;margin:14px 0}
.panel h2{margin:0 0 8px;font-size:19px}
ul.checks{list-style:none;padding:0;margin:0}
ul.checks li{position:relative;padding:7px 28px 7px 0;border-bottom:1px solid var(--line)}
ul.checks li:last-child{border-bottom:0}
ul.checks li::before{content:"✓";position:absolute;right:2px;top:7px;color:var(--green);font-weight:800}
.share{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.share a,.share button{flex:1;min-width:120px;text-align:center;border:1px solid var(--line);background:#fff;color:var(--text);
font:inherit;font-weight:600;border-radius:8px;padding:10px;text-decoration:none;cursor:pointer}
.share a:hover,.share button:hover{border-color:var(--navy)}
.tgbox{display:flex;align-items:center;gap:14px;background:var(--navy);color:#fff;border-radius:12px;padding:14px;margin:14px 0}
.tgbox img{width:52px;height:52px;border-radius:50%}.tgbox b{display:block;font-size:17px}.tgbox span{color:#c9d2e6;font-size:14px}
.tgbox .btn{margin-inline-start:auto;white-space:nowrap}
@media (max-width:560px){.tgbox{flex-wrap:wrap}.tgbox .btn{width:100%}}
svg.chart{width:100%;height:auto;direction:ltr}svg.chart .line{fill:none;stroke:var(--navy);stroke-width:2.5}
svg.chart .area{fill:url(#ga)}svg.chart circle{fill:var(--navy)}svg.chart circle.now{fill:var(--yellow);stroke:var(--navy);stroke-width:2}
svg.chart .axis{stroke:var(--line)}svg.chart text{fill:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse}th,td{padding:10px 8px;border-bottom:1px solid var(--line);text-align:right}
th{color:var(--muted);font-size:13px;font-weight:600}
td.n{direction:ltr;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
tr.best td{background:#eaf6ee}tr.best td:first-child{font-weight:700}
.besttag{background:var(--green);color:#fff;border-radius:6px;padding:2px 7px;font-size:12px;font-weight:700;margin-inline-start:6px}
.sticky{display:none}
@media (max-width:820px){.sticky{display:flex;position:fixed;bottom:0;left:0;right:0;z-index:30;gap:12px;align-items:center;padding:10px 14px;
background:#fff;border-top:1px solid var(--line)}
.sticky .price{font-size:22px}.sticky .btn-buy{width:auto;flex:1;font-size:17px;padding:12px}body.has-sticky{padding-bottom:82px}}
footer{color:#c9d2e6;font-size:13px;margin-top:40px;padding:20px 0;background:var(--navy)}
footer a{color:#fff}
.ico{width:18px;height:18px;flex:none}
.nav{display:flex;gap:4px;margin-inline-start:auto;margin-inline-end:10px}
.nav a{color:#dfe6f5;text-decoration:none;font-weight:600;padding:8px 10px;border-radius:6px;white-space:nowrap}
.nav a:hover{color:#fff;background:rgba(255,255,255,.1)}
@media (max-width:640px){.hsearch{order:3;flex-basis:100%;max-width:none;margin:0 0 10px}.nav a:nth-child(3){display:none}.nav a{padding:8px 6px}.brand span{display:none}.top .btn-tg{padding:9px 11px;font-size:14px}}
.back{display:inline-flex;align-items:center;gap:6px;color:var(--text);text-decoration:none;font-weight:600;border:1px solid var(--line);
background:#fff;border-radius:8px;padding:7px 12px}
.back:hover{border-color:var(--navy)}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 10px}
.searchbox{flex:1;min-width:240px;display:flex;align-items:center;gap:8px;background:#fff;border:1px solid var(--line);
border-radius:8px;padding:0 12px}
.searchbox:focus-within{border-color:var(--navy)}
.searchbox input{flex:1;background:transparent;border:0;outline:0;color:var(--text);font:inherit;font-size:16px;padding:12px 0}
#sort,#ssort{background:#fff;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:0 12px;font:inherit;min-height:46px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}
.chipf{background:#fff;border:1px solid var(--line);color:var(--text);border-radius:6px;padding:6px 12px;font:inherit;font-weight:600;cursor:pointer}
.chipf.on{background:var(--navy);color:#fff;border-color:var(--navy)}
.grid>.empty,.empty{grid-column:1/-1}
.empty b{display:block;color:var(--text);font-size:20px;margin-bottom:6px}
.card[hidden]{display:none}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
"""


def _layout(cfg: SiteConfig, title: str, description: str, body: str, path: str, depth: int = 0,
            og_image: str = "", json_ld: Optional[dict] = None, body_class: str = "", tail: str = "",
            absolute: bool = False, disclosure: str = "") -> str:
    # absolute: for 404.html, which GitHub Pages serves at ANY missing address (also deep ones like
    # stores/x/y.html) - relative links to style.css / logo.png would break there
    root = (cfg.base_url or "/") if absolute else "../" * depth
    canonical = f"{cfg.base_url}{path}" if cfg.base_url else ""
    tg = (f'<a class="btn btn-tg" href="{e(cfg.telegram_url)}" target="_blank" rel="noopener">{TG_ICON} הצטרפו לערוץ</a>'
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
<nav class="nav"><a href="{root}index.html#deals">עסקאות</a><a href="{root}stores/index.html">לפי חנות</a><a href="{root}how-we-check.html">איך אנחנו בודקים</a></nav>
<form class="hsearch" action="{root}search.html" role="search"><input type="search" name="q" placeholder="חיפוש מוצר" aria-label="חיפוש מוצר" enterkeyhint="search"></form>
{tg}
</div></header>
<main class="wrap">
{body}
</main>
<footer><div class="wrap">
<p>{e(disclosure or DISCLOSURE)}</p>
<p><a href="{root}how-we-check.html">איך אנחנו בודקים מחירים</a> · <a href="{root}feed.xml">RSS</a>{tg_footer}</p>
</div></footer>
{AGO_JS}
{tail}
</body>
</html>
"""


TG_ICON = ('<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M21.9 4.3 18.7 19.5c-.2 1-.9 '
           '1.3-1.8.8l-4.9-3.6-2.4 2.3c-.3.3-.5.5-1 .5l.3-5 9.1-8.2c.4-.4-.1-.6-.6-.2L6.2 13.2l-4.8-1.5c-1-.3-1-1 .2-1.5'
           'L20.5 3c.9-.3 1.6.2 1.4 1.3z"/></svg>')

LISTING_JS = """<script>
(function(){
  var q=document.getElementById("q"), sort=document.getElementById("sort"), none=document.getElementById("none");
  var grids=[].slice.call(document.querySelectorAll(".grid")), store="";
  function norm(t){return (t||"").toLowerCase().replace(/[\u0591-\u05c7]/g,"").trim();}
  function apply(){
    var words=norm(q.value).split(/\\s+/).filter(Boolean), shown=0;
    grids.forEach(function(g){
      var cards=[].slice.call(g.querySelectorAll(".card"));
      var key=sort.value;
      cards.sort(function(a,b){
        if(a.dataset.ended!==b.dataset.ended) return a.dataset.ended-b.dataset.ended;
        if(key==="price") return a.dataset.price-b.dataset.price;
        if(key==="save") return b.dataset.save-a.dataset.save;
        return b.dataset.time-a.dataset.time;
      }).forEach(function(c){g.appendChild(c);});
      cards.forEach(function(c){
        var hay=norm(c.dataset.title+" "+c.dataset.store);
        var ok=(!store||c.dataset.store===store)&&words.every(function(w){return hay.indexOf(w)>=0;});
        c.hidden=!ok; if(ok) shown++;
      });
    });
    none.hidden=shown>0;
  }
  q.addEventListener("input",apply); sort.addEventListener("change",apply);
  [].slice.call(document.querySelectorAll(".chipf")).forEach(function(b){
    b.addEventListener("click",function(){
      document.querySelectorAll(".chipf").forEach(function(x){x.classList.remove("on");});
      b.classList.add("on"); store=b.dataset.store; apply();
    });
  });
  var p=new URLSearchParams(location.search).get("q"); if(p){q.value=p; apply();}
})();
</script>"""

KIND_LABEL = {"price_drop": "ירידת מחיר", "price_change": "ירידת מחיר", "lowest_90d": "הנמוך ב-90 יום", "top_deal": "שווה בדיקה"}


def _low_label(d: dict) -> str:
    if d.get("lowest90"):
        return "הנמוך ב-90 יום"
    if d.get("lowest_since"):
        return f"הנמוך מאז {short_date(d['lowest_since'])}"
    kind = "top_deal" if d.get("kind") == "lowest_90d" else d.get("kind")   # an old "90 days" alert
    return KIND_LABEL.get(kind, "")


def _card(d: dict, cfg: SiteConfig, depth: int = 0) -> str:
    root = "../" * depth
    img = f'<img src="{e(_safe_url(d["image"]))}" alt="{e(d["title"])}" loading="lazy" referrerpolicy="no-referrer">' if d.get("image") else ""
    label = "העסקה הסתיימה" if d["ended"] else _low_label(d)
    ribbon = f'<span class="ribbon{" low" if d["lowest90"] and not d["ended"] else ""}">{e(label)}</span>' if label else ""
    save = (f'<span class="save"><bdi dir="ltr">-{d["savings_pct"]}%</bdi><small>חיסכון</small></span>'
            if d["savings_pct"] and not d["ended"] else "")
    new = '<span class="new">חדש</span>' if d["is_new"] and not d["ended"] else ""
    was = f'<span class="was">{e(_money(d["prev_price"], d["currency"]))}</span>' if d["prev_price"] and not d["ended"] else ""
    stale = '<span class="warn">המחיר עשוי להשתנות</span>' if d["stale"] and not d["ended"] else ""
    created = _parse_iso(d.get("created_at") or "")
    data = (f' data-title="{e((d["title"] or "").lower())}" data-store="{e(d["store"] or "")}" data-price="{d["price"] or 0}"'
            f' data-save="{d["savings_pct"] or 0}" data-time="{int(created.timestamp()) if created else 0}"'
            f' data-ended="{1 if d["ended"] else 0}"')
    return (f'<a class="card{" ended" if d["ended"] else ""}"{data} href="{root}{deal_path(d["id"])}">'
            f'<div class="thumb">{img}{ribbon}{new}{save}</div><div class="b">'
            f'<span class="t">{e(d["title"])}</span>'
            f'<div class="pr"><span class="price">{e(_money(d["price"], d["currency"]))}</span>{was}</div>'
            f'{stale}<span class="meta">{e(d["store"])} · נבדק {_ago_tag(d["checked_at"], "ב-" + _when(d["checked_at"]))}'
            '</span></div></a>')


def collect_deals(db: Database, cfg: SiteConfig, now: Optional[datetime] = None) -> list[dict]:
    """Approved deals + the REAL numbers that make them attractive (nothing here is estimated or invented)."""
    from .alerts import store_prices
    from .history import is_lowest_in_90d, lowest_since, stable_previous_price, stats_for_offer
    now = now or datetime.now(timezone.utc)
    rows = db.query(
        "SELECT a.id, a.kind, a.title, a.price, a.currency, a.content_json, a.created_at, a.status, a.offer_id, "
        "o.url, o.affiliate_url, o.image, o.store, o.domain, o.group_id, o.last_fetched_at, o.source, "
        "o.eilat_price, o.price AS current_price, o.currency AS current_currency, o.price_verified AS current_verified "
        f"FROM deal_alerts a JOIN offers o ON o.id=a.offer_id WHERE a.status IN ({','.join('?' * len(PUBLIC_STATUSES))}) "
        "ORDER BY a.id DESC", PUBLIC_STATUSES)
    out = []
    from .catalog import _setting_for, store_settings
    settings = store_settings()
    for r in rows:
        d = dict(r)
        # your name for the store (sources.json); a shop's page title can be a whole sentence
        st = _setting_for(d.get("domain") or "", settings)
        name = (st or {}).get("name") or d.get("store") or d.get("domain") or ""
        d["store"] = name if len(name) <= 30 else (d.get("domain") or name[:30])
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
        ref = stable_previous_price(st)
        prev = ref if (ref and ref > d["price"]) else None
        d["prev_price"] = prev
        drop = round((prev - d["price"]) / prev * 100) if prev else None
        # computed from the history NOW; a "90 days" claim saved with the deal is not trusted on its own
        d["lowest90"] = is_lowest_in_90d(st)
        d["lowest_since"] = None if d["lowest90"] else lowest_since(st)
        claims = [c for c in d["content"].get("claims", [])
                  if c.get("basis") != "price_history_90d" or d["lowest90"]]
        if d["lowest_since"] and not any(c.get("basis") == "price_history_since" for c in claims):
            claims.append({"text": T["he"]["lowest_since"].format(since=short_date(d["lowest_since"])),
                           "basis": "price_history_since"})
        d["content"]["claims"] = claims
        # savings vs the cheapest OTHER store; other pages of the same store never count
        vs_market = None
        other = [r["price"] for r in store_prices(db, d["group_id"], d["currency"]) if r["domain"] != d["domain"]]
        if other and min(other) > d["price"]:
            vs_market = round((min(other) - d["price"]) / min(other) * 100)
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
    claims = "".join(f"<li>{e(c['text'])}</li>" for c in content.get("claims", [])
                     if c.get("basis") not in ("verified_price", "eilat_price"))
    eilat = ""
    # the Eilat price belongs to the CURRENT store price - show it only while that is still the deal price
    if (d.get("eilat_price") and not d["ended"] and d["current_price"] is not None
            and abs(d["current_price"] - d["price"]) <= 0.01 * d["price"] and d["eilat_price"] < d["price"]):
        eilat = (f'<p class="eilat">מחיר באילת: <b>{e(_money(d["eilat_price"], d["currency"]))}</b>'
                 '<small> (לפי אתר החנות, ללא מע"מ)</small></p>')
    hist = db.query("SELECT observed_at, price FROM price_history WHERE offer_id=? AND price_verified=1 AND currency=? "
                    "ORDER BY observed_at", (d["offer_id"], d["currency"]))
    chart = price_chart_svg([(h["observed_at"], h["price"]) for h in hist], d["currency"])
    chart_html = (f'<div class="panel"><h2>היסטוריית המחיר</h2>{chart}'
                  f'<p class="meta">מבוסס על {len(hist)} בדיקות מחיר מאומתות. הנקודה הצהובה היא המחיר האחרון.</p></div>'
                  if chart else "")
    from .alerts import store_prices
    others = store_prices(db, d["group_id"], d["currency"])   # one row per store
    compare = ""
    if len(others) >= 2:
        rows = ""
        for i, o in enumerate(others):
            ship = "חינם" if o["shipping_cost"] == 0 else (_money(o["shipping_cost"], o["currency"]) or "לא ידוע")
            tag = '<span class="besttag">הכי זול</span>' if i == 0 else ""
            rows += (f'<tr class="{"best" if i == 0 else ""}"><td>{e(o["store"])}{tag}</td>'
                     f'<td class="n">{e(_money(o["price"], o["currency"]))}</td><td class="n">{e(ship)}</td>'
                     f'<td class="n meta">{e(_when(o["last_fetched_at"]))}</td></tr>')
        compare = (f'<div class="panel"><h2>אותו מוצר בחנויות אחרות</h2><table><tr><th>חנות</th><th>מחיר</th>'
                   f'<th>משלוח</th><th>נבדק</th></tr>{rows}</table></div>')
    warn = ""
    if d["ended"]:
        warn = ('<p class="warn">המחיר עלה מאז שהעסקה פורסמה, כלומר העסקה הסתיימה. '
                f'המחיר האחרון שנבדק: {e(_money(d["current_price"], d["currency"]))}.</p>')
    elif d["stale"]:
        warn = '<p class="warn">המחיר נבדק לפני יותר מ-48 שעות ועשוי להשתנות. בדקו את המחיר המעודכן באתר החנות.</p>'
    img = f'<img src="{e(_safe_url(d["image"]))}" alt="{e(d["title"])}" referrerpolicy="no-referrer">' if d.get("image") else ""
    label = "העסקה הסתיימה" if d["ended"] else _low_label(d)
    chip = f'<span class="chip">חיסכון {d["savings_pct"]}%</span>' if d["savings_pct"] and not d["ended"] else ""
    was = (f'<p class="meta">מחיר קודם שנבדק באותה חנות: <span class="was">{e(_money(d["prev_price"], d["currency"]))}</span></p>'
           if d["prev_price"] and not d["ended"] else "")
    share = ""
    if url:
        msg = f'{d["title"]} ב-{_money(d["price"], d["currency"])} - נבדק במחיר אמת'
        share = (f'<div class="share"><a href="https://wa.me/?text={quote(msg + " " + url)}" target="_blank" rel="noopener">'
                 f'וואטסאפ</a><a href="https://t.me/share/url?url={quote(url)}&text={quote(msg)}" target="_blank" '
                 f'rel="noopener">{TG_ICON} טלגרם</a><button type="button" data-copy="{e(url)}">העתקת קישור</button></div>')
    tgbox = (f'<div class="tgbox"><img src="../logo.png" alt=""><div><b>רוצים לקבל עסקאות כאלה ראשונים?</b>'
             f'<span>כל עסקה נבדקת לפני שהיא מגיעה אליכם.</span></div>'
             f'<a class="btn btn-tg" href="{e(cfg.telegram_url)}" target="_blank" rel="noopener">{TG_ICON} הצטרפו לערוץ</a></div>'
             if cfg.telegram_url else "")
    buy_text = f'לרכישה {e(heb("ב", d["store"]))} ←'
    body = (f'<p class="crumbs"><a class="back" href="../index.html#deals">→ חזרה לכל העסקאות</a>'
            f'<span class="muted"> · {e(d["store"])}</span></p>'
            f'<div class="deal"><div class="gallery">{img}</div><div>'
            f'<span class="kind">{e(label)}</span>'
            f'<h1>{e(d["title"])}</h1>'
            f'<div class="bigprice"><span class="price">{e(_money(d["price"], d["currency"]))}</span>{chip}</div>{was}{eilat}'
            f'<p class="meta">{e(heb("ב", d["store"]))} · המחיר נבדק ב-{e(_when(d["checked_at"]))}{_ago_tag(d["checked_at"], "", paren=True)}</p>{warn}'
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
<p class="crumbs"><a class="back" href="index.html#deals">→ חזרה לכל העסקאות</a></p>
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


def _claimed_row(p: dict) -> str:
    """A price from a store's own Telegram channel: said so, with the date, and our checked price if we have one."""
    href = _safe_url(p["url"])
    ref = (f'<span class="was">{e(_money(p["prev_price"], p["currency"]))}</span> <small>בפוסט הקודם</small>'
           if p.get("prev_price") else
           (f'<span class="was">{e(_money(p["was_price"], p["currency"]))}</span> <small>לפי החנות</small>'
            if p.get("was_price") else ""))
    match = "".join(f'<span class="meta">נבדק אצלנו: {e(m["store"] or m["domain"])} {e(_money(m["price"], p["currency"]))}</span>'
                    for m in p.get("matches") or [])
    coupon = (f'<span class="meta">קופון לפי החנות: {e(p["coupon"])} (התנאים לפי החנות)</span>'
              if p.get("coupon") else "")
    img = (f'<img src="{e(_safe_url(p["image"]))}" alt="" loading="lazy" referrerpolicy="no-referrer" '
           'onerror="this.replaceWith(document.createTextNode(\'📢\'))">' if p.get("image") else "📢")
    return (f'<a class="prow" href="{e(href)}" target="_blank" rel="sponsored nofollow noopener"><span class="pimg">{img}</span>'
            f'<span class="pb"><span class="t">{e(p["title"])}</span>'
            f'<span class="price">{e(_money(p["price"], p["currency"]))}</span>{ref}'
            f'<span class="meta">לפי ערוץ {e(p["store"])}, {e(_when(p["posted_at"]).split(" ")[0])}. לא נבדק אצלנו בדף המוצר.'
            f'{" התמונה מהפוסט של החנות." if p.get("image") else ""}</span>'
            f'{coupon}{match}</span></a>')


def heb(prefix: str, name: str) -> str:
    """A Hebrew prefix letter before a name: "בבאג", but "ב-KSP" (a hyphen before Latin letters or digits)."""
    name = (name or "").strip()
    return f"{prefix}-{name}" if re.match(r"[A-Za-z0-9]", name) else f"{prefix}{name}"


CLAIMED_DISCLOSURE = ("גילוי נאות: ייתכן שחלק מהקישורים באתר הם קישורי שותפים. רכישה דרכם עשויה לזכות אותנו בעמלה, ללא עלות נוספת עבורך. "
                      "המחירים בעמוד הזה הם לפי ערוץ הטלגרם של החנות ולא נבדקו אצלנו - המחיר הקובע הוא המחיר באתר החנות.")


def _product_row(p: dict, root: str, cfg: SiteConfig) -> str:
    """One product in a store/category list. Our deal page when there is one, else the store's page."""
    if p.get("claimed"):
        return _claimed_row(p)
    if p.get("deal_id"):
        href, attrs = f"{root}{deal_path(p['deal_id'])}", ""
    else:
        href, attrs = _safe_url(p.get("affiliate_url") or p["url"]), ' target="_blank" rel="sponsored nofollow noopener"'
    from .catalog import CAT_BY_SLUG, classify
    icon = CAT_BY_SLUG.get(classify(p.get("title") or ""), ("", "", "🛒"))[2]
    img = (f'<img src="{e(_safe_url(p["image"]))}" alt="" loading="lazy" referrerpolicy="no-referrer" '
           f'onerror="this.replaceWith(document.createTextNode(\'{icon}\'))">' if p.get("image") else icon)
    stars = (f' · ⭐ {p["rating"]:.1f} ({int(p["review_count"])} ביקורות)'
             if p.get("rating") and p.get("review_count") else "")
    deal = '<span class="chip">עסקה שנבדקה</span>' if p.get("deal_id") else ""
    return (f'<a class="prow" href="{e(href)}"{attrs}><span class="pimg">{img}</span><span class="pb">'
            f'<span class="t">{e(p["title"])}</span>'
            f'<span class="price">{e(_money(p["price"], p["currency"]))}</span>{deal}'
            f'<span class="meta">נבדק {_ago_tag(p["last_fetched_at"], "ב-" + _when(p["last_fetched_at"]))}{e(stars)}</span>'
            '</span></a>')


def build_store_pages(db: Database, cfg: SiteConfig, out: Path, now: Optional[datetime] = None) -> list[str]:
    """stores/index.html, stores/<store>.html (its categories), stores/<store>/<category>.html (top 5 lists)."""
    from .catalog import (CAT_BY_SLUG, CATEGORIES, TOP_N, biggest_stated_discount, cheapest, collect_catalog,
                          most_popular, store_slug)
    catalog = collect_catalog(db, now)
    (out / "stores").mkdir(parents=True, exist_ok=True)
    pages = ["stores/index.html"]
    order = sorted(catalog, key=lambda d: catalog[d]["name"])
    tiles = "".join(
        f'<a class="tile" href="{store_slug(d)}.html"><b>{e(catalog[d]["name"])}</b>'
        f'<small>{sum(len(v) for v in catalog[d]["cats"].values())} '
        f'{"מוצרים לפי ערוץ החנות" if catalog[d].get("claimed") else "מוצרים שנבדקו"}</small></a>' for d in order)
    body = ('<p class="crumbs"><a href="../index.html">ראשי</a> › לפי חנות</p><h1>בחרו חנות</h1>'
            '<p class="meta">רק מחירים שנבדקו ב-48 השעות האחרונות, של מוצרים במלאי.</p>'
            f'<div class="tiles">{tiles or "<p class=empty>עוד אין מוצרים שנבדקו. הם יופיעו אחרי הסריקה הבאה.</p>"}</div>')
    (out / "stores" / "index.html").write_text(
        _layout(cfg, f"לפי חנות | {cfg.title}", "בחרו חנות וקטגוריה: 5 המוצרים הזולים והפופולריים, במחירים שנבדקו.",
                body, "stores/index.html", depth=1), encoding="utf-8")
    for dom in order:
        name, cats, slug = catalog[dom]["name"], catalog[dom]["cats"], store_slug(dom)
        (out / "stores" / slug).mkdir(exist_ok=True)
        cat_tiles = "".join(
            f'<a class="tile" href="{slug}/{c}.html"><b>{CAT_BY_SLUG[c][2]} {e(CAT_BY_SLUG[c][1])}</b>'
            f'<small>{len(cats[c])} מוצרים</small></a>' for c, *_ in CATEGORIES if cats.get(c))
        body = (f'<p class="crumbs"><a href="../index.html">ראשי</a> › <a href="index.html">לפי חנות</a> › {e(name)}</p>'
                f'<h1>{e(name)}</h1><p class="meta">בחרו קטגוריה</p><div class="tiles">{cat_tiles}</div>')
        (out / "stores" / f"{slug}.html").write_text(
            _layout(cfg, f"{name} | {cfg.title}",
                    (f"{name}: קטגוריות ומוצרים, במחירים לפי ערוץ הטלגרם של החנות (לא נבדקו אצלנו)."
                     if catalog[dom].get("claimed") else f"{name}: קטגוריות ומוצרים במחירים שנבדקו."), body,
                    f"stores/{slug}.html", depth=1,
                    disclosure=CLAIMED_DISCLOSURE if catalog[dom].get("claimed") else ""), encoding="utf-8")
        pages.append(f"stores/{slug}.html")
        for c, cname, emoji, _w in CATEGORIES:
            if not cats.get(c):
                continue
            low = "".join(_product_row(p, "../../", cfg) for p in cheapest(cats[c]))
            claimed = catalog[dom].get("claimed")
            if claimed:
                pop_items = biggest_stated_discount(cats[c])
                pop_title = f"{TOP_N} ההנחות הגדולות (לפי החנות)"
                pop_empty = '<p class="meta">אין בפוסטים מהשבוע האחרון מחיר קודם להשוואה.</p>'
            else:
                pop_items = most_popular(cats[c])
                pop_title = f"⭐ {TOP_N} הפופולריים (לפי ביקורות בחנות)"
                pop_empty = '<p class="meta">החנות לא מציגה מספיק ביקורות בקטגוריה הזאת, ולכן אין כאן דירוג פופולריות.</p>'
            pop = "".join(_product_row(p, "../../", cfg) for p in pop_items) if pop_items else pop_empty
            note = (f'<p class="warn">המחירים כאן הם מה {e(heb("ש", name))} פרסמה בערוץ הטלגרם שלה בשבוע האחרון. '
                    'הם לא נבדקו אצלנו בדף המוצר. בדקו את המחיר באתר החנות לפני רכישה.</p>' if claimed else "")
            other = "".join(f'<a class="pill" href="{oc}.html">{oe} {e(on)}</a>'
                            for oc, on, oe, _ in CATEGORIES if oc != c and cats.get(oc))
            body = (f'<p class="crumbs"><a href="../../index.html">ראשי</a> › <a href="../index.html">לפי חנות</a> › '
                    f'<a href="../{slug}.html">{e(name)}</a> › {e(cname)}</p>'
                    f'<h1>{emoji} {e(cname)} {e(heb("ב", name))}</h1>{note}'
                    f'<div class="panel"><h2>{TOP_N} הזולים</h2>{low}</div>'
                    f'<div class="panel"><h2>{pop_title}</h2>{pop}</div>'
                    + (f'<p class="meta">עוד קטגוריות {e(heb("ב", name))}:</p><div class="pills">{other}</div>' if other else "")
                    + ('<p class="meta">המחירים לפי ערוץ החנות ולא נבדקו אצלנו. המחיר הקובע הוא באתר החנות.</p>' if claimed else
                       '<p class="meta">המחירים נבדקו במועד המצוין ועשויים להשתנות. המחיר הקובע הוא באתר החנות.</p>'))
            (out / "stores" / slug / f"{c}.html").write_text(
                _layout(cfg, f"{cname} {heb('ב', name)} | {cfg.title}",
                        (f"{cname} {heb('ב', name)}: מחירים לפי ערוץ הטלגרם של החנות מהשבוע האחרון, לא נבדקו אצלנו."
                         if claimed else f"{cname} {heb('ב', name)}: 5 הזולים ו-5 הפופולריים, במחירים שנבדקו."),
                        body, f"stores/{slug}/{c}.html", depth=2,
                        disclosure=CLAIMED_DISCLOSURE if claimed else ""), encoding="utf-8")
            pages.append(f"stores/{slug}/{c}.html")
    return pages


SEARCH_JS = """<script>
(function(){
  var box=document.getElementById("sq"), sort=document.getElementById("ssort"), list=document.getElementById("results"),
      count=document.getElementById("rcount"), data=[];
  function norm(t){return (t||"").toLowerCase().replace(/[\\u0591-\\u05c7]/g,"").replace(/[-_,./()"']/g," ").trim();}
  function money(v){return "\u20aa"+Number(v).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});}
  function el(tag,cls,text){var x=document.createElement(tag); if(cls)x.className=cls; if(text!=null)x.textContent=text; return x;}
  function row(it){
    var a=el("a","prow"); a.href=it.h;
    if(!it.r){a.target="_blank"; a.rel="sponsored nofollow noopener";}
    var im=el("span","pimg");
    if(it.i){var img=el("img"); img.referrerPolicy="no-referrer"; img.src=it.i; img.alt=""; img.loading="lazy";
      img.onerror=function(){im.textContent="\\ud83d\\uded2";}; im.appendChild(img);} else im.textContent=it.c?"📢":"\\ud83d\\uded2";
    var b=el("span","pb"); b.appendChild(el("span","t",it.t)); b.appendChild(el("span","price",money(it.p)));
    if(it.d) b.appendChild(el("span","chip","\u05e2\u05e1\u05e7\u05d4 \u05e9\u05e0\u05d1\u05d3\u05e7\u05d4"));
    b.appendChild(el("span","meta",it.s+(it.c?" \u00b7 \u05dc\u05e4\u05d9 \u05e2\u05e8\u05d5\u05e5 \u05d4\u05d7\u05e0\u05d5\u05ea, \u05dc\u05d0 \u05e0\u05d1\u05d3\u05e7 \u05d0\u05e6\u05dc\u05e0\u05d5":" \u00b7 \u05e0\u05d1\u05d3\u05e7 "+it.w)));
    a.appendChild(im); a.appendChild(b); return a;
  }
  function run(){
    var words=norm(box.value).split(/\\s+/).filter(function(w){return w.length>0;});
    list.innerHTML="";
    if(!words.length){count.textContent="\u05d4\u05e7\u05dc\u05d9\u05d3\u05d5 \u05e9\u05dd \u05e9\u05dc \u05de\u05d5\u05e6\u05e8, \u05de\u05d5\u05ea\u05d2 \u05d0\u05d5 \u05d3\u05d2\u05dd"; return;}
    var hits=data.filter(function(it){var hay=norm(it.t+" "+it.s); return words.every(function(w){return hay.indexOf(w)>=0;});});
    if(sort.value==="price") hits.sort(function(a,b){return a.p-b.p;});
    else hits.sort(function(a,b){return (b.d?1:0)-(a.d?1:0)||(a.c?1:0)-(b.c?1:0)||a.p-b.p;});
    count.textContent=hits.length? hits.length+" \u05ea\u05d5\u05e6\u05d0\u05d5\u05ea"+(hits.length>60?" (\u05de\u05d5\u05e6\u05d2\u05d5\u05ea 60 \u05d4\u05e8\u05d0\u05e9\u05d5\u05e0\u05d5\u05ea)":"") : "\u05dc\u05d0 \u05e0\u05de\u05e6\u05d0 \u05de\u05d5\u05e6\u05e8. \u05e0\u05e1\u05d5 \u05de\u05d9\u05dc\u05d4 \u05d0\u05d7\u05e8\u05ea \u05d0\u05d5 \u05e9\u05dd \u05de\u05d5\u05ea\u05d2.";
    hits.slice(0,60).forEach(function(it){list.appendChild(row(it));});
  }
  var q=new URLSearchParams(location.search).get("q")||""; box.value=q;
  fetch("search.json").then(function(r){return r.json();}).then(function(d){data=d; run();})
    .catch(function(){count.textContent="\u05d4\u05d7\u05d9\u05e4\u05d5\u05e9 \u05dc\u05d0 \u05d6\u05de\u05d9\u05df \u05db\u05e8\u05d2\u05e2.";});
  box.addEventListener("input",run); sort.addEventListener("change",run);
  document.querySelectorAll(".hsearch input").forEach(function(i){i.value=q;});
})();
</script>"""


def build_search(db: Database, cfg: SiteConfig, out: Path, now: Optional[datetime] = None) -> int:
    """search.html + search.json: every checked product (and the stores' channel prices, marked), searched in the
    visitor's browser - a static site needs no server for it. Returns how many products are searchable."""
    from .catalog import search_items
    items = []
    for it in search_items(db, now):
        if it["deal_id"]:
            href, ours = deal_path(it["deal_id"]), True
        else:
            href, ours = _safe_url(it["url"]), False
            if not href.startswith("https://"):
                continue
        items.append({"t": it["title"][:160], "p": round(float(it["price"]), 2), "s": it["store"], "h": href,
                      "i": _safe_url(it["image"]) if it["image"] else "", "c": 1 if it["claimed"] else 0,
                      "d": 1 if it["deal_id"] else 0, "r": 1 if ours else 0, "w": _when(it["checked"]).split(" ")[0]})
    (out / "search.json").write_text(json.dumps(items, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    body = ('<p class="crumbs"><a href="index.html">ראשי</a> › חיפוש</p><h1>חיפוש מוצר</h1>'
            '<div class="toolbar"><label class="searchbox">🔎<input id="sq" type="search" '
            'placeholder="למשל: PS5, אוזניות JBL, מחשב נייד" aria-label="חיפוש מוצר" autofocus></label> '
            '<select id="ssort" aria-label="מיון">'
            '<option value="best">עסקאות שנבדקו קודם</option><option value="price">הזול ביותר קודם</option></select></div>'
            '<p class="results-count" id="rcount"></p><div class="panel" id="results"></div>'
            f'<p class="meta">חיפוש ב-{len(items)} מוצרים: מחירים שנבדקו ב-48 השעות האחרונות, ומחירים מערוצי החנויות '
            '(מסומנים "לפי ערוץ החנות"). המחיר הקובע הוא באתר החנות.</p>')
    (out / "search.html").write_text(_layout(cfg, f"חיפוש מוצר | {cfg.title}",
                                             "חיפוש מוצר בין כל המחירים שנבדקו בחנויות בישראל.", body, "search.html",
                                             tail=SEARCH_JS), encoding="utf-8")
    return len(items)


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

    # the lists only show deals with a proven saving (or an ended one, marked as such)
    listed = [d for d in deals if d["savings_pct"] or d["lowest90"] or d["ended"]]
    recent = best_per_product([d for d in listed if d["recent"]])
    cards = "".join(_card(d, cfg) for d in recent) or '<p class="empty">עסקאות חדשות יופיעו כאן בקרוב.</p>'
    older = best_per_product([d for d in listed if not d["recent"]])
    older_html = ('<h2 class="section">עסקאות קודמות</h2><div class="grid">' + "".join(_card(d, cfg) for d in older[:60])
                  + "</div>" if older else "")
    # real counters only
    n_stores = db.query("SELECT COUNT(DISTINCT domain) n FROM offers WHERE price_verified=1")[0]["n"]
    n_checks = db.query("SELECT COUNT(*) n FROM price_history WHERE price_verified=1")[0]["n"]
    tg_btn = (f'<a class="btn btn-tg" href="{e(cfg.telegram_url)}" target="_blank" rel="noopener">{TG_ICON} הצטרפו לערוץ בטלגרם</a>'
              if cfg.telegram_url else "")
    hero = (f'<section class="hero"><h1>בדקנו אם <span class="hl">המבצע</span> באמת מבצע.</h1>'
            f'<p>כל עסקה כאן נבדקה מול המחיר שלה קודם באותה חנות, ואיפה שאפשר גם מול חנויות אחרות. מה שלא עבר את הבדיקה, לא עלה.</p>'
            f'<div class="cta-row">{tg_btn}<a class="btn btn-ghost" href="how-we-check.html">איך בודקים</a></div>'
            f'<div class="stats"><div class="stat"><b>{len(deals):,}</b><span>{"עסקה שעלתה" if len(deals) == 1 else "עסקאות שעלו"}</span></div>'
            f'<div class="stat"><b>{n_stores:,}</b><span>חנויות שמשווים</span></div>'
            f'<div class="stat"><b>{n_checks:,}</b><span>בדיקות מחיר עד היום</span></div></div>'
            f'</section>')
    stores = sorted({d["store"] for d in recent + older if d.get("store")})
    chips = "".join(f'<button type="button" class="chipf" data-store="{e(st)}">{e(st)}</button>' for st in stores)
    toolbar = ""
    if recent or older:
        toolbar = (f'<div class="toolbar"><div class="searchbox"><span aria-hidden="true">🔎</span>'
                   f'<input id="q" type="search" placeholder="חיפוש: שם מוצר, דגם או חנות" aria-label="חיפוש עסקאות"></div>'
                   f'<select id="sort" aria-label="מיון"><option value="time">החדשות ביותר</option>'
                   f'<option value="save">החיסכון הגדול ביותר</option><option value="price">המחיר הנמוך ביותר</option></select>'
                   f'</div><div class="chips"><button type="button" class="chipf on" data-store="">כל החנויות</button>{chips}</div>'
                   f'<p id="none" class="empty" hidden>לא נמצאו עסקאות שמתאימות לחיפוש. נסו מילה אחרת.</p>')
    empty_tg = (f'<p><a class="btn btn-tg" href="{e(cfg.telegram_url)}" target="_blank" rel="noopener">{TG_ICON} '
                f'קבלו את העסקה הראשונה בטלגרם</a></p>' if cfg.telegram_url else "")
    if not recent:
        cards = f'<div class="empty"><b>עסקאות חדשות יופיעו כאן בקרוב.</b><p>אנחנו בודקים מחירים כל יום.</p>{empty_tg}</div>'
    (out / "index.html").write_text(_layout(cfg, f"{cfg.title} | עסקאות שנבדקו באמת",
                                            "עסקאות ודילים בישראל שנבדקו מול היסטוריית המחיר ומול חנויות אחרות.",
                                            f'{hero}<h2 class="section" id="deals">עלו לאחרונה</h2>{toolbar}'
                                            f'<div class="grid" id="grid">{cards}</div>{older_html}', "index.html",
                                            tail=LISTING_JS if (recent or older) else ""), encoding="utf-8")
    (out / "404.html").write_text(_layout(cfg, f"העמוד לא נמצא | {cfg.title}", "העמוד לא נמצא.",
                                          '<div class="hero"><h1>העמוד לא נמצא</h1>'
                                          '<p>ייתכן שהעסקה הוסרה, או שהאתר מתעדכן ממש עכשיו. נסו שוב בעוד דקה.</p>'
                                          f'<div class="cta-row"><a class="btn btn-buy" style="width:auto" href="{cfg.base_url or "/"}index.html">'
                                          f'לכל העסקאות</a> <a class="btn" style="width:auto" href="{cfg.base_url or "/"}stores/index.html">'
                                          '🏬 לפי חנות</a></div></div>', "404.html", absolute=True), encoding="utf-8")
    (out / "how-we-check.html").write_text(_layout(cfg, f"איך אנחנו בודקים מחירים | {cfg.title}",
                                                   "השיטה: מחיר מאומת, היסטוריית מחיר, השוואה בין חנויות ואישור ידני.",
                                                   HOW_WE_CHECK, "how-we-check.html"), encoding="utf-8")
    build_search(db, cfg, out, now)
    pages = ["index.html", "how-we-check.html", "search.html", *[deal_path(d["id"]) for d in deals],
             *build_store_pages(db, cfg, out, now)]
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


def publish_to_branch(cfg: SiteConfig, run=subprocess.run) -> str:
    """Commit the built site to ``cfg.publish_branch`` of the project's own repository and push it.

    Uses a separate temporary git index, so the files you work on, your current branch and its history are
    never touched: the website lives only in its own branch (GitHub Pages: "Deploy from a branch" -> gh-pages).
    """
    import os
    import tempfile
    repo = Path(cfg.repo_dir).resolve()
    src = Path(cfg.out_dir).resolve()
    branch = cfg.publish_branch
    if not (repo / ".git").exists():
        raise RuntimeError(f"{repo} is not a git repository - run the system from its folder")
    if not (src / "index.html").exists():
        raise RuntimeError("build the site first")
    if branch in ("main", "master") or not all(ch.isalnum() or ch in "-_/." for ch in branch):
        raise RuntimeError(f"publish_branch {branch!r} not allowed - use a separate branch such as gh-pages")
    tmp = Path(tempfile.mkdtemp(prefix="site-index-"))
    env = {**os.environ, "GIT_INDEX_FILE": str(tmp / "index")}
    gitdir = ["git", f"--git-dir={repo / '.git'}"]

    def git(*args: str, use_index: bool = False, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
        return run([*gitdir, *args], capture_output=True, text=True, check=False, env=env if use_index else None,
                   cwd=str(cwd) if cwd else None)

    try:
        git("fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")   # ok if it doesn't exist yet
        added = git(f"--work-tree={src}", "add", "-A", "-f", ".", use_index=True, cwd=src)
        if added.returncode != 0:
            raise RuntimeError(f"git add failed: {added.stderr.strip()[:300]}")
        tree = git("write-tree", use_index=True).stdout.strip()
        parent = (git("rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}").stdout.strip()
                  or git("rev-parse", "-q", "--verify", f"refs/heads/{branch}").stdout.strip())
        if parent and git("rev-parse", f"{parent}^{{tree}}").stdout.strip() == tree:
            return "no changes"
        msg = f"Update site {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"
        made = git("commit-tree", tree, *(["-p", parent] if parent else []), "-m", msg)
        if made.returncode != 0:
            raise RuntimeError(f"git commit failed: {made.stderr.strip()[:300]}")
        commit = made.stdout.strip()
        git("update-ref", f"refs/heads/{branch}", commit)
        pushed = git("push", "origin", f"refs/heads/{branch}:refs/heads/{branch}")
        if pushed.returncode != 0:
            raise RuntimeError(f"git push failed: {pushed.stderr.strip()[:300]}")
        return f"published to branch {branch}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def publish_site(cfg: SiteConfig, run=subprocess.run) -> str:
    """Publish the built site: into a branch of this repository (publish_branch) or a separate clone (publish_dir)."""
    from .telegram import agent_run
    if agent_run():
        raise RuntimeError("publishing the website is blocked while the agent manager runs - the owner publishes")
    if cfg.publish_branch:
        return publish_to_branch(cfg, run)
    target = Path(cfg.publish_dir)
    if not cfg.publish_dir or not (target / ".git").exists():
        raise RuntimeError("the website has nowhere to be published - run: py main.py site setup --same-repo "
                           "(or --publish-dir <a git clone>), see RUN_ON_WINDOWS.md, website section")
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

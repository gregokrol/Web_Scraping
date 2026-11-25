"""Instagram carousel images from verified deals: 1080x1350 slides in the "מחיר אמת" look.

A cover, one slide per deal (price, verified saving, the earlier price, store, check date) and a closing
slide (the channel, the disclosure). Only facts the deal already has - the same rule as the posts. The
slides are HTML rendered by the local Chromium (Playwright); nothing is posted - the files and a caption
go to the owner for approval.
"""

from __future__ import annotations

import base64
import html
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from .content import short_date

e = html.escape


def heb(prefix: str, name: str) -> str:
    from .site import heb as _heb
    return _heb(prefix, name)
W, H = 1080, 1350
DISCLOSURE = ("ייתכן שחלק מהקישורים הם קישורי שותפים. רכישה דרכם עשויה לזכות אותנו בעמלה, ללא עלות נוספת. "
              "המחירים נבדקו במועד המצוין ועשויים להשתנות.")
HASHTAGS = "#מחיראמת #דילים #מבצעים #חיסכון #קניותחכמות #בלאקפריידי #גיימינג #טכנולוגיה"

CSS = f"""
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{width:{W}px;height:{H}px}}
body{{font-family:"Rubik","Segoe UI",Arial,sans-serif;direction:rtl;color:#f2f6ff;
background:radial-gradient(120% 80% at 80% 0%,#163a7a 0%,#0a1430 45%,#060b18 100%);overflow:hidden}}
.slide{{position:relative;width:{W}px;height:{H}px;padding:72px 72px 64px;display:flex;flex-direction:column}}
.brand{{display:flex;align-items:center;gap:18px;font-weight:800;font-size:40px}}
.brand img{{width:84px;height:84px;border-radius:50%;box-shadow:0 0 30px rgba(34,211,238,.6)}}
.n{{position:absolute;top:84px;left:72px;font-size:30px;color:#9db0d4;font-weight:600}}
h1{{font-size:92px;line-height:1.08;font-weight:800;margin-top:120px}}
h1 .y{{color:#ffd21f}}
.sub{{font-size:40px;color:#9db0d4;margin-top:36px;line-height:1.35}}
.pic{{margin-top:44px;height:520px;border-radius:36px;background:#fff;display:flex;align-items:center;justify-content:center;
overflow:hidden;font-size:220px}}
.pic img{{max-width:92%;max-height:92%;object-fit:contain}}
.title{{font-size:46px;font-weight:700;line-height:1.25;margin-top:40px;max-height:174px;overflow:hidden}}
.row{{display:flex;align-items:center;gap:26px;margin-top:28px;flex-wrap:wrap}}
.price{{font-size:104px;font-weight:800;direction:ltr}}
.save{{background:linear-gradient(135deg,#ffd21f,#ffb800);color:#1a1300;font-weight:800;font-size:44px;
border-radius:22px;padding:12px 26px}}
.was{{font-size:40px;color:#9db0d4;text-decoration:line-through;direction:ltr}}
.meta{{margin-top:auto;font-size:30px;color:#9db0d4;line-height:1.4}}
.meta b{{color:#f2f6ff}}
.cta{{margin-top:70px;font-size:60px;font-weight:800;line-height:1.2}}
.tg{{margin-top:44px;display:inline-block;background:#2aabee;color:#fff;font-size:46px;font-weight:700;border-radius:26px;
padding:22px 40px;direction:ltr}}
.disc{{margin-top:auto;font-size:26px;color:#9db0d4;line-height:1.45}}
.checks{{margin-top:56px;font-size:42px;line-height:1.7}}
.peek{{margin-top:64px;display:flex;flex-direction:column;gap:22px}}
.peek div{{display:flex;justify-content:space-between;align-items:center;gap:20px;background:rgba(255,255,255,.06);
border:1px solid #22345e;border-radius:24px;padding:22px 28px;font-size:36px}}
.peek span{{overflow:hidden;white-space:nowrap;text-overflow:ellipsis}}
.peek b{{color:#ffd21f;white-space:nowrap}}
.biglogo{{width:300px;height:300px;border-radius:50%;margin:70px auto 0;display:block;box-shadow:0 0 60px rgba(34,211,238,.55)}}
"""


def _money(v: Optional[float]) -> str:
    return f"₪{v:,.2f}" if v is not None else ""


def _date(ts: str) -> str:
    from .validation import _parse_iso
    dt = _parse_iso(ts or "")
    return dt.astimezone(timezone(timedelta(hours=2))).strftime("%d.%m.%Y %H:%M") if dt else ""


def _logo_uri() -> str:
    logo = Path(__file__).resolve().parents[1] / "assets" / "logo.png"
    try:
        return "data:image/png;base64," + base64.b64encode(logo.read_bytes()).decode()
    except OSError:
        return ""


def _page(body: str) -> str:
    return (f'<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">'
            '<link href="https://fonts.googleapis.com/css2?family=Rubik:wght@400;600;700;800&display=swap" rel="stylesheet">'
            f'<style>{CSS}</style></head><body>{body}</body></html>')


def _brand(logo: str) -> str:
    img = f'<img src="{logo}" alt="">' if logo else ""
    return f'<div class="brand">{img}מחיר אמת</div>'


def _peek(deals: list[dict]) -> str:
    """The cover shows what is inside: name, price and saving of up to 4 deals."""
    rows = []
    for d in deals[:4]:
        save = f" · -{d['savings_pct']}%" if d.get("savings_pct") else ""
        rows.append(f'<div><span>{e((d.get("title") or "")[:38])}</span>'
                    f'<b><bdi dir="ltr">{e(_money(d.get("price")))}{e(save)}</bdi></b></div>')
    return f'<div class="peek">{"".join(rows)}</div>'


def slides_html(deals: list[dict], telegram_url: str = "", product_images: bool = True,
                now: Optional[datetime] = None) -> list[str]:
    """[cover, deal..., closing] as full HTML pages."""
    from .catalog import CAT_BY_SLUG, classify
    now = now or datetime.now(timezone.utc)
    logo, total = _logo_uri(), len(deals) + 2
    out = [_page(f'<div class="slide">{_brand(logo)}<div class="n">1/{total}</div>'
                 f'<h1>{len(deals)} עסקאות<br>שעברו <span class="y">את הבדיקה</span></h1>'
                 '<div class="sub">מחיר שנבדק, לא מבצע מזויף.<br>בדקנו את היסטוריית המחיר של כל מוצר.</div>'
                 + _peek(deals) +
                 f'<div class="meta">{e(now.astimezone(timezone(timedelta(hours=2))).strftime("%d.%m.%Y"))} · '
                 'החליקו ← לעסקאות</div></div>')]
    for i, d in enumerate(deals, start=2):
        icon = CAT_BY_SLUG.get(classify(d.get("title") or ""), ("", "", "🛒"))[2]
        pic = (f'<img src="{e(d["image"])}" alt="" referrerpolicy="no-referrer">'
               if product_images and (d.get("image") or "").startswith("https://") else icon)
        save = f'<span class="save">חיסכון {d["savings_pct"]}%</span>' if d.get("savings_pct") else ""
        was = f'<span class="was">{e(_money(d.get("prev_price")))}</span>' if d.get("prev_price") else ""
        low = (" · <b>הנמוך ב-90 יום</b>" if d.get("lowest90") else
               f" · <b>הנמוך מאז {short_date(d['lowest_since'])}</b>" if d.get("lowest_since") else "")
        out.append(_page(
            f'<div class="slide">{_brand(logo)}<div class="n">{i}/{total}</div>'
            f'<div class="pic">{pic}</div><div class="title">{e((d.get("title") or "")[:110])}</div>'
            f'<div class="row"><span class="price">{e(_money(d.get("price")))}</span>{save}{was}</div>'
            f'<div class="meta">ב<b>{e(d.get("store") or "")}</b>{low}<br>'
            f'המחיר נבדק ב-<bdi dir="ltr">{e(_date(d.get("checked_at") or ""))}</bdi>. המחיר הקובע הוא באתר החנות.</div></div>'))
    tg = f'<div class="tg">{e(telegram_url.removeprefix("https://"))}</div>' if telegram_url else ""
    out.append(_page(f'<div class="slide">{_brand(logo)}<div class="n">{total}/{total}</div>'
                     '<div class="cta">כל עסקה כזאת, ברגע שהיא נבדקת,<br>בערוץ הטלגרם שלנו</div>'
                     '<div class="checks">✔ היסטוריית מחיר<br>✔ מועד בדיקה בכל עסקה<br>✔ בלי מבצעים מזויפים</div>'
                     f'{tg}' + (f'<img class="biglogo" src="{logo}" alt="">' if logo else "")
                     + f'<div class="disc">{e(DISCLOSURE)}</div></div>'))
    return out


def caption_text(deals: list[dict], telegram_url: str = "") -> str:
    lines = [f"{len(deals)} עסקאות שעברו את הבדיקה שלנו 👇", ""]
    for d in deals:
        save = f" (חיסכון {d['savings_pct']}%)" if d.get("savings_pct") else ""
        lines.append(f"• {(d.get('title') or '')[:70]}: {_money(d.get('price'))} {heb('ב', d.get('store') or '')}{save}")
    lines += ["", "המחירים נבדקו במועד המצוין בכל שקופית ועשויים להשתנות.",
              f"כל העסקאות המאומתות בערוץ הטלגרם (קישור בביו){': ' + telegram_url if telegram_url else ''}",
              "", DISCLOSURE, "", HASHTAGS]
    return "\n".join(lines)


def render(pages: list[str], out_dir: Path, browser_path: Optional[str] = None) -> list[Path]:
    """PNG files of the pages (Playwright + Chromium). Raises RuntimeError with the install hint."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed: py -m pip install playwright && "
                           "py -m playwright install chromium") from exc
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    exe = browser_path or os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe, headless=True, args=["--no-sandbox"] if exe else [])
        try:
            page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
            for i, doc in enumerate(pages, start=1):
                page.set_content(doc, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)   # fonts, product images
                except Exception:  # noqa: BLE001 - slow image: render what is there
                    pass
                path = out_dir / f"slide_{i:02d}.png"
                page.screenshot(path=str(path), clip={"x": 0, "y": 0, "width": W, "height": H})
                files.append(path)
        finally:
            browser.close()
    return files

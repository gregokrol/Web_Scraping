"""Stores' own public Telegram channels (e.g. t.me/KSPcoil, t.me/payngoil) as a source of LEADS.

Some stores block bots on their website, but announce their deals in a public channel. Telegram shows a
public channel as a web page (https://t.me/s/<name>); it is read like any page (PoliteFetcher: robots.txt,
delay). Only facts are kept - product, price, the "instead of" price, the store's link - never the post text.

A price in a channel is what the STORE SAYS, not a price we checked on the product page. So these claims
go to their own table, never to the verified offers, never become a deal by themselves. They are sent to
the owner as leads, next to the same product's VERIFIED price in the stores we do check.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .db import Database
from .validation import _parse_iso

CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_PRICE = re.compile(r"₪\s*([\d,]+(?:\.\d{1,2})?)|([\d,]+(?:\.\d{1,2})?)\s*(?:₪|ש\"ח|שח)")
# "₪1,999 במקום ₪2,999" - the parts may be on separate lines of the post
_PAIR = re.compile(r"₪?\s*([\d,]+(?:\.\d{1,2})?)\s*₪?(?:\s*\n\s*)*\s*(?:ב)?מקום(?:\s*\n\s*)*\s*₪?\s*([\d,]+(?:\.\d{1,2})?)")
_LETTER = re.compile(r"[A-Za-z֐-׿]")

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_claims (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    post_id TEXT NOT NULL,
    post_url TEXT,
    posted_at TEXT,
    title TEXT,
    price REAL,
    was_price REAL,
    currency TEXT DEFAULT 'ILS',
    link TEXT,
    fetched_at TEXT,
    UNIQUE(channel, post_id)
);
"""


def channel_page(name: str) -> str:
    name = name.strip().removeprefix("https://").removeprefix("t.me/").removeprefix("s/").strip("/@ ")
    if not CHANNEL_RE.match(name):
        raise ValueError(f"not a Telegram channel name: {name!r}")
    return f"https://t.me/s/{name}"


def _num(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


_SKIP_SEG = re.compile(r"^(?:לרכישה|לכל ה|למגוון|במבצע|רק ב|עכשיו ב|במקום|ב-?$|»)", re.U)
_ONLY = re.compile(r"\s(?:רק|ב-?רק)\s*-?\s*₪?\s*([\d,]+(?:\.\d{1,2})?)\s*(?:₪|ש\"ח)")
_MODEL_LINE = re.compile(r"^(?:דגם|מק\"ט|model)\s*[:：]\s*(\S.{1,40})$", re.I)
_LATIN_MODEL = re.compile(r"[A-Za-z]{2,}")


def _clean(seg: str) -> str:
    seg = _PRICE.sub("", seg)
    return re.sub(r"\s+", " ", seg).strip(" -–—»«|:.,!").strip()


def _name_before(segs: list[str], idx: int) -> str:
    """The product name for a price found in segment ``idx``: the nearest line before it that names a product
    (a Latin brand/model word wins), skipping "לרכישה מהירה ב-", emoji and other short connecting lines."""
    fallback = ""
    for j in range(idx, max(-1, idx - 4), -1):
        seg = _clean(segs[j])
        if len(_LETTER.findall(seg)) < 4 or _SKIP_SEG.match(seg) or seg.endswith("?"):
            continue
        if _LATIN_MODEL.search(seg):
            return seg[:120]
        fallback = fallback or seg[:120]
    return fallback


def parse_post(text: str) -> list[dict]:
    """The products a post names with a clear price: [{"title", "price", "was_price"}].

    "X במקום Y" pairs are read one by one, each with the line before it as its name ("לדוגמא:" posts list
    several products). "החל מ-" (a starting price for a whole range) is not one product's price - skipped,
    and so is a lone price without "במקום": those posts name a brand or a range, not one product.
    A "דגם: ..." line is added to the name, which makes matching the product in other stores reliable."""
    segs = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    model = next((m.group(1).strip() for ln in segs if (m := _MODEL_LINE.match(ln))), "")
    joined, starts = "", []
    for seg in segs:                       # one string, remembering where each line starts
        starts.append(len(joined))
        joined += seg + " \n "
    seg_at = lambda pos: max(i for i, st in enumerate(starts) if st <= pos)  # noqa: E731
    items = []
    for m in _PAIR.finditer(joined):
        before = joined[max(0, m.start() - 16):m.start()]
        if "החל" in before or "לכל הדגמים" in before:
            continue
        price, was = _num(m.group(1)), _num(m.group(2))
        if not price or not was or was <= price:
            continue
        name = _name_before(segs, seg_at(m.start()))
        if not name:
            continue
        if model and model.lower() not in name.lower() and not items:
            name = f"{name} {model}"
        items.append({"title": name[:160], "price": price, "was_price": was})
    if not items:
        # "<product> רק 149.90₪" on ONE line (no "instead of" price): the store's price for that product
        for seg in segs:
            m = _ONLY.search(seg)
            if m and "החל" not in seg and not re.search(r"\bעד\b", seg[:m.start()]):
                name = _clean(seg[:m.start()])
                if len(_LETTER.findall(name)) >= 6:
                    items.append({"title": (f"{name} {model}" if model else name)[:160],
                                  "price": _num(m.group(1)), "was_price": None})
                    break
    return items


def parse_channel_html(page_html: str, channel: str) -> list[dict]:
    soup = BeautifulSoup(page_html, "lxml")
    posts = []
    for m in soup.select(".tgme_widget_message[data-post]"):
        body = m.select_one(".tgme_widget_message_text")
        if body is None:
            continue
        for br in body.find_all("br"):
            br.replace_with("\n")
        text = body.get_text("\n")
        links = []
        for a in body.select("a[href]"):
            href = html.unescape(a["href"]).strip()
            host = (urlparse(href).hostname or "").lower()
            if href.startswith(("http://", "https://")) and host not in ("t.me", "telegram.me") and href not in links:
                links.append(href)
        t = m.select_one("time[datetime]")
        pid = m["data-post"].split("/")[-1]
        for k, info in enumerate(parse_post(text)):
            posts.append({"channel": channel, "post_id": pid if k == 0 else f"{pid}#{k + 1}",
                          "post_url": f"https://t.me/{m['data-post']}", "posted_at": t["datetime"] if t else "",
                          "link": links[0] if links else "", **info})
    return posts


def fetch_channel(fetcher, name: str) -> list[dict]:
    url = channel_page(name)
    page = fetcher.get(url)
    if page is None or page.status != 200:
        return []
    return parse_channel_html(page.text, url.rsplit("/", 1)[-1])


def save_claims(db: Database, posts: list[dict], exclude_keywords=None) -> int:
    """Store the posts that name a product and a price. Returns how many were new."""
    from .alerts import excluded_reason
    db.conn.executescript(SCHEMA)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    new = 0
    with db.tx() as c:
        for p in posts:
            if not p.get("price") or not p.get("title") or excluded_reason(p["title"], exclude_keywords):
                continue
            cur = c.execute("INSERT OR IGNORE INTO channel_claims(channel, post_id, post_url, posted_at, title, price, "
                            "was_price, link, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (p["channel"], p["post_id"], p["post_url"], p["posted_at"], p["title"], p["price"],
                             p.get("was_price"), p.get("link"), now))
            new += cur.rowcount
    return new


_TOK = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*|[֐-׿]{2,}", re.I)
_STOP = {"של", "עם", "את", "על", "או", "גם", "בלי", "חדש", "מבית", "במבצע", "the", "and", "for", "with", "new", "pro"}


def _tokens(s: str) -> set[str]:
    return {t.lower() for t in _TOK.findall(s or "") if t.lower() not in _STOP}


def verified_matches(db: Database, title: str, limit: int = 2, days: int = 3) -> list[dict]:
    """The same product in the stores we DO check (verified price, seen lately, in stock), best first.
    A match needs a shared model-like token (letters+digits, e.g. "WH-1000XM5") or most of the words."""
    want = _tokens(title)
    models = {t for t in want if re.search(r"\d", t) and re.search(r"[a-z]", t, re.I)}
    if len(want) < 2:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = db.query("SELECT title, price, currency, store, domain, url FROM offers WHERE price_verified=1 AND price "
                    "IS NOT NULL AND last_seen >= ? AND availability NOT IN ('OutOfStock','SoldOut','Discontinued')",
                    (since,))
    scored = []
    for r in rows:
        have = _tokens(r["title"])
        common = want & have
        if models and not (models & have):
            continue
        share = len(common) / len(want)
        if (models and models & have and len(common) >= 2) or share >= 0.7:
            scored.append((share, dict(r)))
    scored.sort(key=lambda x: (-x[0], x[1]["price"]))
    seen, out = set(), []
    for _, r in scored:
        if r["domain"] not in seen:
            seen.add(r["domain"])
            out.append(r)
        if len(out) >= limit:
            break
    return out


def leads_text(db: Database, hours: float = 48, limit: int = 8) -> Optional[str]:
    """Plain text for the owner's private chat, or None when there is nothing new."""
    db.conn.executescript(SCHEMA)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = [dict(r) for r in db.query("SELECT * FROM channel_claims ORDER BY posted_at DESC LIMIT 200")]
    rows = [r for r in rows if (_parse_iso(r["posted_at"] or r["fetched_at"]) or since) >= since]
    if not rows:
        return None
    # biggest stated discount first - the store's own figure, shown as such
    rows.sort(key=lambda r: -((r["was_price"] - r["price"]) / r["was_price"] if r.get("was_price") else 0))
    lines = ["🔎 לידים מערוצי החנויות (מחיר לפי החנות - לא נבדק בדף המוצר)", ""]
    for r in rows[:limit]:
        was = f" במקום ₪{r['was_price']:,.0f} (לפי החנות)" if r.get("was_price") else ""
        lines.append(f"• {r['title'][:70]}")
        lines.append(f"  {r['channel']}: ₪{r['price']:,.2f}{was}")
        for m in verified_matches(db, r["title"]):
            lines.append(f"  נבדק אצלנו: {m['store'] or m['domain']} ₪{m['price']:,.2f}")
        lines.append(f"  {r['link'] or r['post_url']}")
        lines.append("")
    lines.append("לפרסום: רק אחרי בדיקה שלך. מחיר החנות לא נחשב 'מאומת' ולא קובע חיסכון.")
    return "\n".join(lines)


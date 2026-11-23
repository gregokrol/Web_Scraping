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
# added later (ensure_schema migrates an existing table): the same product in a later post, and price drops
_LATER_COLUMNS = (("item_key", "TEXT"), ("prev_price", "REAL"), ("prev_posted_at", "TEXT"),
                  ("drop_notified", "INTEGER DEFAULT 0"))

# your names for the stores behind the channels (shown on the website and in the channel menu)
CHANNEL_STORES = {"kspcoil": "KSP", "payngoil": "מחסני חשמל", "traklin": "טרקלין", "il_home_center": "הום סנטר",
                  "almsales": "א.ל.מ"}
DROP_MIN_PCT = 1.0          # a later post at least 1% cheaper than the earlier one is a drop
CLAIM_DAYS = 7              # a channel price is shown for a week after the post


def store_name(channel: str) -> str:
    return CHANNEL_STORES.get((channel or "").lower(), channel)


def item_key(title: str) -> str:
    """The same product in different posts: its model words (letters+digits, e.g. "forerunner|265"),
    else its main words."""
    toks = _tokens(title)
    models = sorted(t for t in toks if re.search(r"\d", t) and re.search(r"[a-z]", t, re.I))
    latin = sorted(t for t in toks if re.fullmatch(r"[a-z][a-z0-9.-]*", t))
    if models:
        return "|".join(sorted(set(models) | set(latin)))[:200]
    return "|".join(sorted(toks)[:8])[:200]


def _ident(title: str) -> tuple[set, set]:
    """(brand/model words, numbers) - what identifies a product across posts, without the Hebrew wording."""
    toks = _tokens(title)
    latin = {t for t in toks if re.search(r"[a-z]", t, re.I)}
    nums = {t for t in toks if re.search(r"\d", t)}
    return latin | nums, nums


def same_product(a: str, b: str) -> bool:
    """Two posts name the same product: the same numbers/models, and the brand/model words of one are all in
    the other ("Acme Runner 265" = "Acme Runner 265 GPS"; not = "Acme Runner 165")."""
    (ia, na), (ib, nb) = _ident(a), _ident(b)
    if len(ia) < 2 or len(ib) < 2 or na != nb:
        return False
    return ia <= ib or ib <= ia


def ensure_schema(db: Database) -> None:
    db.conn.executescript(SCHEMA)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(channel_claims)")}
    for col, typ in _LATER_COLUMNS:
        if col not in cols:
            db.conn.execute(f"ALTER TABLE channel_claims ADD COLUMN {col} {typ}")
    for r in db.conn.execute("SELECT id, title FROM channel_claims WHERE item_key IS NULL").fetchall():
        db.conn.execute("UPDATE channel_claims SET item_key=? WHERE id=?", (item_key(r[1]), r[0]))
    db.conn.commit()


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
# sentences, not product names: "כל מי ש...", "משדרגים את...", "הגיע הזמן...", "אל תפספסו"
_SLOGAN = re.compile(r"(?:^|\s)(?:כל מי ש|משדרגים|מכינים|הגיע הזמן|אל תפספס|זה הזמן|תתקדמו|תקשיבו|מחכים|רוכשים|"
                     r"מקבלים|קונים רק|לכל המגוון)", re.U)


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
        if _LATIN_MODEL.search(seg) and not _SLOGAN.search(seg):
            return seg[:120]
        fallback = fallback or seg[:120]
    # no brand/model line right before the price: the post's own title line with a brand (e.g. "... מבית Makita")
    # beats a marketing sentence ("כל מי שעובד עם מברגה יודע...")
    for j in range(0, idx + 1):
        seg = _clean(segs[j])
        if _LATIN_MODEL.search(seg) and len(_LETTER.findall(seg)) >= 6 and not _SLOGAN.search(seg) and len(seg) <= 90:
            return seg[:120]
    return "" if _SLOGAN.search(fallback) else fallback


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
    """Store the posts that name a product and a price. Returns how many were new.

    When the same product (item_key) was posted before by the same channel at a higher price, the new row
    remembers that price (prev_price): a price drop, which is told to the owner once (new_drops)."""
    from .alerts import excluded_reason
    ensure_schema(db)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    new = 0
    with db.tx() as c:
        for p in sorted(posts, key=lambda p: p.get("posted_at") or ""):
            if not p.get("price") or not p.get("title") or excluded_reason(p["title"], exclude_keywords):
                continue
            key = item_key(p["title"])
            earlier = c.execute("SELECT price, posted_at, title FROM channel_claims WHERE channel=? AND post_id<>? "
                                "AND posted_at < ? ORDER BY posted_at DESC LIMIT 300",
                                (p["channel"], p["post_id"], p.get("posted_at") or now)).fetchall()
            prev = next(((r[0], r[1]) for r in earlier if same_product(r[2], p["title"])), None)
            prev_price = prev_at = None
            if prev and prev[0] and p["price"] < prev[0] * (1 - DROP_MIN_PCT / 100):
                prev_price, prev_at = prev[0], prev[1]
            cur = c.execute("INSERT OR IGNORE INTO channel_claims(channel, post_id, post_url, posted_at, title, price, "
                            "was_price, link, fetched_at, item_key, prev_price, prev_posted_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (p["channel"], p["post_id"], p["post_url"], p["posted_at"], p["title"], p["price"],
                             p.get("was_price"), p.get("link"), now, key, prev_price, prev_at))
            new += cur.rowcount
    return new


def new_drops(db: Database) -> list[dict]:
    """Price drops (a later post cheaper than the earlier one) not told to the owner yet."""
    ensure_schema(db)
    return [dict(r) for r in db.query("SELECT * FROM channel_claims WHERE prev_price IS NOT NULL AND "
                                      "COALESCE(drop_notified, 0)=0 ORDER BY posted_at")]


def mark_drops_told(db: Database, ids: list[int]) -> None:
    with db.tx() as c:
        c.executemany("UPDATE channel_claims SET drop_notified=1 WHERE id=?", [(i,) for i in ids])


def _date(ts: str) -> str:
    dt = _parse_iso(ts or "")
    return dt.astimezone(timezone(timedelta(hours=2))).strftime("%d.%m.%Y") if dt else ""


def drop_text(db: Database, r: dict) -> str:
    store = store_name(r["channel"])
    pct = round((r["prev_price"] - r["price"]) / r["prev_price"] * 100)
    lines = [f"📉 המחיר ירד שוב (לפי ערוץ {store}): {r['title'][:90]}",
             f"₪{r['price']:,.2f} עכשיו, לעומת ₪{r['prev_price']:,.2f} בפוסט מ-{_date(r['prev_posted_at'])} (ירידה של {pct}%)"]
    for m in verified_matches(db, r["title"]):
        lines.append(f"נבדק אצלנו: {m['store'] or m['domain']} ₪{m['price']:,.2f}")
    lines += [r["link"] or r["post_url"], "",
              f"לפרסום בערוץ (אחרי שבדקת): py main.py channels post {r['id']} --publish"]
    return "\n".join(lines)


def post_text(db: Database, r: dict, disclosure: str) -> str:
    """The public post for a channel price - stated as the store's price, never as a verified one."""
    store = store_name(r["channel"])
    lines = [f"🔥 {r['title'][:120]}",
             f"• ₪{r['price']:,.2f} לפי ערוץ {store} ({_date(r['posted_at'])}). לא נבדק אצלנו בדף המוצר."]
    if r.get("prev_price"):
        lines.append(f"• בפוסט הקודם של {store} ({_date(r['prev_posted_at'])}): ₪{r['prev_price']:,.2f}")
    elif r.get("was_price"):
        lines.append(f"• במקום ₪{r['was_price']:,.2f}, לפי {store}")
    for m in verified_matches(db, r["title"]):
        lines.append(f"• נבדק אצלנו: {m['store'] or m['domain']} ₪{m['price']:,.2f}")
    lines += ["", disclosure]
    return "\n".join(lines)


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


def recent_claims(db: Database, days: float = CLAIM_DAYS) -> list[dict]:
    """The latest post of each product per channel from the last ``days`` - for the website store pages."""
    ensure_schema(db)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    latest: dict = {}
    for r in db.query("SELECT * FROM channel_claims WHERE posted_at >= ? ORDER BY posted_at", (since,)):
        latest[(r["channel"], r["item_key"] or r["title"])] = dict(r)
    return list(latest.values())


def leads_text(db: Database, hours: float = 48, limit: int = 8, niche_only: bool = True) -> Optional[str]:
    """Plain text for the owner's private chat, or None when there is nothing new.
    ``niche_only``: only products in the niche categories (catalog.CATEGORIES: gaming, computers, mobile and
    headphones, appliances, home) - a baby sheet or winter socks are not leads for this channel."""
    ensure_schema(db)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = [dict(r) for r in db.query("SELECT * FROM channel_claims ORDER BY posted_at DESC LIMIT 200")]
    rows = [r for r in rows if (_parse_iso(r["posted_at"] or r["fetched_at"]) or since) >= since]
    if niche_only:
        from .catalog import classify
        rows = [r for r in rows if classify(r["title"])]
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


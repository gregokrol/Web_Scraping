"""Browse by store: website pages "store -> category -> top 5 cheapest / top 5 most popular",
and a pinned channel post with one button per store that opens them.

Only verified prices of products in stock, checked in the last 48 hours, never a forbidden product.
Categories are OUR niche taxonomy (each store names its own differently), assigned from the product name.
"Popular" = what the store itself shows: number of reviews and rating. Without enough reviews in a
category the page says so and shows the price order only - nothing is guessed.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import Database

# (slug, Hebrew name, emoji, keywords) - checked in this order, the first match wins
CATEGORIES = [
    ("gaming", "גיימינג", "🎮", ["playstation", "ps5", "ps4", "xbox", "nintendo", "switch", "גיימינג", "gaming",
                                 "קונסולה", "קונסולת", "dualsense", "בקר משחק", "בקר אלחוטי", "משחק ל", "steelseries",
                                 "razer", "hyperx", "logitech g", "כיסא גיימינג", "משקפי מציאות"]),
    ("computers", "מחשבים", "💻", ["מחשב נייד", "מחשב", "לפטופ", "laptop", "notebook", "macbook", "מסך מחשב", "monitor",
                                    "מקלדת", "keyboard", "עכבר", "mouse", "כרטיס מסך", "rtx", "ssd", "כונן", "מדפסת",
                                    "printer", "נתב", "router", "ראוטר", "ipad", "טאבלט", "tablet", "msi", "lenovo",
                                    "asus", "acer", "dell", "zenbook", "chromebook"]),
    ("mobile", "סלולר ואוזניות", "📱", ["iphone", "galaxy", "סמארטפון", "טלפון סלולרי", "airpods", "אוזניות", "headphones",
                                        "earbuds", "רמקול", "speaker", "jbl", "sonos", "שעון חכם", "smartwatch", "garmin",
                                        "apple watch", "מטען", "charger", "סוללה ניידת", "power bank", "כבל usb"]),
    ("appliances", "מוצרי חשמל", "🔌", ["שואב אבק", "שואב", "שואב רובוטי", "roborock", "dyson", "מכונת קפה", "קפסולות",
                                        "nespresso", "delonghi", "מקרר", "מקפיא", "מכונת כביסה", "מייבש", "מדיח", "תנור",
                                        "מיקרוגל", "טוסטר", "קומקום", "בלנדר", "מעבד מזון", "סיר טיגון", "airfryer",
                                        "air fryer", "מזגן", "מאוורר", "תנור חימום", "מפזר חום", "מטהר אוויר", "מטהר מים", "טיהור מים", "בר מים", "סודה בר", "מכשיר סודה", "טלוויזיה",
                                        "tv", "oled", "qled", "מקרן", "מגהץ", "מייבש שיער", "מכונת גילוח"]),
    ("home", "בית", "🏠", ["מצעים", "סדין", "שמיכה", "כרית", "מגבת", "סיר", "מחבת", "סכו\"ם", "צלחות", "כוסות", "מקדחה",
                           "מברגה", "כלי עבודה", "ארגז כלים", "גריל", "ברביקיו", "רהיט", "שולחן", "כיסא", "ארון", "מדף",
                           "תאורה", "מנורה", "שטיח", "וילון", "makita", "bosch", "קרצ'ר", "karcher", "צינור השקיה"]),
]
CAT_BY_SLUG = {c[0]: c for c in CATEGORIES}
TOP_N = 5
MIN_REVIEWED = 3               # a "popular" list needs at least this many products with reviews


def _kw_re(words: list[str]) -> re.Pattern:
    alt = "|".join(re.escape(w.lower()) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![^\W\d_])[הובלמשכ]?(?:{alt})(?![^\W\d_])", re.I | re.U)


_CAT_RES = [(slug, _kw_re(words)) for slug, _n, _e, words in CATEGORIES]


def classify(title: str, category: str = "") -> Optional[str]:
    text = f"{title or ''} {category or ''}".lower()
    for slug, rx in _CAT_RES:
        if rx.search(text):
            return slug
    return None


def store_slug(domain: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (domain or "store").lower().removeprefix("www.")).strip("-")


def collect_catalog(db: Database, now: Optional[datetime] = None, hours: float = 48) -> dict:
    """{domain: {"name": store name, "cats": {slug: [product, ...]}}} - verified, in stock, fresh."""
    from .alerts import excluded_reason
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    deal_for = {r["offer_id"]: r["id"] for r in db.query(
        "SELECT offer_id, MAX(id) id FROM deal_alerts WHERE status IN ('approved','publishing','published') "
        "GROUP BY offer_id")}
    rows = db.query("SELECT id, title, price, currency, store, domain, url, affiliate_url, image, rating, review_count, "
                    "category, last_fetched_at FROM offers WHERE price_verified=1 AND price IS NOT NULL AND price > 0 "
                    "AND last_seen >= ? AND availability IN ('InStock','LimitedAvailability','OnlineOnly','InStoreOnly')",
                    (since,))
    out: dict = {}
    names: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        r = dict(r)
        if not r["domain"] or excluded_reason(r["title"]) or excluded_reason(r.get("category") or ""):
            continue
        cat = classify(r["title"], r.get("category") or "")
        if not cat:
            continue
        r["deal_id"] = deal_for.get(r["id"])
        names[r["domain"]][r["store"] or r["domain"]] += 1
        out.setdefault(r["domain"], {"name": "", "cats": defaultdict(list)})["cats"][cat].append(r)
    for dom, entry in out.items():
        entry["name"] = names[dom].most_common(1)[0][0]
    return out


def cheapest(products: list[dict], n: int = TOP_N) -> list[dict]:
    seen, out = set(), []
    for p in sorted(products, key=lambda p: (p["price"], p["title"] or "")):
        key = (p["title"] or "").strip().lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
        if len(out) >= n:
            break
    return out


def most_popular(products: list[dict], n: int = TOP_N) -> list[dict]:
    """By the store's own review count (then rating). Empty when too few products have reviews."""
    reviewed = [p for p in products if (p.get("review_count") or 0) > 0]
    if len(reviewed) < MIN_REVIEWED:
        return []
    reviewed.sort(key=lambda p: (-(p["review_count"] or 0), -(p.get("rating") or 0), p["price"]))
    return reviewed[:n]

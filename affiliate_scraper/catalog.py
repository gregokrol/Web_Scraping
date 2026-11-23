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
    # before "home": "כיסא בטיחות" is a baby product, "כיסא" alone is furniture
    ("baby", "תינוקות", "🍼", ["תינוק", "תינוקות", "לתינוק", "עגלת תינוק", "עגלה", "טיולון", "כיסא בטיחות", "בוסטר",
                              "מוצץ", "בקבוק האכלה", "חיתול", "חיתולים", "טיטולים", "עריסה", "עריסת", "לול", "משאבת חלב",
                              "מנשא", "בגד גוף", "בגדי גוף", "שידת החתלה", "מוניטור תינוק"]),
    ("kids", "ילדים וצעצועים", "🧸", ["צעצוע", "צעצועים", "צעצועי", "לגו", "lego", "פליימוביל", "playmobil", "בובה", "בובת",
                                      "פאזל", "משחק קופסה", "משחקי קופסה", "יצירה", "ערכת יצירה", "דינוזאור", "ברבי",
                                      "barbie", "hot wheels", "nerf", "לילדים", "ילדים", "מכונית על שלט", "רובוט",
                                      "silverlit", "lexibook"]),
    ("sport", "ספורט", "⚽", ["נעלי ריצה", "נעלי ספורט", "ריצה", "משקולת", "משקולות", "הליכון", "אופניים", "אופני",
                             "כדורגל", "כדורסל", "כדור", "מזרן יוגה", "יוגה", "כושר", "fitness", "nike", "adidas", "puma",
                             "asics", "new balance", "under armour", "טניס", "שחייה", "בגד ים", "קטנוע"]),
    ("fashion", "אופנה", "👕", ["חולצה", "חולצת", "טישרט", "טי שירט", "מכנס", "מכנסיים", "מכנסי", "ג'ינס", "שמלה", "חצאית",
                               "סוודר", "קפוצ'ון", "ז'קט", "מעיל", "נעליים", "נעל", "סניקרס", "כפכפים", "סנדלים", "תיק",
                               "ארנק", "גרביים", "גרבי", "תחתונים", "הלבשה", "פיג'מה", "בגד", "כובע", "צעיף", "חגורה",
                               "משקפי שמש", "jeans", "shirt", "dress", "hoodie", "sneakers", "tote"]),
    ("home", "בית", "🏠", ["מצעים", "סדין", "שמיכה", "כרית", "מגבת", "סיר", "מחבת", "סכו\"ם", "צלחות", "כוסות", "מקדחה",
                           "מברגה", "כלי עבודה", "ארגז כלים", "גריל", "ברביקיו", "רהיט", "שולחן", "כיסא", "ארון", "מדף",
                           "תאורה", "מנורה", "שטיח", "וילון", "makita", "bosch", "קרצ'ר", "karcher", "צינור השקיה"]),
]
CAT_BY_SLUG = {c[0]: c for c in CATEGORIES}
# the owner's niche (docs/agents/AUDIENCE.md): the channel leads stay in it; the website shows every category
NICHE = {"gaming", "computers", "mobile", "appliances", "home"}
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


def store_settings(path: str = "sources.json") -> dict:
    """{domain: {"name": your name for the store, "enabled": bool}} from sources.json (empty if there is none)."""
    import json
    from pathlib import Path
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(st.get("domain", "")).lower().removeprefix("www."): {"name": str(st.get("name") or "").strip(),
                                                                     "enabled": st.get("enabled") is not False}
            for st in data.get("stores", []) if st.get("domain")}


def _setting_for(domain: str, settings: dict) -> Optional[dict]:
    d = (domain or "").lower().removeprefix("www.")
    for k in sorted(settings, key=len, reverse=True):
        if d == k or d.endswith("." + k):
            return settings[k]
    return None


def collect_catalog(db: Database, now: Optional[datetime] = None, hours: float = 48,
                    settings: Optional[dict] = None) -> dict:
    """{domain: {"name": store name, "cats": {slug: [product, ...]}}} - verified, in stock, fresh.

    ``settings`` (default: sources.json in this folder): a store you disabled is left out, and the store's name
    is YOUR name for it - the name a shop writes on its pages can be a whole sentence."""
    from .alerts import excluded_reason
    settings = store_settings() if settings is None else settings
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
        st = _setting_for(r["domain"], settings)
        if st is not None and not st["enabled"]:
            continue
        cat = classify(r["title"], r.get("category") or "")
        if not cat:
            continue
        r["deal_id"] = deal_for.get(r["id"])
        names[r["domain"]][r["store"] or r["domain"]] += 1
        out.setdefault(r["domain"], {"name": "", "cats": defaultdict(list)})["cats"][cat].append(r)
    _add_channel_stores(db, out, excluded_reason)
    for dom, entry in out.items():
        if entry.get("claimed"):
            continue
        st = _setting_for(dom, settings)
        name = (st or {}).get("name") or names[dom].most_common(1)[0][0]
        if len(name) > 30:                    # a shop's page title, not a name: fall back to the address
            name = dom.removeprefix("www.")
        entry["name"] = name
    return out


def _add_channel_stores(db: Database, out: dict, excluded_reason) -> None:
    """Stores read from their Telegram channel (KSP, מחסני חשמל...): their STATED prices from the last week.
    Kept apart ("tg-<channel>") and marked claimed - never mixed with checked prices."""
    from .channels import recent_claims, store_name, verified_matches
    for r in recent_claims(db):
        cat = classify(r["title"])
        if not cat or excluded_reason(r["title"]):
            continue
        key = f"tg-{r['channel'].lower()}"
        entry = out.setdefault(key, {"name": store_name(r["channel"]), "cats": defaultdict(list), "claimed": True,
                                     "channel": r["channel"]})
        entry["cats"][cat].append({
            "title": r["title"], "price": r["price"], "currency": r.get("currency") or "ILS", "claimed": True,
            "was_price": r.get("was_price"), "prev_price": r.get("prev_price"), "posted_at": r["posted_at"],
            "url": r["link"] or r["post_url"], "affiliate_url": "", "image": "", "store": entry["name"],
            "matches": verified_matches(db, r["title"], limit=1), "deal_id": None})


def biggest_stated_discount(products: list[dict], n: int = TOP_N) -> list[dict]:
    """Channel stores: the biggest discount THE STORE states ("במקום" or its own earlier post)."""
    def pct(p):
        ref = p.get("prev_price") or p.get("was_price")
        return (ref - p["price"]) / ref if ref else 0
    return [p for p in sorted(products, key=lambda p: -pct(p)) if pct(p) > 0][:n]


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


def search_items(db: Database, now: Optional[datetime] = None, hours: float = 48,
                 settings: Optional[dict] = None) -> list[dict]:
    """Everything a visitor can search on the website: checked products (fresh, in stock, allowed, from
    enabled stores - any category) and the stores' channel prices (marked claimed). One entry per product+store."""
    from .alerts import excluded_reason
    from .channels import recent_claims, store_name
    now = now or datetime.now(timezone.utc)
    settings = store_settings() if settings is None else settings
    since = (now - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    deal_for = {r["offer_id"]: r["id"] for r in db.query(
        "SELECT offer_id, MAX(id) id FROM deal_alerts WHERE status IN ('approved','publishing','published') "
        "GROUP BY offer_id")}
    items, seen = [], set()
    for r in db.query("SELECT id, title, price, currency, store, domain, url, affiliate_url, image, last_fetched_at, "
                      "category FROM offers WHERE price_verified=1 AND price IS NOT NULL AND price > 0 AND last_seen >= ? "
                      "AND availability IN ('InStock','LimitedAvailability','OnlineOnly','InStoreOnly') ORDER BY price",
                      (since,)):
        r = dict(r)
        st = _setting_for(r["domain"], settings)
        if not r["title"] or excluded_reason(r["title"]) or (st is not None and not st["enabled"]):
            continue
        store = (st or {}).get("name") or r["store"] or r["domain"]
        if len(store) > 30:
            store = r["domain"]
        key = ((r["title"] or "").strip().lower(), r["domain"])
        if key in seen:
            continue
        seen.add(key)
        items.append({"title": r["title"], "price": r["price"], "currency": r["currency"], "store": store,
                      "url": r["affiliate_url"] or r["url"], "image": r["image"] or "", "deal_id": deal_for.get(r["id"]),
                      "checked": r["last_fetched_at"] or "", "claimed": False, "cat": classify(r["title"], r["category"] or "")})
    for r in recent_claims(db):
        if excluded_reason(r["title"]):
            continue
        items.append({"title": r["title"], "price": r["price"], "currency": r.get("currency") or "ILS",
                      "store": store_name(r["channel"]), "url": r["link"] or r["post_url"], "image": "", "deal_id": None,
                      "checked": r["posted_at"] or "", "claimed": True, "cat": classify(r["title"])})
    return items

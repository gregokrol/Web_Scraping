"""Promotional content generated ONLY from verified facts.

Every sentence maps to a data point that was checked:

* price + "as of" time (always shown, prices change)
* "N% below the median of K stores" - only with >= 2 comparable verified offers and a certain match
* "lowest price in 90 days" / "dropped X%" - only with sufficient price history
* the store's "was" price - only if the discount claim was verified against history
* free shipping / rating - only if known (rating only with >= 10 reviews)

Nothing is invented: no fake urgency, no unverified superlatives. The affiliate
disclosure is always included.
"""

from __future__ import annotations

import html
from typing import Optional

DISCLOSURE = {
    "he": "גילוי נאות: הפוסט כולל קישורי שותפים. רכישה דרכם עשויה לזכות אותנו בעמלה, ללא עלות נוספת עבורך. "
          "המחירים נכונים למועד הבדיקה ועשויים להשתנות.",
    "en": "Disclosure: this post contains affiliate links. We may earn a commission at no extra cost to you. "
          "Prices were correct when checked and may change.",
}

T = {
    "he": {"price": "מחיר: {price}", "eilat": "מחיר באילת: {price}", "asof": "(נבדק ב-{when} UTC)", "below_median": "{pct}% מתחת למחיר החציוני ב-{n} חנויות",
           "lowest90": "המחיר הנמוך ביותר ב-90 הימים האחרונים", "drop": "ירד ב-{pct}% (מ-{old})",
           "was": "במקום {was} (הנחה של {pct}%, אומתה מול היסטוריית המחירים)", "free_ship": "משלוח חינם",
           "ship": "משלוח: {cost}", "rating": "דירוג {rating}/5 ({n} ביקורות)", "store": "בחנות {store}",
           "cta": "לפרטים ורכישה", "excl_ship": "(לא כולל משלוח)"},
    "en": {"price": "Price: {price}", "eilat": "Eilat price: {price}", "asof": "(checked {when} UTC)", "below_median": "{pct}% below the median price across {n} stores",
           "lowest90": "Lowest price in the last 90 days", "drop": "Down {pct}% (from {old})",
           "was": "Was {was} ({pct}% off, verified against price history)", "free_ship": "Free shipping",
           "ship": "Shipping: {cost}", "rating": "Rated {rating}/5 ({n} reviews)", "store": "at {store}",
           "cta": "See the deal", "excl_ship": "(excl. shipping)"},
}


def _money(value: Optional[float], currency: str) -> str:
    if value is None:
        return ""
    symbol = {"ILS": "₪", "USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    return f"{symbol}{value:,.2f}" if symbol else f"{value:,.2f} {currency}"


def build_content(offer: dict, history: Optional[dict] = None, comparison: Optional[dict] = None,
                  price_drop: Optional[dict] = None, discount_check: Optional[dict] = None,
                  lang: str = "he", disclosure: Optional[str] = None) -> dict:
    """``offer``: an offers-table row (dict). Returns {title, telegram, markdown, html, claims, link}."""
    lang = lang if lang in T else "en"
    t = T[lang]
    cur = offer.get("currency", "")
    link = offer.get("affiliate_url") or offer.get("url", "")
    claims: list[dict] = []

    def claim(text: str, basis: str) -> None:
        claims.append({"text": text, "basis": basis})

    when = (offer.get("last_fetched_at") or "")[:16].replace("T", " ")
    claim(t["price"].format(price=_money(offer.get("price"), cur)) + " " + t["asof"].format(when=when), "verified_price")
    if offer.get("eilat_price") and offer.get("price") and offer["eilat_price"] < offer["price"]:
        claim(t["eilat"].format(price=_money(offer["eilat_price"], cur)), "eilat_price")

    if (comparison and comparison.get("n_compared", 0) >= 2 and not comparison.get("uncertain")
            and comparison.get("median_price") and offer.get("price") is not None):
        pct = round((comparison["median_price"] - offer["price"]) / comparison["median_price"] * 100)
        if pct >= 3:
            note = f" {t['excl_ship']}" if "shipping_unknown" in (comparison.get("partial_reasons") or "") else ""
            claim(t["below_median"].format(pct=pct, n=comparison["n_compared"]) + note, "market_comparison")

    if history and history.get("lowest_90d"):
        claim(t["lowest90"], "price_history_90d")
    if price_drop:
        claim(t["drop"].format(pct=round(price_drop["drop_pct"]), old=_money(price_drop["from"], cur)), "price_history")
    if discount_check and discount_check.get("status") == "verified" and offer.get("list_price"):
        claim(t["was"].format(was=_money(offer["list_price"], cur), pct=round(discount_check["claimed_pct"])),
              "verified_discount")

    if offer.get("shipping_cost") == 0:
        claim(t["free_ship"], "shipping_known")
    elif offer.get("shipping_cost") is not None:
        claim(t["ship"].format(cost=_money(offer["shipping_cost"], cur)), "shipping_known")
    if offer.get("rating") is not None and (offer.get("review_count") or 0) >= 10:
        claim(t["rating"].format(rating=f"{offer['rating']:.1f}", n=offer["review_count"]), "store_reviews")

    title = offer.get("title") or offer.get("name") or ""
    store = offer.get("store") or ""
    disc = disclosure or DISCLOSURE[lang]
    lines = [c["text"] for c in claims]

    telegram = "\n".join([f"🔥 {title}", *[f"• {ln}" for ln in lines], f"{t['store'].format(store=store)}",
                          f"👉 {link}", "", disc])
    markdown = "\n".join([f"### {title}", "", *[f"- {ln}" for ln in lines], f"- {t['store'].format(store=store)}", "",
                          f"[{t['cta']} →]({link})", "", f"> {disc}"])
    e = html.escape
    rtl = ' dir="rtl"' if lang == "he" else ""
    html_out = (f'<div class="deal"{rtl}><h3>{e(title)}</h3><ul>' + "".join(f"<li>{e(ln)}</li>" for ln in lines)
                + f"<li>{e(t['store'].format(store=store))}</li></ul>"
                + f'<a href="{e(link)}" rel="sponsored nofollow noopener" target="_blank">{e(t["cta"])}</a>'
                + f'<p class="disclosure">{e(disc)}</p></div>')
    return {"title": title, "telegram": telegram, "markdown": markdown, "html": html_out, "claims": claims,
            "link": link, "lang": lang, "store": store, "disclosure": disc, "cta": t["cta"]}

"""Promotional content generated ONLY from verified facts.

Every sentence maps to a data point that was checked:

* price + "as of" time (always shown, prices change)
* "N% below the median of K stores" - only with >= 2 comparable verified offers and a certain match
* "lowest price in 90 days" - only with sufficient price history; "dropped X%" - also from a short history
  when the previous price held for at least two checks in a row (history.detect_price_change)
* the store's "was" price - only if the discount claim was verified against history
* free shipping / rating - only if known (rating only with >= 10 reviews)

Nothing is invented: no fake urgency, no unverified superlatives. The affiliate
disclosure is always included.
"""

from __future__ import annotations

import html
import re
from typing import Optional

def short_date(iso: str) -> str:
    """'2026-11-09' -> '9.11'"""
    try:
        return f"{int(iso[8:10])}.{int(iso[5:7])}"
    except (ValueError, IndexError):
        return iso


DISCLOSURE = {
    "he": "גילוי נאות: ייתכן שחלק מהקישורים הם קישורי שותפים. רכישה דרכם עשויה לזכות אותנו בעמלה, ללא עלות נוספת עבורך. "
          "המחירים נכונים למועד הבדיקה ועשויים להשתנות.",
    "en": "Disclosure: this post contains affiliate links. We may earn a commission at no extra cost to you. "
          "Prices were correct when checked and may change.",
}

T = {
    "he": {"price": "מחיר: {price}", "eilat": "מחיר באילת: {price}", "asof": "(נבדק ב-{when} UTC)", "below_median": "{pct}% מתחת למחיר החציוני ב-{n} חנויות",
           "lowest90": "המחיר הנמוך ביותר ב-90 הימים האחרונים",
           "lowest_since": "המחיר הנמוך ביותר מאז {since} (מאז שהתחלנו לבדוק)", "drop": "ירד ב-{pct}% (מ-{old})",
           "was": "במקום {was} (הנחה של {pct}%, אומתה מול היסטוריית המחירים)", "free_ship": "משלוח חינם",
           "ship": "משלוח: {cost}", "rating": "דירוג {rating}/5 ({n} ביקורות)", "store": "בחנות {store}",
           "cta": "לפרטים ורכישה", "excl_ship": "(לא כולל משלוח)"},
    "en": {"price": "Price: {price}", "eilat": "Eilat price: {price}", "asof": "(checked {when} UTC)", "below_median": "{pct}% below the median price across {n} stores",
           "lowest90": "Lowest price in the last 90 days",
           "lowest_since": "Lowest price since {since} (when our checks began)", "drop": "Down {pct}% (from {old})",
           "was": "Was {was} ({pct}% off, verified against price history)", "free_ship": "Free shipping",
           "ship": "Shipping: {cost}", "rating": "Rated {rating}/5 ({n} reviews)", "store": "at {store}",
           "cta": "See the deal", "excl_ship": "(excl. shipping)"},
}


def _money(value: Optional[float], currency: str) -> str:
    if value is None:
        return ""
    symbol = {"ILS": "₪", "USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    return f"{symbol}{value:,.2f}" if symbol else f"{value:,.2f} {currency}"


def heb_prefix(prefix: str, name: str) -> str:
    """A Hebrew prefix letter before a name: "בבאג", but "ב-KSP" (a hyphen before Latin letters or digits)."""
    name = (name or "").strip()
    return f"{prefix}-{name}" if re.match(r"[A-Za-z0-9]", name) else f"{prefix}{name}"


def short_money(value: Optional[float], currency: str = "ILS") -> str:
    """₪1,399 (no .00 on whole shekels), ₪49.90 otherwise."""
    if value is None:
        return ""
    symbol = {"ILS": "₪", "USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    num = f"{value:,.0f}" if abs(value - round(value)) < 0.005 else f"{value:,.2f}"
    return f"{symbol}{num}" if symbol else f"{num} {currency}"


def israel_time(iso: str) -> str:
    """'2026-11-23T07:03:00+00:00' -> '23.11, 09:03' (Israel time)."""
    from datetime import datetime, timedelta, timezone
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        local = dt.astimezone(ZoneInfo("Asia/Jerusalem"))
    except Exception:                       # Windows without tzdata: Israel standard time
        local = dt.astimezone(timezone(timedelta(hours=2)))
    return f"{local.day}.{local.month}, {local:%H:%M}"


SHORT_DISCLOSURE = ("גילוי נאות: חלק מהקישורים הם קישורי שותפים, ואנחנו עשויים לקבל עמלה, בלי תוספת מחיר לך. "
                    "המחיר נכון לזמן הבדיקה.")
CHANNEL_DISCLOSURE = ("גילוי נאות: חלק מהקישורים הם קישורי שותפים, ואנחנו עשויים לקבל עמלה, בלי תוספת מחיר לך. "
                      "המחיר נכון לזמן הפרסום בערוץ של החנות.")


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
    elif history and history.get("lowest_since"):
        claim(t["lowest_since"].format(since=short_date(history["lowest_since"])), "price_history_since")
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
    from .models import store_label
    store = store_label(offer.get("store") or "", offer.get("domain") or "")
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
    # the same facts, as data: the short channel post (telegram.format_post) is written from these
    was = (price_drop or {}).get("from") or (offer.get("list_price") if discount_check and
                                            discount_check.get("status") == "verified" else None)
    pct = (round(price_drop["drop_pct"]) if price_drop else
           round(discount_check["claimed_pct"]) if was and discount_check else None)
    was_source = "history" if (price_drop or {}).get("from") else ("store_list" if was else None)
    below = next(({"pct": int(c["text"].split("%")[0]), "n": comparison["n_compared"],
                   "excl_ship": t["excl_ship"] in c["text"]} for c in claims
                  if c["basis"] == "market_comparison"), None) if comparison else None
    facts = {"price": offer.get("price"), "currency": cur, "checked_at": offer.get("last_fetched_at") or "",
             "store": store, "eilat": next((offer["eilat_price"] for c in claims if c["basis"] == "eilat_price"), None),
             "was": was, "was_source": was_source, "pct": pct, "below": below,
             "lowest90": any(c["basis"] == "price_history_90d" for c in claims),
             "since": (history or {}).get("lowest_since") if any(c["basis"] == "price_history_since" for c in claims)
             else None,
             "free_ship": offer.get("shipping_cost") == 0,
             "ship_cost": offer.get("shipping_cost") if offer.get("shipping_cost") else None}
    return {"title": title, "telegram": telegram, "markdown": markdown, "html": html_out, "claims": claims,
            "link": link, "lang": lang, "store": store, "disclosure": disc, "cta": t["cta"], "facts": facts}

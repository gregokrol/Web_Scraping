"""Extract product data from HTML.

Page-level source adapters, merged in order of reliability:

    JSON-LD  >  microdata  >  OpenGraph / meta tags  >  configured CSS selectors  >  HTML fallback

Structured data (schema.org / OpenGraph) is what sites publish for Google
Shopping, so it is far more reliable than guessing CSS classes. CSS selectors
are only used for stores you configured explicitly (``sources.json``), and
prices that come only from HTML parsing are marked *unverified*.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Optional

from bs4 import BeautifulSoup, Tag

from .models import Product
from .utils import (absolute_url, clean_text, guess_currency, parse_float, parse_int,
                    parse_price, short_availability, strip_tracking)

log = logging.getLogger(__name__)

PRODUCT_TYPES = {"product", "productgroup", "individualproduct", "productmodel", "car", "book", "vehicle"}
MERGE_FIELDS = ("name", "brand", "model", "description", "category", "sku", "gtin", "price", "list_price",
                "currency", "shipping_cost", "availability", "condition", "color", "size", "warranty_months",
                "image", "rating", "review_count", "store")
CONDITIONS = {"newcondition": "new", "new": "new", "usedcondition": "used", "used": "used",
              "refurbishedcondition": "refurbished", "refurbished": "refurbished",
              "damagedcondition": "damaged", "damaged": "damaged"}


def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # lxml not installed
        return BeautifulSoup(html, "html.parser")


def normalize_condition(value: Any) -> str:
    key = short_availability(value).lower().replace(" ", "")
    return CONDITIONS.get(key, "")


# --------------------------------------------------------------------- JSON-LD
def _types(node: dict) -> set[str]:
    t = node.get("@type", [])
    t = t if isinstance(t, list) else [t]
    return {str(x).lower().rsplit("/", 1)[-1] for x in t}


def _walk(obj: Any) -> Iterable[dict]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _load_json_ld(raw: str) -> Any:
    raw = raw.strip().removeprefix("<!--").removesuffix("-->").strip()
    raw = re.sub(r"^\s*//<!\[CDATA\[|//\]\]>\s*$", "", raw)
    try:
        return json.loads(raw, strict=False)
    except json.JSONDecodeError:
        try:  # common breakage: trailing commas
            return json.loads(re.sub(r",\s*([}\]])", r"\1", raw), strict=False)
        except json.JSONDecodeError:
            return None


def _image(value: Any, base: str) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl") or ""
    return absolute_url(base, value)


def _first_dict(value: Any) -> dict:
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, dict)), {})
    return value if isinstance(value, dict) else {}


def _shipping(offer: dict) -> Optional[float]:
    details = offer.get("shippingDetails")
    rates = []
    for d in (details if isinstance(details, list) else [details]):
        if not isinstance(d, dict):
            continue
        rate = _first_dict(d.get("shippingRate"))
        value = parse_price(rate.get("value") if rate.get("value") is not None else rate.get("price"))
        if value is not None:
            rates.append(value)
    return min(rates) if rates else None


def _warranty_months(value: Any) -> Optional[int]:
    w = _first_dict(value)
    dur = _first_dict(w.get("durationOfWarranty"))
    amount = parse_float(dur.get("value"))
    if amount is None:
        return None
    unit = str(dur.get("unitCode") or dur.get("unitText") or "").upper()
    return int(round(amount * 12)) if unit in ("ANN", "YEAR", "YEARS", "Y") else int(round(amount))


def _offer_fields(offers: Any) -> dict:
    offer_list = offers if isinstance(offers, list) else [offers]
    best: Optional[dict] = None
    for o in offer_list:
        if not isinstance(o, dict):
            continue
        # AggregateOffer may nest individual offers
        if "offers" in o and not o.get("price") and not o.get("lowPrice"):
            nested = _offer_fields(o["offers"])
            if nested.get("price") is not None:
                return nested
        price = parse_price(o.get("price") if o.get("price") not in (None, "") else o.get("lowPrice"))
        currency = clean_text(o.get("priceCurrency"))
        list_price = None
        spec = o.get("priceSpecification")
        for s in (spec if isinstance(spec, list) else [spec]):
            if not isinstance(s, dict):
                continue
            price_type = str(s.get("priceType", "")).lower()
            if "listprice" in price_type or "strikethrough" in price_type or "msrp" in price_type:
                list_price = parse_price(s.get("price"))
            elif price is None:
                price = parse_price(s.get("price"))
            currency = currency or clean_text(s.get("priceCurrency"))
        if price is None:
            continue
        candidate = {
            "price": price,
            "list_price": list_price,
            "currency": currency,
            "availability": short_availability(o.get("availability")),
            "shipping_cost": _shipping(o),
            "condition": normalize_condition(o.get("itemCondition")),
            "store": clean_text(_first_dict(o.get("seller")).get("name")),
            "warranty_months": _warranty_months(o.get("warranty")),
        }
        # prefer the cheapest offer that is not out of stock
        if best is None or (price < best["price"] and "outofstock" not in candidate["availability"].lower()):
            best = candidate
    return {k: v for k, v in (best or {}).items() if v not in (None, "")}


def from_json_ld(soup: BeautifulSoup, base_url: str) -> list[dict]:
    products = []
    for script in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        data = _load_json_ld(script.string or script.get_text() or "")
        if data is None:
            continue
        for node in _walk(data):
            if not (_types(node) & PRODUCT_TYPES):
                continue
            rec: dict = {
                "name": clean_text(node.get("name")),
                "brand": clean_text(node.get("brand") or node.get("manufacturer")),
                "model": clean_text(node.get("model") or node.get("mpn")),
                "description": clean_text(node.get("description"), 500),
                "category": clean_text(node.get("category")),
                "sku": clean_text(node.get("sku") or node.get("productID")),
                "gtin": clean_text(node.get("gtin13") or node.get("gtin") or node.get("gtin12")
                                   or node.get("gtin14") or node.get("gtin8") or node.get("isbn")),
                "color": clean_text(node.get("color")),
                "size": clean_text(node.get("size")),
                "condition": normalize_condition(node.get("itemCondition")),
                "warranty_months": _warranty_months(node.get("warranty")),
                "image": _image(node.get("image"), base_url),
                "url": absolute_url(base_url, node.get("url")) if isinstance(node.get("url"), str) else "",
            }
            offers = node.get("offers")
            if offers is None and "hasVariant" in node:  # ProductGroup
                variants = node["hasVariant"] if isinstance(node["hasVariant"], list) else [node["hasVariant"]]
                offers = [v.get("offers") for v in variants if isinstance(v, dict) and v.get("offers")]
                offers = [x for o in offers for x in (o if isinstance(o, list) else [o])]
            if offers:
                for k, v in _offer_fields(offers).items():
                    if v not in (None, "") and rec.get(k) in (None, ""):
                        rec[k] = v
            rating = node.get("aggregateRating")
            if isinstance(rating, dict):
                value = parse_float(rating.get("ratingValue"))
                best = parse_float(rating.get("bestRating")) or 5.0
                if value is not None and best:
                    rec["rating"] = round(value * 5 / best, 2)  # normalise to /5
                rec["review_count"] = parse_int(rating.get("reviewCount") or rating.get("ratingCount"))
            products.append({k: v for k, v in rec.items() if v not in (None, "")})
    return products


# ------------------------------------------------------------------- microdata
def _itemprop_value(tag: Tag) -> str:
    for attr in ("content", "href", "src", "datetime", "value"):
        if tag.has_attr(attr):
            return tag[attr] if isinstance(tag[attr], str) else " ".join(tag[attr])
    return tag.get_text(" ", strip=True)


def from_microdata(soup: BeautifulSoup, base_url: str) -> list[dict]:
    products = []
    for scope in soup.find_all(attrs={"itemtype": re.compile(r"schema\.org/(Product|IndividualProduct)\b", re.I)}):
        props: dict[str, str] = {}
        for tag in scope.find_all(attrs={"itemprop": True}):
            for name in tag["itemprop"] if isinstance(tag["itemprop"], list) else tag["itemprop"].split():
                props.setdefault(name, _itemprop_value(tag))
        rating = parse_float(props.get("ratingValue"))
        best = parse_float(props.get("bestRating")) or 5.0
        rec = {
            "name": clean_text(props.get("name")),
            "brand": clean_text(props.get("brand")),
            "model": clean_text(props.get("model") or props.get("mpn")),
            "description": clean_text(props.get("description"), 500),
            "sku": clean_text(props.get("sku")),
            "gtin": clean_text(props.get("gtin13") or props.get("gtin") or props.get("gtin12")),
            "price": parse_price(props.get("price") or props.get("lowPrice")),
            "currency": clean_text(props.get("priceCurrency")),
            "availability": short_availability(props.get("availability")),
            "condition": normalize_condition(props.get("itemCondition")),
            "color": clean_text(props.get("color")),
            "image": absolute_url(base_url, props.get("image")),
            "rating": round(rating * 5 / best, 2) if rating is not None else None,
            "review_count": parse_int(props.get("reviewCount") or props.get("ratingCount")),
        }
        products.append({k: v for k, v in rec.items() if v not in (None, "")})
    return products


# ------------------------------------------------------------------ meta / OG
def _meta(soup: BeautifulSoup, *keys: str) -> str:
    for key in keys:
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if tag and tag.get("content"):
            return clean_text(tag["content"])
    return ""


def from_meta(soup: BeautifulSoup, base_url: str) -> dict:
    og_type = _meta(soup, "og:type").lower()
    price_raw = _meta(soup, "product:price:amount", "og:price:amount", "product:sale_price:amount")
    rec = {
        "name": _meta(soup, "og:title", "twitter:title"),
        "description": clean_text(_meta(soup, "og:description", "description", "twitter:description"), 500),
        "image": absolute_url(base_url, _meta(soup, "og:image:secure_url", "og:image", "twitter:image")),
        "brand": _meta(soup, "product:brand", "og:brand"),
        "price": parse_price(price_raw) if price_raw else None,
        "list_price": parse_price(_meta(soup, "product:original_price:amount")) or None,
        "currency": _meta(soup, "product:price:currency", "og:price:currency") or (guess_currency(price_raw) if price_raw else ""),
        "availability": short_availability(_meta(soup, "product:availability", "og:availability")),
        "condition": normalize_condition(_meta(soup, "product:condition", "og:condition")),
        "color": _meta(soup, "product:color"),
        "category": _meta(soup, "product:category"),
        "sku": _meta(soup, "product:retailer_item_id"),
        "gtin": _meta(soup, "product:ean", "product:upc", "product:gtin"),
        "store": _meta(soup, "og:site_name"),
        "_is_product": "product" in og_type or bool(price_raw),
    }
    return {k: v for k, v in rec.items() if v not in (None, "")}


# ------------------------------------------------------------ CSS selectors
_NUMERIC = {"price", "list_price", "shipping_cost"}


def from_selectors(soup: BeautifulSoup, base_url: str, selectors: dict[str, str]) -> dict:
    """Extract fields using store-specific CSS selectors: ``{"price": ".price", "image": "img.main@src"}``."""
    rec: dict = {}
    for fieldname, selector in (selectors or {}).items():
        css, _, attr = str(selector).partition("@")
        try:
            tag = soup.select_one(css.strip())
        except Exception as exc:  # bad selector from config
            log.warning("Bad CSS selector %r for %s: %s", selector, fieldname, exc)
            continue
        if tag is None:
            continue
        raw = tag.get(attr.strip()) if attr else tag.get_text(" ", strip=True)
        if isinstance(raw, list):
            raw = " ".join(raw)
        if raw in (None, ""):
            continue
        if fieldname in _NUMERIC:
            rec[fieldname] = parse_price(raw)
            if fieldname == "price" and "currency" not in selectors:
                rec.setdefault("currency", guess_currency(raw))
        elif fieldname == "image":
            rec[fieldname] = absolute_url(base_url, raw)
        elif fieldname == "rating":
            rec[fieldname] = parse_float(raw)
        elif fieldname == "review_count":
            rec[fieldname] = parse_int(raw)
        elif fieldname == "availability":
            rec[fieldname] = clean_text(raw)
        else:
            rec[fieldname] = clean_text(raw, 500)
    return {k: v for k, v in rec.items() if v not in (None, "")}


def from_html_fallback(soup: BeautifulSoup) -> dict:
    h1 = soup.find("h1")
    title = soup.find("title")
    return {"name": clean_text(h1.get_text(" ") if h1 else (title.get_text() if title else ""), 300)}


def canonical_url(soup: BeautifulSoup, page_url: str) -> str:
    link = soup.find("link", rel=lambda v: v and "canonical" in (v if isinstance(v, list) else [v]))
    href = link.get("href") if link else None
    return strip_tracking(absolute_url(page_url, href) if href else page_url)


# ----------------------------------------------------------------------- merge
def _merge(target: dict, source: dict, label: str, sources: list[str]) -> None:
    used = False
    for key in MERGE_FIELDS:
        if target.get(key) in (None, "") and source.get(key) not in (None, ""):
            target[key] = source[key]
            used = True
    if used:
        sources.append(label)


def _fuller_name(rec: dict, soup) -> None:
    """Some shops put only the product TYPE in their structured data ("אוזניות אלחוטיות" - wireless earbuds)
    while the page heading says which one ("אפל - Apple אוזניות אלחוטיות AirPods 4"). A generic name can't be
    compared or searched, so a short structured name gives way to the heading - only when the heading contains it."""
    name = (rec.get("name") or "").strip()
    if not name or len(name.split()) > 3:
        return
    h1 = soup.find("h1")
    heading = " ".join(h1.get_text(" ", strip=True).split()) if h1 else ""
    if len(name) < len(heading) <= 200 and name.lower() in heading.lower():
        rec["name"] = heading


def extract_products(html: str, page_url: str, require_structured: bool = True,
                     selectors: Optional[dict[str, str]] = None, fetched_at: str = "") -> list[Product]:
    """Return all products found on a page (usually one for a product page).

    If ``require_structured`` is true and no ``selectors`` are configured, pages
    with no product structured data yield nothing - this keeps category, blog
    and search pages out of your results.
    """
    soup = make_soup(html)
    url = canonical_url(soup, page_url)
    ld = from_json_ld(soup, url)
    micro = from_microdata(soup, url)
    meta = from_meta(soup, url)
    css = from_selectors(soup, url, selectors) if selectors else {}

    if not ld and not micro and not meta.get("_is_product") and not css.get("price"):
        if require_structured and not selectors:
            return []

    primary = ld or micro or [{}]
    single = len(primary) == 1
    results = []
    for item in primary:
        rec: dict = {}
        sources: list[str] = []
        if item:
            _merge(rec, item, "json-ld" if ld else "microdata", sources)
        if single:
            if ld and micro:
                _merge(rec, micro[0], "microdata", sources)
            _merge(rec, meta, "og", sources)
            if css:
                _merge(rec, css, "html", sources)
            _merge(rec, from_html_fallback(soup), "html", sources)
            _fuller_name(rec, soup)
        product_url = item.get("url") if (not single and item.get("url")) else url
        kwargs = {"fetched_at": fetched_at} if fetched_at else {}
        # source_type = where the *price* came from (decides price verification)
        price_source = next((s for s, d in (("json-ld", item if ld else {}), ("microdata", micro[0] if micro else {}),
                                            ("og", meta), ("html", css)) if d.get("price") is not None
                             and d.get("price") == rec.get("price")), sources[0] if sources else "")
        p = Product(url=strip_tracking(product_url), source="+".join(dict.fromkeys(sources)),
                    source_type=price_source, **kwargs, **rec)
        if p.is_valid:
            results.append(p)
    return results

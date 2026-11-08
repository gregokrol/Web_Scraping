"""Extract product data from HTML.

Most e-commerce sites publish product data as schema.org structured data for
search engines (Google Shopping / rich results). Reading that data is far more
reliable than guessing CSS classes, and it doesn't break whenever the site's
design changes. Sources are merged in order of reliability:

    JSON-LD  >  microdata  >  OpenGraph / meta tags  >  HTML fallback
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
MERGE_FIELDS = ("name", "brand", "description", "category", "sku", "gtin", "price", "list_price",
                "currency", "availability", "image", "rating", "review_count")


def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # lxml not installed
        return BeautifulSoup(html, "html.parser")


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
        # common breakage: trailing commas
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", raw), strict=False)
        except json.JSONDecodeError:
            return None


def _image(value: Any, base: str) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl") or ""
    return absolute_url(base, value)


def _offer_fields(offers: Any) -> dict:
    out: dict = {}
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
            is_list = "listprice" in price_type or "strikethrough" in price_type or "msrp" in price_type
            if is_list:
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
        }
        # prefer the cheapest in-stock offer
        if best is None or (price < best["price"] and "outofstock" not in candidate["availability"].lower()):
            best = candidate
    if best:
        out.update({k: v for k, v in best.items() if v not in (None, "")})
    return out


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
                "description": clean_text(node.get("description"), 500),
                "category": clean_text(node.get("category")),
                "sku": clean_text(node.get("sku") or node.get("mpn") or node.get("productID")),
                "gtin": clean_text(node.get("gtin13") or node.get("gtin") or node.get("gtin12")
                                   or node.get("gtin14") or node.get("gtin8")),
                "image": _image(node.get("image"), base_url),
                "url": absolute_url(base_url, node.get("url")) if isinstance(node.get("url"), str) else "",
            }
            offers = node.get("offers")
            if offers is None and "hasVariant" in node:  # ProductGroup
                variants = node["hasVariant"] if isinstance(node["hasVariant"], list) else [node["hasVariant"]]
                offers = [v.get("offers") for v in variants if isinstance(v, dict) and v.get("offers")]
                offers = [x for o in offers for x in (o if isinstance(o, list) else [o])]
            if offers:
                rec.update(_offer_fields(offers))
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
            # only take props belonging to this product or its offers/rating/brand children
            for name in tag["itemprop"] if isinstance(tag["itemprop"], list) else tag["itemprop"].split():
                props.setdefault(name, _itemprop_value(tag))
        rating = parse_float(props.get("ratingValue"))
        best = parse_float(props.get("bestRating")) or 5.0
        rec = {
            "name": clean_text(props.get("name")),
            "brand": clean_text(props.get("brand")),
            "description": clean_text(props.get("description"), 500),
            "sku": clean_text(props.get("sku") or props.get("mpn")),
            "gtin": clean_text(props.get("gtin13") or props.get("gtin")),
            "price": parse_price(props.get("price") or props.get("lowPrice")),
            "currency": clean_text(props.get("priceCurrency")),
            "availability": short_availability(props.get("availability")),
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
        "category": _meta(soup, "product:category"),
        "sku": _meta(soup, "product:retailer_item_id"),
        "_is_product": "product" in og_type or bool(price_raw),
    }
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


def extract_products(html: str, page_url: str, require_structured: bool = True) -> list[Product]:
    """Return all products found on a page (usually one for a product page).

    If ``require_structured`` is true, pages with no product structured data
    (JSON-LD / microdata / og:type=product / price meta) yield nothing - this
    keeps category, blog and search pages out of your results.
    """
    soup = make_soup(html)
    url = canonical_url(soup, page_url)
    ld = from_json_ld(soup, url)
    micro = from_microdata(soup, url)
    meta = from_meta(soup, url)

    if not ld and not micro and not meta.get("_is_product"):
        if require_structured:
            return []

    primary = ld or micro or [{}]
    # Many products on a page (category/listing page with JSON-LD ItemList):
    # return each one, but only enrich with page-level meta when there's one.
    single = len(primary) == 1
    results = []
    for item in primary:
        rec: dict = {}
        sources: list[str] = []
        _merge(rec, item, "json-ld" if ld else "microdata", sources)
        if single:
            if ld and micro:
                _merge(rec, micro[0], "microdata", sources)
            _merge(rec, meta, "og", sources)
            _merge(rec, from_html_fallback(soup), "html", sources)
        product_url = item.get("url") if (not single and item.get("url")) else url
        p = Product(url=strip_tracking(product_url), source="+".join(sources), **rec)
        if p.is_valid:
            results.append(p)
    return results

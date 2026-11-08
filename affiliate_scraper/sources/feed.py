"""Product feeds from merchants and affiliate networks - the preferred, authorised source.

Supported formats (auto-detected):

* Google Merchant Center XML (RSS 2.0 / Atom with the ``g:`` namespace) - also used by
  many affiliate networks, Shopify/WooCommerce feed plugins and price-comparison sites
* CSV / TSV feeds (Awin, CJ, Impact, Rakuten, TradeTracker, Google Sheets...). Common column
  names are recognised automatically; anything else can be mapped in ``sources.json``
  (``"csv_mapping": {"name": "ProductTitle", "price": "Price"}``)

Targets: a URL (fetched politely via PoliteFetcher) or a local file path.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lxml import etree

from ..fetcher import FetchError
from ..models import Product
from ..utils import clean_text, domain_of, guess_currency, parse_float, parse_int, parse_price
from .base import SourceAdapter, store_for

log = logging.getLogger(__name__)

G_NS = "http://base.google.com/ns/1.0"
FEED_MAX_BYTES = 200 * 1024 * 1024

# canonical field -> candidate column names (lower-case)
CSV_COLUMNS: dict[str, tuple[str, ...]] = {
    "name": ("title", "product_name", "name", "productname", "product name"),
    "url": ("merchant_deep_link", "link", "url", "product_url", "producturl", "buy_url", "landing_page"),
    "affiliate_url": ("aw_deep_link", "affiliate_link", "deep_link", "deeplink", "tracking_url", "click_url"),
    "price": ("sale_price", "search_price", "price", "store_price", "current_price", "saleprice"),
    "list_price": ("rrp_price", "rrp", "list_price", "retail_price", "msrp", "original_price", "base_price"),
    "currency": ("currency", "price_currency"),
    "image": ("image_link", "aw_image_url", "merchant_image_url", "image_url", "image", "imageurl"),
    "brand": ("brand", "brand_name", "manufacturer"),
    "model": ("mpn", "model", "model_number", "manufacturer_part_number"),
    "gtin": ("gtin", "ean", "upc", "isbn", "barcode", "product_gtin"),
    "sku": ("id", "merchant_product_id", "sku", "product_id", "aw_product_id"),
    "availability": ("availability", "in_stock", "stock_status", "stock"),
    "shipping_cost": ("delivery_cost", "shipping_cost", "shipping", "shipping_price"),
    "condition": ("condition",),
    "color": ("color", "colour"),
    "size": ("size",),
    "category": ("product_type", "category_name", "category", "merchant_category", "google_product_category"),
    "store": ("merchant_name", "advertiser_name", "store", "program_name"),
    "description": ("description", "product_short_description"),
    "rating": ("rating", "average_rating", "reviews_average"),
    "review_count": ("review_count", "number_of_reviews", "reviews"),
}


def _secure_parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=True)


def _price_and_currency(raw: str) -> tuple[Optional[float], str]:
    return parse_price(raw), guess_currency(raw or "")


def parse_google_feed(xml: bytes | str) -> list[dict]:
    data = xml.encode("utf-8") if isinstance(xml, str) else xml
    root = etree.fromstring(data, parser=_secure_parser())
    if root is None:
        return []
    items = root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry")
    out = []
    for item in items:
        def g(tag: str) -> str:
            el = item.find(f"{{{G_NS}}}{tag}")
            if el is None and tag in ("title", "link", "description"):
                el = item.find(tag)
                if el is None:
                    el = item.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
                    if el is not None and tag == "link":
                        return el.get("href", "")
            return clean_text(el.text if el is not None else "")

        price, cur = _price_and_currency(g("price"))
        sale, sale_cur = _price_and_currency(g("sale_price"))
        shipping = None
        ship_el = item.find(f"{{{G_NS}}}shipping")
        if ship_el is not None:
            sp = ship_el.find(f"{{{G_NS}}}price")
            if sp is not None:
                shipping = parse_price(sp.text)
        rec = {
            "name": g("title"),
            "url": g("link"),
            "description": clean_text(g("description"), 500),
            "price": sale if sale is not None else price,
            "list_price": price if sale is not None else None,
            "currency": sale_cur or cur,
            "image": g("image_link"),
            "brand": g("brand"),
            "model": g("mpn"),
            "gtin": g("gtin"),
            "sku": g("id"),
            "availability": g("availability"),
            "condition": g("condition").lower(),
            "color": g("color"),
            "size": g("size"),
            "category": g("product_type") or g("google_product_category"),
            "shipping_cost": shipping,
        }
        out.append(rec)
    return out


def parse_csv_feed(text: str, mapping: Optional[dict[str, str]] = None) -> list[dict]:
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), dialect=dialect)
    headers = {h.lower().strip(): h for h in (reader.fieldnames or []) if h}
    colmap: dict[str, str] = {}
    for fieldname, candidates in CSV_COLUMNS.items():
        for c in candidates:
            if c in headers:
                colmap[fieldname] = headers[c]
                break
    for fieldname, column in (mapping or {}).items():
        colmap[fieldname] = column
    out = []
    for row in reader:
        rec: dict = {}
        for fieldname, column in colmap.items():
            raw = (row.get(column) or "").strip()
            if not raw:
                continue
            if fieldname in ("price", "list_price", "shipping_cost"):
                rec[fieldname] = parse_price(raw)
                if fieldname == "price" and "currency" not in colmap:
                    rec.setdefault("currency", guess_currency(raw))
            elif fieldname == "rating":
                rec[fieldname] = parse_float(raw)
            elif fieldname == "review_count":
                rec[fieldname] = parse_int(raw)
            elif fieldname == "condition":
                rec[fieldname] = raw.lower()
            elif fieldname == "availability" and raw.lower() in ("1", "0", "y", "n"):
                # Awin/CJ style boolean stock flags (only meaningful inside a feed column)
                rec[fieldname] = "InStock" if raw.lower() in ("1", "y") else "OutOfStock"
            else:
                rec[fieldname] = clean_text(raw, 500)
        out.append(rec)
    return out


class ProductFeedAdapter(SourceAdapter):
    name = "feed"
    source_type = "feed"

    def _load(self, target: str) -> Optional[tuple[str, str]]:
        """Return (text, fetched_at)."""
        if target.startswith(("http://", "https://")):
            page = self.ctx.fetcher.get(target, max_bytes=FEED_MAX_BYTES)
            return (page.text, page.fetched_at) if page else None
        path = Path(target.removeprefix("file://"))
        if not path.is_file():
            self.ctx.fetcher.errors.append(FetchError(url=target, kind="feed_missing", message="file not found"))
            return None
        mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat()
        return path.read_text(encoding="utf-8-sig", errors="replace"), mtime

    def fetch(self, target: str) -> list[Product]:
        loaded = self._load(target)
        if loaded is None:
            return []
        text, fetched_at = loaded
        store = store_for(domain_of(target), self.ctx.stores) if target.startswith("http") else None
        fmt = (store.feed_format if store else "") or ("google" if text.lstrip()[:1] == "<" else "csv")
        try:
            records = parse_google_feed(text) if fmt == "google" else parse_csv_feed(text, store.csv_mapping if store else None)
        except (etree.XMLSyntaxError, csv.Error, ValueError) as exc:
            self.ctx.fetcher.errors.append(FetchError(url=target, kind="feed_parse_error", message=str(exc)))
            return []
        products = []
        for rec in records:
            url = rec.pop("url", "") or rec.get("affiliate_url", "")
            if not url or not rec.get("name"):
                continue
            products.append(Product(url=url, source="feed", source_type="feed", fetched_at=fetched_at,
                                    **{k: v for k, v in rec.items() if v not in (None, "")}))
        log.info("Feed %s: %d products", target, len(products))
        return self.apply_store_defaults(products)

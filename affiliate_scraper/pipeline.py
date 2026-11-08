"""Glue: discover -> fetch -> extract -> filter -> rank -> affiliate links."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from .affiliate import AffiliateConfig, apply_affiliate_links
from .extractors import extract_products
from .fetcher import PoliteFetcher
from .models import Product

log = logging.getLogger(__name__)


@dataclass
class Filters:
    min_rating: Optional[float] = None
    min_reviews: Optional[int] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_discount: Optional[float] = None
    in_stock_only: bool = False
    keywords: tuple[str, ...] = ()

    def accept(self, p: Product) -> bool:
        if self.min_rating is not None and (p.rating is None or p.rating < self.min_rating):
            return False
        if self.min_reviews is not None and (p.review_count or 0) < self.min_reviews:
            return False
        if self.min_price is not None and (p.price is None or p.price < self.min_price):
            return False
        if self.max_price is not None and (p.price is None or p.price > self.max_price):
            return False
        if self.min_discount is not None and (p.discount_pct or 0) < self.min_discount:
            return False
        if self.in_stock_only and p.in_stock is False:
            return False
        if self.keywords:
            haystack = f"{p.name} {p.brand} {p.category} {p.description}".lower()
            if not any(k.lower() in haystack for k in self.keywords):
                return False
        return True


def dedupe(products: Iterable[Product]) -> list[Product]:
    seen: set[str] = set()
    out = []
    for p in products:
        key = (p.domain + ":" + (p.sku or p.gtin)) if (p.sku or p.gtin) else p.url
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def scrape(
    urls: Iterable[str],
    fetcher: PoliteFetcher,
    affiliate: AffiliateConfig | None = None,
    filters: Filters | None = None,
    sort_by: str = "score",
    limit: Optional[int] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> list[Product]:
    urls = list(dict.fromkeys(urls))  # unique, order preserved
    products: list[Product] = []
    for i, url in enumerate(urls, 1):
        if progress:
            progress(i, len(urls), url)
        page = fetcher.get(url)
        if page is None:
            continue
        found = extract_products(page.text, page.url)
        if not found:
            log.info("No product data on %s", url)
        products.extend(found)

    products = dedupe(products)
    if filters:
        products = [p for p in products if filters.accept(p)]
    products = apply_affiliate_links(products, affiliate or AffiliateConfig())

    key_funcs = {
        "score": lambda p: -p.score,
        "rating": lambda p: -(p.rating or 0),
        "reviews": lambda p: -(p.review_count or 0),
        "discount": lambda p: -(p.discount_pct or 0),
        "price": lambda p: p.price if p.price is not None else float("inf"),
        "none": None,
    }
    key = key_funcs.get(sort_by)
    if key:
        products.sort(key=key)
    return products[:limit] if limit else products

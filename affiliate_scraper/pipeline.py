"""Glue: route targets to source adapters -> validate -> dedupe -> filter -> compare -> affiliate links -> rank."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .affiliate import AffiliateConfig, apply_affiliate_links
from .fetcher import FetchError, PoliteFetcher
from .matching import PriceComparison, compare_prices
from .models import Product
from .sources import (AdapterContext, BrowserPageAdapter, EbayBrowseAdapter, ProductFeedAdapter, SourceAdapter,
                      StoreConfig, WebPageAdapter)
from .sources.base import store_for
from .utils import domain_of
from .validation import validate_product

log = logging.getLogger(__name__)

FEED_SUFFIXES = (".xml", ".csv", ".tsv", ".txt", ".xml.gz")


@dataclass
class Filters:
    min_rating: Optional[float] = None
    min_reviews: Optional[int] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_discount: Optional[float] = None
    in_stock_only: bool = False
    verified_only: bool = False
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
        # v3 fix: unknown stock (None) is NOT treated as in stock
        if self.in_stock_only and p.in_stock is not True:
            return False
        if self.verified_only and not p.price_verified:
            return False
        if self.keywords:
            haystack = f"{p.name} {p.brand} {p.category} {p.description}".lower()
            if not any(k.lower() in haystack for k in self.keywords):
                return False
        return True


def _url_key(url: str) -> str:
    u = re.sub(r"^https?://(www\.)?", "", url.lower()).rstrip("/")
    return u


def dedupe(products: Iterable[Product]) -> list[Product]:
    """Drop duplicate listings of the same offer (same store + GTIN/SKU, or same URL).

    When duplicates are found, the record with the most verified data wins.
    """
    chosen: dict[str, Product] = {}
    order: list[str] = []
    url_to_key: dict[str, str] = {}
    for p in products:
        keys = [f"{p.domain}:gtin:{p.gtin}" if p.gtin else "", f"{p.domain}:sku:{p.sku}" if p.sku else "",
                f"url:{_url_key(p.url)}"]
        keys = [k for k in keys if k]
        existing = next((url_to_key[k] for k in keys if k in url_to_key), None)
        if existing is None:
            existing = keys[0]
            order.append(existing)
            chosen[existing] = p
        else:
            cur = chosen[existing]
            rank = lambda x: (x.price_verified, x.in_stock is not None, x.price is not None, len(x.source))  # noqa: E731
            if rank(p) > rank(cur):
                chosen[existing] = p
        for k in keys:
            url_to_key.setdefault(k, existing)
    return [chosen[k] for k in order]


def classify_target(target: str, stores: list[StoreConfig], render: str = "static") -> tuple[str, str]:
    """Return (adapter_name, normalised_target)."""
    if target.startswith("ebay:"):
        return "ebay", target
    if target.startswith("feed:"):
        return "feed", target[5:]
    if not target.startswith(("http://", "https://")):
        if Path(target).suffix.lower() in (".xml", ".csv", ".tsv", ".txt", ".gz") or Path(target).is_file():
            return "feed", target
        return "web", target  # will be rejected by the URL safety check
    store = store_for(domain_of(target), stores)
    if store and store.type == "feed":
        return "feed", target
    if (store and store.type == "browser") or render == "js":
        return "browser", target
    return "web", target


@dataclass
class ScrapeResult:
    products: list[Product] = field(default_factory=list)
    comparisons: list[PriceComparison] = field(default_factory=list)
    errors: list[FetchError] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    raw_count: int = 0


class Collector:
    """Owns the adapters for one run (so the browser is launched at most once)."""

    def __init__(self, ctx: AdapterContext):
        self.ctx = ctx
        self._adapters: dict[str, SourceAdapter] = {}

    def adapter(self, name: str) -> SourceAdapter:
        if name not in self._adapters:
            cls = {"web": WebPageAdapter, "browser": BrowserPageAdapter, "feed": ProductFeedAdapter,
                   "ebay": EbayBrowseAdapter}[name]
            self._adapters[name] = cls(self.ctx)
        return self._adapters[name]

    def collect(self, targets: list[str], render: str = "static",
                progress: Optional[Callable[[int, int, str], None]] = None) -> list[Product]:
        products: list[Product] = []
        for i, target in enumerate(targets, 1):
            if progress:
                progress(i, len(targets), target)
            name, norm = classify_target(target, self.ctx.stores, render)
            try:
                products.extend(self.adapter(name).fetch(norm))
            except RuntimeError as exc:  # e.g. Playwright not installed
                self.ctx.fetcher.errors.append(FetchError(url=target, kind="adapter_error", message=str(exc)))
        return products

    def close(self) -> None:
        for a in self._adapters.values():
            a.close()


SORT_KEYS: dict[str, Optional[Callable[[Product], object]]] = {
    "score": lambda p: -p.score,
    "deal": lambda p: -(p.deal_score if p.deal_score is not None else -1),
    "affiliate": lambda p: -(p.affiliate_score if p.affiliate_score is not None else -1),
    "rating": lambda p: -(p.rating or 0),
    "reviews": lambda p: -(p.review_count or 0),
    "discount": lambda p: -(p.discount_pct or 0),
    "price": lambda p: p.price if p.price is not None else float("inf"),
    "none": None,
}


def run_scrape(
    targets: Iterable[str],
    fetcher: PoliteFetcher,
    affiliate: AffiliateConfig | None = None,
    filters: Filters | None = None,
    sort_by: str = "score",
    limit: Optional[int] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
    stores: Optional[list[StoreConfig]] = None,
    render: str = "static",
    max_age_hours: float = 24.0,
    browser_path: Optional[str] = None,
    match_confidence: float = 0.75,
    post_process: Optional[Callable[[list[Product], list[PriceComparison]], None]] = None,
) -> ScrapeResult:
    targets = list(dict.fromkeys(targets))  # unique, order preserved
    ctx = AdapterContext(fetcher=fetcher, stores=stores or [], browser_path=browser_path)
    collector = Collector(ctx)
    try:
        raw = collector.collect(targets, render, progress)
    finally:
        collector.close()

    for p in raw:
        validate_product(p, max_age_hours=max_age_hours)
    products = [p for p in dedupe(raw) if p.validation_status != "invalid"]
    if filters:
        products = [p for p in products if filters.accept(p)]
    products = apply_affiliate_links(products, affiliate or AffiliateConfig())
    comparisons = compare_prices(products, min_confidence=match_confidence)
    if post_process:
        post_process(products, comparisons)

    key = SORT_KEYS.get(sort_by)
    if key:
        products.sort(key=key)
    if limit:
        products = products[:limit]
    return ScrapeResult(products=products, comparisons=comparisons, errors=list(fetcher.errors),
                        targets=targets, raw_count=len(raw))


def scrape(urls: Iterable[str], fetcher: PoliteFetcher, affiliate: AffiliateConfig | None = None,
           filters: Filters | None = None, sort_by: str = "score", limit: Optional[int] = None,
           progress: Optional[Callable[[int, int, str], None]] = None, **kwargs) -> list[Product]:
    """v2-compatible wrapper returning just the product list."""
    return run_scrape(urls, fetcher, affiliate, filters, sort_by, limit, progress, **kwargs).products

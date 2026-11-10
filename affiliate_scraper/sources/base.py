"""Adapter interface and store configuration (``sources.json``)."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from ..fetcher import PoliteFetcher
from ..models import Product
from ..utils import domain_matches

VALID_TYPES = ("web", "browser", "feed", "api")
AFFILIATE_STATUSES = ("confirmed", "signs", "not_found")
AFFILIATE_KNOWN = ("confirmed", "signs")      # the store has a program (published, or an affiliate app on the site)


@dataclass
class StoreConfig:
    """Per-store settings. Only facts you know go here - nothing is guessed."""

    domain: str
    name: str = ""
    type: str = "web"                       # web | browser | feed | api
    trust_score: Optional[float] = None     # 0..1, your judgement of the store's reliability
    shipping_cost: Optional[float] = None   # store-wide shipping policy, if known (0 = free)
    warranty_months: Optional[int] = None
    selectors: dict[str, str] = field(default_factory=dict)
    feed_url: str = ""
    feed_format: str = ""                   # google | csv | "" (auto)
    csv_mapping: dict[str, str] = field(default_factory=dict)
    xml_mapping: dict = field(default_factory=dict)  # {"item": "product", "fields": {"name": "title", "image": "img@src"}}
    currency: str = ""                      # currency of feed prices when the feed doesn't say (e.g. "ILS")
    enabled: bool = True
    start_urls: list[str] = field(default_factory=list)
    discover: str = "none"                  # none | sitemap | links
    include: str = ""
    # regex whose first group is the barcode inside the product URL, for stores that put it there but not in
    # the page data (e.g. "-(\\d{12,14})(?:[?#]|$)"). Used only when the page has no GTIN; the check digit is validated.
    gtin_from_url: str = ""
    # automation limits for this store (None = the automation's max_pages / the default of 10 listing pages)
    max_pages: Optional[int] = None
    max_listing_pages: Optional[int] = None
    # product search (`py main.py search` / the app's search box): the store's own search page with {q}
    # where the words go, e.g. "https://www.shop.co.il/?s={q}&post_type=product". search_include is the
    # product-URL regex on that page (default: include); search_selector a CSS selector for the result cards.
    search_url: str = ""
    search_include: str = ""
    search_selector: str = ""
    # for a store whose robots.txt forbids its own search page but that publishes product sitemaps with the
    # product name in each URL: the site's address (e.g. "https://www.shop.co.il/"). A search then looks for the
    # words in the sitemaps' product URLs (search_include = the product-URL regex) instead of a search page.
    search_sitemap: str = ""
    # what is known about the store's own affiliate (partner) program, and where it was seen:
    # {"status": "confirmed" | "signs" | "not_found", "url": "...", "network": "...", "note": "...", "checked": "YYYY-MM-DD"}
    # confirmed = the store publishes a program page (url); signs = an affiliate app runs on the site but no public
    # sign-up page was found; not_found = looked and found nothing - which is not proof that there is none.
    affiliate: dict = field(default_factory=dict)

    @property
    def has_affiliate(self) -> bool:
        return self.affiliate.get("status") in AFFILIATE_KNOWN

    @classmethod
    def from_dict(cls, d: dict) -> "StoreConfig":
        if not d.get("domain"):
            raise ValueError("store entry needs a 'domain'")
        t = d.get("type", "web")
        if t not in VALID_TYPES:
            raise ValueError(f"store {d['domain']}: type must be one of {VALID_TYPES}")
        trust = d.get("trust_score")
        if trust is not None and not (0 <= float(trust) <= 1):
            raise ValueError(f"store {d['domain']}: trust_score must be between 0 and 1")
        if d.get("gtin_from_url"):
            try:
                groups = re.compile(d["gtin_from_url"]).groups
            except re.error as exc:
                raise ValueError(f"store {d['domain']}: gtin_from_url is not a valid regex: {exc}") from None
            if groups < 1:
                raise ValueError(f"store {d['domain']}: gtin_from_url needs a (group) around the barcode")
        if d.get("search_url"):
            u = str(d["search_url"])
            if not u.startswith(("http://", "https://")) or u.count("{q}") != 1:
                raise ValueError(f"store {d['domain']}: search_url must be an http(s) URL with one {{q}} "
                                 "where the search words go")
        if d.get("search_sitemap") and not str(d["search_sitemap"]).startswith(("http://", "https://")):
            raise ValueError(f"store {d['domain']}: search_sitemap is the site's http(s) address")
        if d.get("search_url") and d.get("search_sitemap"):
            raise ValueError(f"store {d['domain']}: use search_url (the store's search page) or search_sitemap, not both")
        if d.get("search_include"):
            try:
                re.compile(d["search_include"])
            except re.error as exc:
                raise ValueError(f"store {d['domain']}: search_include is not a valid regex: {exc}") from None
        if d.get("search_selector"):
            from ..discovery import check_selector
            try:
                check_selector(d["search_selector"])
            except ValueError as exc:
                raise ValueError(f"store {d['domain']}: search_selector: {exc}") from None
        aff = d.get("affiliate")
        if aff not in (None, {}):
            if not isinstance(aff, dict) or aff.get("status") not in AFFILIATE_STATUSES:
                raise ValueError(f"store {d['domain']}: affiliate.status must be one of {AFFILIATE_STATUSES}")
            if aff.get("url") and not str(aff["url"]).startswith(("https://", "http://")):
                raise ValueError(f"store {d['domain']}: affiliate.url must be an http(s) URL")
            if aff.get("checked") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(aff["checked"])):
                raise ValueError(f"store {d['domain']}: affiliate.checked is a date, YYYY-MM-DD")
        known ={f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def load_stores(path: str | Path | None) -> list[StoreConfig]:
    if not path:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [StoreConfig.from_dict(s) for s in data.get("stores", [])]


def store_for(domain: str, stores: list[StoreConfig]) -> Optional[StoreConfig]:
    for s in sorted(stores, key=lambda s: len(s.domain), reverse=True):
        if domain_matches(domain, s.domain):
            return s
    return None


@dataclass
class AdapterContext:
    fetcher: PoliteFetcher
    stores: list[StoreConfig] = field(default_factory=list)
    browser_path: Optional[str] = None


class SourceAdapter(ABC):
    """Turns one *target* (URL, feed location, API query) into product offers."""

    name: str = "base"
    source_type: str = ""

    def __init__(self, ctx: AdapterContext):
        self.ctx = ctx

    @abstractmethod
    def fetch(self, target: str) -> list[Product]:
        ...

    def close(self) -> None:  # pragma: no cover - optional
        pass

    def apply_store_defaults(self, products: list[Product]) -> list[Product]:
        """Fill shipping / warranty / store name from sources.json where the page didn't say."""
        for p in products:
            store = store_for(p.domain, self.ctx.stores)
            if not store:
                continue
            if p.shipping_cost is None and store.shipping_cost is not None:
                p.shipping_cost = float(store.shipping_cost)
            if p.warranty_months is None and store.warranty_months is not None:
                p.warranty_months = int(store.warranty_months)
            if store.name and (not p.store or p.store == p.domain):
                p.store = store.name
            if not p.currency and store.currency and p.price is not None:
                p.currency = store.currency.upper()
            if not p.gtin and store.gtin_from_url:
                m = re.search(store.gtin_from_url, unquote(p.url))
                if m and m.group(1):
                    p.gtin = m.group(1)   # validate_product() checks the check digit and drops a bad one
        return products

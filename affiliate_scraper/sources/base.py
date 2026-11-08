"""Adapter interface and store configuration (``sources.json``)."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..fetcher import PoliteFetcher
from ..models import Product
from ..utils import domain_matches

VALID_TYPES = ("web", "browser", "feed", "api")


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
    enabled: bool = True
    start_urls: list[str] = field(default_factory=list)
    discover: str = "none"                  # none | sitemap | links
    include: str = ""

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
        known = {f for f in cls.__dataclass_fields__}
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
        return products

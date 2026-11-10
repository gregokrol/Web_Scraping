"""Data model for a scraped product / offer."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from .utils import normalize_availability


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_now_iso = now_iso  # backwards compatibility

IN_STOCK = {"InStock", "LimitedAvailability", "OnlineOnly", "InStoreOnly"}
NOT_IN_STOCK = {"OutOfStock", "SoldOut", "Discontinued", "PreOrder", "PreSale", "BackOrder", "MadeToOrder", "Reserved"}


@dataclass
class Product:
    """One offer: a product as sold by one store at one point in time."""

    url: str
    name: str = ""
    brand: str = ""          # manufacturer / brand
    model: str = ""          # model number / MPN
    description: str = ""
    category: str = ""
    sku: str = ""
    gtin: str = ""           # normalised to 14 digits by validation
    price: Optional[float] = None
    list_price: Optional[float] = None
    eilat_price: Optional[float] = None     # VAT-free price for Eilat, only when the store's page states it
    currency: str = ""
    shipping_cost: Optional[float] = None   # None = unknown, 0 = free
    tax: Optional[float] = None             # known extra tax/fees not included in price
    availability: str = ""
    condition: str = ""      # new | used | refurbished | damaged
    color: str = ""
    size: str = ""
    warranty_months: Optional[int] = None
    image: str = ""
    rating: Optional[float] = None
    review_count: Optional[int] = None
    store: str = ""
    store_url: str = ""
    affiliate_url: str = ""
    commission_rate: Optional[float] = None   # only when supplied by the network API itself (e.g. AliExpress)
    source: str = ""          # extractors that supplied data, e.g. "json-ld+og"
    source_type: str = ""     # primary source: api | feed | json-ld | microdata | og | html
    scraped_at: str = field(default_factory=now_iso)
    fetched_at: str = ""      # when the page/feed was actually downloaded (cache-aware)
    price_verified: bool = False
    validation_status: str = ""
    validation_issues: list[str] = field(default_factory=list)
    group_id: str = ""        # cross-store match group
    match_confidence: Optional[float] = None
    deal_score: Optional[float] = None        # consumer value 0-100 (None = insufficient data)
    affiliate_score: Optional[float] = None   # commercial value 0-100 (None = insufficient data)
    deal_status: str = ""                     # ok | insufficient_data | out_of_stock
    affiliate_status: str = ""
    deal_notes: str = ""                      # which data the scores were based on
    discount_check: str = ""                  # no_claim | insufficient_data | verified | overstated | misleading

    def __post_init__(self) -> None:
        if not self.fetched_at:
            self.fetched_at = self.scraped_at
        if not self.store_url and self.url:
            p = urlparse(self.url)
            if p.scheme and p.netloc:
                self.store_url = f"{p.scheme}://{p.netloc}"
        if not self.store:
            self.store = self.domain

    # ------------------------------------------------------------------ derived
    @property
    def domain(self) -> str:
        return (urlparse(self.url).hostname or "").removeprefix("www.")

    @property
    def in_stock(self) -> Optional[bool]:
        """True only when the store confirms the item is in stock; None when unknown."""
        av = normalize_availability(self.availability)
        if av in IN_STOCK:
            return True
        if av in NOT_IN_STOCK:
            return False
        return None

    @property
    def discount_pct(self) -> Optional[float]:
        """Discount *claimed* by the store (vs its list price). Verify with price history."""
        if self.price and self.list_price and self.list_price > self.price > 0:
            return round((1 - self.price / self.list_price) * 100, 1)
        return None

    @property
    def total_price(self) -> Optional[float]:
        """Price + shipping + known tax. None if price or shipping is unknown."""
        if self.price is None or self.shipping_cost is None:
            return None
        return round(self.price + self.shipping_cost + (self.tax or 0), 2)

    @property
    def score(self) -> float:
        """Legacy listing-quality score (v2). See scoring.py for Deal / Affiliate scores."""
        s = 0.0
        if self.rating is not None:
            confidence = min(1.0, math.log10(1 + (self.review_count or 0)) / 3)
            s += (self.rating / 5) * 40 * (0.4 + 0.6 * confidence)
        if self.discount_pct:
            s += min(self.discount_pct, 50) * 0.5
        if self.in_stock is True:
            s += 15
        elif self.in_stock is False:
            s -= 30
        completeness = sum(bool(x) for x in (self.name, self.image, self.price, self.description, self.brand))
        s += completeness * 4
        return round(max(0.0, min(100.0, s)), 1)

    @property
    def is_valid(self) -> bool:
        return bool(self.name)

    # ------------------------------------------------------------------ export
    def to_dict(self) -> dict:
        d = asdict(self)
        d["validation_issues"] = ";".join(self.validation_issues)
        d["domain"] = self.domain
        d["in_stock"] = self.in_stock
        d["discount_pct"] = self.discount_pct
        d["total_price"] = self.total_price
        d["score"] = self.score
        return d

    @classmethod
    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)] + ["domain", "in_stock", "discount_pct", "total_price", "score"]

    @classmethod
    def from_dict(cls, d: dict) -> "Product":
        names = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in names}
        issues = data.get("validation_issues")
        if isinstance(issues, str):
            data["validation_issues"] = [i for i in issues.split(";") if i]
        return cls(**data)

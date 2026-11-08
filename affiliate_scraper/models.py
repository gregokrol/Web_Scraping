"""Data model for a scraped product."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Product:
    url: str
    name: str = ""
    brand: str = ""
    description: str = ""
    category: str = ""
    sku: str = ""
    gtin: str = ""
    price: Optional[float] = None
    list_price: Optional[float] = None
    currency: str = ""
    availability: str = ""
    image: str = ""
    rating: Optional[float] = None
    review_count: Optional[int] = None
    affiliate_url: str = ""
    source: str = ""  # which extractor(s) supplied the data, e.g. "json-ld+og"
    scraped_at: str = field(default_factory=_now_iso)

    # ------------------------------------------------------------------ derived
    @property
    def domain(self) -> str:
        return (urlparse(self.url).hostname or "").removeprefix("www.")

    @property
    def in_stock(self) -> Optional[bool]:
        if not self.availability:
            return None
        a = self.availability.lower()
        if any(k in a for k in ("outofstock", "soldout", "discontinued", "out of stock")):
            return False
        return any(k in a for k in ("instock", "in stock", "limitedavailability",
                                    "onlineonly", "presale", "preorder"))

    @property
    def discount_pct(self) -> Optional[float]:
        if self.price and self.list_price and self.list_price > self.price > 0:
            return round((1 - self.price / self.list_price) * 100, 1)
        return None

    @property
    def score(self) -> float:
        """Heuristic 0-100 "promotability" score.

        Rewards social proof (rating weighted by review volume), discounts and
        availability; penalises incomplete listings. It is intentionally simple
        and transparent so you can tune it to your niche.
        """
        s = 0.0
        if self.rating is not None:
            # rating out of 5 -> up to 40 pts, scaled by confidence in #reviews
            confidence = min(1.0, math.log10(1 + (self.review_count or 0)) / 3)  # 1000 reviews = full
            s += (self.rating / 5) * 40 * (0.4 + 0.6 * confidence)
        if self.discount_pct:
            s += min(self.discount_pct, 50) * 0.5  # up to 25 pts
        if self.in_stock is True:
            s += 15
        elif self.in_stock is False:
            s -= 30
        completeness = sum(bool(x) for x in (self.name, self.image, self.price, self.description, self.brand))
        s += completeness * 4  # up to 20 pts
        return round(max(0.0, min(100.0, s)), 1)

    @property
    def is_valid(self) -> bool:
        return bool(self.name)

    # ------------------------------------------------------------------ export
    def to_dict(self) -> dict:
        d = asdict(self)
        d["domain"] = self.domain
        d["in_stock"] = self.in_stock
        d["discount_pct"] = self.discount_pct
        d["score"] = self.score
        return d

    @classmethod
    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)] + ["domain", "in_stock", "discount_pct", "score"]

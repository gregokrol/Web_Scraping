"""Find the same product across stores and compare prices.

Matching, strongest first:

1. identical GTIN / EAN / UPC (normalised to GTIN-14)          confidence 1.00
2. same brand + same model number                               confidence 0.90
3. same brand + similar title + shared model-number tokens      confidence 0.55-0.85

Offers are never merged when variant attributes conflict (size, pack, colour,
condition, warranty, importer) or when both have *different* GTINs. Matches
below ``uncertain_below`` are flagged ``uncertain``.

Each group gets a price comparison; if material data is missing (unverified
prices, unknown shipping, mixed currencies, a single store) the comparison is
marked ``partial`` with the reasons.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from dataclasses import dataclass, field
from typing import Optional

from .attributes import VariantAttrs, conflicts, extract_attributes
from .models import Product

HARD_CONFLICTS = {"gtin", "gtin_differs", "conflict", "brand_differs", "model_differs"}  # with confidence 0
STOPWORDS = {"the", "and", "with", "for", "new", "of", "in", "a", "an", "by", "-", "–", "|", "&",
             "של", "עם", "ל", "ו", "חדש", "מקורי"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9֐-׿]", "", (s or "").lower())


def _tokens(p: Product) -> set[str]:
    words = re.findall(r"[a-z0-9֐-׿]+", (p.name or "").lower())
    brand = _norm(p.brand)
    return {w for w in words if w not in STOPWORDS and w != brand and len(w) > 1}


def match_confidence(a: Product, b: Product, aa: VariantAttrs, ba: VariantAttrs) -> tuple[float, str, list[str]]:
    """Return (confidence 0..1, method, conflict reasons). confidence 0 = not the same product."""
    if a.gtin and b.gtin:
        if a.gtin != b.gtin:
            return 0.0, "gtin_differs", []
        reasons = conflicts(aa, ba, identical_gtin=True)
        return (0.0 if reasons else 1.0), "gtin", reasons

    reasons = conflicts(aa, ba)
    if reasons:
        return 0.0, "conflict", reasons

    brand_a, brand_b = _norm(a.brand), _norm(b.brand)
    same_brand = bool(brand_a) and brand_a == brand_b
    if brand_a and brand_b and not same_brand:
        return 0.0, "brand_differs", []

    model_a, model_b = _norm(a.model), _norm(b.model)
    if same_brand and model_a and model_b:
        return (0.9, "brand_model", []) if model_a == model_b else (0.0, "model_differs", [])

    if not same_brand:
        return 0.0, "insufficient", []

    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0, "insufficient", []
    jaccard = len(ta & tb) / len(ta | tb)
    ma, mb = aa.model_tokens, ba.model_tokens
    if ma and mb and not (ma & mb):
        return 0.0, "model_differs", []
    shared_model = bool(ma & mb)
    if jaccard < 0.5 and not shared_model:
        return 0.0, "titles_differ", []
    conf = 0.55 + 0.25 * jaccard + (0.05 if shared_model else 0)
    return round(min(conf, 0.85), 2), "attributes", []


@dataclass
class PriceComparison:
    group_id: str
    title: str
    brand: str = ""
    model: str = ""
    gtin: str = ""
    method: str = ""
    confidence: float = 1.0
    uncertain: bool = False
    offers: list[Product] = field(default_factory=list)
    currency: str = ""
    n_offers: int = 0
    n_stores: int = 0
    n_compared: int = 0
    min_price: Optional[float] = None
    median_price: Optional[float] = None
    max_price: Optional[float] = None
    spread: Optional[float] = None
    savings_vs_median_pct: Optional[float] = None
    savings_vs_max_pct: Optional[float] = None
    min_total: Optional[float] = None
    best_offer_url: str = ""
    best_store: str = ""
    partial: bool = False
    partial_reasons: list[str] = field(default_factory=list)

    def to_dict(self, include_offers: bool = True) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "offers"}
        d["partial_reasons"] = ";".join(self.partial_reasons)
        if include_offers:
            d["offers"] = [{"store": o.store, "url": o.url, "affiliate_url": o.affiliate_url, "price": o.price,
                            "currency": o.currency, "shipping_cost": o.shipping_cost, "total_price": o.total_price,
                            "in_stock": o.in_stock, "price_verified": o.price_verified,
                            "match_confidence": o.match_confidence} for o in self.offers]
        return d


class _DSU:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x


def group_products(products: list[Product], min_confidence: float = 0.75) -> list[list[tuple[int, float, str]]]:
    """Cluster offers. Returns groups of (index, confidence, method)."""
    attrs = [extract_attributes(p) for p in products]
    n = len(products)
    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            conf, method, _ = match_confidence(products[i], products[j], attrs[i], attrs[j])
            if conf >= min_confidence:
                edges.append((conf, i, j, method))
    edges.sort(key=lambda e: -e[0])  # strongest evidence first

    dsu = _DSU(n)
    members: dict[int, list[int]] = {i: [i] for i in range(n)}
    node_conf: dict[int, tuple[float, str]] = {}

    def hard_conflict(x: int, y: int) -> bool:
        c, m, _ = match_confidence(products[x], products[y], attrs[x], attrs[y])
        return c == 0.0 and m in HARD_CONFLICTS

    for conf, i, j, method in edges:
        ri, rj = dsu.find(i), dsu.find(j)
        if ri == rj:
            continue
        # never let transitivity (A~B, B~C) merge A and C if they conflict
        if any(hard_conflict(x, y) for x in members[ri] for y in members[rj]):
            continue
        dsu.parent[rj] = ri
        members[ri].extend(members.pop(rj))
        for k in (i, j):
            if k not in node_conf or conf < node_conf[k][0]:
                node_conf[k] = (conf, method)

    return [[(k, *node_conf.get(k, (1.0, "single"))) for k in idxs] for idxs in members.values()]


def _group_id(p: Product) -> str:
    key = p.gtin or f"{_norm(p.brand)}|{_norm(p.model)}|{_norm(p.name)}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def compare_group(offers: list[Product], confidence: float, method: str, uncertain_below: float = 0.95) -> PriceComparison:
    lead = next((o for o in offers if o.gtin), offers[0])
    comp = PriceComparison(group_id=_group_id(lead), title=lead.name, brand=lead.brand, model=lead.model,
                           gtin=lead.gtin, method=method, confidence=round(confidence, 2),
                           uncertain=confidence < uncertain_below, offers=offers)
    comp.n_offers = len(offers)
    comp.n_stores = len({o.domain for o in offers})
    reasons = []

    usable = [o for o in offers if o.price is not None and o.price_verified and o.in_stock is not False]
    unverified = [o for o in offers if o.price is not None and not o.price_verified]
    if unverified:
        reasons.append(f"unverified_price_excluded:{len(unverified)}")
    if any(o.in_stock is False for o in offers):
        reasons.append("out_of_stock_excluded")
    currencies = [o.currency for o in usable]
    if currencies:
        comp.currency = statistics.mode(currencies)
        if len(set(currencies)) > 1:
            reasons.append("mixed_currency")
            usable = [o for o in usable if o.currency == comp.currency]
    if comp.n_stores < 2:
        reasons.append("single_store")
    if usable and any(o.shipping_cost is None for o in usable):
        reasons.append("shipping_unknown")

    comp.n_compared = len(usable)
    if usable:
        prices = sorted(o.price for o in usable)
        comp.min_price, comp.max_price = prices[0], prices[-1]
        comp.median_price = round(statistics.median(prices), 2)
        comp.spread = round(comp.max_price - comp.min_price, 2)
        if len(prices) >= 2 and comp.median_price:
            comp.savings_vs_median_pct = round((comp.median_price - comp.min_price) / comp.median_price * 100, 1)
            comp.savings_vs_max_pct = round((comp.max_price - comp.min_price) / comp.max_price * 100, 1)
        totals = [(o.total_price, o) for o in usable if o.total_price is not None]
        best = min(totals, key=lambda t: t[0])[1] if totals else min(usable, key=lambda o: o.price)
        comp.min_total = min(t for t, _ in totals) if totals else None
        comp.best_offer_url, comp.best_store = best.affiliate_url or best.url, best.store
    else:
        reasons.append("no_comparable_prices")
    if len(usable) < 2 and "single_store" not in reasons:
        reasons.append("fewer_than_2_comparable_offers")
    comp.partial_reasons = reasons
    comp.partial = bool(set(reasons) - {"out_of_stock_excluded"})
    comp.offers = sorted(offers, key=lambda o: (o.total_price if o.total_price is not None else
                                                 (o.price if o.price is not None else float("inf"))))
    return comp


def compare_prices(products: list[Product], min_confidence: float = 0.75,
                   uncertain_below: float = 0.95) -> list[PriceComparison]:
    """Group offers across stores and compute a comparison per group (sets group_id on products)."""
    comparisons = []
    for group in group_products(products, min_confidence):
        offers = [products[i] for i, _, _ in group]
        conf = min(c for _, c, _ in group)
        methods = {m for _, _, m in group if m != "single"}
        method = "gtin" if methods == {"gtin"} else (",".join(sorted(methods)) or "single")
        comp = compare_group(offers, conf, method, uncertain_below)
        for (i, c, _), o in zip(group, offers):
            products[i].group_id = comp.group_id
            products[i].match_confidence = round(c, 2) if len(group) > 1 else None
        comparisons.append(comp)
    comparisons.sort(key=lambda c: (-c.n_stores, -(c.savings_vs_median_pct or 0)))
    return comparisons

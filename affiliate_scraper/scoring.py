"""Two separate scores - never mixed:

**Deal Score** (0-100) - is this a good buy *for the consumer*?
    market price position (35) · price history (25) · store reliability (15) ·
    shipping cost (10) · warranty (5) · verified availability (10)

**Affiliate Score** (0-100) - is it worth promoting *commercially*?
    Deal Score (40) · verified commission (25) · observed demand (10) ·
    audience fit (10) · real conversion data (15)

Each component is used ONLY when real data exists for it; missing components are
left out (never guessed) and the score is normalised over the components that
were available. The Affiliate Score additionally requires a verified commission
or real conversion data - without knowing what you earn it is ``insufficient_data``. ``confidence`` tells how much of the full model was covered.
If the essential data is missing the score is ``None`` with status
``insufficient_data``. Commission counts only when marked verified in your
config; demand uses observed review counts only; conversions only from imported reports.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Optional

from .history import HistoryStats
from .matching import PriceComparison
from .models import Product

DEAL_WEIGHTS = {"market": 35, "history": 25, "store": 15, "shipping": 10, "warranty": 5, "availability": 10}
AFFILIATE_WEIGHTS = {"deal": 40, "commission": 25, "demand": 10, "audience": 10, "conversion": 15}
MIN_CLICKS_FOR_CONVERSION = 30


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class ScoreResult:
    value: Optional[float]
    status: str                      # ok | insufficient_data | out_of_stock
    confidence: float = 0.0
    components: dict[str, dict] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{k}:{v['points']:.0f}/{v['max']}" for k, v in self.components.items()]
        return ", ".join(parts + self.reasons)


def _finish(components: dict[str, dict], weights: dict[str, int], reasons: list[str], extra_penalty: float = 0) -> ScoreResult:
    avail = sum(c["max"] for c in components.values())
    pts = sum(c["points"] for c in components.values())
    value = round(_clamp((pts / avail) * 100 - extra_penalty, 0, 100), 1) if avail else None
    return ScoreResult(value=value, status="ok", confidence=round(avail / sum(weights.values()), 2),
                       components=components, reasons=reasons)


def deal_score(p: Product, comparison: Optional[PriceComparison] = None, history: Optional[HistoryStats] = None,
               store_trust: Optional[float] = None, discount_check: Optional[dict] = None) -> ScoreResult:
    W = DEAL_WEIGHTS
    if p.price is None or not p.price_verified:
        return ScoreResult(None, "insufficient_data", reasons=["price_unverified"])
    if p.in_stock is False:
        return ScoreResult(0.0, "out_of_stock", reasons=["not_in_stock"])

    comps: dict[str, dict] = {}
    reasons: list[str] = []

    # market position: needs >= 2 comparable verified offers in the same currency.
    # Compare totals (price + shipping) when known for this offer and at least one other.
    if comparison and comparison.n_compared >= 2 and comparison.median_price and comparison.currency == p.currency:
        comparable = [o for o in comparison.offers if o.price_verified and o.currency == comparison.currency
                      and o.in_stock is not False and o.price is not None]
        totals = sorted(o.total_price for o in comparable if o.total_price is not None)
        if p.total_price is not None and len(totals) >= 2:
            median, own_cmp = statistics.median(totals), p.total_price
        else:
            median, own_cmp = comparison.median_price, p.price
        position = (median - own_cmp) / median if median else 0
        frac = _clamp(0.4 + 3 * position)
        if comparison.uncertain:
            frac *= comparison.confidence  # discount uncertain matches
            reasons.append("uncertain_match")
        comps["market"] = {"points": frac * W["market"], "max": W["market"],
                           "detail": f"{position * 100:+.1f}% vs median of {comparison.n_compared} offers"}

    # price history
    if history and history.sufficient and history.median_90d:
        ref = history.prior_median_90d or history.median_90d
        r = (ref - p.price) / ref if ref else 0
        frac = 1.0 if (history.min_90d is not None and p.price <= history.min_90d) else _clamp(0.4 + 3 * r)
        comps["history"] = {"points": frac * W["history"], "max": W["history"],
                            "detail": f"{r * 100:+.1f}% vs 90-day median ({history.n_obs} obs)"}

    if not ("market" in comps or "history" in comps):
        return ScoreResult(None, "insufficient_data", reasons=["no_market_comparison_or_history"])

    if store_trust is not None:
        comps["store"] = {"points": _clamp(store_trust) * W["store"], "max": W["store"], "detail": f"trust {store_trust}"}
    if p.shipping_cost is not None:
        frac = 1.0 if p.shipping_cost == 0 else _clamp(1 - (p.shipping_cost / p.price) * 5)
        comps["shipping"] = {"points": frac * W["shipping"], "max": W["shipping"], "detail": f"shipping {p.shipping_cost}"}
    if p.warranty_months is not None:
        m = p.warranty_months
        frac = 1.0 if m >= 24 else 0.7 if m >= 12 else 0.4 if m > 0 else 0.0
        comps["warranty"] = {"points": frac * W["warranty"], "max": W["warranty"], "detail": f"{m} months"}
    if p.in_stock is True:
        comps["availability"] = {"points": W["availability"], "max": W["availability"], "detail": "in stock (confirmed)"}

    penalty = 0.0
    if discount_check and discount_check.get("status") == "misleading":
        penalty = 15.0
        reasons.append("misleading_discount_claim")
    elif discount_check and discount_check.get("status") == "overstated":
        penalty = 5.0
        reasons.append("overstated_discount_claim")
    return _finish(comps, W, reasons, penalty)


def affiliate_score(p: Product, deal: ScoreResult, program: Optional[dict] = None,
                    audience: Optional[dict] = None, performance: Optional[dict] = None) -> ScoreResult:
    """``program``: {"commission_rate": 0.05, "commission_verified": True}; ``audience``:
    {"keywords": [...], "categories": [...]}; ``performance``: {"clicks": n, "conversions": n}."""
    W = AFFILIATE_WEIGHTS
    if deal.value is None:
        return ScoreResult(None, "insufficient_data", reasons=["deal_score_insufficient"])
    if deal.status == "out_of_stock":
        return ScoreResult(0.0, "out_of_stock", reasons=["not_in_stock"])

    comps: dict[str, dict] = {"deal": {"points": deal.value / 100 * W["deal"], "max": W["deal"],
                                       "detail": f"deal score {deal.value}"}}
    reasons: list[str] = []

    if program and program.get("commission_verified") and program.get("commission_rate") is not None:
        rate = float(program["commission_rate"])
        comps["commission"] = {"points": _clamp(rate / 0.10) * W["commission"], "max": W["commission"],
                               "detail": f"{rate * 100:.1f}% verified"}
    elif program and program.get("commission_rate") is not None:
        reasons.append("commission_not_verified")

    if p.review_count:  # observed demand proxy - real reviews on the store
        comps["demand"] = {"points": _clamp(math.log10(1 + p.review_count) / 3) * W["demand"], "max": W["demand"],
                           "detail": f"{p.review_count} reviews"}

    kws = [k.lower() for k in (audience or {}).get("keywords", [])]
    cats = [c.lower() for c in (audience or {}).get("categories", [])]
    if kws or cats:
        hay = f"{p.name} {p.brand} {p.category} {p.description}".lower()
        hit = any(k in hay for k in kws) or any(c in p.category.lower() for c in cats if p.category)
        comps["audience"] = {"points": W["audience"] if hit else 0.0, "max": W["audience"],
                             "detail": "matches audience" if hit else "outside audience"}

    if performance and (performance.get("clicks") or 0) >= MIN_CLICKS_FOR_CONVERSION:
        cr = (performance.get("conversions") or 0) / performance["clicks"]
        comps["conversion"] = {"points": _clamp(cr / 0.05) * W["conversion"], "max": W["conversion"],
                               "detail": f"CR {cr * 100:.1f}% ({performance['clicks']} clicks, {performance.get('level')})"}
    elif performance:
        reasons.append("too_few_clicks_for_conversion_rate")

    # Commercial value can't be judged without knowing what you earn: require a verified
    # commission or real conversion data - never just "deal + reviews".
    if "commission" not in comps and "conversion" not in comps:
        return ScoreResult(None, "insufficient_data",
                           reasons=["no_verified_commission_or_conversion_data", *reasons])
    return _finish(comps, W, reasons)


def apply_scores(products: list[Product], comparisons: list[PriceComparison], db=None, stores=None,
                 affiliate_config=None, now=None) -> None:
    """Compute both scores for every product (in place). Uses DB history when ``db`` is given."""
    from .history import check_discount_claim, stats_for_offer
    from .sources.base import store_for

    by_group = {c.group_id: c for c in comparisons}
    audience = getattr(affiliate_config, "audience", None) or {}
    for p in products:
        comp = by_group.get(p.group_id)
        hist = None
        if db is not None:
            oid = db.offer_id(p.url)
            if oid is not None:
                hist = stats_for_offer(db, oid, now=now)
        disc = check_discount_claim(hist, p.price, p.list_price) if hist else (
            {"status": "insufficient_data"} if p.discount_pct else {"status": "no_claim"})
        p.discount_check = disc["status"]

        trust = None
        store = store_for(p.domain, stores or [])
        if store and store.trust_score is not None:
            trust = store.trust_score
        elif db is not None:
            trust = db.source_trust(p.domain)

        d = deal_score(p, comp, hist, trust, disc)
        program = None
        if affiliate_config is not None:
            program = affiliate_config.program_for(p.domain)
        if program is None and db is not None:
            row = db.program_for(p.domain)
            program = dict(row) if row else None
        perf = db.performance_for(p.url, p.domain) if db is not None else None
        a = affiliate_score(p, d, program, audience, perf)

        p.deal_score, p.deal_status = d.value, d.status
        p.affiliate_score, p.affiliate_status = a.value, a.status
        p.deal_notes = f"deal[{d.summary()}] conf={d.confidence}; affiliate[{a.summary()}] conf={a.confidence}"

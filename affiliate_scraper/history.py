"""Price-history analytics: 30/90-day lows and medians, real price drops, misleading discounts.

Rules (accuracy first):

* only *verified* observations are used
* a statistic is reported only with enough data (``MIN_OBSERVATIONS`` points
  spanning at least ``MIN_DAYS``); otherwise the status is ``insufficient_data``
* a store's "was" price is never trusted on its own. A discount is called
  ``verified`` only when the current price is really below what the store charged
  in the previous 30 days (the EU Omnibus-directive reference price)
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .validation import _parse_iso

MIN_OBSERVATIONS = 3
MIN_DAYS = 7


@dataclass
class HistoryStats:
    n_obs: int = 0
    days_covered: float = 0.0
    first_seen: str = ""
    current_price: Optional[float] = None
    previous_price: Optional[float] = None
    min_30d: Optional[float] = None
    min_90d: Optional[float] = None
    median_30d: Optional[float] = None
    median_90d: Optional[float] = None
    max_90d: Optional[float] = None
    prior_min_30d: Optional[float] = None    # lowest price in the 30 days BEFORE the current observation
    prior_median_90d: Optional[float] = None
    prior_max_90d: Optional[float] = None
    sufficient: bool = False
    status: str = "insufficient_data"
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _median(xs: list[float]) -> Optional[float]:
    return round(statistics.median(xs), 2) if xs else None


def compute_stats(observations: list[tuple[str, float]], now: Optional[datetime] = None,
                  currency_filter: Optional[list[str]] = None) -> HistoryStats:
    """``observations``: [(observed_at_iso, price)] for ONE offer, verified only, any order."""
    now = now or datetime.now(timezone.utc)
    obs = sorted((dt, price) for ts, price in observations
                 if price is not None and (dt := _parse_iso(ts)) is not None and dt <= now + timedelta(minutes=5))
    st = HistoryStats(n_obs=len(obs))
    if not obs:
        return st
    st.first_seen = obs[0][0].isoformat()
    st.days_covered = round((obs[-1][0] - obs[0][0]).total_seconds() / 86400, 2)
    st.current_price = obs[-1][1]
    # The current price "period" starts at the first of the trailing observations at (about) the
    # current price. Reference prices must come from BEFORE that period - otherwise seeing the
    # same new price twice would make it its own reference and hide real drops / discounts.
    k = len(obs) - 1
    while k > 0 and abs(obs[k - 1][1] - st.current_price) <= 0.005 * st.current_price:
        k -= 1
    period_start = obs[k][0]
    prior = obs[:k]
    if prior:
        st.previous_price = prior[-1][1]

    in30 = [p for dt, p in obs if dt >= now - timedelta(days=30)]
    in90 = [p for dt, p in obs if dt >= now - timedelta(days=90)]
    prior30 = [p for dt, p in prior if dt >= period_start - timedelta(days=30)]
    prior90 = [p for dt, p in prior if dt >= period_start - timedelta(days=90)]

    st.sufficient = st.n_obs >= MIN_OBSERVATIONS and st.days_covered >= MIN_DAYS
    st.status = "ok" if st.sufficient else "insufficient_data"
    if not st.sufficient:
        return st
    st.min_30d, st.min_90d = (min(in30) if in30 else None), (min(in90) if in90 else None)
    st.median_30d, st.median_90d = _median(in30), _median(in90)
    st.max_90d = max(in90) if in90 else None
    st.prior_min_30d = min(prior30) if prior30 else None
    st.prior_median_90d = _median(prior90)
    st.prior_max_90d = max(prior90) if prior90 else None
    return st


def detect_price_drop(st: HistoryStats, threshold_pct: float = 10.0) -> Optional[dict]:
    """A significant, real drop: vs previous observation AND below the prior 90-day median."""
    if not st.sufficient or st.current_price is None or st.previous_price is None:
        return None
    cur, prev = st.current_price, st.previous_price
    drop_pct = (prev - cur) / prev * 100 if prev else 0
    below_median = st.prior_median_90d is not None and cur < st.prior_median_90d * (1 - threshold_pct / 200)
    if drop_pct >= threshold_pct and below_median:
        return {"kind": "price_drop", "drop_pct": round(drop_pct, 1), "from": prev, "to": cur,
                "lowest_90d": is_lowest_in_90d(st)}
    return None


def is_lowest_in_90d(st: HistoryStats) -> bool:
    return bool(st.sufficient and st.current_price is not None and st.min_90d is not None
                and st.current_price <= st.min_90d and st.prior_max_90d is not None
                and st.current_price < st.prior_max_90d)


def check_discount_claim(st: HistoryStats, price: Optional[float], list_price: Optional[float]) -> dict:
    """Is the store's claimed discount (list/"was" price) real?

    status: no_claim | insufficient_data | verified | overstated | misleading
    """
    if not price or not list_price or list_price <= price:
        return {"status": "no_claim"}
    claimed = round((list_price - price) / list_price * 100, 1)
    if not st.sufficient:
        return {"status": "insufficient_data", "claimed_pct": claimed}
    if st.prior_min_30d is None:
        # price never changed during a sufficiently long observed period -> it was never the "was" price
        return {"status": "misleading", "claimed_pct": claimed, "real_pct_vs_30d_low": 0.0,
                "reason": f"price unchanged for {st.days_covered:g} observed days", "never_sold_at_list_price": True}
    ref = st.prior_min_30d
    real = round((ref - price) / ref * 100, 1) if ref else 0.0
    out = {"claimed_pct": claimed, "real_pct_vs_30d_low": real, "reference_price": ref}
    never_sold_at_list = st.prior_max_90d is not None and list_price > st.prior_max_90d * 1.05
    if real <= 0.5:
        out["status"] = "misleading"          # not cheaper than the store's own recent price
    elif real >= claimed * 0.8:
        out["status"] = "verified"
    else:
        out["status"] = "overstated"
    if never_sold_at_list:
        out["never_sold_at_list_price"] = True
        if out["status"] == "verified" and real < claimed * 0.8:
            out["status"] = "overstated"
    return out


def stats_for_offer(db, offer_id: int, now: Optional[datetime] = None) -> HistoryStats:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=90)).isoformat()
    rows = db.history(offer_id, since=since, verified_only=True)
    # use one currency only (the most recent one) so we never mix ILS and USD
    if rows:
        cur = rows[-1]["currency"]
        rows = [r for r in rows if r["currency"] == cur]
    return compute_stats([(r["observed_at"], r["price"]) for r in rows], now=now)

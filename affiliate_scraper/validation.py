"""Validate and normalise product data so wrong or stale numbers are never shown as fact.

Every product gets:

* ``price_verified``     – True only when price + ISO currency came from structured data
                           (API / feed / JSON-LD / microdata / OpenGraph) and is not stale
* ``validation_status``  – ``verified`` | ``partial`` | ``unverified`` | ``invalid``
* ``validation_issues``  – machine-readable reasons, e.g. ``currency_invalid``, ``stale``
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import Product
from .utils import normalize_availability

ISO_4217 = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD CAD
CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ
GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR
LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN
PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB
TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWL
""".split())

STRUCTURED_SOURCES = ("api", "feed", "json-ld", "microdata", "og")
MAX_PRICE = 1_000_000.0
MINOR_ISSUES = {"list_price_dropped", "image_invalid", "rating_without_reviews", "gtin_invalid"}


def gtin_check_digit_ok(digits: str) -> bool:
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def normalize_gtin(value: str) -> str:
    """Return a 14-digit GTIN (zero padded) if valid, else ''. Accepts EAN-8/UPC-12/EAN-13/GTIN-14."""
    digits = re.sub(r"[\s-]", "", str(value or ""))
    if not digits.isdigit() or len(digits) not in (8, 12, 13, 14):
        return ""
    if not gtin_check_digit_ok(digits):
        return ""
    gtin14 = digits.zfill(14)
    return "" if set(gtin14) == {"0"} else gtin14


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_stale(fetched_at: str, max_age_hours: float, now: Optional[datetime] = None) -> bool:
    dt = _parse_iso(fetched_at)
    if dt is None:
        return True
    return (now or datetime.now(timezone.utc)) - dt > timedelta(hours=max_age_hours)


def validate_product(p: Product, max_age_hours: float = 24.0, now: Optional[datetime] = None) -> Product:
    """Normalise fields in place and set price_verified / validation_status / validation_issues."""
    issues: list[str] = []

    if not p.name:
        issues.append("name_missing")

    # --- price & currency
    if p.price is not None and not (0 < p.price <= MAX_PRICE):
        issues.append("price_out_of_range")
        p.price = None
    if p.price is None:
        issues.append("price_missing")
    cur = (p.currency or "").strip().upper()
    if cur and cur not in ISO_4217:
        issues.append("currency_invalid")
        cur = ""
    elif not cur and p.price is not None:
        issues.append("currency_missing")
    p.currency = cur

    if p.list_price is not None and (p.price is None or not (p.price < p.list_price <= MAX_PRICE)):
        issues.append("list_price_dropped")
        p.list_price = None
    if p.shipping_cost is not None and not (0 <= p.shipping_cost <= MAX_PRICE):
        issues.append("shipping_invalid")
        p.shipping_cost = None

    # --- availability
    raw_av = p.availability
    p.availability = normalize_availability(raw_av)
    if not p.availability:
        issues.append("availability_unknown")

    # --- rating
    if p.rating is not None and not (0 <= p.rating <= 5):
        issues.append("rating_out_of_range")
        p.rating = None
    if p.review_count is not None and p.review_count < 0:
        p.review_count = None
    if p.rating is not None and p.review_count == 0:
        issues.append("rating_without_reviews")

    # --- identifiers
    if p.gtin:
        g = normalize_gtin(p.gtin)
        if not g:
            issues.append("gtin_invalid")
        p.gtin = g

    if p.image and not p.image.startswith(("http://", "https://")):
        issues.append("image_invalid")
        p.image = ""

    # --- freshness
    stale = is_stale(p.fetched_at or p.scraped_at, max_age_hours, now)
    if stale:
        issues.append("stale")

    structured = (p.source_type or p.source.split("+", 1)[0]) in STRUCTURED_SOURCES
    if p.price is not None and not structured:
        issues.append("price_from_html_parsing")

    p.price_verified = bool(p.price is not None and p.currency and structured and not stale)

    if "name_missing" in issues:
        status = "invalid"
    elif not p.price_verified:
        status = "unverified"
    elif p.in_stock is None or (set(issues) - MINOR_ISSUES):
        status = "partial"
    else:
        status = "verified"
    p.validation_status = status
    p.validation_issues = issues
    return p

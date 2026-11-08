"""Small parsing helpers."""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

_TRACKING_PARAMS = re.compile(
    r"^(utm_\w+|fbclid|gclid|gbraid|wbraid|msclkid|mc_cid|mc_eid|_ga|_gl|yclid|igshid|ref_?|tag|aff\w*|"
    r"affiliate\w*|clickid|irclickid|srsltid)$",
    re.I,
)

_CURRENCY_SYMBOLS = {
    "$": "USD", "€": "EUR", "£": "GBP", "₪": "ILS", "¥": "JPY", "₹": "INR",
    "₩": "KRW", "₽": "RUB", "₺": "TRY", "zł": "PLN", "R$": "BRL", "A$": "AUD", "C$": "CAD",
}


def clean_text(value: Any, max_len: int = 0) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("name") or value.get("@value") or ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if max_len and len(text) > max_len:
        text = text[: max_len - 1].rsplit(" ", 1)[0] + "…"
    return text


def parse_price(value: Any) -> Optional[float]:
    """Parse '1,299.90', '1.299,90 €', '₪ 49', 19.5 -> float."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    s = re.sub(r"[^\d.,]", "", str(value))
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        # whichever separator appears last is the decimal separator
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        # "1,299" -> thousands; "12,99" -> decimal
        s = s.replace(",", "") if len(tail) == 3 and head else s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def guess_currency(text: str) -> str:
    text = text or ""
    m = re.search(r"\b([A-Z]{3})\b", text)
    if m:
        return m.group(1)
    for sym in sorted(_CURRENCY_SYMBOLS, key=len, reverse=True):
        if sym in text:
            return _CURRENCY_SYMBOLS[sym]
    return ""


def parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("@value")
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def parse_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None


def short_availability(value: Any) -> str:
    """'https://schema.org/InStock' -> 'InStock'."""
    text = clean_text(value)
    return text.rstrip("/").rsplit("/", 1)[-1] if text else ""


def absolute_url(base: str, href: Any) -> str:
    href = clean_text(href)
    if isinstance(href, str) and href:
        return urljoin(base, href)
    return ""


def strip_tracking(url: str) -> str:
    """Remove tracking / foreign affiliate params so we never hijack someone else's tag."""
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)]
    return urlunparse(parts._replace(query=urlencode(query), fragment=""))


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def domain_matches(domain: str, pattern: str) -> bool:
    pattern = pattern.lower().removeprefix("www.")
    return domain == pattern or domain.endswith("." + pattern)

"""Variant attributes used to decide whether two offers are *really* the same product.

Two listings must never be merged if they differ in volume / weight / capacity,
pack size (contents), colour, condition (new / used / refurbished), warranty or
importer type (official vs parallel import). Attributes are taken from structured
fields first and from the product title as a fallback (English + Hebrew).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .models import Product

# unit -> (dimension, factor to base unit)
UNITS = {
    "ml": ("volume", 1), 'מ"ל': ("volume", 1), "מל": ("volume", 1), "cl": ("volume", 10),
    "l": ("volume", 1000), "lt": ("volume", 1000), "ltr": ("volume", 1000), "liter": ("volume", 1000),
    "litre": ("volume", 1000), "liters": ("volume", 1000), "ליטר": ("volume", 1000), "fl oz": ("volume", 29.5735),
    "g": ("weight", 1), "gr": ("weight", 1), "gram": ("weight", 1), "grams": ("weight", 1), "גרם": ("weight", 1),
    "kg": ("weight", 1000), 'ק"ג': ("weight", 1000), "קג": ("weight", 1000), "קילו": ("weight", 1000),
    "oz": ("weight", 28.3495), "lb": ("weight", 453.592), "lbs": ("weight", 453.592),
    "mb": ("storage", 1 / 1024), "gb": ("storage", 1), "tb": ("storage", 1024), "ג'יגה": ("storage", 1),
    "mah": ("battery", 1), "w": ("power", 1), "watt": ("power", 1), "וואט": ("power", 1),
    "inch": ("screen", 1), "in": ("screen", 1), '"': ("screen", 1), "אינץ'": ("screen", 1), "אינץ": ("screen", 1),
    "mm": ("length", 1), "cm": ("length", 10), "m": ("length", 1000),
}
_unit_alt = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))
QUANTITY_RE = re.compile(rf"(?<![\w.])(\d+(?:[.,]\d+)?)\s*({_unit_alt})(?![\w])", re.I)

PACK_RES = [
    re.compile(r"(?:pack|set|box|case)\s+of\s+(\d+)", re.I),
    re.compile(r"(\d+)\s*[- ]?(?:pack|pk|pcs|pieces|count|ct|units|יח'|יחידות|יח)\b", re.I),
    re.compile(r"(?:מארז|ארגז|סט)\s*(?:של\s*)?(\d+)", re.I),
    re.compile(r"(?<![\w.])(\d+)\s*[x×]\s*(?=\d)", re.I),   # "6 x 500ml"
    re.compile(r"(?<=\s)[x×]\s*(\d+)\b", re.I),               # "... x6"
]

COLORS = {
    "black": "black", "שחור": "black", "white": "white", "לבן": "white", "red": "red", "אדום": "red",
    "blue": "blue", "כחול": "blue", "navy": "blue", "green": "green", "ירוק": "green",
    "silver": "silver", "כסוף": "silver", "כסף": "silver", "gold": "gold", "זהב": "gold", "זהוב": "gold",
    "gray": "gray", "grey": "gray", "אפור": "gray", "graphite": "gray", "pink": "pink", "ורוד": "pink",
    "purple": "purple", "סגול": "purple", "yellow": "yellow", "צהוב": "yellow", "orange": "orange",
    "כתום": "orange", "brown": "brown", "חום": "brown", "beige": "beige", "בז'": "beige",
    "rose gold": "rose gold", "space gray": "gray", "midnight": "midnight", "starlight": "starlight",
}
_color_re = re.compile(r"(?<![\w])(" + "|".join(sorted(map(re.escape, COLORS), key=len, reverse=True)) + r")(?![\w])", re.I)

CONDITION_WORDS = [
    (re.compile(r"refurbished|renewed|reconditioned|מחודש", re.I), "refurbished"),
    (re.compile(r"\bused\b|pre-?owned|second.?hand|משומש|יד שניה|יד 2", re.I), "used"),
    (re.compile(r"open.?box|ex.?display|פתוח|תצוגה", re.I), "open_box"),
    (re.compile(r"for parts|not working|damaged|פגום", re.I), "damaged"),
]

WARRANTY_RES = [
    (re.compile(r"(\d+)\s*(?:years?|yrs?|שנים|שנות|שנה)\s*(?:of\s+)?(?:warranty|אחריות)", re.I), 12),
    (re.compile(r"(?:warranty|אחריות)[:\s]*(\d+)\s*(?:years?|yrs?|שנים|שנות|שנה)", re.I), 12),
    (re.compile(r"(\d+)\s*(?:months?|חודשים|חודש)\s*(?:of\s+)?(?:warranty|אחריות)", re.I), 1),
    (re.compile(r"(?:warranty|אחריות)[:\s]*(\d+)\s*(?:months?|חודשים|חודש)", re.I), 1),
]
IMPORTER_RES = [
    (re.compile(r"יבואן\s*רשמי|official\s+(?:importer|distributor)|official\s+warranty", re.I), "official"),
    (re.compile(r"יבוא\s*מקביל|יבואן\s*מקביל|parallel\s+import|grey\s+import|international\s+version", re.I), "parallel"),
]

_MODEL_TOKEN = re.compile(r"\b(?=[a-z\-]*\d)(?=[\d\-]*[a-z])[a-z0-9\-]{3,}\b", re.I)


@dataclass
class VariantAttrs:
    quantities: dict[str, float] = field(default_factory=dict)  # dimension -> base-unit value
    pack: Optional[int] = None
    color: str = ""
    condition: str = ""          # new | used | refurbished | open_box | damaged | "" (unknown)
    warranty_months: Optional[int] = None
    importer: str = ""           # official | parallel | ""
    model_tokens: frozenset[str] = frozenset()


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def extract_attributes(p: Product) -> VariantAttrs:
    text = " ".join(x for x in (p.name, p.size, p.description[:300] if p.description else "") if x)
    title = " ".join(x for x in (p.name, p.size) if x)
    a = VariantAttrs()

    for m in QUANTITY_RE.finditer(title):
        unit = m.group(2).lower()
        dim, factor = UNITS.get(unit, UNITS.get(m.group(2), (None, None)))
        if dim and dim not in a.quantities:
            a.quantities[dim] = round(_num(m.group(1)) * factor, 3)

    for rx in PACK_RES:
        m = rx.search(title)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 1000:
                a.pack = n
                break

    if p.color:
        a.color = COLORS.get(p.color.strip().lower(), p.color.strip().lower())
    else:
        m = _color_re.search(title)
        if m:
            a.color = COLORS[m.group(1).lower()] if m.group(1).lower() in COLORS else COLORS.get(m.group(1), "")

    cond = (p.condition or "").lower()
    if cond:
        a.condition = "used" if cond in ("used", "pre-owned") else cond
    else:
        for rx, value in CONDITION_WORDS:
            if rx.search(title):
                a.condition = value
                break

    if p.warranty_months is not None:
        a.warranty_months = p.warranty_months
    else:
        for rx, mult in WARRANTY_RES:
            m = rx.search(text)
            if m:
                a.warranty_months = int(m.group(1)) * mult
                break

    for rx, value in IMPORTER_RES:
        if rx.search(text):
            a.importer = value
            break

    tokens = {t.lower().replace("-", "") for t in _MODEL_TOKEN.findall(title)}
    if p.model:
        tokens.add(re.sub(r"[^a-z0-9]", "", p.model.lower()))
    # drop tokens that are just quantities like "500ml", "128gb"
    a.model_tokens = frozenset(t for t in tokens if t and not QUANTITY_RE.fullmatch(t))
    return a


def conflicts(a: VariantAttrs, b: VariantAttrs, identical_gtin: bool = False) -> list[str]:
    """Reasons why two offers must NOT be treated as the same product."""
    reasons = []
    for dim in set(a.quantities) & set(b.quantities):
        x, y = a.quantities[dim], b.quantities[dim]
        if abs(x - y) > 0.02 * max(x, y):
            reasons.append(f"{dim}_differs")
    if a.pack and b.pack and a.pack != b.pack:
        reasons.append("pack_differs")
    elif not identical_gtin and ((a.pack or 1) != (b.pack or 1)):
        reasons.append("pack_unclear")
    if a.color and b.color and a.color != b.color:
        reasons.append("color_differs")
    ca, cb = a.condition or "new_or_unknown", b.condition or "new_or_unknown"
    norm = lambda c: "new_or_unknown" if c in ("new", "new_or_unknown") else c  # noqa: E731
    if norm(ca) != norm(cb):
        reasons.append("condition_differs")
    if a.warranty_months is not None and b.warranty_months is not None and a.warranty_months != b.warranty_months:
        reasons.append("warranty_differs")
    if a.importer and b.importer and a.importer != b.importer:
        reasons.append("importer_differs")
    return reasons

"""Turn product URLs into affiliate links.

Configuration (JSON) example - see ``affiliate_config.example.json``::

    {
      "utm": {"utm_source": "mysite", "utm_medium": "affiliate", "utm_campaign": "{domain}"},
      "programs": {
        "amazon.com":    {"params": {"tag": "mytag-20"}},
        "ebay.com":      {"params": {"mkcid": "1", "mkrid": "711-53200-19255-0", "campid": "5338XXXXXX", "toolid": "10001"}},
        "shop.example":  {"template": "https://www.awin1.com/cread.php?awinmid=1234&awinaffid=5678&ued={url_encoded}"}
      },
      "only_affiliated": false
    }

* ``params``   – query parameters appended to the product URL (Amazon, eBay, most in-house programs)
* ``template`` – deep-link format used by networks (Awin, CJ, Impact, Rakuten, ShareASale,
  AliExpress Portals...). Placeholders: ``{url}``, ``{url_encoded}``, ``{domain}``, ``{sku}``.
* Domains match by suffix, so ``amazon.com`` also covers ``smile.amazon.com``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from .models import Product
from .utils import domain_matches, strip_tracking


@dataclass
class AffiliateConfig:
    programs: dict[str, dict] = field(default_factory=dict)
    utm: dict[str, str] = field(default_factory=dict)
    only_affiliated: bool = False

    @classmethod
    def load(cls, path: str | Path | None) -> "AffiliateConfig":
        if not path:
            return cls()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            programs={k.lower(): v for k, v in data.get("programs", {}).items()},
            utm=data.get("utm", {}),
            only_affiliated=bool(data.get("only_affiliated", False)),
        )

    def program_for(self, domain: str) -> dict | None:
        # longest (most specific) matching pattern wins
        for pattern in sorted(self.programs, key=len, reverse=True):
            if domain_matches(domain, pattern):
                return self.programs[pattern]
        return None


def _add_params(url: str, params: dict[str, str]) -> str:
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({k: str(v) for k, v in params.items()})
    return urlunparse(parts._replace(query=urlencode(query)))


def build_affiliate_url(product: Product, config: AffiliateConfig) -> str:
    """Return the affiliate URL, or "" if no program is configured for the domain."""
    clean = strip_tracking(product.url)
    domain = product.domain
    fmt = {"domain": domain, "sku": product.sku}
    utm = {k: str(v).format(**fmt) for k, v in config.utm.items()}

    program = config.program_for(domain)
    if program is None:
        return _add_params(clean, utm) if (utm and not config.only_affiliated) else ""

    target = _add_params(clean, utm) if utm else clean
    if "template" in program:
        return program["template"].format(url=target, url_encoded=quote(target, safe=""), **fmt)
    return _add_params(target, {k: str(v).format(**fmt) for k, v in program.get("params", {}).items()})


def apply_affiliate_links(products: list[Product], config: AffiliateConfig) -> list[Product]:
    kept = []
    for p in products:
        p.affiliate_url = build_affiliate_url(p, config)
        if config.only_affiliated and config.program_for(p.domain) is None:
            continue
        kept.append(p)
    return kept

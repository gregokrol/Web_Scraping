"""Sites whose terms forbid automated collection - the crawler refuses them.

robots.txt is only the minimum. Some sites forbid scraping in their terms of use even
when robots.txt allows it. Those domains are blocked here, and ``--ignore-robots`` does NOT
unblock them. When the site gives you written permission (or a product feed hosted on
its domain), list the domain under ``"permitted_domains"`` in sources.json.

sources.json::

    {
      "blocked_domains": {"example-shop.co.il": "terms section 4 forbid scraping"},
      "permitted_domains": ["ksp.co.il"],          # only after you received written permission
      "stores": [...]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .utils import domain_matches, domain_of

# Built-in: terms that were read and found to forbid automated collection without permission.
DEFAULT_BLOCKED: dict[str, str] = {
    "ksp.co.il": "KSP terms (AI policy s.6.7): automated collection from the site requires prior written permission",
}


@dataclass
class CrawlPolicy:
    blocked: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_BLOCKED))
    permitted: set[str] = field(default_factory=set)

    def blocked_reason(self, url: str) -> Optional[str]:
        domain = domain_of(url)
        if any(domain_matches(domain, p) for p in self.permitted):
            return None
        for pattern in sorted(self.blocked, key=len, reverse=True):
            if domain_matches(domain, pattern):
                return self.blocked[pattern]
        return None

    @classmethod
    def load(cls, path: str | Path | None) -> "CrawlPolicy":
        policy = cls()
        if not path or not Path(path).is_file():
            return policy
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        extra = data.get("blocked_domains") or {}
        if isinstance(extra, list):
            extra = {d: "listed in blocked_domains" for d in extra}
        policy.blocked.update({str(k).lower().removeprefix("www."): str(v) for k, v in extra.items()})
        policy.permitted = {str(d).lower().removeprefix("www.") for d in data.get("permitted_domains") or []}
        return policy

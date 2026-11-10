"""robots.txt matching as crawlers actually read it (RFC 9309), not as urllib does.

Python's ``urllib.robotparser`` matches rules as plain path prefixes. It ignores the ``*`` and ``$`` wildcards
that most stores use (e.g. WooCommerce's ``Disallow: /*?add-to-cart=``), and it applies the first matching rule
instead of the most specific one. So a crawler built on it fetches URLs the site has forbidden.

``RobotsParser`` keeps the stdlib class for crawl-delay and sitemaps, and replaces ``can_fetch``:

* the group for our user-agent is used (groups naming it are merged); otherwise the ``*`` groups
* ``*`` matches any run of characters, a trailing ``$`` anchors the end of the URL
* the longest matching rule wins; on a tie ``Allow`` wins; no matching rule means allowed
* paths are compared percent-decoded, so ``%D7%A4`` / ``%d7%a4`` / the raw Hebrew letter are the same path
"""

from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import unquote, urlparse
from urllib.robotparser import RobotFileParser


def _decoded(path: str) -> str:
    return unquote(path or "/")


def _compile(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    rx = ".*".join(re.escape(part) for part in _decoded(body).split("*"))
    return re.compile(rx + (r"\Z" if anchored else ""), re.S)


class _Group:
    def __init__(self) -> None:
        self.agents: list[str] = []
        self.rules: list[tuple[int, bool, re.Pattern]] = []   # (specificity, allow, pattern)


class RobotsParser(RobotFileParser):
    def __init__(self, url: str = "") -> None:
        super().__init__(url)
        self._groups: list[_Group] = []

    def parse(self, lines: Iterable[str]) -> None:
        lines = list(lines)
        super().parse(lines)           # crawl-delay, request-rate and sitemaps stay with the stdlib
        groups: list[_Group] = []
        current: Optional[_Group] = None
        last_was_agent = False
        for raw in lines:
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (s.strip() for s in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if current is None or not last_was_agent:   # consecutive user-agent lines share one group
                    current = _Group()
                    groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if current is None or key not in ("allow", "disallow"):
                continue
            if key == "disallow" and not value:
                continue                                       # "Disallow:" (empty) allows everything
            if value:
                current.rules.append((len(_decoded(value)), key == "allow", _compile(value)))
        self._groups = groups

    def _rules_for(self, useragent: str) -> list[tuple[int, bool, re.Pattern]]:
        token = (useragent or "").split("/")[0].strip().lower()
        named = [g for g in self._groups if any(a != "*" and a and a in token for a in g.agents)]
        chosen = named or [g for g in self._groups if "*" in g.agents]
        return [r for g in chosen for r in g.rules]

    def can_fetch(self, useragent: str, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        if not self.last_checked:
            return False                # not read yet: assume nothing is allowed (stdlib behaviour)
        p = urlparse(url)
        path = _decoded(p.path) + ("?" + unquote(p.query) if p.query else "")
        if path == "/robots.txt":
            return True
        best: Optional[tuple[int, bool]] = None
        for length, allow, rx in self._rules_for(useragent):
            if rx.match(path) and (best is None or (length, allow) > best):
                best = (length, allow)
        return True if best is None else best[1]

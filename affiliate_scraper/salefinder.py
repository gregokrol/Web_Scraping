"""Find a store's own sale page ("מבצעים") from its home page - so nobody has to copy links by hand.

Reads the home page (robots.txt and the per-host delay apply as for any page), scores the links by
sale words in their text or address, opens the best few and keeps the one that really lists products.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

SALE_WORDS = ("מבצע", "סייל", "sale", "outlet", "אאוטלט", "דיל", "deal", "סוף עונה", "הנחה", "הנחות",
              "מחירים מיוחדים", "black friday", "בלאק פריידי", "clearance", "חיסול", "מחיר מיוחד")
SKIP = re.compile(r"(cart|checkout|account|login|signin|register|wishlist|blog|contact|terms|policy|policies|"
                  r"privacy|takanon|תקנון|faq|careers|jobs|stores?-locator|branches)", re.I)
MIN_PRODUCTS = 3


def _norm(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc.lower(), p.path or "/", "", p.query, ""))


def _abs(href: str, base_url: str, domain: str) -> str:
    """Some shops write links as "www.shop.co.il/sale" (no scheme) - that is their own host, not a path."""
    h = href.strip()
    bare = h.split("/")[0].lower()
    if bare and (bare == domain or bare.endswith("." + domain)) and not h.startswith(("http:", "https:", "//")):
        h = "https://" + h
    return urljoin(base_url, h)


def _same_site(url: str, domain: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


def sale_candidates(html: str, base_url: str, domain: str, product_re: re.Pattern) -> list[tuple[int, str]]:
    """[(score, url)] best first: links on the page whose text or address has sale words."""
    soup = BeautifulSoup(html, "lxml")
    best: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = _norm(_abs(href, base_url, domain))
        if not url.startswith(("http://", "https://")) or not _same_site(url, domain):
            continue
        path = unquote(urlparse(url).path + "?" + urlparse(url).query).lower()
        if product_re.search(url) or SKIP.search(path) or urlparse(url).path in ("", "/"):
            continue
        text = " ".join(a.get_text(" ").split() + [a.get("title") or "", a.get("aria-label") or ""]).lower()
        score = sum(3 for w in SALE_WORDS if w in text) + sum(1 for w in SALE_WORDS if w in path)
        if score:
            best[url] = max(best.get(url, 0), score)
    return sorted(((s, u) for u, s in best.items()), key=lambda x: (-x[0], len(x[1])))


def product_links(html: str, base_url: str, domain: str, product_re: re.Pattern) -> set[str]:
    soup = BeautifulSoup(html, "lxml")
    out = set()
    for a in soup.find_all("a", href=True):
        url = _norm(_abs(a["href"], base_url, domain))
        if _same_site(url, domain) and product_re.search(url):
            out.add(url.split("?")[0])
    return out


def find_sale_page(fetcher, home: str, domain: str, product_pattern: str, max_checks: int = 4) -> Optional[dict]:
    """{"url", "products", "score"} of the best sale page, or None. Only reads pages (never posts)."""
    product_re = re.compile(product_pattern)
    page = fetcher.get(home)
    if page is None or page.status != 200:
        return None
    found = None
    for score, url in sale_candidates(page.text, page.url, domain, product_re)[:max_checks]:
        p = fetcher.get(url)
        if p is None or p.status != 200:
            continue
        n = len(product_links(p.text, p.url, domain, product_re))
        if n >= MIN_PRODUCTS and (found is None or (score, n) > (found["score"], found["products"])):
            found = {"url": url, "products": n, "score": score}
    return found

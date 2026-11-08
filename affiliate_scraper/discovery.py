"""Find product URLs on a site: via XML sitemaps or by following links on listing pages."""

from __future__ import annotations

import logging
import re
from typing import Iterable, Optional
from urllib.parse import urldefrag, urlparse

from bs4 import BeautifulSoup

from .fetcher import PoliteFetcher
from .security import safe_regex
from .utils import absolute_url, domain_of

log = logging.getLogger(__name__)

# URL shapes commonly used for product pages by Shopify, WooCommerce, Magento, Amazon, etc.
DEFAULT_PRODUCT_PATTERN = r"/(products?|item|p|dp|gp/product|shop)/[^/?#]+"


def _parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """Return (child_sitemaps, page_urls)."""
    soup = BeautifulSoup(xml, "xml") if "<" in xml else None
    if soup is None:
        return [], []
    children = [loc.get_text(strip=True) for sm in soup.find_all("sitemap") for loc in sm.find_all("loc")]
    pages = [loc.get_text(strip=True) for u in soup.find_all("url") for loc in u.find_all("loc", limit=1)]
    return children, pages


def from_sitemaps(
    fetcher: PoliteFetcher,
    site_url: str,
    include: Optional[str] = DEFAULT_PRODUCT_PATTERN,
    limit: int = 100,
    max_sitemaps: int = 25,
) -> list[str]:
    """Collect product URLs from robots.txt-declared sitemaps (falls back to /sitemap.xml)."""
    p = urlparse(site_url)
    origin = f"{p.scheme}://{p.netloc}"
    queue = fetcher.sitemaps_from_robots(site_url) or [origin + "/sitemap.xml"]
    # Prefer product sitemaps (Shopify: sitemap_products_1.xml, Woo: product-sitemap.xml)
    pattern = safe_regex(include)
    seen_maps: set[str] = set()
    found: list[str] = []

    while queue and len(found) < limit and len(seen_maps) < max_sitemaps:
        sm_url = queue.pop(0)
        if sm_url in seen_maps:
            continue
        seen_maps.add(sm_url)
        page = fetcher.get(sm_url)
        if page is None:
            continue
        children, urls = _parse_sitemap(page.text)
        product_maps = [c for c in children if "product" in c.lower()]
        other_maps = [c for c in children if "product" not in c.lower()]
        queue = product_maps + queue + other_maps
        for u in urls:
            if (pattern is None or pattern.search(u)) and u not in found and fetcher.allowed(u):
                found.append(u)
                if len(found) >= limit:
                    break
    log.info("Sitemaps: %d product URLs from %d sitemap(s)", len(found), len(seen_maps))
    return found


def links_on_page(html: str, base_url: str) -> Iterable[str]:
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        url = urldefrag(absolute_url(base_url, a["href"]))[0]
        if url.startswith(("http://", "https://")):
            yield url


def from_listing_pages(
    fetcher: PoliteFetcher,
    start_urls: list[str],
    include: str = DEFAULT_PRODUCT_PATTERN,
    limit: int = 100,
    next_page_pattern: Optional[str] = r"[?&](page|p|pg)=\d+|/page/\d+",
    max_listing_pages: int = 10,
) -> list[str]:
    """Follow links from category/search pages and keep those that look like product pages.

    Pagination links matching ``next_page_pattern`` on the same domain are followed
    (up to ``max_listing_pages``).
    """
    product_re = safe_regex(include) or re.compile(".")
    next_re = safe_regex(next_page_pattern)
    allowed_domains = {domain_of(u) for u in start_urls}
    queue = list(start_urls)
    visited: set[str] = set()
    found: list[str] = []

    while queue and len(found) < limit and len(visited) < max_listing_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        page = fetcher.get(url)
        if page is None:
            continue
        for link in links_on_page(page.text, page.url):
            if domain_of(link) not in allowed_domains:
                continue
            if product_re.search(link):
                if link not in found and fetcher.allowed(link):
                    found.append(link)
            elif next_re and next_re.search(link) and link not in visited:
                queue.append(link)
            if len(found) >= limit:
                break
    log.info("Listing pages: %d product URLs from %d page(s)", len(found), len(visited))
    return found

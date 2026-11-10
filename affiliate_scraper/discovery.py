"""Find product URLs on a site: via XML sitemaps or by following links on listing pages."""

from __future__ import annotations

import logging
import re
from typing import Iterable, Optional
from urllib.parse import parse_qsl, unquote, urldefrag, urlencode, urlparse, urlunparse

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


# product sitemaps (Shopify: sitemap_products_1.xml, Woo/Yoast: product-sitemap3.xml) - not the product
# category / tag / brand / attribute sitemaps (Yoast: product_cat-sitemap.xml), which list no products
_PRODUCT_MAP = re.compile(r"product(?![-_]?(cat|tag|brand|attribute|type))", re.I)


def from_sitemaps(
    fetcher: PoliteFetcher,
    site_url: str,
    include: Optional[str] = DEFAULT_PRODUCT_PATTERN,
    limit: int = 100,
    max_sitemaps: int = 25,
    products_only: bool = False,
) -> list[str]:
    """Collect product URLs from robots.txt-declared sitemaps (falls back to /sitemap.xml).

    ``products_only``: of a sitemap index, read only the product sitemaps (if it names any) - for reading a
    store's whole product list without the dozens of category/attribute sitemaps big shops also publish."""
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
        product_maps = [c for c in children if _PRODUCT_MAP.search(c.rsplit("/", 1)[-1])]
        other_maps = [c for c in children if c not in product_maps]
        if products_only and product_maps:
            other_maps = []
        queue = product_maps + queue + other_maps
        for u in urls:
            if (pattern is None or pattern.search(u)) and u not in found and fetcher.allowed(u):
                found.append(u)
                if len(found) >= limit:
                    break
    log.info("Sitemaps: %d product URLs from %d sitemap(s)", len(found), len(seen_maps))
    return found


def relevance(query: str, *texts: str) -> float:
    """Share of the search words found in a result's title / URL (0..1).

    Stores' own search engines return loose matches (a search for "airpods" brings Air Max shoes), so results
    are ranked by this, and those with none of the words are shown apart. The URL counts because some shops'
    product names are generic ("אוזניות אלחוטיות") while the URL says /apple/airpods/4/.
    """
    words = [w for w in " ".join(str(query).split()).lower().split() if len(w) >= 2] or [str(query).lower()]
    hay = " ".join(unquote(t or "") for t in texts).lower().replace("-", " ").replace("_", " ")
    squashed = hay.replace(" ", "")
    return round(sum(1 for w in words if w in hay or w in squashed) / len(words), 2)


def search_sitemaps(
    fetcher: PoliteFetcher,
    site_url: str,
    words: str,
    include: Optional[str] = DEFAULT_PRODUCT_PATTERN,
    limit: int = 12,
    max_sitemaps: int = 60,
) -> list[str]:
    """Product URLs from the site's product sitemaps whose address holds the search words, best match first.

    For stores whose robots.txt forbids their own search page but that publish product sitemaps for crawlers,
    with the product name in each product URL (".../product/מקרר-samsung-rf50/"). The sitemaps come from the
    page cache after the first search, so a search costs a request or two plus the product pages.
    """
    every = from_sitemaps(fetcher, site_url, include=include, limit=10 ** 6, max_sitemaps=max_sitemaps,
                          products_only=True)
    scored = sorted(((relevance(words, u), i, u) for i, u in enumerate(every)), key=lambda s: (-s[0], s[1]))
    picked = [u for r, _, u in scored if r > 0][:limit]
    log.info("Sitemap search %r: %d of %d product URLs match", words, len(picked), len(every))
    return picked


# Links that DO something (add to cart / wishlist / compare, remove from cart). Shops render them as plain <a href>,
# often on listing pages and with the page number in the path ("/page/2/?add-to-cart=123"), so they look like
# pagination. Following one changes state on the store's side - never follow them, whatever robots.txt says.
ACTION_LINK = re.compile(r"[?&](add[-_]to[-_](cart|wishlist|compare)|add_to_wishlist|remove_item|removed_item)=", re.I)


# Click-tracking parameters shops add to result links (Shopify search: ?_pos=1&_sid=ab12&_ss=r). They change on
# every search, so the same product would be fetched twice and never come from the cache.
TRACKING_PARAM = re.compile(r"^(utm_[a-z]+|_pos|_sid|_ss|_psq|srsltid|gclid|fbclid|mc_cid|mc_eid)$", re.I)


def strip_tracking(url: str) -> str:
    p = urlparse(url)
    if not p.query:
        return url
    pairs = parse_qsl(p.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if not TRACKING_PARAM.match(k)]
    if len(kept) == len(pairs):
        return url
    return urlunparse(p._replace(query=urlencode(kept)))


def _url_key(url: str) -> str:
    """Identity of a URL for de-duplication: %D7%A4, %d7%a4 and the raw letter are the same page."""
    return unquote(url).rstrip("/").lower()


def links_on_page(html: str, base_url: str) -> Iterable[str]:
    for url, _ in _page_links(html, base_url):
        yield url


def _page_links(html: str, base_url: str, selector: Optional[str] = None) -> list[tuple[str, bool]]:
    """Every link on the page, flagged True when it is inside ``selector`` (a CSS selector for product cards)."""
    soup = BeautifulSoup(html, "lxml")
    marked = {id(a) for a in soup.select(selector)} if selector else set()
    out = []
    for a in soup.find_all("a", href=True):
        url = urldefrag(absolute_url(base_url, a["href"]))[0]
        if url.startswith(("http://", "https://")):
            out.append((url, id(a) in marked))
    return out


def check_selector(selector: Optional[str]) -> Optional[str]:
    """Raise ValueError for a CSS selector BeautifulSoup cannot use."""
    if selector:
        try:
            BeautifulSoup("<a></a>", "lxml").select(selector)
        except Exception as exc:  # soupsieve raises its own SelectorSyntaxError
            raise ValueError(f"not a valid CSS selector: {selector!r} ({exc})") from None
    return selector


def from_listing_pages(
    fetcher: PoliteFetcher,
    start_urls: list[str],
    include: str = DEFAULT_PRODUCT_PATTERN,
    limit: int = 100,
    next_page_pattern: Optional[str] = r"[?&](page|p|pg)=\d+|/page/\d+",
    max_listing_pages: int = 10,
    link_selector: Optional[str] = None,
) -> list[str]:
    """Follow links from category/search pages and keep those that look like product pages.

    Pagination links matching ``next_page_pattern`` on the same domain are followed
    (up to ``max_listing_pages``). With ``link_selector`` (CSS, e.g. ``a.product-preview-image``)
    only links inside the store's product cards count as products - menus, banners and
    "recommended" strips are ignored, and the cards keep the store's own order (relevance on a search page).
    """
    product_re = safe_regex(include) or re.compile(".")
    next_re = safe_regex(next_page_pattern)
    check_selector(link_selector)
    allowed_domains = {domain_of(u) for u in start_urls}
    queue = list(start_urls)
    visited: set[str] = set()      # keys from _url_key: one entry per page, however its URL is encoded
    queued: set[str] = {_url_key(u) for u in start_urls}
    found: list[str] = []
    found_keys: set[str] = set()

    while queue and len(found) < limit and len(visited) < max_listing_pages:
        url = queue.pop(0)
        if _url_key(url) in visited:
            continue
        visited.add(_url_key(url))
        page = fetcher.get(url)
        if page is None:
            continue
        links = _page_links(page.text, page.url, link_selector)
        candidates = [ln for ln, in_card in links if product_re.search(ln) and (in_card or not link_selector)]
        # a store search with exactly one hit often redirects straight to that product's page
        if (_url_key(page.url) != _url_key(url) and product_re.search(page.url) and not candidates
                and _url_key(page.url) not in found_keys):
            found.append(page.url)
            found_keys.add(_url_key(page.url))
            continue
        for link, in_card in links:
            if domain_of(link) not in allowed_domains or ACTION_LINK.search(link):
                continue
            key = _url_key(link)
            if product_re.search(link) and (in_card or not link_selector):
                link = strip_tracking(link)
                key = _url_key(link)
                if key not in found_keys and fetcher.allowed(link):
                    found.append(link)
                    found_keys.add(key)
            elif next_re and next_re.search(link) and key not in queued and fetcher.allowed(link):
                queue.append(link)
                queued.add(key)
            if len(found) >= limit:
                break
    log.info("Listing pages: %d product URLs from %d page(s)", len(found), len(visited))
    return found

"""robots.txt wildcards (RFC 9309) and listing-page discovery that never follows cart/action links."""

import pytest

from affiliate_scraper.discovery import from_listing_pages
from affiliate_scraper.fetcher import PoliteFetcher
from affiliate_scraper.robots import RobotsParser

UA = "Web_Scraping/3.0 (+https://example.org/bot)"

WOO = """# typical WooCommerce store
User-agent: *
Disallow: /cart/
Disallow: /*?add-to-cart=
Disallow: /*?*orderby=
Disallow: /*.pdf$
Allow: /wp-admin/admin-ajax.php
Disallow: /wp-admin/
Crawl-delay: 4
Sitemap: https://shop.example/sitemap.xml

User-agent: meta-externalagent
Disallow: /
"""


def parser(text: str) -> RobotsParser:
    rp = RobotsParser("https://shop.example/robots.txt")
    rp.parse(text.splitlines())
    return rp


@pytest.mark.parametrize("path, allowed", [
    ("/product/kettle/", True),
    ("/c/puzzles/page/2/?add-to-cart=948180", False),     # urllib.robotparser says True here
    ("/c/puzzles/?orderby=price", False),
    ("/c/puzzles/?page=2&orderby=price", False),
    ("/c/puzzles/?page=2", True),
    ("/files/manual.pdf", False),
    ("/files/manual.pdf?v=2", True),                      # "$" anchors the end of the URL
    ("/cart/", False),
    ("/wp-admin/admin-ajax.php", True),                   # longer Allow beats shorter Disallow
    ("/wp-admin/options.php", False),
    ("/robots.txt", True),
])
def test_wildcards_anchor_and_longest_match(path, allowed):
    assert parser(WOO).can_fetch(UA, "https://shop.example" + path) is allowed


def test_named_group_replaces_star_group_and_stdlib_parts_still_work():
    rp = parser(WOO)
    assert rp.can_fetch("meta-externalagent/1.1", "https://shop.example/product/kettle/") is False
    assert rp.crawl_delay(UA) == 4
    assert rp.site_maps() == ["https://shop.example/sitemap.xml"]


def test_tie_goes_to_allow_and_empty_disallow_allows_all():
    rp = parser("User-agent: *\nDisallow: /shop\nAllow: /shop\n")
    assert rp.can_fetch(UA, "https://shop.example/shop/a")
    assert parser("User-agent: *\nDisallow:\n").can_fetch(UA, "https://shop.example/anything")


def test_percent_encoding_case_and_raw_letters_are_the_same_path():
    rp = parser("User-agent: *\nDisallow: /%D7%A2%D7%95%D7%93%D7%A4%D7%99%D7%9D/\n")   # /עודפים/
    for url in ("https://shop.example/עודפים/x", "https://shop.example/%d7%a2%d7%95%d7%93%d7%a4%d7%99%d7%9d/x"):
        assert rp.can_fetch(UA, url) is False
    assert rp.can_fetch(UA, "https://shop.example/פאזלים/x")


# ---------------------------------------------------------------- discovery
def test_listing_discovery_skips_cart_links_and_duplicate_encodings(local_site):
    local_site.add("/robots.txt", "User-agent: *\nDisallow: /*?add-to-cart=\n", content_type="text/plain")
    local_site.add("/c/", """
        <a href="/product/a/">A</a>
        <a href="/product/%D7%90/">alef</a> <a href="/product/%d7%90/">alef again</a>
        <a href="/c/page/2/">2</a> <a href="/c/page/2/?add-to-cart=7">buy</a>
        <a href="/c/page/2/?add_to_wishlist=7">wish</a>""")
    local_site.add("/c/page/2/", '<a href="/product/b/">B</a> <a href="/c/">1</a>')
    f = PoliteFetcher(min_delay=0, allow_private=True, cache_dir="")
    found = from_listing_pages(f, [local_site.base + "/c/"], include="/product/")
    assert [u.rsplit("/product/", 1)[1] for u in found] == ["a/", "%D7%90/", "b/"]
    assert local_site.count("/c/page/2/?add-to-cart=7") == 0
    assert local_site.count("/c/page/2/?add_to_wishlist=7") == 0
    assert local_site.count("/c/") == 1 and local_site.count("/c/page/2/") == 1

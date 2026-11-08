from urllib.parse import parse_qs, unquote, urlparse

from affiliate_scraper.affiliate import AffiliateConfig, apply_affiliate_links, build_affiliate_url
from affiliate_scraper.models import Product
from affiliate_scraper.pipeline import Filters, dedupe

CONFIG = AffiliateConfig(
    programs={
        "amazon.com": {"params": {"tag": "mytag-20"}},
        "shop.example": {"template": "https://track.example/c?id=42&u={url_encoded}"},
    },
    utm={"utm_source": "mysite", "utm_campaign": "{domain}"},
)


def test_param_program_replaces_existing_tag():
    p = Product(url="https://smile.amazon.com/dp/B01?tag=other-20")
    q = parse_qs(urlparse(build_affiliate_url(p, CONFIG)).query)
    assert q["tag"] == ["mytag-20"]
    assert q["utm_campaign"] == ["smile.amazon.com"]


def test_template_program_encodes_target():
    p = Product(url="https://shop.example/products/a?x=1")
    aff = build_affiliate_url(p, CONFIG)
    assert aff.startswith("https://track.example/c?id=42&u=https%3A%2F%2Fshop.example")
    assert "utm_source=mysite" in unquote(aff)


def test_unknown_domain_and_only_affiliated():
    p = Product(url="https://other.example/p/1", name="x")
    assert "utm_source=mysite" in build_affiliate_url(p, CONFIG)
    strict = AffiliateConfig(programs=CONFIG.programs, only_affiliated=True)
    assert apply_affiliate_links([p], strict) == []


def test_filters_and_dedupe():
    a = Product(url="https://s.example/a", name="Good mug", rating=4.6, review_count=50, price=10, sku="1")
    b = Product(url="https://s.example/a?v=2", name="Good mug", rating=4.6, review_count=50, price=10, sku="1")
    c = Product(url="https://s.example/c", name="Bad mug", rating=3.0, price=10)
    f = Filters(min_rating=4, max_price=20, keywords=("mug",))
    assert [p.name for p in dedupe([a, b, c]) if f.accept(p)] == ["Good mug"]

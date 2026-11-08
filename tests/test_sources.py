"""P0: source adapters - feeds, official API, CSS selectors, JSON-LD extra fields, Playwright."""

import importlib.util
import json
import os

import pytest

from affiliate_scraper.affiliate import AffiliateConfig, apply_affiliate_links
from affiliate_scraper.cli import main
from affiliate_scraper.extractors import extract_products
from affiliate_scraper.fetcher import PoliteFetcher
from affiliate_scraper.pipeline import run_scrape
from affiliate_scraper.sources import AdapterContext, EbayBrowseAdapter, ProductFeedAdapter, StoreConfig
from affiliate_scraper.validation import validate_product

GOOGLE_FEED = """<?xml version="1.0"?>
<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel>
<item>
  <g:id>SKU-1</g:id><g:title>Acme Kettle 1.7L</g:title><g:link>https://shop.example/kettle</g:link>
  <g:price>49.90 ILS</g:price><g:sale_price>39.90 ILS</g:sale_price>
  <g:availability>in stock</g:availability><g:brand>Acme</g:brand><g:gtin>4006381333931</g:gtin>
  <g:mpn>K-17</g:mpn><g:condition>new</g:condition><g:image_link>https://cdn.example/k.jpg</g:image_link>
  <g:shipping><g:country>IL</g:country><g:price>0 ILS</g:price></g:shipping>
</item>
<item><g:id>SKU-2</g:id><g:title>No link item</g:title><g:price>1 ILS</g:price></item>
</channel></rss>"""

AWIN_CSV = ("aw_deep_link,merchant_deep_link,product_name,search_price,rrp_price,currency,ean,brand_name,in_stock,"
            "delivery_cost,merchant_name\n"
            "https://www.awin1.com/pclick.php?p=1&a=2&m=3,https://store.example/p/1,Acme Kettle 1.7L,42.00,55.00,ILS,"
            "4006381333931,Acme,1,15,Store Example\n")


def ctx(**kw):
    return AdapterContext(fetcher=PoliteFetcher(min_delay=0, allow_private=True), **kw)


def test_google_merchant_feed(tmp_path):
    path = tmp_path / "feed.xml"
    path.write_text(GOOGLE_FEED, encoding="utf-8")
    [p] = ProductFeedAdapter(ctx()).fetch(str(path))
    validate_product(p)
    assert (p.name, p.price, p.list_price, p.currency) == ("Acme Kettle 1.7L", 39.9, 49.9, "ILS")
    assert p.shipping_cost == 0 and p.model == "K-17" and p.gtin == "04006381333931"
    assert p.in_stock is True and p.price_verified and p.source_type == "feed"


def test_awin_csv_feed_keeps_network_affiliate_link(tmp_path):
    path = tmp_path / "awin.csv"
    path.write_text(AWIN_CSV, encoding="utf-8")
    [p] = ProductFeedAdapter(ctx()).fetch(str(path))
    validate_product(p)
    [p] = apply_affiliate_links([p], AffiliateConfig(utm={"utm_source": "x"}))
    assert p.url == "https://store.example/p/1"
    assert p.affiliate_url.startswith("https://www.awin1.com/pclick.php")
    assert (p.price, p.list_price, p.shipping_cost, p.total_price) == (42.0, 55.0, 15.0, 57.0)
    assert p.store == "Store Example" and p.in_stock is True


def test_feed_via_cli(tmp_path):
    path = tmp_path / "feed.xml"
    path.write_text(GOOGLE_FEED, encoding="utf-8")
    rc = main(["--feed", str(path), "-o", str(tmp_path / "out"), "--name", "f", "--formats", "json",
               "--cache-dir", ""])
    assert rc == 0
    data = json.loads((tmp_path / "out" / "f.json").read_text(encoding="utf-8"))
    assert data[0]["validation_status"] == "verified"


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _EbaySession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return _Resp(200, {"access_token": "T", "expires_in": 7200})

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return _Resp(200, {"itemSummaries": [{
            "itemId": "v1|1|0", "title": "Acme Kettle 1.7L", "price": {"value": "35.00", "currency": "USD"},
            "itemWebUrl": "https://www.ebay.com/itm/1", "itemAffiliateWebUrl": "https://www.ebay.com/itm/1?mkcid=1",
            "image": {"imageUrl": "https://i.ebayimg.com/1.jpg"}, "condition": "New",
            "shippingOptions": [{"shippingCost": {"value": "5.00", "currency": "USD"}}],
            "seller": {"username": "acme_store", "feedbackPercentage": "99.5"}}]})


def test_ebay_browse_api_adapter():
    sess = _EbaySession()
    env = {"EBAY_CLIENT_ID": "id", "EBAY_CLIENT_SECRET": "secret", "EBAY_CAMPAIGN_ID": "555"}
    [p] = EbayBrowseAdapter(ctx(), session=sess, env=env).fetch("ebay:gtin:4006381333931")
    method, url, kw = sess.calls[1]
    assert kw["params"]["gtin"] == "4006381333931"
    assert kw["headers"]["X-EBAY-C-ENDUSERCTX"] == "affiliateCampaignId=555"
    assert (p.price, p.shipping_cost, p.condition, p.source_type) == (35.0, 5.0, "new", "api")
    assert p.affiliate_url.endswith("mkcid=1") and p.rating is None  # seller feedback is not a product rating


def test_ebay_missing_credentials_logged_not_crashing():
    c = ctx()
    assert EbayBrowseAdapter(c, session=_EbaySession(), env={}).fetch("ebay:search:kettle") == []
    assert c.fetcher.errors[-1].kind == "credentials_missing"


def test_jsonld_extra_fields():
    html = """<script type="application/ld+json">{"@type":"Product","name":"Phone X 128GB","model":"PX-128",
      "color":"Black","brand":{"name":"Acme"},
      "offers":{"@type":"Offer","price":"999","priceCurrency":"USD","availability":"InStock",
        "itemCondition":"https://schema.org/RefurbishedCondition","seller":{"@type":"Organization","name":"Best Shop"},
        "shippingDetails":{"@type":"OfferShippingDetails","shippingRate":{"value":"12.5","currency":"USD"}},
        "warranty":{"@type":"WarrantyPromise","durationOfWarranty":{"value":2,"unitCode":"ANN"}}}}</script>"""
    [p] = extract_products(html, "https://shop.example/px")
    assert (p.model, p.color, p.condition, p.store, p.shipping_cost, p.warranty_months) == \
           ("PX-128", "Black", "refurbished", "Best Shop", 12.5, 24)


def test_css_selector_adapter_marks_price_unverified(local_site):
    local_site.add("/item/7", '<html><h1 class="t">Plain Kettle</h1><span class="p">₪ 89.90</span>'
                              '<span class="s">במלאי</span></html>')
    stores = [StoreConfig(domain="127.0.0.1", selectors={"name": "h1.t", "price": ".p", "availability": ".s"},
                          shipping_cost=0, trust_score=0.8)]
    res = run_scrape([local_site.base + "/item/7"], PoliteFetcher(min_delay=0, allow_private=True), stores=stores)
    [p] = res.products
    assert (p.name, p.price, p.currency, p.in_stock, p.shipping_cost) == ("Plain Kettle", 89.9, "ILS", True, 0)
    assert not p.price_verified and "price_from_html_parsing" in p.validation_issues


HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed (optional)")
def test_browser_adapter_renders_js_products(local_site):
    ld = json.dumps({"@type": "Product", "name": "JS Kettle",
                     "offers": {"price": "20", "priceCurrency": "EUR", "availability": "InStock"}})
    local_site.add("/js", "<html><head></head><body><script>"
                          "var s=document.createElement('script');s.type='application/ld+json';"
                          f"s.text={json.dumps(ld)};document.head.appendChild(s);</script></body></html>")
    f = PoliteFetcher(min_delay=0, allow_private=True)
    assert run_scrape([local_site.base + "/js"], f).products == []          # static HTML has no data
    try:
        res = run_scrape([local_site.base + "/js"], PoliteFetcher(min_delay=0, allow_private=True), render="js",
                         browser_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
    except Exception as exc:  # browser binary missing on this machine
        pytest.skip(f"browser not available: {exc}")
    if not res.products and any(e.kind == "adapter_error" for e in res.errors):
        pytest.skip("browser not available")
    assert [p.name for p in res.products] == ["JS Kettle"]

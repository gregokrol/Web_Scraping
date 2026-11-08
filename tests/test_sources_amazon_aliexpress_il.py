"""Amazon Creators API, AliExpress Affiliate API and Israeli-store XML feeds (offline, fake HTTP)."""

import hashlib
import json
from urllib.parse import parse_qs, urlparse

from affiliate_scraper.cli import main
from affiliate_scraper.db import Database
from affiliate_scraper.fetcher import PoliteFetcher
from affiliate_scraper.matching import compare_prices
from affiliate_scraper.models import Product
from affiliate_scraper.scoring import apply_scores
from affiliate_scraper.sources import AdapterContext, AliExpressAffiliateAdapter, AmazonCreatorsAdapter
from affiliate_scraper.sources.aliexpress import sign_request
from affiliate_scraper.sources.feed import parse_generic_xml_feed
from affiliate_scraper.validation import validate_product


def ctx():
    return AdapterContext(fetcher=PoliteFetcher(min_delay=0, allow_private=True))


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.responses.pop(0)


# ---------------------------------------------------------------- Amazon
AMZ_ITEM = {
    "asin": "B0TESTASIN", "detailPageURL": "https://www.amazon.com/dp/B0TESTASIN?tag=mytag-20&linkCode=ogi",
    "itemInfo": {"title": {"displayValue": "Acme Kettle 1.7L"},
                 "byLineInfo": {"brand": {"displayValue": "Acme"}},
                 "externalIds": {"eans": {"displayValues": ["4006381333931"]}},
                 "manufactureInfo": {"model": {"displayValue": "K-17"}},
                 "productInfo": {"color": {"displayValue": "Black"}}},
    "images": {"primary": {"large": {"url": "https://m.media-amazon.com/images/I/x.jpg"}}},
    "customerReviews": {"count": 1520, "starRating": {"value": 4.6}},
    "offersV2": {"listings": [
        {"isBuyBoxWinner": True, "price": {"money": {"amount": 39.99, "currency": "USD"},
                                           "savingBasis": {"money": {"amount": 59.99, "currency": "USD"}}},
         "availability": {"type": "IN_STOCK", "message": "In Stock"}, "condition": {"value": "New"},
         "merchantInfo": {"name": "Amazon.com"}}]},
}
AMZ_ENV = {"AMAZON_CREDENTIAL_ID": "cid", "AMAZON_CREDENTIAL_SECRET": "csecret", "AMAZON_CREDENTIAL_VERSION": "2.1",
           "AMAZON_PARTNER_TAG": "mytag-20"}


def test_amazon_search_v2_protocol_and_mapping():
    sess = FakeSession([Resp(200, {"access_token": "TOK", "expires_in": 3600}),
                        Resp(200, {"searchResult": {"items": [AMZ_ITEM]}})])
    [p] = AmazonCreatorsAdapter(ctx(), session=sess, env=AMZ_ENV).fetch("amazon:search:electric kettle")
    token_url, token_kw = sess.calls[0]
    assert token_url == "https://creatorsapi.auth.us-east-1.amazoncognito.com/oauth2/token"
    assert token_kw["data"]["scope"] == "creatorsapi/default" and token_kw["data"]["grant_type"] == "client_credentials"
    url, kw = sess.calls[1]
    assert url == "https://creatorsapi.amazon/catalog/v1/searchItems"
    assert kw["headers"]["Authorization"] == "Bearer TOK, Version 2.1"
    assert kw["headers"]["x-marketplace"] == "www.amazon.com"
    assert kw["json"]["keywords"] == "electric kettle" and kw["json"]["partnerTag"] == "mytag-20"
    assert "offersV2.listings.price" in kw["json"]["resources"]
    validate_product(p)
    assert (p.name, p.brand, p.model, p.price, p.list_price, p.currency) == ("Acme Kettle 1.7L", "Acme", "K-17", 39.99, 59.99, "USD")
    assert p.gtin == "04006381333931" and p.in_stock is True and p.condition == "new"
    assert (p.rating, p.review_count) == (4.6, 1520)
    assert p.affiliate_url.startswith("https://www.amazon.com/dp/B0TESTASIN?tag=mytag-20")
    assert p.url == "https://www.amazon.com/dp/B0TESTASIN" and p.price_verified


def test_amazon_v3_uses_lwa_json_and_plain_bearer():
    env = {**AMZ_ENV, "AMAZON_CREDENTIAL_VERSION": "3.2", "AMAZON_MARKETPLACE": "www.amazon.co.uk"}
    sess = FakeSession([Resp(200, {"access_token": "T3"}), Resp(200, {"itemsResult": {"items": [AMZ_ITEM]}})])
    [p] = AmazonCreatorsAdapter(ctx(), session=sess, env=env).fetch("amazon:asin:b0testasin")
    assert sess.calls[0][0] == "https://api.amazon.co.uk/auth/o2/token"
    assert sess.calls[0][1]["json"]["scope"] == "creatorsapi::default"
    url, kw = sess.calls[1]
    assert url.endswith("/catalog/v1/getItems") and kw["json"]["itemIds"] == ["B0TESTASIN"]
    assert kw["headers"]["Authorization"] == "Bearer T3" and kw["headers"]["x-marketplace"] == "www.amazon.co.uk"


def test_amazon_errors_are_logged_not_crashing():
    c = ctx()
    assert AmazonCreatorsAdapter(c, session=FakeSession([]), env={}).fetch("amazon:search:x") == []
    assert c.fetcher.errors[-1].kind == "credentials_missing"
    assert AmazonCreatorsAdapter(c, session=FakeSession([]), env=AMZ_ENV).fetch("amazon:asin:bad!") == []
    assert c.fetcher.errors[-1].kind == "bad_target"
    sess = FakeSession([Resp(200, {"access_token": "T"}), Resp(429, {"message": "TooManyRequests"})])
    assert AmazonCreatorsAdapter(c, session=sess, env=AMZ_ENV).fetch("amazon:search:x") == []
    assert c.fetcher.errors[-1].kind == "http_429"


def test_amazon_prices_not_kept_in_history(tmp_path):
    sess = FakeSession([Resp(200, {"access_token": "T"}), Resp(200, {"searchResult": {"items": [AMZ_ITEM]}})])
    [p] = AmazonCreatorsAdapter(ctx(), session=sess, env=AMZ_ENV).fetch("amazon:search:kettle")
    validate_product(p)
    db = Database(tmp_path / "a.db")
    db.save_products([p])
    assert db.offer_id(p.url) is not None and db.stats()["price_history"] == 0


# ---------------------------------------------------------------- AliExpress
ALI_ENV = {"ALIEXPRESS_APP_KEY": "12345", "ALIEXPRESS_APP_SECRET": "s3cr3t", "ALIEXPRESS_TRACKING_ID": "my_tracking"}
ALI_RESULT = {"aliexpress_affiliate_product_query_response": {"resp_result": {"resp_code": 200, "resp_msg": "ok", "result": {
    "current_record_count": 1, "products": {"product": [{
        "product_id": 1005001234567890, "product_title": "USB C Hub 7 in 1",
        "promotion_link": "https://s.click.aliexpress.com/e/_abc123",
        "product_main_image_url": "https://ae01.alicdn.com/kf/x.jpg",
        "target_sale_price": "12.49", "target_sale_price_currency": "USD",
        "target_original_price": "24.98", "target_original_price_currency": "USD",
        "commission_rate": "7.0%", "second_level_category_name": "Computer Accessories"}]}}}}}


def test_aliexpress_sign_md5_and_sha256():
    params = {"b": "2", "a": "1"}
    assert sign_request(params, "S") == hashlib.md5(b"Sa1b2S").hexdigest().upper()
    import hmac
    assert sign_request(params, "S", "sha256") == hmac.new(b"S", b"a1b2", hashlib.sha256).hexdigest().upper()


def test_aliexpress_search_protocol_and_mapping():
    sess = FakeSession([Resp(200, ALI_RESULT)])
    [p] = AliExpressAffiliateAdapter(ctx(), session=sess, env=ALI_ENV).fetch("aliexpress:search:usb c hub")
    url, kw = sess.calls[0]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith("https://api-sg.aliexpress.com/sync?")
    assert q["method"] == "aliexpress.affiliate.product.query" and q["app_key"] == "12345" and q["sign_method"] == "md5"
    body = kw["data"]
    assert body["keywords"] == "usb c hub" and body["tracking_id"] == "my_tracking" and body["ship_to_country"] == "IL"
    expected = sign_request({k: v for k, v in q.items() if k != "sign"} | body, "s3cr3t")
    assert q["sign"] == expected
    validate_product(p)
    assert (p.price, p.list_price, p.currency, p.commission_rate) == (12.49, 24.98, "USD", 0.07)
    assert p.affiliate_url == "https://s.click.aliexpress.com/e/_abc123"
    assert p.url == "https://www.aliexpress.com/item/1005001234567890.html" and p.price_verified


def test_aliexpress_api_errors():
    c = ctx()
    sess = FakeSession([Resp(200, {"error_response": {"code": "IncompleteSignature", "msg": "bad sign"}})])
    assert AliExpressAffiliateAdapter(c, session=sess, env=ALI_ENV).fetch("aliexpress:search:x") == []
    assert "IncompleteSignature" in c.fetcher.errors[-1].message
    assert AliExpressAffiliateAdapter(c, session=FakeSession([]), env={}).fetch("aliexpress:search:x") == []
    assert c.fetcher.errors[-1].kind == "credentials_missing"


def test_network_commission_counts_as_verified_for_affiliate_score():
    sess = FakeSession([Resp(200, ALI_RESULT)])
    [ali] = AliExpressAffiliateAdapter(ctx(), session=sess, env=ALI_ENV).fetch("aliexpress:search:hub")
    other = Product(url="https://shop.example/hub", name="USB C Hub 7 in 1", price=20.0, currency="USD",
                    availability="InStock", source="json-ld", source_type="json-ld")
    ali.brand = other.brand = "Ugreen"
    ps = [validate_product(ali), validate_product(other)]
    apply_scores(ps, compare_prices(ps))
    assert ali.deal_status == "ok" and ali.affiliate_status == "ok" and "commission:" in ali.deal_notes


# ---------------------------------------------------------------- Israeli store XML feeds
IL_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<products>
  <product>
    <id>A-100</id><name>קומקום חשמלי Acme 1.7 ליטר</name><link>https://shop.co.il/p/100</link>
    <price>₪ 189.90</price><old_price>249.90</old_price><barcode>4006381333931</barcode>
    <brand>Acme</brand><image>https://shop.co.il/i/100.jpg</image><stock>במלאי</stock>
    <shipping>0</shipping><category>מטבח</category>
  </product>
  <product>
    <id>A-101</id><name>מטחנת קפה</name><link>https://shop.co.il/p/101</link>
    <price>99</price><stock>0</stock>
  </product>
</products>"""


def test_generic_israeli_xml_feed_autodetect():
    a, b = parse_generic_xml_feed(IL_FEED)
    assert (a["name"], a["price"], a["list_price"], a["currency"]) == ("קומקום חשמלי Acme 1.7 ליטר", 189.9, 249.9, "ILS")
    assert (a["gtin"], a["availability"], a["shipping_cost"], a["category"]) == ("4006381333931", "במלאי", 0.0, "מטבח")
    assert b["availability"] == "OutOfStock" and "currency" not in b


def test_custom_xml_mapping_with_attributes():
    xml = '<catalog><it sku="7"><t>Mixer</t><p cur="ILS">350</p><u>https://x.co.il/7</u><pic src="https://x.co.il/7.jpg"/></it></catalog>'
    [r] = parse_generic_xml_feed(xml, {"item": "it", "fields": {"name": "t", "price": "p", "url": "u",
                                                                  "image": "pic@src", "sku": "@sku", "currency": "p@cur"}})
    assert (r["name"], r["price"], r["currency"], r["image"], r["sku"]) == ("Mixer", 350.0, "ILS", "https://x.co.il/7.jpg", "7")


def test_israeli_feed_via_cli_with_store_currency(tmp_path):
    feed = tmp_path / "shop.xml"
    feed.write_text(IL_FEED.replace("₪ 189.90", "189.90"), encoding="utf-8")
    sources = {"stores": [{"domain": "shop.co.il", "name": "חנות לדוגמה", "type": "feed", "feed_url": str(feed),
                           "currency": "ILS", "trust_score": 0.9}]}
    (tmp_path / "sources.json").write_text(json.dumps(sources, ensure_ascii=False), encoding="utf-8")
    rc = main(["--feed", str(feed), "--sources", str(tmp_path / "sources.json"), "-o", str(tmp_path / "o"),
               "--name", "il", "--formats", "csv,json", "--cache-dir", "", "--db", str(tmp_path / "il.db")])
    assert rc == 0
    data = json.loads((tmp_path / "o" / "il.json").read_text(encoding="utf-8"))
    kettle = next(d for d in data if d["sku"] == "A-100")
    assert kettle["currency"] == "ILS" and kettle["store"] == "חנות לדוגמה" and kettle["price_verified"]
    assert kettle["in_stock"] is True
    assert "חנות לדוגמה" in (tmp_path / "o" / "il.csv").read_text(encoding="utf-8-sig")

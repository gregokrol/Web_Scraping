from affiliate_scraper.extractors import extract_products
from affiliate_scraper.utils import parse_price, strip_tracking

JSON_LD_PAGE = """
<html><head>
<link rel="canonical" href="https://shop.example/products/mug?utm_source=x&color=blue">
<meta property="og:image" content="/img/og.jpg">
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"BreadcrumbList","itemListElement":[]},
  {"@type":"Product","name":"Ceramic Mug  350ml","brand":{"@type":"Brand","name":"MugCo"},
   "sku":"MUG-1","gtin13":"1234567890123","image":["/img/mug.jpg"],
   "description":"A very nice mug.",
   "aggregateRating":{"@type":"AggregateRating","ratingValue":"9","bestRating":"10","reviewCount":"1,204"},
   "offers":{"@type":"Offer","price":"19.90","priceCurrency":"USD",
             "availability":"https://schema.org/InStock",
             "priceSpecification":[{"@type":"UnitPriceSpecification","priceType":"https://schema.org/ListPrice","price":"29.90"}]}
  }
]}
</script></head><body><h1>Ignored</h1></body></html>
"""

MICRODATA_PAGE = """
<html><body>
<div itemscope itemtype="https://schema.org/Product">
  <h1 itemprop="name">Trail Shoe</h1>
  <img itemprop="image" src="shoe.jpg">
  <div itemprop="aggregateRating" itemscope itemtype="https://schema.org/AggregateRating">
     <span itemprop="ratingValue">4.4</span> (<span itemprop="reviewCount">87</span>)
  </div>
  <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
     <meta itemprop="priceCurrency" content="EUR"><span itemprop="price" content="89.00">89,00 €</span>
     <link itemprop="availability" href="https://schema.org/OutOfStock">
  </div>
</div></body></html>
"""

OG_PAGE = """
<html><head>
<meta property="og:type" content="product">
<meta property="og:title" content="כוס קפה">
<meta property="og:image" content="https://cdn.example/c.jpg">
<meta property="product:price:amount" content="49.90">
<meta property="product:price:currency" content="ILS">
</head><body></body></html>
"""

BLOG_PAGE = "<html><head><title>My blog</title></head><body><h1>Hello</h1></body></html>"

LISTING_PAGE = """
<script type="application/ld+json">[
 {"@type":"Product","name":"A","url":"/products/a","offers":{"@type":"AggregateOffer","lowPrice":"5","highPrice":"9","priceCurrency":"USD"}},
 {"@type":"Product","name":"B","url":"/products/b","offers":[{"price":12,"priceCurrency":"USD"},{"price":10,"priceCurrency":"USD"}]}
]</script>
"""


def test_json_ld_product():
    [p] = extract_products(JSON_LD_PAGE, "https://shop.example/products/mug?fbclid=abc")
    assert p.url == "https://shop.example/products/mug?color=blue"  # canonical, tracking stripped
    assert p.name == "Ceramic Mug 350ml"
    assert p.brand == "MugCo"
    assert p.sku == "MUG-1" and p.gtin == "1234567890123"
    assert p.price == 19.9 and p.list_price == 29.9 and p.currency == "USD"
    assert p.discount_pct == 33.4
    assert p.rating == 4.5  # 9/10 normalised to /5
    assert p.review_count == 1204
    assert p.image == "https://shop.example/img/mug.jpg"
    assert p.in_stock is True
    assert p.source.startswith("json-ld")
    assert p.score > 60


def test_microdata_product():
    [p] = extract_products(MICRODATA_PAGE, "https://run.example/p/trail")
    assert p.name == "Trail Shoe"
    assert p.price == 89.0 and p.currency == "EUR"
    assert p.rating == 4.4 and p.review_count == 87
    assert p.image == "https://run.example/p/shoe.jpg"
    assert p.in_stock is False


def test_opengraph_only_product():
    [p] = extract_products(OG_PAGE, "https://il.example/item/1")
    assert p.name == "כוס קפה"
    assert p.price == 49.9 and p.currency == "ILS"


def test_non_product_page_is_ignored():
    assert extract_products(BLOG_PAGE, "https://blog.example/post") == []


def test_listing_page_returns_each_product_with_own_url():
    products = extract_products(LISTING_PAGE, "https://shop.example/collections/all")
    assert [p.name for p in products] == ["A", "B"]
    assert products[0].url == "https://shop.example/products/a"
    assert products[0].price == 5.0 and products[0].list_price is None  # highPrice is not a list price
    assert products[1].price == 10.0  # cheapest offer


def test_parse_price_formats():
    assert parse_price("1,299.90") == 1299.9
    assert parse_price("1.299,90 €") == 1299.9
    assert parse_price("₪ 49") == 49.0
    assert parse_price("12,99") == 12.99
    assert parse_price("1,299") == 1299.0
    assert parse_price("free") is None
    assert parse_price(15) == 15.0


def test_strip_tracking_removes_foreign_affiliate_tags():
    url = "https://www.amazon.com/dp/B0001?tag=someone-20&th=1&utm_medium=x#reviews"
    assert strip_tracking(url) == "https://www.amazon.com/dp/B0001?th=1"

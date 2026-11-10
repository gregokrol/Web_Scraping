"""Product search: the stores' own search pages -> result cards -> prices compared across stores."""

import json
import time

import pytest

from affiliate_scraper.cli import main
from affiliate_scraper.commands import clean_query, relevance, search_jobs
from affiliate_scraper.db import Database
from affiliate_scraper.discovery import from_listing_pages
from affiliate_scraper.fetcher import PoliteFetcher
from affiliate_scraper.sources.base import StoreConfig

from .conftest import product_html

GTIN = "4005556167555"


# ---------------------------------------------------------------- discovery
def test_link_selector_keeps_only_result_cards_in_store_order(local_site):
    local_site.add("/search?q=x", """
        <nav><a href="/brand/apple/menu-item">menu</a></nav>
        <div class="card"><a class="pi" href="/brand/b/second">2</a></div>
        <div class="card"><a class="pi" href="/brand/a/first">1</a><a href="/brand/a/first">again</a></div>
        <aside><a href="/brand/c/recommended">ad</a></aside>""")
    f = PoliteFetcher(min_delay=0, allow_private=True, cache_dir="")
    found = from_listing_pages(f, [local_site.base + "/search?q=x"], include=".", link_selector="a.pi")
    assert [u.rsplit("/", 1)[1] for u in found] == ["second", "first"]


def test_result_links_lose_click_tracking(local_site):
    local_site.add("/search?q=y", """
        <div class="card__heading"><a href="/products/a?_pos=1&_sid=ab12&_ss=r">A</a></div>
        <div class="card__heading"><a href="/products/a?_pos=1&_sid=ab12&_ss=r?variant=7">A, one variant</a></div>
        <div class="card__heading"><a href="/catalog.php?id=5&utm_source=x">B</a></div>""")
    f = PoliteFetcher(min_delay=0, allow_private=True, cache_dir="")
    found = from_listing_pages(f, [local_site.base + "/search?q=y"], include=".", link_selector=".card__heading a")
    assert [u.split(local_site.base)[1] for u in found] == ["/products/a", "/catalog.php?id=5"]


def test_search_that_redirects_to_the_only_product(local_site):
    local_site.add("/search?q=one", "", status=302, headers={"Location": "/product/only/"})
    local_site.add("/product/only/", product_html("Only one", 10, gtin=GTIN))
    f = PoliteFetcher(min_delay=0, allow_private=True, cache_dir="")
    assert from_listing_pages(f, [local_site.base + "/search?q=one"], include="/product/", max_listing_pages=1) \
        == [local_site.base + "/product/only/"]


def test_bad_selector_is_refused():
    with pytest.raises(ValueError):
        from_listing_pages(PoliteFetcher(min_delay=0, cache_dir=""), [], link_selector="a[")


# ---------------------------------------------------------------- config
@pytest.mark.parametrize("bad", [
    {"search_url": "https://s.example/search"},              # no {q}
    {"search_url": "ftp://s.example/?q={q}"},
    {"search_url": "https://s.example/?q={q}&again={q}"},
    {"search_url": "https://s.example/?q={q}", "search_include": "(("},
    {"search_url": "https://s.example/?q={q}", "search_selector": "a["},
])
def test_store_search_settings_are_validated(bad):
    with pytest.raises(ValueError):
        StoreConfig.from_dict({"domain": "s.example", **bad})


def test_results_ranked_by_search_words():
    assert relevance("airpods 4", "אוזניות אלחוטיות", "https://www.bug.co.il/brand/apple/airpods/4/anc") == 1.0
    assert relevance("airpods", "AirPods Max 2 - Starlight") == 1.0
    assert relevance("airpods", "נעלי סניקרס AIR MAX FIRE - ילדות", "https://m.example/products/air-max") == 0.0
    assert relevance("לגו טכניק", "לגו טכניק מפלסת שלג 42148") == 1.0
    assert relevance("לגו טכניק", "בלוקס טכניקס רובוט סיור") == 0.5
    assert relevance("air pods", "AirPods 5") == 1.0                       # words written apart / together


def test_generic_structured_name_gives_way_to_the_heading():
    from affiliate_scraper.extractors import extract_products
    page = product_html("אוזניות אלחוטיות", 439, "ILS").replace(
        "</head>", "</head><body><h1>אפל - Apple  אוזניות אלחוטיות AirPods 4</h1></body>")
    [p] = extract_products(page, "https://shop.example/brand/apple/airpods/4/")
    assert p.name == "אפל - Apple אוזניות אלחוטיות AirPods 4"
    long_name = product_html("פאזל 1000 חלקים - אחוזת טרקוטה", 168.9, "ILS").replace(
        "</head>", "</head><body><h1>Ravensburger פאזל 1000 חלקים - אחוזת טרקוטה</h1></body>")
    assert extract_products(long_name, "https://shop.example/p")[0].name == "פאזל 1000 חלקים - אחוזת טרקוטה"
    other_h1 = product_html("Kettle", 90).replace("</head>", "</head><body><h1>Welcome to our shop</h1></body>")
    assert extract_products(other_h1, "https://shop.example/k")[0].name == "Kettle"


def test_query_is_cleaned():
    assert clean_query("  לגו   42115 \n") == "לגו 42115"
    for bad in ("x", "", "a" * 101, "ab\x00cd"):
        with pytest.raises(ValueError):
            clean_query(bad)


def test_search_jobs_one_per_searchable_store(tmp_path):
    (tmp_path / "sources.json").write_text(json.dumps({"stores": [
        {"domain": "a.example", "search_url": "https://a.example/search?q={q}", "search_selector": "a.card",
         "include": "-\\d{13}$"},
        {"domain": "b.example", "search_url": "https://b.example/?s={q}&post_type=product", "include": "/product/"},
        {"domain": "c.example", "start_urls": ["https://c.example/"]},                       # no search_url
        {"domain": "d.example", "search_url": "https://d.example/?q={q}", "enabled": False},
    ]}), encoding="utf-8")
    jobs = search_jobs({"sources_config": str(tmp_path / "sources.json"), "delay": 3}, "לגו 42115", per_store=7)
    assert [d for d, _ in jobs] == ["a.example", "b.example"]
    a, b = jobs[0][1], jobs[1][1]
    assert a[0] == "https://a.example/search?q=%D7%9C%D7%92%D7%95+42115"
    assert "--link-selector=a.card" in a and "--include=." in a          # the cards decide, not the category regex
    assert "--include=/product/" in b and not any(x.startswith("--link-selector") for x in b)
    assert a[a.index("--max-listing-pages") + 1] == "1"
    assert a[len(a) - 1 - a[::-1].index("--max-pages") + 1] == "7"         # the last --max-pages wins


# ---------------------------------------------------------------- end to end
@pytest.fixture()
def two_stores(local_site, tmp_path):
    """Two 'stores' on one local server: 127.0.0.1 and localhost, each with its own search page."""
    port = local_site.base.rsplit(":", 1)[1]
    local_site.add("/a/search?q=puzzle+1000", '<a class="hit" href="/a/p/1">1</a><a class="hit" href="/a/p/2">2</a>'
                                              '<a href="/a/p/ad">ad</a>')
    local_site.add("/a/p/1", product_html("Puzzle 1000 Night", 120, "ILS", gtin=GTIN))
    local_site.add("/a/p/2", product_html("Puzzle 1000 Day", 80, "ILS"))
    local_site.add("/b/?s=puzzle+1000", '<ul><li><a href="/b/product/night/">Night</a></li></ul>')
    local_site.add("/b/product/night/", product_html("Night puzzle 1000 pieces", 99, "ILS", gtin=GTIN))
    (tmp_path / "sources.json").write_text(json.dumps({"stores": [
        {"domain": "127.0.0.1", "name": "Store A", "search_url": local_site.base + "/a/search?q={q}",
         "search_selector": "a.hit"},
        {"domain": "localhost", "name": "Store B", "search_url": f"http://localhost:{port}/b/?s={{q}}",
         "search_include": "/product/"},
    ]}), encoding="utf-8")
    cfg = {"sources_config": str(tmp_path / "sources.json"), "db": str(tmp_path / "d.db"), "delay": 0,
           "allow_private_hosts": True, "out_dir": str(tmp_path / "out")}
    (tmp_path / "automation.json").write_text(json.dumps(cfg), encoding="utf-8")
    return tmp_path, local_site


def test_search_command_compares_the_stores(two_stores, capsys):
    tmp, site = two_stores
    assert main(["search", "puzzle", "1000", "--config", str(tmp / "automation.json")]) == 0
    assert site.count("/a/p/ad") == 0                                        # not a result card
    db = Database(str(tmp / "d.db"))
    rows = {r["url"].split("/", 3)[3]: r for r in db.query("SELECT url, price, group_id FROM offers")}
    db.close()
    assert set(rows) == {"a/p/1", "a/p/2", "b/product/night/"}
    assert rows["a/p/1"]["group_id"] and rows["a/p/1"]["group_id"] == rows["b/product/night/"]["group_id"]
    assert "Search 'puzzle 1000': 3 product(s) in 2 of 2 store(s)." in capsys.readouterr().err


def test_search_with_no_searchable_store_says_so(tmp_path, capsys):
    (tmp_path / "automation.json").write_text("{}")
    assert main(["search", "lego", "--config", str(tmp_path / "automation.json")]) == 2
    assert "search_url" in capsys.readouterr().err


def test_search_box_in_the_app(two_stores):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from affiliate_scraper.dashboard.app import create_app
    tmp, _ = two_stores
    client = TestClient(create_app(str(tmp / "d.db"), automation_config=str(tmp / "automation.json"),
                                   allowed_hosts=["testserver"], token="t"))
    h = {"X-Dashboard-Token": "t"}
    assert client.post("/api/search", json={"q": "x"}, headers=h).status_code == 400
    job = client.post("/api/search", json={"q": "puzzle 1000"}, headers=h).json()
    assert job["kind"] == "search" and [s["name"] for s in job["stores"]] == ["Store A", "Store B"]
    for _ in range(300):
        if client.get(f"/api/jobs/{job['id']}").json()["status"] != "running":
            break
        time.sleep(0.02)
    j = client.get(f"/api/jobs/{job['id']}").json()
    assert (j["fetched"], j["targets"], j["stores_done"]) == (3, 3, 2)     # both stores, searched side by side
    r = client.get(f"/api/search/{job['id']}").json()
    assert r["status"] == "done" and r["q"] == "puzzle 1000"
    [g] = r["groups"]
    assert [o["price"] for o in g["offers"]] == [99.0, 120.0] and [o["domain"] for o in g["offers"]] == ["localhost", "127.0.0.1"]
    assert [o["title"] for o in r["offers"]] == ["Puzzle 1000 Day"]           # found in one store only
    assert {s["name"]: s["found"] for s in r["stores"]} == {"Store A": 2, "Store B": 1}
    assert client.get("/api/summary").json()["store_names"] == {"127.0.0.1": "Store A", "localhost": "Store B"}
    assert client.get(f"/api/search/{job['id']}x").status_code == 404


# ---------------------------------------------------------------- only stores with an affiliate program
@pytest.mark.parametrize("bad", [
    {"status": "yes"},
    {"status": "confirmed", "url": "javascript:alert(1)"},
    {"status": "signs", "checked": "9/11/2026"},
    "confirmed",
])
def test_affiliate_facts_are_validated(bad):
    with pytest.raises(ValueError):
        StoreConfig.from_dict({"domain": "s.example", "affiliate": bad})


def test_affiliate_status_decides():
    has = lambda a: StoreConfig.from_dict({"domain": "s.example", "affiliate": a}).has_affiliate
    assert has({"status": "confirmed", "url": "https://s.example/pages/affiliates"}) and has({"status": "signs"})
    assert not has({"status": "not_found"}) and not has({})


def test_search_jobs_affiliate_only(tmp_path):
    (tmp_path / "sources.json").write_text(json.dumps({"stores": [
        {"domain": "a.example", "search_url": "https://a.example/?q={q}", "affiliate": {"status": "confirmed"}},
        {"domain": "b.example", "search_url": "https://b.example/?q={q}", "affiliate": {"status": "not_found"}},
        {"domain": "c.example", "search_url": "https://c.example/?q={q}"},
        {"domain": "d.example", "search_url": "https://d.example/?q={q}", "affiliate": {"status": "signs"}},
        {"domain": "e.example", "search_url": "https://e.example/?q={q}"},
    ]}), encoding="utf-8")
    db = Database(str(tmp_path / "d.db"))
    db.upsert_program("www.e.example", network="in-house", params_json='{"ref": "me"}')   # you set one up yourself
    db.close()
    cfg = {"sources_config": str(tmp_path / "sources.json"), "db": str(tmp_path / "d.db")}
    assert [d for d, _ in search_jobs(cfg, "lego")] == ["a.example", "b.example", "c.example", "d.example", "e.example"]
    assert [d for d, _ in search_jobs(cfg, "lego", affiliate_only=True)] == ["a.example", "d.example", "e.example"]


def test_affiliate_only_search_with_no_such_store_says_so(two_stores, capsys):
    tmp, _ = two_stores
    assert main(["search", "puzzle", "--affiliate-only", "--config", str(tmp / "automation.json")]) == 2
    assert "affiliate program" in capsys.readouterr().err


def _run(client, job_id, timeout=60.0):
    """Wait for a background job. Generous timeout: the job crawls politely (delays), slower machines need longer."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get(f"/api/jobs/{job_id}").json()["status"] != "running":
            return
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still running after {timeout}s")


def test_affiliate_filter_in_the_app(two_stores):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from affiliate_scraper.dashboard.app import create_app
    tmp, site = two_stores
    client = TestClient(create_app(str(tmp / "d.db"), automation_config=str(tmp / "automation.json"),
                                   allowed_hosts=["testserver"], token="t"))
    h = {"X-Dashboard-Token": "t"}
    r = client.post("/api/search", json={"q": "puzzle 1000", "affiliate_only": True}, headers=h)
    assert r.status_code == 400 and "no_affiliate_store" in r.json()["detail"]     # nothing known yet

    _run(client, client.post("/api/search", json={"q": "puzzle 1000"}, headers=h).json()["id"])  # both stores
    assert len(client.get("/api/comparisons").json()) == 1
    assert client.get("/api/offers?affiliate_only=true").json() == []
    assert client.get("/api/comparisons?affiliate_only=true").json() == []

    src = json.loads((tmp / "sources.json").read_text(encoding="utf-8"))           # Store A publishes a program
    src["stores"][0]["affiliate"] = {"status": "confirmed", "url": "https://a.example/affiliates", "checked": "2026-11-09"}
    (tmp / "sources.json").write_text(json.dumps(src), encoding="utf-8")
    assert client.get("/api/summary").json()["affiliate_domains"] == ["127.0.0.1"]
    rows = client.get("/api/offers?affiliate_only=true").json()
    assert len(rows) == 2 and {o["domain"] for o in rows} == {"127.0.0.1"}
    assert client.get("/api/comparisons?affiliate_only=true").json() == []         # one affiliate store: nothing to compare

    job = client.post("/api/search", json={"q": "puzzle 1000", "affiliate_only": True}, headers=h).json()
    assert [s["name"] for s in job["stores"]] == ["Store A"]
    _run(client, job["id"])
    log = "\n".join(client.get(f"/api/jobs/{job['id']}").json()["log"])
    assert "=== scrape: " + site.base + "/a/search" in log and "/b/?s=" not in log      # Store B not searched
    res = client.get(f"/api/search/{job['id']}").json()
    assert res["affiliate_only"] and [s["name"] for s in res["stores"]] == ["Store A"]
    assert res["groups"] == []                       # Store B's earlier price is not an affiliate offer
    assert {o["domain"] for o in res["offers"]} == {"127.0.0.1"}

    client.post("/api/programs", json={"domain": "localhost", "params": {"ref": "me"}}, headers=h)  # you join B
    assert len(client.get("/api/comparisons?affiliate_only=true").json()) == 1
    known = {k["domain"]: k for k in client.get("/api/affiliate-stores").json()}
    assert known["127.0.0.1"]["status"] == "confirmed" and not known["127.0.0.1"]["joined"]
    assert known["localhost"]["joined"] and known["localhost"]["has_program"] and known["localhost"]["in_sources"]


# ---------------------------------------------------------------- stores that forbid their search page
FRIDGE = "/product/%D7%9E%D7%A7%D7%A8%D7%A8-samsung-rf50/"          # /product/מקרר-samsung-rf50/


@pytest.fixture()
def sitemap_store(local_site):
    """A WooCommerce-like store: robots.txt forbids ?s= but lists a Yoast sitemap index."""
    b = local_site.base
    local_site.add("/robots.txt", f"User-agent: *\nDisallow: /*?s=\nSitemap: {b}/sitemap_index.xml\n",
                   content_type="text/plain")
    maps = ["product-sitemap.xml", "product-sitemap2.xml", "product_cat-sitemap.xml", "pa_color-sitemap.xml"]
    local_site.add("/sitemap_index.xml", '<?xml version="1.0"?><sitemapindex>' +
                   "".join(f"<sitemap><loc>{b}/{m}</loc></sitemap>" for m in maps) + "</sitemapindex>",
                   content_type="application/xml")
    urlset = lambda *paths: ('<?xml version="1.0"?><urlset>' + "".join(f"<url><loc>{b}{p}</loc></url>" for p in paths)
                             + "</urlset>")
    local_site.add("/product-sitemap.xml", urlset("/product/lg-fridge-gr500/", "/product/samsung-tv-55/"),
                   content_type="application/xml")
    local_site.add("/product-sitemap2.xml", urlset(FRIDGE, "/product/kettle/"), content_type="application/xml")
    local_site.add("/product_cat-sitemap.xml", urlset("/product-category/samsung/"), content_type="application/xml")
    local_site.add("/pa_color-sitemap.xml", urlset("/color/white/"), content_type="application/xml")
    local_site.add(FRIDGE, product_html("מקרר Samsung RF50", 4990, "ILS", gtin=GTIN))
    local_site.add("/product/samsung-tv-55/", product_html("Samsung TV 55", 2490, "ILS"))
    return local_site


def test_sitemap_search_ranks_product_urls_by_the_words(sitemap_store):
    from affiliate_scraper.discovery import search_sitemaps
    site = sitemap_store
    f = PoliteFetcher(min_delay=0, allow_private=True, cache_dir="")
    found = search_sitemaps(f, site.base + "/", "מקרר samsung", include="/product/", limit=5)
    assert [u.split(site.base)[1] for u in found] == [FRIDGE, "/product/samsung-tv-55/"]   # both words, then one
    assert site.count("/product_cat-sitemap.xml") == 0 and site.count("/pa_color-sitemap.xml") == 0
    assert search_sitemaps(f, site.base + "/", "dyson", include="/product/") == []


@pytest.mark.parametrize("bad", [
    {"search_sitemap": "www.shop.example"},
    {"search_sitemap": "https://s.example/", "search_url": "https://s.example/?s={q}"},
])
def test_search_sitemap_setting_is_validated(bad):
    with pytest.raises(ValueError):
        StoreConfig.from_dict({"domain": "s.example", **bad})


def test_match_needs_sitemap_discovery(capsys):
    assert main(["https://s.example/", "--match", "lego", "--dry-run"]) == 2
    assert "--match" in capsys.readouterr().err


def test_search_reads_a_sitemap_store(sitemap_store, tmp_path, capsys):
    site = sitemap_store
    (tmp_path / "sources.json").write_text(json.dumps({"stores": [
        {"domain": "127.0.0.1", "name": "Sitemap Store", "search_sitemap": site.base + "/", "search_include": "/product/",
         "affiliate": {"status": "confirmed"}}]}), encoding="utf-8")
    cfg = {"sources_config": str(tmp_path / "sources.json"), "db": str(tmp_path / "d.db"), "delay": 0,
           "allow_private_hosts": True, "out_dir": str(tmp_path / "out")}
    [(_, argv)] = search_jobs(cfg, "מקרר samsung", per_store=1)
    assert argv[:4] == [site.base + "/", "--discover", "sitemap", "--match=מקרר samsung"] and "--include=/product/" in argv
    (tmp_path / "automation.json").write_text(json.dumps(cfg), encoding="utf-8")
    assert main(["search", "מקרר", "samsung", "--per-store", "1", "--affiliate-only",
                 "--config", str(tmp_path / "automation.json")]) == 0
    db = Database(str(tmp_path / "d.db"))
    rows = db.query("SELECT title, price FROM offers")
    db.close()
    assert [(r["title"], r["price"]) for r in rows] == [("מקרר Samsung RF50", 4990.0)]   # the best match only
    assert site.count("/product/samsung-tv-55/") == 0
    assert "1 product(s) in 1 of 1 store(s)" in capsys.readouterr().err

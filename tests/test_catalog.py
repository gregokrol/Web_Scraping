"""Browse by store: store -> category -> top 5 cheapest / most popular, and the channel menu."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from affiliate_scraper.catalog import classify, cheapest, collect_catalog, most_popular, store_slug
from affiliate_scraper.cli import main
from affiliate_scraper.db import Database
from affiliate_scraper.models import Product
from affiliate_scraper.site import SiteConfig, build_site
from affiliate_scraper.validation import validate_product


def add(db, url, name, price, reviews=None, rating=None, store="Bug", stock="InStock"):
    p = validate_product(Product(url=url, name=name, price=price, currency="ILS", availability=stock, source="json-ld",
                                 source_type="json-ld", store=store, review_count=reviews, rating=rating))
    db.save_products([p])


def test_classify_into_the_niche():
    assert classify("קונסולת PlayStation 5 Slim") == "gaming"
    assert classify("כיסא גיימינג Razer") == "gaming"                    # gaming wins over "כיסא" (home)
    assert classify("מחשב נייד Lenovo IdeaPad 15") == "computers"
    assert classify("אוזניות JBL TUNE 520BT") == "mobile"
    assert classify("שואב אבק רובוטי Roborock Q7") == "appliances"
    assert classify("מקדחה רוטטת Makita 18V") == "home"
    assert classify("רובוט דינוזאור לילדים") is None                     # a toy is not an appliance
    assert classify("קרם לחות") is None


def test_catalog_lists_only_fresh_verified_in_stock_allowed(tmp_path):
    db = Database(tmp_path / "c.db")
    add(db, "https://bug.example/a", "אוזניות JBL TUNE 520BT", 199)
    add(db, "https://bug.example/b", "אוזניות Sony WH-1000XM5", 1299, stock="OutOfStock")    # not in stock
    add(db, "https://bug.example/c", "אוזניות Acme 18+ edition", 99)                           # forbidden word
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    add(db, "https://bug.example/d", "אוזניות Philips TAH60", 250)
    db.conn.execute("UPDATE offers SET last_seen=? WHERE url=?", (old, "https://bug.example/d"))  # stale
    db.conn.commit()
    cat = collect_catalog(db)
    [only] = cat["bug.example"]["cats"]["mobile"]
    assert only["title"] == "אוזניות JBL TUNE 520BT" and cat["bug.example"]["name"] == "Bug"


def test_top_lists():
    ps = [{"title": f"p{i}", "price": 100 - i, "review_count": rc, "rating": 4.5} for i, rc in
          enumerate([0, 12, 50, 3, 7, 0, 30])]
    assert [p["price"] for p in cheapest(ps)] == [94, 95, 96, 97, 98]
    assert [p["review_count"] for p in most_popular(ps)] == [50, 30, 12, 7, 3]
    assert most_popular(ps[:3]) == []                                      # too few reviewed: no guessing


def test_store_pages_and_navigation(tmp_path):
    db = Database(tmp_path / "c.db")
    for i in range(7):
        add(db, f"https://bug.example/g{i}", f"בקר אלחוטי DualSense צבע {i}", 200 + i * 10, reviews=10 * i or None,
            rating=4.0 + i / 10)
    add(db, "https://ivory.example/l", "מחשב נייד Acer Aspire 5", 2499, store="Ivory")
    cfg = SiteConfig(base_url="https://u.github.io/s/", out_dir=str(tmp_path / "site"))
    build_site(db, cfg)
    out = Path(cfg.out_dir)
    index = (out / "stores" / "index.html").read_text(encoding="utf-8")
    assert 'href="bug-example.html"' in index and "Ivory" in index
    store = (out / "stores" / "bug-example.html").read_text(encoding="utf-8")
    assert 'href="bug-example/gaming.html"' in store
    page = (out / "stores" / "bug-example" / "gaming.html").read_text(encoding="utf-8")
    assert page.count('class="prow"') == 10                               # 5 cheapest + 5 most popular
    assert "₪200.00" in page and 'rel="sponsored nofollow noopener"' in page
    assert 'href="../../index.html"' in page                               # back to the home page
    assert "stores/bug-example/gaming.html" in (out / "sitemap.xml").read_text(encoding="utf-8")
    laptops = (out / "stores" / "ivory-example" / "computers.html").read_text(encoding="utf-8")
    assert "אין כאן דירוג פופולריות" in laptops                            # no reviews -> said, not guessed
    assert store_slug("www.Bug.co.il") == "bug-co-il"


def test_channel_menu_preview_goes_to_you(tmp_path, local_site, monkeypatch, capsys):
    import affiliate_scraper.telegram as tgmod
    monkeypatch.chdir(tmp_path)
    db = Database(tmp_path / "c.db")
    add(db, "https://bug.example/a", "אוזניות JBL TUNE 520BT", 199)
    db.close()
    (tmp_path / "automation.json").write_text(json.dumps({"db": str(tmp_path / "c.db"),
                                                          "site": {"base_url": "https://u.github.io/s/"}}),
                                              encoding="utf-8")
    monkeypatch.setattr(tgmod, "API", local_site.base)
    monkeypatch.setattr(tgmod, "MIN_INTERVAL", 0)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@deals_il")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT", "42")
    local_site.post_reply = b'{"ok": true, "result": {"message_id": 5}}'
    assert main(["channel-menu", "--no-check"]) == 0
    [sent] = [json.loads(p["body"]) for p in local_site.posts]
    assert sent["chat_id"] == "42"                                        # preview: never the channel
    urls = [b["url"] for row in sent["reply_markup"]["inline_keyboard"] for b in row]
    assert "https://u.github.io/s/stores/bug-example.html?utm_source=telegram&utm_medium=pinned&utm_campaign=store-menu" in urls
    local_site.posts.clear()
    assert main(["channel-menu", "--publish", "--no-check"]) == 0
    methods = [p["path"].rsplit("/", 1)[-1] for p in local_site.posts]
    assert methods == ["sendMessage", "pinChatMessage"]
    assert json.loads(local_site.posts[0]["body"])["chat_id"] == "@deals_il"


def test_404_page_works_at_any_depth(tmp_path):
    db = Database(tmp_path / "c.db")
    cfg = SiteConfig(base_url="https://u.github.io/s/", out_dir=str(tmp_path / "site"))
    build_site(db, cfg)
    page = (Path(cfg.out_dir) / "404.html").read_text(encoding="utf-8")
    assert 'href="https://u.github.io/s/style.css"' in page            # not "style.css" (breaks in stores/x/)
    assert 'href="https://u.github.io/s/stores/index.html"' in page


def test_channel_menu_refuses_buttons_to_missing_pages(tmp_path, monkeypatch, capsys):
    import requests
    monkeypatch.chdir(tmp_path)
    db = Database(tmp_path / "c.db")
    add(db, "https://bug.example/a", "אוזניות JBL TUNE 520BT", 199)
    db.close()
    (tmp_path / "automation.json").write_text(json.dumps({"db": str(tmp_path / "c.db"),
                                                          "site": {"base_url": "https://u.github.io/s/"}}),
                                              encoding="utf-8")
    monkeypatch.setattr(requests, "get", lambda url, timeout=0: type("R", (), {"status_code": 404})())
    assert main(["channel-menu"]) == 1
    assert "not on the website yet" in capsys.readouterr().err


def test_disabled_stores_are_left_out_and_your_store_names_are_used(tmp_path):
    db = Database(tmp_path / "c.db")
    add(db, "https://www.bug.example/a", "אוזניות JBL TUNE 520BT", 199, store="BUG מחשבים בע\"מ")
    add(db, "https://www.sport.example/b", "שעון חכם Garmin Forerunner 55", 590, store="Sport")
    add(db, "https://toys.example/c", "אוזניות Acme TWS-5", 99,
        store="משחקים לילדים לגור, פליימוביל, מליסה וכל המותגים המובילים במקום אחד")
    settings = {"bug.example": {"name": "באג", "enabled": True},
                "sport.example": {"name": "מגה ספורט", "enabled": False}}
    cat = collect_catalog(db, settings=settings)
    assert set(cat) == {"bug.example", "toys.example"}                    # the disabled store is out
    assert cat["bug.example"]["name"] == "באג"                          # your name, not the shop's title
    assert cat["toys.example"]["name"] == "toys.example"                    # a whole sentence -> the address


def test_store_settings_read_from_sources_json(tmp_path, monkeypatch):
    from affiliate_scraper.catalog import store_settings
    monkeypatch.chdir(tmp_path)
    assert store_settings() == {}
    (tmp_path / "sources.json").write_text(json.dumps({"stores": [
        {"domain": "www.Bug.co.il", "name": "באג"}, {"domain": "fox.co.il", "name": "פוקס", "enabled": False}]}),
        encoding="utf-8")
    assert store_settings() == {"bug.co.il": {"name": "באג", "enabled": True},
                                "fox.co.il": {"name": "פוקס", "enabled": False}}


def test_search_page_and_index(tmp_path):
    db = Database(tmp_path / "c.db")
    add(db, "https://bug.example/a", "אוזניות JBL TUNE 520BT", 199)
    add(db, "https://bug.example/b", "קרם לחות לפנים", 49)                 # any category is searchable
    add(db, "https://bug.example/c", "מארז וייפ Acme", 99)                 # a forbidden product never is
    cfg = SiteConfig(base_url="https://u.github.io/s/", out_dir=str(tmp_path / "site"))
    build_site(db, cfg)
    out = Path(cfg.out_dir)
    index = json.loads((out / "search.json").read_text(encoding="utf-8"))
    titles = {i["t"] for i in index}
    assert titles == {"אוזניות JBL TUNE 520BT", "קרם לחות לפנים"}
    jbl = next(i for i in index if "JBL" in i["t"])
    assert jbl["p"] == 199.0 and jbl["h"] == "https://bug.example/a" and jbl["c"] == 0
    page = (out / "search.html").read_text(encoding="utf-8")
    assert 'id="sq"' in page and "search.json" in page and "textContent" in page   # built with DOM, not innerHTML
    home = (out / "index.html").read_text(encoding="utf-8")
    assert 'action="search.html"' in home                                   # the search box on every page
    deep = (out / "stores" / "bug-example" / "mobile.html").read_text(encoding="utf-8")
    assert 'action="../../search.html"' in deep


def test_your_store_names_also_on_the_deal_cards(tmp_path, monkeypatch):
    from affiliate_scraper.alerts import DealRules, decide, detect_deals
    from affiliate_scraper.site import collect_deals

    from .test_p2_automation import seed_db
    monkeypatch.chdir(tmp_path)
    db, oid = seed_db(tmp_path)
    db.conn.execute("UPDATE offers SET store=? WHERE id=?",
                    ("משחקים לילדים וצעצועים זה אמיגו. לגו, פליימוביל, מליסה וכל המותגים", oid))
    db.conn.commit()
    [a] = detect_deals(db, DealRules())
    decide(db, [a["id"]], True)
    dom = db.query("SELECT domain FROM offers WHERE id=?", (oid,))[0]["domain"]
    (tmp_path / "sources.json").write_text(json.dumps({"stores": [{"domain": dom, "name": "אמיגו"}]}), encoding="utf-8")
    [d] = collect_deals(db, SiteConfig(out_dir=str(tmp_path / "s")))
    assert d["store"] == "אמיגו"

"""P1: SQLite storage, price history, misleading-discount detection, Deal Score, Affiliate Score."""

import json
from datetime import datetime, timedelta, timezone

from affiliate_scraper.cli import main
from affiliate_scraper.db import Database
from affiliate_scraper.history import (check_discount_claim, compute_stats, detect_price_drop, is_lowest_in_90d,
                                       stats_for_offer)
from affiliate_scraper.matching import compare_prices
from affiliate_scraper.models import Product
from affiliate_scraper.scoring import affiliate_score, deal_score
from affiliate_scraper.validation import validate_product

from .conftest import product_html

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def prod(price=100.0, **kw):
    kw.setdefault("url", "https://a.example/p")
    kw.setdefault("name", "Acme Kettle")
    kw.setdefault("currency", "ILS")
    kw.setdefault("availability", "InStock")
    return validate_product(Product(price=price, source="json-ld", source_type="json-ld", **kw), now=NOW)


# ---------------------------------------------------------------- database
def test_schema_has_required_tables(tmp_path):
    db = Database(tmp_path / "t.db")
    tables = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"products", "offers", "price_history", "sources", "crawl_runs", "affiliate_programs"} <= tables


def test_history_appends_and_ignores_duplicate_observation(tmp_path):
    db = Database(tmp_path / "t.db")
    p1 = prod(100, fetched_at=ago(2))
    p2 = prod(90, fetched_at=ago(1))
    db.save_products([p1])
    db.save_products([p1])          # same observation again (e.g. served from cache) -> no duplicate
    db.save_products([p2])
    oid = db.offer_id(p1.url)
    rows = db.history(oid, verified_only=False)
    assert [r["price"] for r in rows] == [100, 90]
    assert [r["price_verified"] for r in rows] == [0, 1]   # 2-day-old price is stale -> stored but unverified
    assert [r["price"] for r in db.history(oid)] == [90]
    assert rows[0]["observed_at"] == p1.fetched_at and rows[0]["source"] == "json-ld"


def test_variants_get_separate_product_identity(tmp_path):
    db = Database(tmp_path / "t.db")
    db.save_products([prod(10, url="https://a.example/1", brand="Acme", model="Z1", name="Acme Z1 Black"),
                      prod(10, url="https://a.example/2", brand="Acme", model="Z1", name="Acme Z1 White")])
    assert db.stats()["products"] == 2


# ---------------------------------------------------------------- history stats
def test_stats_need_enough_data():
    assert compute_stats([(ago(1), 100), (ago(0), 90)], now=NOW).status == "insufficient_data"
    st = compute_stats([(ago(100), 50), (ago(60), 120), (ago(20), 110), (ago(10), 100), (ago(0), 95)], now=NOW)
    assert st.sufficient and st.n_obs == 5
    assert (st.min_30d, st.min_90d, st.median_90d, st.max_90d) == (95, 95, 105.0, 120)  # day-100 point excluded


def test_price_drop_detection():
    st = compute_stats([(ago(30), 100), (ago(20), 100), (ago(10), 100), (ago(0), 80)], now=NOW)
    drop = detect_price_drop(st)
    assert drop and drop["drop_pct"] == 20.0 and drop["lowest_90d"] is True
    assert is_lowest_in_90d(st)
    small = compute_stats([(ago(30), 100), (ago(20), 100), (ago(10), 100), (ago(0), 98)], now=NOW)
    assert detect_price_drop(small) is None


def test_misleading_and_verified_discount_claims():
    flat = compute_stats([(ago(25), 100), (ago(15), 100), (ago(5), 100), (ago(0), 100)], now=NOW)
    assert check_discount_claim(flat, 100, 150)["status"] == "misleading"     # "was 150" but always 100
    real = compute_stats([(ago(25), 150), (ago(15), 150), (ago(5), 150), (ago(0), 100)], now=NOW)
    assert check_discount_claim(real, 100, 150)["status"] == "verified"
    some = compute_stats([(ago(25), 120), (ago(15), 120), (ago(5), 120), (ago(0), 100)], now=NOW)
    assert check_discount_claim(some, 100, 150)["status"] == "overstated"
    thin = compute_stats([(ago(1), 150), (ago(0), 100)], now=NOW)
    assert check_discount_claim(thin, 100, 150)["status"] == "insufficient_data"
    assert check_discount_claim(flat, 100, None)["status"] == "no_claim"


def test_stats_for_offer_uses_only_verified_single_currency(tmp_path):
    db = Database(tmp_path / "t.db")
    p = prod(100, fetched_at=ago(1))
    db.save_products([p])
    oid = db.offer_id(p.url)
    db.conn.executemany("INSERT INTO price_history(offer_id, observed_at, price, currency, price_verified) VALUES (?,?,?,?,?)",
                        [(oid, ago(30), 1, "ILS", 0), (oid, ago(20), 30, "USD", 1), (oid, ago(10), 110, "ILS", 1),
                         (oid, ago(5), 105, "ILS", 1)])
    db.conn.commit()
    st = stats_for_offer(db, oid, now=NOW)
    assert st.n_obs == 3 and st.min_90d == 100  # unverified 1 ILS and the USD row ignored


# ---------------------------------------------------------------- deal score
def market(price_self=80, others=(100, 110), **kw):
    ps = [prod(price_self, url="https://a.example/p", gtin="4006381333931", shipping_cost=0, **kw)]
    ps += [prod(x, url=f"https://s{i}.example/p", gtin="4006381333931", shipping_cost=0) for i, x in enumerate(others)]
    comps = compare_prices(ps)
    return ps[0], comps[0]


def test_deal_score_insufficient_without_market_or_history():
    r = deal_score(prod(100))
    assert r.value is None and r.status == "insufficient_data"
    unverified = prod(100, fetched_at=ago(5))  # stale
    assert deal_score(unverified).reasons == ["price_unverified"]


def test_deal_score_with_market_data_and_missing_components():
    p, comp = market()
    r = deal_score(p, comp)
    assert r.status == "ok" and 0 < r.value <= 100
    assert set(r.components) == {"market", "shipping", "availability"}   # store/warranty/history unknown -> left out
    assert r.confidence == 0.55
    cheaper, comp2 = market(price_self=60)
    assert deal_score(cheaper, comp2).value >= r.value


def test_deal_score_out_of_stock_and_misleading_penalty():
    p, comp = market()
    p.availability = "OutOfStock"
    assert deal_score(p, comp).status == "out_of_stock"
    p2, comp2 = market(price_self=95)
    base = deal_score(p2, comp2).value
    penalised = deal_score(p2, comp2, discount_check={"status": "misleading"})
    assert penalised.value < base and "misleading_discount_claim" in penalised.reasons


# ---------------------------------------------------------------- affiliate score (separate)
def test_affiliate_score_separate_and_only_verified_commission():
    p, comp = market(review_count=500)
    d = deal_score(p, comp, store_trust=0.9)
    verified = affiliate_score(p, d, {"commission_rate": 0.08, "commission_verified": True})
    unverified = affiliate_score(p, d, {"commission_rate": 0.08, "commission_verified": False})
    assert "commission" in verified.components and "commission" not in unverified.components
    assert "commission_not_verified" in unverified.reasons
    assert verified.value != d.value  # two different scores


def test_affiliate_score_insufficient_and_conversion_threshold():
    p, comp = market()
    d = deal_score(p, comp)
    assert affiliate_score(p, d).status == "insufficient_data"                        # only deal data
    few = affiliate_score(p, d, performance={"clicks": 10, "conversions": 5, "level": "url"})
    assert "conversion" not in few.components
    many = affiliate_score(p, d, performance={"clicks": 200, "conversions": 6, "level": "url"})
    assert many.components["conversion"]["points"] > 0
    aud = affiliate_score(p, d, audience={"keywords": ["kettle"]})
    assert aud.components["audience"]["points"] == 10
    assert affiliate_score(prod(1), deal_score(prod(1))).status == "insufficient_data"


# ---------------------------------------------------------------- CLI end-to-end with history
def _scrape(local_site, tmp_path, *urls, extra=()):
    return main([*urls, "--delay", "0", "--allow-private-hosts", "--no-cache", "--db", str(tmp_path / "h.db"),
                 "-o", str(tmp_path / "out"), "--name", "r", "--formats", "json", *extra])


def test_cli_history_scores_and_commands(local_site, tmp_path, capsys):
    extra = {"offers": {"price": "90", "priceCurrency": "ILS", "availability": "InStock",
                        "priceSpecification": {"priceType": "https://schema.org/ListPrice", "price": "150"},
                        "shippingDetails": {"shippingRate": {"value": 0}}}}
    local_site.add("/products/k", product_html("Acme Kettle", "90", currency="ILS", gtin="4006381333931",
                                               extra=extra))
    url = local_site.base + "/products/k"
    assert _scrape(local_site, tmp_path, url) == 0

    db = Database(tmp_path / "h.db")
    oid = db.offer_id(url)
    now = datetime.now(timezone.utc)
    db.conn.executemany("INSERT INTO price_history(offer_id, observed_at, price, currency, price_verified) VALUES (?,?,?,?,1)",
                        [(oid, (now - timedelta(days=d)).isoformat(), 120, "ILS") for d in (40, 25, 15, 8)])
    db.conn.commit()
    db.close()

    assert _scrape(local_site, tmp_path, url) == 0
    [row] = json.loads((tmp_path / "out" / "r.json").read_text(encoding="utf-8"))
    assert row["deal_score"] is not None and row["deal_status"] == "ok"
    assert "history:" in row["deal_notes"]
    assert row["discount_check"] == "overstated"          # claims 40% off, real is 25% vs 30-day low
    assert row["affiliate_status"] == "insufficient_data"  # no commission/demand/audience data -> not invented

    capsys.readouterr()
    assert main(["history", url, "--db", str(tmp_path / "h.db"), "--json"]) == 0
    hist = json.loads(capsys.readouterr().out)
    assert hist[0]["stats"]["sufficient"] and hist[0]["discount_check"]["status"] == "overstated"

    assert main(["deals", "--db", str(tmp_path / "h.db")]) == 0
    assert "Acme Kettle" in capsys.readouterr().out

    db = Database(tmp_path / "h.db")
    runs = db.query("SELECT status, products FROM crawl_runs ORDER BY id")
    assert [r["status"] for r in runs] == ["ok", "ok"]
    db.close()


def test_import_performance_feeds_affiliate_score(local_site, tmp_path, capsys):
    csv_path = tmp_path / "report.csv"
    csv_path.write_text("domain,clicks,conversions,revenue\n127.0.0.1,400,12,300\n", encoding="utf-8")
    assert main(["import-performance", str(csv_path), "--db", str(tmp_path / "h.db")]) == 0
    db = Database(tmp_path / "h.db")
    assert db.performance_for("https://x", "127.0.0.1")["conversions"] == 12
    db.close()


def test_no_db_flag_writes_nothing(local_site, tmp_path):
    local_site.add("/products/a", product_html("A", "5"))
    rc = main([local_site.base + "/products/a", "--delay", "0", "--allow-private-hosts", "--no-db",
               "--db", str(tmp_path / "none.db"), "-o", str(tmp_path / "o")])
    assert rc == 0 and not (tmp_path / "none.db").exists()

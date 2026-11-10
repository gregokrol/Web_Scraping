"""Cross-store comparison in automation runs, and barcodes taken from product URLs."""

from types import SimpleNamespace

import pytest

from affiliate_scraper.commands import compare_across_stores
from affiliate_scraper.db import Database
from affiliate_scraper.models import Product, now_iso
from affiliate_scraper.sources.base import AdapterContext, SourceAdapter, StoreConfig
from affiliate_scraper.validation import validate_product

URL_GTIN = r"-(\d{12,14})(?:[?#]|$)"


def _with_store_defaults(store: StoreConfig, *products: Product) -> list[Product]:
    adapter = SimpleNamespace(ctx=AdapterContext(fetcher=None, stores=[store]))
    return SourceAdapter.apply_store_defaults(adapter, list(products))


# ---------------------------------------------------------------- gtin_from_url
def test_gtin_taken_from_url_when_page_has_none():
    store = StoreConfig.from_dict({"domain": "bug.co.il", "gtin_from_url": URL_GTIN})
    [p] = _with_store_defaults(store, Product(url="https://www.bug.co.il/%D7%A4%D7%90%D7%96%D7%9C-ravensburger-4005555017165",
                                              name="x", price=10.0, currency="ILS"))
    validate_product(p)
    assert p.gtin == "04005555017165"


def test_gtin_from_page_wins_and_bad_check_digit_is_dropped():
    store = StoreConfig.from_dict({"domain": "bug.co.il", "gtin_from_url": URL_GTIN})
    page_gtin = Product(url="https://www.bug.co.il/a-4005555017165", gtin="4005556125562")
    bad_digit = Product(url="https://www.bug.co.il/b-4005555017166", name="b", price=10.0, currency="ILS")
    no_code = Product(url="https://www.bug.co.il/brand/jumbo/puzzle/cruise")
    _with_store_defaults(store, page_gtin, bad_digit, no_code)
    assert page_gtin.gtin == "4005556125562" and no_code.gtin == ""
    validate_product(bad_digit)
    assert bad_digit.gtin == "" and "gtin_invalid" in bad_digit.validation_issues


@pytest.mark.parametrize("regex", ["(", r"-\d{13}$"])
def test_gtin_from_url_must_be_a_regex_with_a_group(regex):
    with pytest.raises(ValueError):
        StoreConfig.from_dict({"domain": "bug.co.il", "gtin_from_url": regex})


# ---------------------------------------------------------------- compare_across_stores
def _offer(url, price, gtin="4005556125562"):
    p = Product(url=url, name="Eiffel Tower 3D 216", price=price, currency="ILS", gtin=gtin,
                availability="InStock", fetched_at=now_iso(), source="json-ld", source_type="json-ld")
    validate_product(p)
    return p


def test_stores_scraped_in_separate_jobs_are_compared(tmp_path):
    db = Database(tmp_path / "a.db")
    since = now_iso()
    db.save_products([_offer("https://www.bug.co.il/eiffel-4005556125562", 129.9)])   # job 1
    db.save_products([_offer("https://www.amigo.co.il/product/eiffel/", 101.62),       # job 2
                      _offer("https://centertoys.co.il/product/other/", 99.0, gtin="")])
    comparisons = compare_across_stores(db, {}, since=since)

    shared = [c for c in comparisons if c.n_stores >= 2]
    assert len(shared) == 1 and shared[0].n_stores == 2
    rows = {r["domain"]: r for r in db.query("SELECT domain, group_id, deal_score, deal_notes FROM offers")}
    assert rows["bug.co.il"]["group_id"] == rows["amigo.co.il"]["group_id"] != ""
    assert rows["centertoys.co.il"]["group_id"] != rows["bug.co.il"]["group_id"]
    assert "market:" in rows["amigo.co.il"]["deal_notes"]                             # market position now scored
    assert rows["amigo.co.il"]["deal_score"] > rows["bug.co.il"]["deal_score"]         # cheaper store scores higher


def test_only_offers_from_this_run_are_compared(tmp_path):
    db = Database(tmp_path / "a.db")
    db.save_products([_offer("https://www.bug.co.il/eiffel-4005556125562", 129.9)])
    db.conn.execute("UPDATE offers SET last_seen='2020-01-01T00:00:00+00:00'")          # stale offer
    db.conn.commit()
    since = now_iso()
    db.save_products([_offer("https://www.amigo.co.il/product/eiffel/", 101.62)])
    assert [c for c in compare_across_stores(db, {}, since=since) if c.n_stores >= 2] == []

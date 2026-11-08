"""P0: identifying the same product across stores and comparing prices."""

from affiliate_scraper.matching import compare_prices
from affiliate_scraper.models import Product
from affiliate_scraper.validation import validate_product


def offer(store, name, price, currency="ILS", shipping=0.0, **kw):
    kw.setdefault("availability", "InStock")
    p = Product(url=f"https://{store}.example/p", name=name, price=price, currency=currency,
                shipping_cost=shipping, source="json-ld", source_type="json-ld", **kw)
    return validate_product(p)


def groups(products, **kw):
    return [sorted(o.store for o in c.offers) for c in compare_prices(products, **kw)]


def test_gtin_match_and_price_stats():
    ps = [offer("a", "Acme Kettle", 100, gtin="4006381333931"),
          offer("b", "Kettle by Acme 1.7", 80, gtin="4006381333931", shipping=10),
          offer("c", "ACME electric kettle", 120, gtin="04006381333931")]
    [c] = compare_prices(ps)
    assert c.method == "gtin" and c.confidence == 1.0 and not c.uncertain
    assert (c.min_price, c.median_price, c.max_price, c.spread) == (80, 100, 120, 40)
    assert c.savings_vs_median_pct == 20.0 and c.savings_vs_max_pct == 33.3
    assert c.min_total == 90 and c.best_store == "b.example" and not c.partial


def test_different_volume_not_merged():
    ps = [offer("a", "Acme Olive Oil 500ml", 30, brand="Acme"), offer("b", "Acme Olive Oil 1L", 50, brand="Acme")]
    assert len(groups(ps)) == 2


def test_same_gtin_but_refurbished_not_merged():
    ps = [offer("a", "Phone X 128GB", 3000, gtin="4006381333931"),
          offer("b", "Phone X 128GB (Refurbished)", 2000, gtin="4006381333931")]
    assert len(groups(ps)) == 2


def test_color_pack_warranty_importer_not_merged():
    base = dict(brand="Acme", model="Z1")
    assert len(groups([offer("a", "Acme Z1 Black", 10, **base), offer("b", "Acme Z1 White", 10, **base)])) == 2
    assert len(groups([offer("a", "Acme Z1 2-pack", 10, **base), offer("b", "Acme Z1", 10, **base)])) == 2
    assert len(groups([offer("a", "Acme Z1 אחריות 1 שנה", 10, **base),
                       offer("b", "Acme Z1 אחריות 3 שנים", 10, **base)])) == 2
    assert len(groups([offer("a", "Acme Z1 יבואן רשמי", 10, **base),
                       offer("b", "Acme Z1 יבוא מקביל", 10, **base)])) == 2


def test_brand_model_match_is_flagged_uncertain():
    [c] = compare_prices([offer("a", "Acme Z1 speaker", 100, brand="Acme", model="Z-1"),
                          offer("b", "Speaker Z1 by Acme", 90, brand="ACME", model="z1")])
    assert c.method == "brand_model" and c.confidence == 0.9 and c.uncertain and c.n_stores == 2


def test_title_similarity_match_and_threshold():
    ps = [offer("a", "Acme Turbo Blender TB500 Pro", 300, brand="Acme"),
          offer("b", "Acme Turbo Blender TB500 Pro Edition", 280, brand="Acme")]
    [c] = compare_prices(ps)
    assert c.method == "attributes" and 0.75 <= c.confidence <= 0.85 and c.uncertain
    assert len(groups(ps, min_confidence=0.9)) == 2


def test_different_brands_or_models_not_merged():
    assert len(groups([offer("a", "Turbo Blender TB500", 1, brand="Acme"),
                       offer("b", "Turbo Blender TB500", 1, brand="Other")])) == 2
    assert len(groups([offer("a", "Acme Blender TB500", 1, brand="Acme"),
                       offer("b", "Acme Blender TB700", 1, brand="Acme")])) == 2


def test_transitive_merge_blocked_by_conflict():
    ps = [offer("a", "Acme Z1", 10, brand="Acme", model="Z1", color="black"),
          offer("b", "Acme Z1", 11, brand="Acme", model="Z1"),
          offer("c", "Acme Z1", 12, brand="Acme", model="Z1", color="white")]
    gs = groups(ps)
    assert not any({"a.example", "c.example"} <= set(g) for g in gs)


def test_partial_comparison_reasons():
    ps = [offer("a", "Acme Kettle", 100, gtin="4006381333931", shipping=None),
          offer("b", "Acme Kettle", 25, currency="USD", gtin="4006381333931"),
          offer("c", "Acme Kettle", 90, gtin="4006381333931")]
    ps[2].price_verified = False
    [c] = compare_prices(ps)
    assert c.partial
    assert {"mixed_currency", "unverified_price_excluded:1", "shipping_unknown"} <= set(c.partial_reasons)
    assert c.currency in ("ILS", "USD")


def test_single_store_is_partial():
    [c] = compare_prices([offer("a", "Lonely", 5)])
    assert c.partial and "single_store" in c.partial_reasons

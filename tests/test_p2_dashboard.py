"""P2: local dashboard (FastAPI) - security guards and every API area."""

import json
from datetime import datetime, timedelta, timezone

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from affiliate_scraper.alerts import DealRules, detect_deals  # noqa: E402
from affiliate_scraper.dashboard.app import create_app  # noqa: E402
from affiliate_scraper.db import Database  # noqa: E402
from affiliate_scraper.matching import compare_prices  # noqa: E402
from affiliate_scraper.models import Product  # noqa: E402
from affiliate_scraper.validation import validate_product  # noqa: E402

TOKEN = "test-token"


@pytest.fixture()
def setup(tmp_path):
    path = str(tmp_path / "d.db")
    db = Database(path)
    ps = []
    for store, price in (("a", 90.0), ("b", 120.0), ("c", 110.0)):
        p = validate_product(Product(url=f"https://{store}.example/k", name="קומקום Acme", price=price, currency="ILS",
                                     gtin="4006381333931", availability="InStock", category="מטבח",
                                     source="json-ld", source_type="json-ld", shipping_cost=0))
        ps.append(p)
    compare_prices(ps)
    ps[0].deal_score, ps[0].deal_status = 85.0, "ok"
    db.save_products(ps)
    db.update_scores(ps)
    oid = db.offer_id(ps[0].url)
    now = datetime.now(timezone.utc)
    db.conn.executemany("INSERT INTO price_history(offer_id, observed_at, price, currency, price_verified) VALUES (?,?,?,?,1)",
                        [(oid, (now - timedelta(days=d)).isoformat(), 120, "ILS") for d in (25, 15, 8)])
    db.conn.commit()
    db.close()
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0

    app = create_app(path, automation_config=str(tmp_path / "automation.json"),
                     allowed_hosts=["testserver", "127.0.0.1"], crawl_runner=runner, token=TOKEN)
    client = TestClient(app)
    return client, path, oid, calls


def post(client, url, **kw):
    return client.post(url, headers={"X-Dashboard-Token": TOKEN}, **kw)


def test_page_embeds_token_and_security_headers(setup):
    client, *_ = setup
    r = client.get("/")
    assert r.status_code == 200 and TOKEN in r.text and 'dir="rtl"' in r.text
    assert r.headers["X-Frame-Options"] == "DENY" and "default-src 'self'" in r.headers["Content-Security-Policy"]


def test_csrf_token_and_host_guard(setup):
    client, *_ = setup
    assert client.post("/api/sources", json={"domain": "x.com"}).status_code == 403
    assert client.post("/api/sources", json={"domain": "x.com"}, headers={"X-Dashboard-Token": "wrong"}).status_code == 403
    assert client.get("/api/summary", headers={"Host": "evil.example"}).status_code == 400


def test_summary_offers_filters_history_and_comparison(setup):
    client, path, oid, _ = setup
    s = client.get("/api/summary").json()
    assert s["stats"]["offers"] == 3
    rows = client.get("/api/offers", params={"sort": "deal"}).json()
    assert rows[0]["deal_score"] == 85.0
    assert len(client.get("/api/offers", params={"category": "מטבח"}).json()) == 3
    assert client.get("/api/offers", params={"q": "nothing-like-this"}).json() == []
    h = client.get(f"/api/offers/{oid}/history").json()
    assert h["stats"]["sufficient"] and len(h["same_product"]) == 3
    [c] = client.get("/api/comparisons").json()
    assert (c["n_stores"], c["min_price"], c["median_price"]) == (3, 90.0, 110.0)
    assert client.get("/api/offers/99999/history").status_code == 404


def test_sources_crud_and_validation(setup):
    client, *_ = setup
    assert post(client, "/api/sources", json={"domain": "shop.example", "trust_score": 0.8,
                                              "start_urls": ["https://shop.example/"]}).status_code == 200
    assert post(client, "/api/sources", json={"domain": "bad", "trust_score": 3}).status_code == 400
    assert post(client, "/api/sources", json={"domain": "x.example", "start_urls": ["http://127.0.0.1/"]}).status_code == 400
    [src] = [s for s in client.get("/api/sources").json() if s["domain"] == "shop.example"]
    assert client.delete(f"/api/sources/{src['id']}", headers={"X-Dashboard-Token": TOKEN}).json()["deleted"] == 1


def test_programs_crud_and_validation(setup):
    client, *_ = setup
    ok = post(client, "/api/programs", json={"domain": "a.example", "params": {"ref": "me"}, "commission_rate": 0.05,
                                             "commission_verified": True, "commission_source": "dashboard 2026-11"})
    assert ok.status_code == 200
    assert post(client, "/api/programs", json={"domain": "b.example", "template": "https://t.example/?x=1"}).status_code == 400
    assert post(client, "/api/programs", json={"domain": "b.example", "commission_rate": 5}).status_code == 400
    [p] = client.get("/api/programs").json()
    assert p["commission_verified"] == 1 and p["commission_rate"] == 0.05


def test_alert_approval_flow_via_api(setup):
    client, path, oid, _ = setup
    db = Database(path)
    db.conn.execute("UPDATE offers SET last_seen=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), oid))
    db.conn.commit()
    [a] = detect_deals(db, DealRules())
    db.close()
    alerts = client.get("/api/alerts", params={"status": "pending"}).json()
    assert alerts[0]["content"]["claims"]
    assert post(client, f"/api/alerts/{a['id']}/approve").status_code == 200
    assert post(client, f"/api/alerts/{a['id']}/approve").status_code == 409     # no longer pending
    assert post(client, "/api/publish").status_code == 400                        # no webhook configured


def test_manual_crawl_uses_polite_pipeline(setup):
    client, path, _, calls = setup
    r = post(client, "/api/crawl", json={"urls": ["https://shop.example/products/1"], "discover": "none"})
    assert r.status_code == 200
    job = r.json()
    for _ in range(50):
        if client.get(f"/api/jobs/{job['id']}").json()["status"] != "running":
            break
    argv = calls[0]
    assert "--db" in argv and path in argv and "--ignore-robots" not in argv and "--allow-private-hosts" not in argv
    assert post(client, "/api/crawl", json={"urls": ["http://192.168.1.1/"]}).status_code == 400
    assert post(client, "/api/crawl", json={"urls": []}).status_code == 400


def test_export_csv_is_hebrew_safe(setup):
    client, *_ = setup
    r = client.get("/api/export", params={"format": "csv"})
    assert r.text.startswith("﻿") and "קומקום Acme" in r.text
    data = client.get("/api/export", params={"format": "json"}).json()
    assert len(data) == 3

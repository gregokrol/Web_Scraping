"""P2: local dashboard (FastAPI) - security guards and every API area."""

import json
import threading
import time
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


def wait_job(client, job_id):
    for _ in range(200):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_run_now_runs_automation_and_reports_progress(tmp_path):
    (tmp_path / "automation.json").write_text("{}")
    gate = threading.Event()

    def runner(argv):
        print("=== scrape: https://www.shop.example/category")
        print("Fetching 3 page(s)")
        print("  [2/3] https://www.shop.example/p/2")
        gate.wait(5)
        print("  [3/3] https://www.shop.example/p/3")
        return 0

    client = TestClient(create_app(str(tmp_path / "d.db"), automation_config=str(tmp_path / "automation.json"),
                                   allowed_hosts=["testserver"], crawl_runner=runner, token=TOKEN))
    job = post(client, "/api/run").json()
    assert job["kind"] == "run"
    for _ in range(200):
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j.get("done") == 2:
            break
        time.sleep(0.01)
    assert (j["stage"], j["done"], j["total"], j["stages_started"]) == ("shop.example", 2, 3, 1)
    assert "Fetching 3 page(s)" in j["log"] and not any("[2/3]" in line for line in j["log"])  # no per-page flood
    assert post(client, "/api/run").status_code == 409            # one scan at a time
    assert client.get("/api/ping").json()["busy"] is True
    gate.set()
    assert wait_job(client, job["id"])["status"] == "done"
    assert [j["id"] for j in client.get("/api/summary").json()["jobs"]] == [job["id"]]


def test_run_now_needs_the_automation_config(setup):
    client, *_ = setup
    assert post(client, "/api/run").status_code == 400          # setup's automation.json does not exist
    assert client.get("/api/jobs/nope").status_code == 404


def test_schedule_endpoints(tmp_path):
    calls, state = [], {"supported": True, "exists": False}

    def runner(argv):
        calls.append(argv)
        state["exists"] = argv[1] != "--remove"
        return 0

    client = TestClient(create_app(str(tmp_path / "d.db"), automation_config="automation.json", allowed_hosts=["testserver"],
                                   crawl_runner=runner, token=TOKEN, schedule_query=lambda: dict(state)))
    assert client.get("/api/schedule").json() == {"supported": True, "exists": False}
    assert post(client, "/api/schedule", json={"at": "25:00"}).status_code == 400
    assert post(client, "/api/schedule", json={"at": "07:43 & del"}).status_code == 400
    assert post(client, "/api/schedule", json={"at": "07:43"}).json()["exists"] is True
    assert calls[-1] == ["schedule", "--every", "daily", "--at", "07:43", "--config", "automation.json", "--hidden", "--create"]
    r = client.delete("/api/schedule", headers={"X-Dashboard-Token": TOKEN})
    assert r.json()["exists"] is False and calls[-1] == ["schedule", "--remove"]
    assert client.delete("/api/schedule").status_code == 403    # still needs the CSRF token


def test_shutdown_only_from_the_desktop_app(setup, tmp_path):
    client, *_ = setup
    assert post(client, "/api/shutdown").status_code == 404
    assert client.get("/api/summary").json()["can_shutdown"] is False
    stopped = threading.Event()
    app = create_app(str(tmp_path / "e.db"), allowed_hosts=["testserver"], token=TOKEN, on_shutdown=stopped.set)
    desktop = TestClient(app)
    assert desktop.get("/api/summary").json()["can_shutdown"] is True
    assert desktop.post("/api/shutdown").status_code == 403                 # no token, no shutdown
    assert post(desktop, "/api/shutdown").status_code == 200 and stopped.wait(3)
    assert app.state.last_ping is not None


def test_comparisons_list_each_store_cheapest_first(setup):
    client, *_ = setup
    [c] = client.get("/api/comparisons").json()
    assert [o["price"] for o in c["offers"]] == [90.0, 110.0, 120.0]
    assert [o["domain"] for o in c["offers"]] == ["a.example", "c.example", "b.example"]
    s = client.get("/api/summary").json()
    assert (s["compared_products"], s["tracked_offers"], s["tracked_stores"]) == (1, 3, 3)


def test_favicon_is_served(setup):
    client, *_ = setup
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/x-icon" and r.content[:4] == b"\0\0\1\0"


def test_export_csv_is_hebrew_safe(setup):
    client, *_ = setup
    r = client.get("/api/export", params={"format": "csv"})
    assert r.text.startswith("﻿") and "קומקום Acme" in r.text
    data = client.get("/api/export", params={"format": "json"}).json()
    assert len(data) == 3

"""P2: promo content, deal alerts without duplicates, manual approval, n8n webhook, automation, scheduler."""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

from affiliate_scraper.alerts import DealRules, decide, detect_deals, list_alerts, notify_pending, publish_approved
from affiliate_scraper.cli import main
from affiliate_scraper.content import build_content
from affiliate_scraper.db import Database
from affiliate_scraper.models import Product
from affiliate_scraper.scheduler import build_bat, build_schtasks_create, build_vbs, console_python
from affiliate_scraper.validation import validate_product
from affiliate_scraper.webhook import WebhookError, send_event

from .conftest import product_html


def _counts(res):
    return {k: res[k] for k in ("published", "failed")}


# ---------------------------------------------------------------- content
OFFER = {"title": "Acme Kettle", "store": "Shop A", "price": 90.0, "currency": "ILS", "list_price": 150.0,
         "shipping_cost": 0, "rating": 4.6, "review_count": 120, "url": "https://a.example/k",
         "affiliate_url": "https://a.example/k?ref=me", "last_fetched_at": "2026-11-08T10:00:00+00:00"}


def test_content_only_states_verified_facts():
    c = build_content(OFFER, lang="he")
    bases = [x["basis"] for x in c["claims"]]
    assert bases == ["verified_price", "shipping_known", "store_reviews"]   # no market/history/discount claims
    assert "150" not in c["telegram"]                                       # unverified "was" price not shown
    assert "גילוי נאות" in c["telegram"] and "?ref=me" in c["telegram"]


def test_content_with_market_history_and_verified_discount():
    comp = {"n_compared": 4, "median_price": 120.0, "uncertain": False, "partial_reasons": ""}
    c = build_content(OFFER, {"lowest_90d": True}, comp, {"drop_pct": 25, "from": 120.0},
                      {"status": "verified", "claimed_pct": 40}, lang="en")
    bases = {x["basis"] for x in c["claims"]}
    assert {"market_comparison", "price_history_90d", "price_history", "verified_discount"} <= bases
    assert "25% below the median price across 4 stores" in c["telegram"]
    uncertain = build_content(OFFER, comparison={**comp, "uncertain": True}, lang="en")
    assert "median" not in uncertain["telegram"]
    assert 'rel="sponsored nofollow noopener"' in c["html"]


# ---------------------------------------------------------------- alerts & publishing
def seed_db(tmp_path, history=(120, 120, 120), current=90.0, deal=80.0):
    db = Database(tmp_path / "a.db")
    now = datetime.now(timezone.utc)
    p = validate_product(Product(url="https://a.example/k", name="Acme Kettle", price=current, currency="ILS",
                                 availability="InStock", source="json-ld", source_type="json-ld", shipping_cost=0))
    p.deal_score, p.deal_status, p.affiliate_url = deal, "ok", "https://a.example/k?ref=me"
    db.save_products([p])
    oid = db.offer_id(p.url)
    db.conn.executemany("INSERT INTO price_history(offer_id, observed_at, price, currency, price_verified) VALUES (?,?,?,?,1)",
                        [(oid, (now - timedelta(days=30 - 10 * i)).isoformat(), v, "ILS") for i, v in enumerate(history)])
    db.conn.commit()
    return db, oid


def set_price(db, oid, price):
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    # price_history is UNIQUE(offer_id, observed_at). On Windows the clock ticks every ~15.6 ms, so two quick
    # calls can get the same time; always step past the newest row instead.
    last = db.conn.execute("SELECT MAX(observed_at) FROM price_history WHERE offer_id=?", (oid,)).fetchone()[0]
    if last and now <= datetime.fromisoformat(last):
        now = datetime.fromisoformat(last) + timedelta(milliseconds=1)
    db.conn.execute("UPDATE offers SET price=?, last_seen=? WHERE id=?", (price, now.isoformat(), oid))
    db.conn.execute("INSERT INTO price_history(offer_id, observed_at, price, currency, price_verified) VALUES (?,?,?,?,1)",
                    (oid, now.isoformat(), price, "ILS"))
    db.conn.commit()


def test_detect_creates_one_pending_alert_without_duplicates(tmp_path):
    db, oid = seed_db(tmp_path)
    [a] = detect_deals(db, DealRules())
    assert a["kind"] == "price_drop" and list_alerts(db)[0]["status"] == "pending"
    assert detect_deals(db, DealRules()) == []          # same deal again -> nothing new
    set_price(db, oid, 89.0)                              # ~1% further drop -> still a duplicate
    assert detect_deals(db, DealRules()) == []
    set_price(db, oid, 80.0)                              # real further drop -> new alert
    assert len(detect_deals(db, DealRules())) == 1


def test_rejected_deal_not_realerted_at_same_price(tmp_path):
    db, oid = seed_db(tmp_path)
    [a] = detect_deals(db, DealRules(cooldown_days=0))
    decide(db, [a["id"]], approve=False)
    assert detect_deals(db, DealRules(cooldown_days=0)) == []


def test_out_of_stock_or_unverified_never_alerted(tmp_path):
    db, oid = seed_db(tmp_path)
    db.conn.execute("UPDATE offers SET availability='OutOfStock' WHERE id=?", (oid,))
    db.conn.commit()
    assert detect_deals(db, DealRules()) == []
    db.conn.execute("UPDATE offers SET availability='InStock', price_verified=0 WHERE id=?", (oid,))
    db.conn.commit()
    assert detect_deals(db, DealRules()) == []


def test_publish_requires_approval_and_happens_once(tmp_path, local_site, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret")
    db, oid = seed_db(tmp_path)
    [a] = detect_deals(db, DealRules())
    hook = local_site.base + "/webhook"
    assert _counts(publish_approved(db, hook)) == {"published": 0, "failed": 0}   # pending -> nothing sent
    assert local_site.events() == []
    decide(db, [a["id"]], approve=True)
    assert _counts(publish_approved(db, hook)) == {"published": 1, "failed": 0}
    assert _counts(publish_approved(db, hook)) == {"published": 0, "failed": 0}   # never twice
    [ev] = local_site.events("deal.publish")
    assert ev["data"]["affiliate_url"].endswith("?ref=me") and "גילוי נאות" in ev["data"]["content"]["telegram"]
    post = local_site.posts[0]
    sig = hmac.new(b"s3cret", post["body"], hashlib.sha256).hexdigest()
    assert post["headers"]["X-Signature-SHA256"] == sig


def test_failed_publish_is_retried_later(tmp_path, local_site):
    db, oid = seed_db(tmp_path)
    [a] = detect_deals(db, DealRules())
    decide(db, [a["id"]], True)
    local_site.post_status = [500, 500, 500]
    assert publish_approved(db, local_site.base + "/hook")["failed"] == 1
    assert list_alerts(db)[0]["status"] == "failed"
    assert publish_approved(db, local_site.base + "/hook")["published"] == 1


def test_notify_pending_once(tmp_path, local_site):
    db, oid = seed_db(tmp_path)
    detect_deals(db, DealRules())
    assert notify_pending(db, local_site.base + "/hook") == 1
    assert notify_pending(db, local_site.base + "/hook") == 0
    assert local_site.events("deals.pending_approval")[0]["data"]["count"] == 1


def test_webhook_rejects_bad_url():
    with pytest.raises(WebhookError):
        send_event("ftp://x", "e", {})


# ---------------------------------------------------------------- scheduler
def test_scheduler_commands():
    bat = build_bat(r"C:\Web_Scraping", r"C:\Python311\python.exe", "automation.json")
    assert 'cd /d "C:\\Web_Scraping"' in bat and "main.py run --config" in bat and "\r\n" in bat
    cmd = build_schtasks_create("WebScraping", r"C:\Web_Scraping\run_automation.bat", "daily", "08:17")
    assert cmd[:3] == ["schtasks", "/Create", "/TN"] and ["/SC", "DAILY", "/ST", "08:17"] == cmd[-4:]
    assert "/MO" in build_schtasks_create("W", "x.bat", "hourly", "00:05", 4)
    with pytest.raises(ValueError):
        build_schtasks_create("W", "x.bat", "daily", "25:00")
    with pytest.raises(ValueError):
        build_schtasks_create("bad&name", "x.bat")


def test_hidden_schedule_runs_through_wscript(tmp_path):
    vbs = build_vbs(r"C:\My Projects\run_automation.bat")
    assert '"""C:\\My Projects\\run_automation.bat""", 0, True' in vbs    # window style 0 = hidden
    cmd = build_schtasks_create("W", r"C:\p\run_automation.bat", vbs_path=r"C:\p\run_automation.vbs")
    assert cmd[cmd.index("/TR") + 1] == 'wscript.exe "C:\\p\\run_automation.vbs"'
    (tmp_path / "pythonw.exe").write_text("")
    (tmp_path / "python.exe").write_text("")
    assert console_python(str(tmp_path / "pythonw.exe")) == str(tmp_path / "python.exe")
    assert console_python(r"C:\Python\python.exe") == r"C:\Python\python.exe"


def test_schedule_command_prints_without_creating(tmp_path, capsys):
    assert main(["schedule", "--at", "07:43"]) == 0
    out = capsys.readouterr().out
    assert "schtasks /Create" in out and "Not registered" in out
    assert (tmp_path / "run_automation.bat").exists()


# ---------------------------------------------------------------- full automation run
def test_automation_run_end_to_end(tmp_path, local_site):
    local_site.add("/products/k", product_html("Acme Kettle", "90", currency="ILS", gtin="4006381333931",
                                               extra={"offers": {"price": "90", "priceCurrency": "ILS",
                                                                 "availability": "InStock"}}))
    cfg = {"db": str(tmp_path / "auto.db"), "targets": [local_site.base + "/products/k"], "delay": 0,
           "no_cache": True, "allow_private_hosts": True, "out_dir": str(tmp_path / "out"),
           "webhook_url": local_site.base + "/n8n", "deals": {"min_deal_score": 0}}
    (tmp_path / "automation.json").write_text(json.dumps(cfg), encoding="utf-8")

    # history so the new price is a real drop
    assert main(["run", "--config", "automation.json"]) == 0
    db = Database(tmp_path / "auto.db")
    oid = db.offer_id(local_site.base + "/products/k")
    now = datetime.now(timezone.utc)
    db.conn.executemany("INSERT INTO price_history(offer_id, observed_at, price, currency, price_verified) VALUES (?,?,?,?,1)",
                        [(oid, (now - timedelta(days=d)).isoformat(), 120, "ILS") for d in (28, 18, 9)])
    db.conn.commit()
    db.close()

    assert main(["run", "--config", "automation.json"]) == 0
    assert len(local_site.events("deals.pending_approval")) == 1
    assert local_site.events("deal.publish") == []          # approval required by default

    db = Database(tmp_path / "auto.db")
    [alert] = list_alerts(db, "pending")
    db.close()
    assert main(["approve", str(alert["id"]), "--db", str(tmp_path / "auto.db")]) == 0
    assert main(["run", "--config", "automation.json"]) == 0
    assert main(["run", "--config", "automation.json"]) == 0
    assert len(local_site.events("deal.publish")) == 1      # published exactly once
    assert len(local_site.events("deals.pending_approval")) == 1


def test_automation_dry_run_writes_nothing(tmp_path, local_site, capsys):
    cfg = {"db": str(tmp_path / "dry.db"), "targets": [local_site.base + "/products/k"], "allow_private_hosts": True}
    (tmp_path / "automation.json").write_text(json.dumps(cfg), encoding="utf-8")
    assert main(["run", "--dry-run"]) == 0
    assert "would run" in capsys.readouterr().out
    assert not (tmp_path / "dry.db").exists() and local_site.count("/products/k") == 0


def test_scrape_webhook_export(tmp_path, local_site):
    local_site.add("/products/a", product_html("A", "5"))
    rc = main([local_site.base + "/products/a", "--delay", "0", "--allow-private-hosts", "--no-db",
               "-o", str(tmp_path / "o"), "--webhook", local_site.base + "/n8n"])
    assert rc == 0
    [ev] = local_site.events("scrape.completed")
    assert ev["data"]["products"][0]["name"] == "A"


def test_twice_daily_schedule_and_per_store_delay(tmp_path):
    cmd = build_schtasks_create("WebScraping", r"C:\Web_Scraping\run_automation.bat", "twice-daily", "08:13")
    assert cmd[-6:] == ["/SC", "HOURLY", "/MO", "12", "/ST", "08:13"]

    from affiliate_scraper.commands import automation_scrape_jobs
    sources = {"permitted_domains": ["ksp.co.il"], "stores": [
        {"domain": "ksp.co.il", "name": "KSP", "start_urls": ["https://ksp.co.il/web/item/1"],
         "discover": "none", "delay": 10, "max_pages": 20}]}
    (tmp_path / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
    [job] = automation_scrape_jobs({"sources_config": str(tmp_path / "sources.json"), "delay": 3})
    import argparse
    from affiliate_scraper.cli import build_parser
    args = build_parser().parse_args(job)
    assert args.delay == 10.0 and args.max_pages == 20 and args.discover == "none"
    assert args.sources == str(tmp_path / "sources.json")


def test_targets_command_edits_automation_json(tmp_path, capsys):
    (tmp_path / "automation.json").write_text(json.dumps({"targets": [], "delay": 3}), encoding="utf-8")
    assert main(["targets", "add", "https://shop.example/p/1", "https://shop.example/p/1", "https://ksp.co.il/web/item/5",
                 "file:///etc/passwd", "https://other.example/x"]) == 0
    cfg = json.loads((tmp_path / "automation.json").read_text(encoding="utf-8"))
    assert cfg["targets"] == ["https://shop.example/p/1", "https://other.example/x"] and cfg["delay"] == 3
    err = capsys.readouterr().err
    assert "site terms" in err and "bad link" in err
    assert main(["targets", "remove", "1"]) == 0
    assert json.loads((tmp_path / "automation.json").read_text(encoding="utf-8"))["targets"] == ["https://other.example/x"]
    capsys.readouterr()
    assert main(["targets", "list"]) == 0 and "other.example" in capsys.readouterr().out


def test_automation_include_regex_may_start_with_dash(tmp_path):
    from affiliate_scraper.cli import build_parser
    from affiliate_scraper.commands import automation_scrape_jobs

    regex = r"-\d{12,14}(\?|$)|/puzzle/"     # e.g. product URLs that end with a barcode
    src = tmp_path / "sources.json"
    src.write_text(json.dumps({"stores": [{"domain": "shop.example", "start_urls": ["https://shop.example/c"],
                                           "discover": "links", "include": regex}]}), encoding="utf-8")
    [job] = automation_scrape_jobs({"sources_config": str(src)})
    args = build_parser().parse_args(job)        # used to fail: "--include: expected one argument"
    assert args.include == regex and args.discover == "links"


def test_automation_per_store_page_limits(tmp_path):
    from affiliate_scraper.cli import build_parser
    from affiliate_scraper.commands import automation_scrape_jobs

    src = tmp_path / "sources.json"
    src.write_text(json.dumps({"stores": [
        {"domain": "big.example", "start_urls": ["https://big.example/c"], "discover": "links",
         "max_pages": 500, "max_listing_pages": 35},
        {"domain": "small.example", "start_urls": ["https://small.example/c"], "discover": "links"}]}),
        encoding="utf-8")
    big, small = (build_parser().parse_args(j) for j in automation_scrape_jobs(
        {"sources_config": str(src), "max_pages": 100}))
    assert (big.max_pages, big.max_listing_pages) == (500, 35)       # store limits override the common ones
    assert (small.max_pages, small.max_listing_pages) == (100, 10)


def test_saved_searches_command_and_run(tmp_path, monkeypatch, capsys):
    (tmp_path / "automation.json").write_text(json.dumps({"db": str(tmp_path / "s.db")}), encoding="utf-8")
    assert main(["searches", "add", "פאזל 1000", "פאזל 1000", "x", "אוזניות JBL"]) == 0
    cfg = json.loads((tmp_path / "automation.json").read_text(encoding="utf-8"))
    assert cfg["searches"] == ["פאזל 1000", "אוזניות JBL"]          # duplicate and 1-letter query skipped
    capsys.readouterr()

    import affiliate_scraper.commands as cmds
    calls = []
    real_main = cmds.cli.main

    def fake_main(argv=None):
        if argv and argv[0] == "search":
            calls.append(argv[1])
            return 0
        return real_main(argv)
    monkeypatch.setattr(cmds.cli, "main", fake_main)
    assert main(["run", "--config", "automation.json"]) == 0           # no stores/targets: searches alone are enough
    assert calls == ["פאזל 1000", "אוזניות JBL"]

    assert main(["searches", "remove", "1"]) == 0
    assert json.loads((tmp_path / "automation.json").read_text(encoding="utf-8"))["searches"] == ["אוזניות JBL"]


def test_high_score_without_real_saving_is_not_a_deal(tmp_path):
    # e.g. a gift card sold at face value: price never changed, other stores unknown
    db, oid = seed_db(tmp_path, history=(90, 90, 90), current=90.0, deal=95.0)
    assert detect_deals(db, DealRules()) == []


def test_small_saving_below_threshold_is_skipped(tmp_path):
    db, oid = seed_db(tmp_path, history=(100, 100, 100), current=97.0, deal=95.0)
    assert detect_deals(db, DealRules()) == []                       # 3% < 5%
    assert len(detect_deals(db, DealRules(min_savings_pct=2))) == 1


def test_vague_title_is_skipped(tmp_path):
    from affiliate_scraper.alerts import is_vague_title
    assert is_vague_title("כרטיס זכרון") and is_vague_title("")
    assert not is_vague_title("כרטיס זכרון 128GB")
    assert not is_vague_title("Acme Kettle")
    assert not is_vague_title("קומקום חשמלי נירוסטה")
    db, oid = seed_db(tmp_path)
    db.conn.execute("UPDATE offers SET title=? WHERE id=?", ("כרטיס זכרון", oid))
    db.conn.commit()
    assert detect_deals(db, DealRules()) == []
    assert len(detect_deals(db, DealRules(skip_vague_titles=False))) == 1

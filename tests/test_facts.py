"""`py main.py facts`: the system's numbers counted from the data, the only source for numbers in posts."""

import json
from datetime import datetime, timedelta, timezone

from affiliate_scraper.cli import main
from affiliate_scraper.db import Database
from affiliate_scraper.facts import collect, remember_subscribers, scan_runs, text

NOW = datetime(2026, 11, 25, 8, 0, tzinfo=timezone.utc)


def _setup(tmp_path):
    (tmp_path / "sources.json").write_text(json.dumps({"stores": [
        {"domain": "a.example", "name": "Store A", "start_urls": ["https://a.example/s1", "https://a.example/s2"],
         "search_url": "https://a.example/search?q={q}"},
        {"domain": "b.example", "name": "Store B", "start_urls": ["https://b.example/sale"],
         "search_url": "https://b.example/search?q={q}", "search": False},
        {"domain": "c.example", "name": "Store C", "enabled": False, "start_urls": ["https://c.example/x"]},
    ]}), encoding="utf-8")
    cfg = {"sources_config": str(tmp_path / "sources.json"), "db": str(tmp_path / "d.db"),
           "searches": ["iPhone", "AirPods", " "], "category_searches": {"baby": ["טיולון"]}}
    (tmp_path / "automation.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    db = Database(cfg["db"])
    iso = lambda dt: dt.isoformat()   # noqa: E731
    with db.tx() as c:
        # two full runs (6 scrapes each, minutes apart) and one manual check of 2 pages - that is not a run
        for start in (NOW - timedelta(hours=36), NOW - timedelta(hours=12)):
            for i in range(6):
                c.execute("INSERT INTO crawl_runs(started_at, command) VALUES (?, 'scrape x')",
                          (iso(start + timedelta(minutes=3 * i)),))
        for i in range(2):
            c.execute("INSERT INTO crawl_runs(started_at, command) VALUES (?, 'scrape y')",
                      (iso(NOW - timedelta(hours=5, minutes=i)),))
        c.execute("INSERT INTO offers(id, url, title, last_seen) VALUES (1, 'https://a.example/p/1', 'x', ?)",
                  (iso(NOW - timedelta(days=1)),))
        c.execute("INSERT INTO offers(id, url, title, last_seen) VALUES (2, 'https://a.example/p/2', 'y', ?)",
                  (iso(NOW - timedelta(days=20)),))
        c.execute("INSERT INTO price_history(offer_id, observed_at, price) VALUES (1, '2026-11-09T10:00:00+00:00', 5)")
        c.execute("INSERT INTO price_history(offer_id, observed_at, price) VALUES (1, '2026-11-20T10:00:00+00:00', 4)")
        for i, (status, days, pct) in enumerate([("published", 2, 20.0), ("published", 10, 30.0),
                                                  ("published", 40, 90.0), ("withdrawn", 3, 25.0),
                                                  ("pending", 1, 60.0)]):
            c.execute("INSERT INTO deal_alerts(offer_id, kind, fingerprint, status, published_at, savings_pct) "
                      "VALUES (1, 'price_drop', ?, ?, ?, ?)", (f"f{i}", status, iso(NOW - timedelta(days=days)), pct))
    return cfg, db


def test_the_facts_are_counted_from_the_data(tmp_path):
    cfg, db = _setup(tmp_path)
    f = collect(cfg, db, now=NOW)
    assert f["stores"] == ["Store A", "Store B"]                       # the disabled store is not counted
    assert f["sale_pages"] == 3 and f["stores_with_sale_pages"] == 2
    assert f["searchable_stores"] == 1                                 # search turned off for B
    assert f["search_words"] == 2 and f["category_words"] == {"baby": 1}
    assert f["scan_runs_7d"] == 2 and f["last_scan_run"] == (NOW - timedelta(hours=12)).isoformat()
    assert f["products_tracked"] == 2 and f["products_seen_7d"] == 1
    assert f["price_checks"] == 2 and f["price_checks_since"].startswith("2026-11-09")
    assert f["deals_published"] == 3 and f["deals_published_7d"] == 1 and f["deals_withdrawn"] == 1
    assert f["savings_30d"] == {"deals": 2, "avg_pct": 25.0, "max_pct": 30.0}   # not the 40-day-old 90%
    assert f["sale_checks_published"] == 0 and f["subscribers"] is None
    page = text(f)
    assert "דפי מבצעים שנסרקים: 3, ב-2 חנויות" in page and "ממוצע 25%" in page
    assert "לא כותבים מספר מנויים" in page                               # no count -> no number in a post
    db.close()


def test_the_subscriber_count_is_kept_one_a_day_to_compare_with_a_week_ago(tmp_path):
    cfg, db = _setup(tmp_path)
    for days, count in ((9, 3), (8, 4), (1, 6)):
        remember_subscribers(db, count, NOW - timedelta(days=days))
    remember_subscribers(db, 7, NOW - timedelta(days=1))                 # the same day again: replaced
    f = collect(cfg, db, now=NOW, subscribers=9)
    assert f["subscribers"] == {"now": 9, "week_ago": 4}                  # the last count at least a week old
    assert "מנויים בערוץ: 9 (לפני 7 ימים: 4)" in text(f)
    saved = json.loads(db.query("SELECT value FROM meta WHERE key='facts_subscribers'")[0]["value"])
    assert saved[(NOW - timedelta(days=1)).date().isoformat()] == 7 and len(saved) == 4
    db.close()


def test_a_manual_check_is_not_a_scan_run(tmp_path):
    cfg, db = _setup(tmp_path)
    assert len(scan_runs(db, NOW - timedelta(hours=6))) == 0             # only the 2-page check is that recent
    db.close()


def test_the_command_prints_the_page_and_json(tmp_path, capsysbinary):
    _setup(tmp_path)[1].close()
    conf = ["--config", str(tmp_path / "automation.json")]
    assert main(["facts", *conf]) == 0
    assert "עובדות המערכת" in capsysbinary.readouterr().out.decode("utf-8")
    assert main(["facts", "--json", *conf]) == 0
    data = json.loads(capsysbinary.readouterr().out.decode("utf-8"))
    assert data["sale_pages"] == 3 and data["deals_published"] == 3


def test_subscribers_without_telegram_say_so_and_count_nothing(tmp_path, capsys, monkeypatch):
    _setup(tmp_path)[1].close()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHANNEL", raising=False)
    assert main(["facts", "--subscribers", "--json", "--config", str(tmp_path / "automation.json")]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["subscribers"] is None and "Telegram is not set up" in out.err

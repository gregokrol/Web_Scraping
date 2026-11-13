"""Daily self-check."""

import json
from datetime import datetime, timedelta, timezone

from affiliate_scraper.alerts import DealRules, decide, detect_deals
from affiliate_scraper.cli import main
from affiliate_scraper.health import due, format_report, mark_sent, run_health

from .test_p2_automation import seed_db, set_price


class FakeTG:
    configured = True

    def __init__(self, admin=True, fail=False):
        self.admin, self.fail = admin, fail

    def check(self):
        if self.fail:
            raise RuntimeError("network down")
        return {"bot": "b", "channel": "מחיר אמת", "bot_is_admin": self.admin}


def _run(db, hours_ago, products=5, errors=()):
    t = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    db.conn.execute("INSERT INTO crawl_runs(started_at, finished_at, command, products, errors, status, errors_json) "
                    "VALUES (?,?,?,?,?,?,?)", (t, t, "scrape", products, len(errors), "ok", json.dumps(list(errors))))
    db.conn.commit()


def test_everything_fine(tmp_path):
    db, oid = seed_db(tmp_path)
    _run(db, 2)
    res = run_health(db, {}, FakeTG())
    assert res["ok"] and not res["problems"] and not res["warnings"]
    assert format_report(res).startswith("✅")


def test_no_run_store_errors_and_bot_not_admin(tmp_path):
    db, oid = seed_db(tmp_path)
    assert "עוד לא רצה" in run_health(db, {})["problems"][0]
    _run(db, 30, products=0, errors=[{"url": "https://shop.example/p", "kind": "http_403"}] * 3)
    res = run_health(db, {}, FakeTG(admin=False))
    text = format_report(res)
    assert not res["ok"] and "לא רצה 30 שעות" in text and "הבוט כבר לא מנהל" in text
    _run(db, 1, products=0, errors=[{"url": "https://shop.example/p", "kind": "http_403"}] * 3)
    res = run_health(db, {}, FakeTG(fail=True))
    text = format_report(res)
    assert "shop.example: 3 שגיאות http_403" in text and "לא עוקפים" in text
    assert "לא נמצא אף מוצר" in text and "בעיה בחיבור לטלגרם" in text


def test_published_deal_that_ended_is_listed_for_withdrawal(tmp_path):
    db, oid = seed_db(tmp_path)
    _run(db, 1)
    [a] = detect_deals(db, DealRules())
    decide(db, [a["id"]], True)
    db.conn.execute("UPDATE deal_alerts SET status='published' WHERE id=?", (a["id"],))
    db.conn.commit()
    assert run_health(db, {})["withdraw_ids"] == []
    set_price(db, oid, 130.0)                                # price went back up
    res = run_health(db, {})
    assert res["withdraw_ids"] == [a["id"]] and f"withdraw {a['id']}" in format_report(res)


def test_report_once_a_day(tmp_path):
    db, _ = seed_db(tmp_path)
    assert due(db)
    mark_sent(db)
    assert not due(db)
    assert due(db, now=datetime.now(timezone.utc) + timedelta(hours=21))


def test_health_command_prints_report(tmp_path, capsys):
    db, _ = seed_db(tmp_path)
    db.close()
    code = main(["health", "--db", str(tmp_path / "a.db"), "--config", str(tmp_path / "none.json"), "--no-site-check"])
    assert code == 1 and "עוד לא רצה" in capsys.readouterr().out


def test_automation_run_sends_the_daily_report_once(tmp_path, local_site, monkeypatch):
    import affiliate_scraper.telegram as tgmod

    from .conftest import product_html
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tgmod, "API", local_site.base)
    monkeypatch.setattr(tgmod, "MIN_INTERVAL", 0)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@deals_il")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT", "42")
    local_site.post_reply = b'{"ok": true, "result": {"message_id": 1, "id": 5, "username": "b", "title": "x"}}'
    local_site.add("/products/k", product_html("Acme Kettle", "90", currency="ILS"))
    cfg = {"db": str(tmp_path / "t.db"), "targets": [local_site.base + "/products/k"], "delay": 0, "no_cache": True,
           "allow_private_hosts": True, "out_dir": str(tmp_path / "out"), "telegram": {"enabled": True}}
    (tmp_path / "automation.json").write_text(json.dumps(cfg), encoding="utf-8")

    def reports():
        bodies = [json.loads(p["body"]) for p in local_site.posts if p["path"].endswith("sendMessage")]
        return [b for b in bodies if b.get("chat_id") == "42" and "בדיקה יומית" in b.get("text", "")]

    assert main(["run"]) == 0
    assert len(reports()) == 1
    assert main(["run"]) == 0
    assert len(reports()) == 1                       # twice a day -> still one report a day

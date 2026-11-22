"""Daily summary to share in WhatsApp / Facebook groups."""

import json
from datetime import datetime, timedelta, timezone

from affiliate_scraper.alerts import DealRules, decide, detect_deals
from affiliate_scraper.cli import main
from affiliate_scraper.digest import build_digest
from affiliate_scraper.site import SiteConfig

from .test_p2_automation import seed_db, set_price

CFG = SiteConfig(base_url="https://user.github.io/mechiremet/", telegram_url="https://t.me/mechiremet")


def published(tmp_path):
    db, oid = seed_db(tmp_path)
    [a] = detect_deals(db, DealRules())
    decide(db, [a["id"]], True)
    db.conn.execute("UPDATE deal_alerts SET status='published' WHERE id=?", (a["id"],))
    db.conn.commit()
    return db, oid, a


def test_summary_has_only_verified_facts_and_site_links(tmp_path):
    db, oid, a = published(tmp_path)
    text = build_digest(db, CFG)
    assert "העסקה הכי טובה היום" in text and "Acme Kettle" in text
    assert "₪90" in text or "90" in text
    assert "חיסכון 25%" in text and "במקום ₪120" in text                     # 120 -> 90, the real history
    assert f"https://user.github.io/mechiremet/deals/{a['id']}.html" in text   # the deal page, not the shop
    assert "https://t.me/mechiremet" in text and "קישורי שותפים" in text
    assert "<" not in text                                                    # plain text for WhatsApp


def test_ended_old_or_unproven_deals_are_left_out(tmp_path):
    db, oid, a = published(tmp_path)
    assert build_digest(db, CFG, now=datetime.now(timezone.utc) + timedelta(hours=60)) is None   # older than 48 h
    set_price(db, oid, 130.0)                                                 # price went back up -> ended
    assert build_digest(db, CFG) is None


def test_digest_command_saves_the_text(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, oid, a = published(tmp_path)
    db.close()
    (tmp_path / "automation.json").write_text(json.dumps({"db": str(tmp_path / "a.db"), "out_dir": "out",
                                                          "site": {"base_url": CFG.base_url}}), encoding="utf-8")
    assert main(["digest"]) == 0
    saved = (tmp_path / "out" / "daily_summary.txt").read_text(encoding="utf-8")
    assert "Acme Kettle" in saved and "daily_summary.txt" in capsys.readouterr().out
    assert main(["digest", "--count", "11"]) == 2


def test_automation_run_sends_the_summary_once_a_day(tmp_path, local_site, monkeypatch):
    import affiliate_scraper.telegram as tgmod

    from .conftest import product_html
    monkeypatch.chdir(tmp_path)
    db, oid, a = published(tmp_path)             # a.db holds one public deal with a real saving
    db.close()
    monkeypatch.setattr(tgmod, "API", local_site.base)
    monkeypatch.setattr(tgmod, "MIN_INTERVAL", 0)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@deals_il")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT", "42")
    local_site.post_reply = b'{"ok": true, "result": {"message_id": 1}}'
    local_site.add("/products/z", product_html("Other Thing", "50", currency="ILS"))
    cfg = {"db": str(tmp_path / "a.db"), "targets": [local_site.base + "/products/z"], "delay": 0, "no_cache": True,
           "allow_private_hosts": True, "out_dir": str(tmp_path / "out"), "telegram": {"enabled": True},
           "health": {"daily": False}, "site": {"base_url": CFG.base_url}}
    (tmp_path / "automation.json").write_text(json.dumps(cfg), encoding="utf-8")

    def summaries():
        bodies = [json.loads(p["body"]) for p in local_site.posts if p["path"].endswith("sendMessage")]
        return [b for b in bodies if b.get("chat_id") == "42" and "הכי טוב" in b.get("text", "")]

    assert main(["run"]) == 0
    [s] = summaries()
    assert "Acme Kettle" in s["text"]
    assert main(["run"]) == 0
    assert len(summaries()) == 1                 # twice a day -> still one summary a day


def test_manual_digest_says_so_in_telegram_when_empty(tmp_path, local_site, capsys, monkeypatch):
    import affiliate_scraper.telegram as tgmod
    monkeypatch.chdir(tmp_path)
    db, _ = seed_db(tmp_path)
    db.close()
    monkeypatch.setattr(tgmod, "API", local_site.base)
    monkeypatch.setattr(tgmod, "MIN_INTERVAL", 0)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT", "42")
    local_site.post_reply = b'{"ok": true, "result": {"message_id": 1}}'
    assert main(["digest", "--db", str(tmp_path / "a.db"), "--telegram"]) == 1
    [body] = [json.loads(p["body"]) for p in local_site.posts]
    assert body["chat_id"] == "42" and "אין כרגע עסקאות לשיתוף" in body["text"]

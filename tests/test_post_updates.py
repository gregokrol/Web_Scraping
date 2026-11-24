"""A published deal whose price changed: its channel post says so (alerts.post_updates, commands.update_posts)."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from affiliate_scraper.alerts import mark_post_updated, post_updates
from affiliate_scraper.db import Database
from affiliate_scraper.telegram import TelegramError, friendly_post

from .test_p2_automation import seed_db

CONTENT = {"title": "Acme Kettle", "facts": {"price": 90.0, "currency": "ILS", "store": "Acme"},
           "disclosure": "גילוי נאות", "claims": [{"text": "מחיר: ₪90", "basis": "verified_price"}]}


def published(db, oid, price=90.0, msg=51):
    db.conn.execute("INSERT INTO deal_alerts(offer_id, kind, fingerprint, price, currency, title, content_json, status, "
                    "created_at, published_ref) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (oid, "price_drop", f"f{msg}", price, "ILS", "Acme Kettle", json.dumps(CONTENT, ensure_ascii=False),
                     "published", datetime.now(timezone.utc).isoformat(),
                     json.dumps({"telegram": {"chat_id": -1001, "message_id": msg}})))
    db.conn.commit()


def checked(db, oid, price, availability="InStock", hours_ago=1, verified=1):
    when = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    db.conn.execute("UPDATE offers SET price=?, availability=?, last_fetched_at=?, price_verified=? WHERE id=?",
                    (price, availability, when, verified, oid))
    db.conn.commit()


def step(db):
    """One run: what changes, then remember it as done."""
    todo = post_updates(db)
    for t in todo:
        mark_post_updated(db, t["id"], t["state"], t["price"])
    return todo


def test_up_more_back_down_and_out_of_stock_each_said_once(tmp_path):
    db, oid = seed_db(tmp_path)
    published(db, oid)
    checked(db, oid, 90.0)
    assert step(db) == []                                         # the post is right
    checked(db, oid, 120.0)
    [t] = step(db)
    assert t["state"] == "up" and "עלה ל-₪120" in t["note"] and "המבצע הסתיים" in t["note"]
    assert t["payload"]["content"]["update"] == t["note"] and t["ref"]["message_id"] == 51
    assert step(db) == []                                         # said once, not every run
    checked(db, oid, 120.5)
    assert step(db) == []                                         # agorot: no rewrite
    checked(db, oid, 125.0)
    [t] = step(db)
    assert "₪125" in t["note"]
    checked(db, oid, 90.0)
    [t] = step(db)
    assert t["state"] == "ok" and t["note"] is None and "update" not in t["payload"]["content"]
    checked(db, oid, 80.0)
    [t] = step(db)
    assert t["state"] == "down" and "ירד עוד, ל-₪80" in t["note"]
    checked(db, oid, 80.0, availability="OutOfStock")
    [t] = step(db)
    assert t["state"] == "gone" and "אזל במלאי" in t["note"]
    assert step(db) == []
    [row] = db.query("SELECT shown_state, shown_price, post_updated_at, content_json FROM deal_alerts")
    assert row["shown_state"] == "gone" and row["post_updated_at"]
    assert "update" not in json.loads(row["content_json"])       # the deal's own text stays as approved


@pytest.mark.parametrize("hours_ago,verified,why", [(72, 1, "not checked lately"), (1, 0, "price not verified")])
def test_a_price_we_did_not_check_changes_nothing(tmp_path, hours_ago, verified, why):
    db, oid = seed_db(tmp_path)
    published(db, oid)
    checked(db, oid, 150.0, hours_ago=hours_ago, verified=verified)
    assert post_updates(db) == [], why


def test_only_published_posts_with_a_message(tmp_path):
    db, oid = seed_db(tmp_path)
    published(db, oid)
    db.conn.execute("UPDATE deal_alerts SET status='withdrawn'")
    db.conn.commit()
    checked(db, oid, 150.0)
    assert post_updates(db) == []


def test_the_update_line_comes_first_in_the_post():
    text = friendly_post({**CONTENT, "update": "⚠️ עדכון 25.11, 08:20: המחיר עלה ל-₪120, כלומר המבצע הסתיים."})
    assert text.startswith("<b>⚠️ עדכון") and "Acme Kettle" in text and "גילוי נאות" in text
    assert not friendly_post(CONTENT).startswith("<b>⚠️")


class FakeTG:
    def __init__(self, error=None):
        self.channel, self.admin_chat, self.edits, self.notes, self.error = "@deals_il", "42", [], [], error

    def edit_deal(self, chat_id, message_id, payload):
        if self.error:
            raise TelegramError(self.error)
        self.edits.append((chat_id, message_id, payload["content"].get("update")))
        return "caption"

    def notify_admin(self, text):
        self.notes.append(text)


def test_the_run_rewrites_the_post_and_tells_you_once(tmp_path, monkeypatch):
    from affiliate_scraper.commands import update_posts
    db, oid = seed_db(tmp_path)
    published(db, oid)
    checked(db, oid, 120.0)
    for cfg in ({"post_updates": {"enabled": False}},):
        tg = FakeTG()
        assert update_posts(cfg, db, tg) == [] and tg.edits == []
    monkeypatch.setenv("MECHIR_AGENT_RUN", "1")                   # the agent manager never edits the channel
    tg = FakeTG()
    assert update_posts({}, db, tg) == [] and tg.edits == []
    monkeypatch.delenv("MECHIR_AGENT_RUN")
    tg = FakeTG()
    [done] = update_posts({}, db, tg)
    assert tg.edits == [(-1001, 51, done["note"])] and len(tg.notes) == 1 and "עדכנתי 1 פוסטים" in tg.notes[0]
    tg = FakeTG()
    assert update_posts({}, db, tg) == [] and tg.notes == []      # nothing new: no edit, no message


def test_a_failed_edit_is_tried_again_but_a_deleted_post_is_not(tmp_path):
    from affiliate_scraper.commands import update_posts
    db, oid = seed_db(tmp_path)
    published(db, oid)
    checked(db, oid, 120.0)
    assert update_posts({}, db, FakeTG("Telegram editMessageCaption failed: 502 Bad Gateway")) == []
    assert len(post_updates(db)) == 1                             # still to do
    assert len(update_posts({}, db, FakeTG("Telegram editMessageCaption failed: 400 message to edit not found"))) == 1
    assert post_updates(db) == []                                 # remembered: not tried every run


def test_an_old_database_gets_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE deal_alerts (id INTEGER PRIMARY KEY, offer_id INTEGER NOT NULL, kind TEXT NOT NULL, "
                "fingerprint TEXT UNIQUE NOT NULL, price REAL, currency TEXT, deal_score REAL, affiliate_score REAL, "
                "title TEXT, content_json TEXT, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT, "
                "decided_at TEXT, published_at TEXT, notified_at TEXT, error TEXT)")
    con.commit()
    con.close()
    db = Database(path)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(deal_alerts)")}
    assert {"shown_price", "shown_state", "post_updated_at", "published_ref"} <= cols


def test_post_updates_command_lists(tmp_path, monkeypatch, capsys):
    from affiliate_scraper.cli import main
    db, oid = seed_db(tmp_path)
    published(db, oid)
    checked(db, oid, 120.0)
    db.close()
    (tmp_path / "automation.json").write_text(json.dumps({"db": str(tmp_path / "a.db")}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["post-updates"]) == 0
    out = capsys.readouterr().out
    assert "#1 (message 51, up)" in out and "1 post(s) to update" in out

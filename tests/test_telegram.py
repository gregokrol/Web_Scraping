"""Direct Telegram channel publishing (fake Bot API, no network)."""

import json

import pytest

import affiliate_scraper.telegram as tgmod
from affiliate_scraper.alerts import DealRules, decide, detect_deals, list_alerts, publish_approved
from affiliate_scraper.cli import main
from affiliate_scraper.content import build_content
from affiliate_scraper.telegram import CAPTION_LIMIT, TelegramError, TelegramPublisher, format_post

from .test_p2_automation import OFFER, seed_db


class R:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        return self._d


class Bot:
    """Fake Telegram Bot API session."""

    def __init__(self, replies=None):
        self.calls, self.replies = [], list(replies or [])

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json, url))
        if self.replies:
            return self.replies.pop(0)
        return R({"ok": True, "result": {"message_id": len(self.calls)}})


def pub(session, **kw):
    return TelegramPublisher(token="123:SECRET", channel="@deals_il", admin_chat="42", session=session,
                             min_interval=0, **kw)


def content():
    return build_content(OFFER, {"lowest_90d": True}, lang="he")


def test_format_post_html_escaped_and_complete():
    c = {**content(), "title": "Kettle <b>hack</b> & co"}
    text = format_post(c, 4096)
    assert "<b>Kettle &lt;b&gt;hack&lt;/b&gt; &amp; co</b>" in text
    assert 'href="https://a.example/k?ref=me"' in text and "גילוי נאות" in text
    assert "המחיר הנמוך ביותר ב-90 הימים האחרונים" in text


def test_long_caption_drops_claims_never_disclosure_or_link():
    c = {**content(), "claims": [{"text": "x" * 300, "basis": "b"}] * 10}
    text = format_post(c, CAPTION_LIMIT)
    assert len(text) <= CAPTION_LIMIT and "גילוי נאות" in text and "?ref=me" in text


def test_post_with_photo_and_button():
    bot = Bot()
    pub(bot).post_deal({"content": content(), "affiliate_url": "https://a.example/k?ref=me",
                        "image": "https://cdn.example/k.jpg"})
    method, body, url = bot.calls[0]
    assert method == "sendPhoto" and body["chat_id"] == "@deals_il" and body["photo"] == "https://cdn.example/k.jpg"
    assert body["parse_mode"] == "HTML" and len(body["caption"]) <= CAPTION_LIMIT
    assert body["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://a.example/k?ref=me"


def test_photo_failure_falls_back_to_text():
    bot = Bot([R({"ok": False, "error_code": 400, "description": "Bad Request: wrong file identifier/HTTP URL specified"}, 400)])
    pub(bot).post_deal({"content": content(), "affiliate_url": "https://a.example/k", "image": "https://x/broken.jpg"})
    assert [c[0] for c in bot.calls] == ["sendPhoto", "sendMessage"]


def test_rate_limit_retry_after(monkeypatch):
    monkeypatch.setattr(tgmod.time, "sleep", lambda s: None)
    bot = Bot([R({"ok": False, "error_code": 429, "description": "Too Many Requests",
                  "parameters": {"retry_after": 2}}, 429)])
    pub(bot).post_deal({"content": content(), "affiliate_url": "https://a.example/k"})
    assert [c[0] for c in bot.calls] == ["sendMessage", "sendMessage"]


def test_errors_do_not_leak_token():
    class Down:
        def post(self, url, **kw):
            import requests
            raise requests.ConnectionError(f"cannot reach {url}")
    with pytest.raises(TelegramError) as ei:
        TelegramPublisher(token="123:SECRET", channel="@c", session=Down(), min_interval=0)._api("getMe", {}, retries=0)
    assert "SECRET" not in str(ei.value)


def test_publish_approved_to_telegram_once(tmp_path):
    db, oid = seed_db(tmp_path)
    [a] = detect_deals(db, DealRules())
    bot = Bot()
    tg = pub(bot)
    assert publish_approved(db, tg.send_event)["published"] == 0       # not approved yet -> nothing posted
    decide(db, [a["id"]], True)
    assert publish_approved(db, tg.send_event)["published"] == 1
    assert publish_approved(db, tg.send_event)["published"] == 0       # never twice
    assert len([c for c in bot.calls if c[0] in ("sendMessage", "sendPhoto")]) == 1


def test_failed_telegram_post_marked_failed(tmp_path):
    db, oid = seed_db(tmp_path)
    [a] = detect_deals(db, DealRules())
    decide(db, [a["id"]], True)
    bot = Bot([R({"ok": False, "error_code": 403, "description": "Forbidden: bot is not a member of the channel"}, 403)])
    assert publish_approved(db, pub(bot).send_event) == {"published": 0, "failed": 1}
    [row] = list_alerts(db)
    assert row["status"] == "failed" and "not a member" in row["error"]


def test_pending_notification_goes_to_admin_chat():
    bot = Bot()
    pub(bot).send_event("deals.pending_approval", {"count": 1, "alerts": [
        {"id": 7, "title": "Kettle", "price": 90, "currency": "ILS", "deal_score": 80}]})
    method, body, _ = bot.calls[0]
    assert method == "sendMessage" and body["chat_id"] == "42" and "#7" in body["text"]
    with pytest.raises(TelegramError):
        TelegramPublisher(token="t", channel="@c", session=Bot(), min_interval=0).notify_admin("x")


def test_check_reports_admin_rights():
    me = {"ok": True, "result": {"id": 99, "username": "my_bot"}}
    chat = {"ok": True, "result": {"title": "Deals IL"}}
    admins = {"ok": True, "result": [{"user": {"id": 99}}]}
    info = pub(Bot([R(me), R(chat), R(admins)])).check()
    assert info == {"bot": "my_bot", "channel": "Deals IL", "bot_is_admin": True}


def test_publish_cli_requires_telegram_env(tmp_path, monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL"):
        monkeypatch.delenv(k, raising=False)
    assert main(["publish", "--telegram", "--db", str(tmp_path / "x.db")]) == 2


def test_automation_run_posts_to_telegram_channel(tmp_path, local_site, monkeypatch):
    """Full run: scrape -> detect -> approval request to admin chat -> approve -> one channel post."""
    from datetime import datetime, timedelta, timezone

    from affiliate_scraper.db import Database
    from .conftest import product_html

    monkeypatch.setattr(tgmod, "API", local_site.base)
    monkeypatch.setattr(tgmod, "MIN_INTERVAL", 0)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@deals_il")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT", "42")
    local_site.post_reply = b'{"ok": true, "result": {"message_id": 1}}'
    local_site.add("/products/k", product_html("Acme Kettle", "90", currency="ILS", gtin="4006381333931"))
    cfg = {"db": str(tmp_path / "t.db"), "targets": [local_site.base + "/products/k"], "delay": 0, "no_cache": True,
           "allow_private_hosts": True, "out_dir": str(tmp_path / "out"), "telegram": {"enabled": True},
           "deals": {"min_deal_score": 0}}
    (tmp_path / "automation.json").write_text(json.dumps(cfg), encoding="utf-8")
    assert main(["run"]) == 0
    db = Database(tmp_path / "t.db")
    oid = db.offer_id(local_site.base + "/products/k")
    now = datetime.now(timezone.utc)
    db.conn.executemany("INSERT INTO price_history(offer_id, observed_at, price, currency, price_verified) VALUES (?,?,?,?,1)",
                        [(oid, (now - timedelta(days=d)).isoformat(), 120, "ILS") for d in (28, 18, 9)])
    db.conn.commit()
    db.close()

    assert main(["run"]) == 0
    sent = [(p["path"].rsplit("/", 1)[-1], json.loads(p["body"])) for p in local_site.posts]
    assert [m for m, b in sent] == ["sendMessage"] and sent[0][1]["chat_id"] == "42"   # approval request only

    db = Database(tmp_path / "t.db")
    [alert] = list_alerts(db, "pending")
    db.close()
    assert main(["approve", str(alert["id"]), "--db", str(tmp_path / "t.db")]) == 0
    assert main(["run"]) == 0
    assert main(["run"]) == 0
    channel_posts = [json.loads(p["body"]) for p in local_site.posts if json.loads(p["body"]).get("chat_id") == "@deals_il"]
    assert len(channel_posts) == 1 and "גילוי נאות" in (channel_posts[0].get("caption") or channel_posts[0].get("text"))


def test_find_private_channel_id():
    updates = {"ok": True, "result": [
        {"update_id": 1, "my_chat_member": {"chat": {"id": -1001234567890, "title": "מחיר אמת", "type": "channel"},
                                            "new_chat_member": {"status": "administrator"}}},
        {"update_id": 2, "channel_post": {"chat": {"id": -1001234567890, "title": "מחיר אמת", "type": "channel"}}},
        {"update_id": 3, "message": {"chat": {"id": 42, "type": "private"}}},
    ]}
    [chat] = pub(Bot([R(updates)])).find_chats()
    assert chat == {"id": -1001234567890, "title": "מחיר אמת", "type": "channel", "username": "",
                    "bot_status": "administrator"}

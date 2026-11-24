"""Posts published before the 90-day rule was fixed: listed, and rewritten in place only with --apply."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from affiliate_scraper.alerts import posts_to_fix
from affiliate_scraper.telegram import TelegramError, TelegramPublisher

from .test_p2_automation import seed_db

OLD = {"title": "Acme Kettle", "cta": "לפרטים ורכישה", "disclosure": "גילוי נאות",
       "claims": [{"text": "מחיר: ₪90", "basis": "verified_price"},
                  {"text": "המחיר הנמוך ביותר ב-90 הימים האחרונים", "basis": "price_history_90d"}]}


def published(db, oid, content=OLD, msg=51):
    db.conn.execute("INSERT INTO deal_alerts(offer_id, kind, fingerprint, price, currency, title, content_json, status, "
                    "created_at, published_ref) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (oid, "lowest_90d", f"f{msg}", 90.0, "ILS", "Acme Kettle", json.dumps(content, ensure_ascii=False),
                     "published", datetime.now(timezone.utc).isoformat(),
                     json.dumps({"telegram": {"chat_id": -1001, "message_id": msg}})))
    db.conn.commit()


def test_only_posts_with_an_unfounded_90_day_line_are_listed(tmp_path):
    db, oid = seed_db(tmp_path)                        # ~30 days of checks
    published(db, oid)
    ok = {**OLD, "claims": OLD["claims"][:1]}
    published(db, oid, ok, msg=52)                     # nothing wrong in this one
    [t] = posts_to_fix(db, lambda aid: f"https://site.example/deals/{aid}.html")
    assert t["ref"]["message_id"] == 51
    bases = [c["basis"] for c in t["content"]["claims"]]
    assert "price_history_90d" not in bases and "price_history_since" in bases
    assert t["payload"]["page_url"].endswith(".html")


class Bot:
    def __init__(self, caption_error=None):
        self.calls, self.caption_error = [], caption_error

    def post(self, url, json=None, **kw):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        if method == "editMessageCaption" and self.caption_error:
            return SimpleNamespace(status_code=400, json=lambda: {"ok": False, "error_code": 400,
                                                                  "description": self.caption_error})
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {}})


def tg(bot):
    return TelegramPublisher(token="123:" + "A" * 35, channel="@deals_il", admin_chat="42", session=bot, min_interval=0)


def test_edit_keeps_the_buttons_and_handles_text_posts(tmp_path):
    payload = {"url": "https://a.example/k", "page_url": "https://site.example/deals/1.html",
               "content": {**OLD, "claims": OLD["claims"][:1]}}
    bot = Bot()
    assert tg(bot).edit_deal(-1001, 51, payload) == "caption"
    method, body = bot.calls[-1]
    assert method == "editMessageCaption" and "90 הימים" not in body["caption"]
    assert body["reply_markup"]["inline_keyboard"][0][1]["url"].endswith("1.html")     # the buttons stay
    bot = Bot("Bad Request: there is no caption in the message to edit")
    assert tg(bot).edit_deal(-1001, 51, payload) == "text" and bot.calls[-1][0] == "editMessageText"
    assert tg(Bot("Bad Request: message is not modified")).edit_deal(-1001, 51, payload) == "unchanged"


def test_the_agent_manager_cannot_edit_the_channel_even_by_numeric_id(monkeypatch):
    monkeypatch.setenv("MECHIR_AGENT_RUN", "1")
    bot = Bot()
    with pytest.raises(TelegramError):
        tg(bot).edit_deal(-1001, 51, {"content": OLD})
    assert bot.calls == []
    tg(bot).notify_admin("still allowed: your private chat")
    assert bot.calls[-1][1]["chat_id"] == "42"


def test_fix_posts_command_lists_then_applies(tmp_path, monkeypatch, capsys):
    from affiliate_scraper import telegram
    from affiliate_scraper.cli import main
    db, oid = seed_db(tmp_path)
    published(db, oid)
    db.close()
    (tmp_path / "automation.json").write_text(json.dumps({"db": str(tmp_path / "a.db")}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    edits = []

    class FakeTG:
        channel = "@deals_il"

        def edit_deal(self, chat_id, message_id, payload):
            edits.append((chat_id, message_id, payload))
            return "caption"

    monkeypatch.setattr(telegram, "TelegramPublisher", FakeTG)
    assert main(["fix-posts"]) == 0 and edits == []              # without --apply: only a list
    assert "#1" in capsys.readouterr().out
    assert main(["fix-posts", "--apply"]) == 0
    assert [(c, m) for c, m, _ in edits] == [(-1001, 51)]
    assert main(["fix-posts", "--apply"]) == 0 and len(edits) == 1   # fixed once, not again
    assert "No post needs fixing" in capsys.readouterr().out

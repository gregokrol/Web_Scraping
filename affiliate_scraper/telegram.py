"""Publish approved deals straight to a Telegram channel (Telegram Bot API) - no n8n needed.

Setup (one time):
1. In Telegram open @BotFather, send /newbot, copy the token.
2. Create your channel, add the bot as ADMIN with "Post messages" permission.
3. Set environment variables (never put the token in a file):

       setx TELEGRAM_BOT_TOKEN "123456:ABC..."
       setx TELEGRAM_CHANNEL "@my_deals_channel"         (or the numeric id -100...)
       setx TELEGRAM_ADMIN_CHAT "123456789"               (optional: YOUR chat id for approval requests)

Posting rules kept by this module:
* only approved deals are posted (alerts.publish_approved), each exactly once
* every post contains the price date, the data-backed claims and the affiliate disclosure
* Telegram limits (photo caption 1024 chars, message 4096) and 429 retry_after are respected
"""

from __future__ import annotations

import html
import logging
import os
import time
from typing import Optional

import requests

from .webhook import PublishError

log = logging.getLogger(__name__)

API = "https://api.telegram.org"


def API_BASE() -> str:  # noqa: N802 - looked up at call time so tests can point it at a local server
    return API
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
MIN_INTERVAL = 3.1   # Telegram allows ~20 posts/minute per channel; stay well below


class TelegramError(PublishError):
    pass


def format_post(content: dict, max_len: int) -> str:
    """Telegram-HTML post from build_content() output. Long posts drop claims, never the disclosure or link."""
    e = html.escape
    title = content.get("title", "")
    claims = [c["text"] for c in content.get("claims", [])]
    footer = []
    if content.get("store"):
        footer.append(f"🏬 {e(content['store'])}")
    footer.append(f'👉 <a href="{e(content.get("link", ""), quote=True)}">{e(content.get("cta") or "Link")}</a>')
    footer += ["", f"<i>{e(content.get('disclosure') or '')}</i>"]

    def build(head: str, items: list[str]) -> str:
        return "\n".join([head, *[f"• {e(c)}" for c in items], *footer])

    head = f"🔥 <b>{e(title)}</b>"
    text = build(head, claims)
    while len(text) > max_len and claims:
        claims.pop()  # claims are ordered most-important (price) first
        text = build(head, claims)
    if len(text) > max_len:  # extremely long title: shorten it, keep link + disclosure
        keep = max(10, len(title) - (len(text) - max_len) - 1)
        text = build(f"🔥 <b>{e(title[:keep])}…</b>", [])
    return text


class TelegramPublisher:
    def __init__(self, token: Optional[str] = None, channel: Optional[str] = None, admin_chat: Optional[str] = None,
                 session: Optional[requests.Session] = None, env: Optional[dict] = None,
                 min_interval: Optional[float] = None):
        e = env if env is not None else os.environ
        self.token = token or e.get("TELEGRAM_BOT_TOKEN", "")
        self.channel = channel or e.get("TELEGRAM_CHANNEL", "")
        self.admin_chat = admin_chat or e.get("TELEGRAM_ADMIN_CHAT", "")
        self.session = session or requests.Session()
        self.min_interval = MIN_INTERVAL if min_interval is None else min_interval
        self._last = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.token and self.channel)

    def _api(self, method: str, payload: dict, retries: int = 2) -> dict:
        if not self.token:
            raise TelegramError("set TELEGRAM_BOT_TOKEN (from @BotFather)")
        for attempt in range(retries + 1):
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.session.post(f"{API_BASE()}/bot{self.token}/{method}", json=payload, timeout=20)
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise TelegramError(f"Telegram unreachable: {str(exc).replace(self.token, '***')}") from None
            if data.get("ok"):
                return data.get("result") or {}
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if resp.status_code == 429 and retry_after and attempt < retries and retry_after <= 60:
                time.sleep(float(retry_after))
                continue
            raise TelegramError(f"Telegram {method} failed: {data.get('error_code')} {data.get('description')}")
        raise TelegramError("Telegram: retries exhausted")

    # ------------------------------------------------------------------ public
    def post_deal(self, payload: dict) -> dict:
        if not self.channel:
            raise TelegramError("set TELEGRAM_CHANNEL (e.g. @my_channel) and make the bot an admin of it")
        content = payload.get("content") or {}
        link = payload.get("affiliate_url") or payload.get("url") or content.get("link", "")
        content = {**content, "link": link}
        button = {"inline_keyboard": [[{"text": content.get("cta") or "🛒", "url": link}]]} if link.startswith("http") else None
        image = payload.get("image") or ""
        if image.startswith("https://"):
            msg = {"chat_id": self.channel, "photo": image, "caption": format_post(content, CAPTION_LIMIT),
                   "parse_mode": "HTML"}
            if button:
                msg["reply_markup"] = button
            try:
                return self._api("sendPhoto", msg)
            except TelegramError as exc:
                # e.g. the store's image URL is not fetchable by Telegram -> post text instead
                # (if the problem is permissions, the text post fails too and the error is reported)
                log.info("sendPhoto failed (%s) - posting text only", exc)
        msg = {"chat_id": self.channel, "text": format_post(content, MESSAGE_LIMIT), "parse_mode": "HTML",
               "link_preview_options": {"is_disabled": False}}
        if button:
            msg["reply_markup"] = button
        return self._api("sendMessage", msg)

    def notify_admin(self, text: str) -> dict:
        if not self.admin_chat:
            raise TelegramError("set TELEGRAM_ADMIN_CHAT (your own chat id) to receive approval requests")
        return self._api("sendMessage", {"chat_id": self.admin_chat, "text": text[:MESSAGE_LIMIT]})

    def send_event(self, event: str, data: dict) -> None:
        """Adapter for alerts.publish_approved / notify_pending."""
        if event == "deal.publish":
            self.post_deal(data)
        elif event == "deals.pending_approval":
            lines = [f"{data['count']} עסקאות ממתינות לאישור:"]
            for a in data["alerts"][:20]:
                lines.append(f"#{a['id']} {a['title'][:60]} - {a['price']} {a['currency']} (Deal {a['deal_score']})")
            lines.append("אישור: py main.py approve <id>  או ב-Dashboard")
            self.notify_admin("\n".join(lines))
        elif event == "crawl.errors" and self.admin_chat:
            self.notify_admin(f"⚠️ {len(data.get('runs', []))} ריצות עם שגיאות. פרטים: py main.py db errors")

    def check(self) -> dict:
        """Verify the token and the channel permissions (getMe + getChat)."""
        me = self._api("getMe", {})
        info = {"bot": me.get("username")}
        if self.channel:
            chat = self._api("getChat", {"chat_id": self.channel})
            info["channel"] = chat.get("title") or chat.get("username")
            admins = self._api("getChatAdministrators", {"chat_id": self.channel})
            info["bot_is_admin"] = any((a.get("user") or {}).get("id") == me.get("id") for a in admins)
        return info

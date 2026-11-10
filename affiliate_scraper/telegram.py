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
import json
import logging
import os
import re
import time
from pathlib import Path
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


TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,50}$")


def normalize_token(raw: str) -> str:
    """Fix common copy/paste slips: spaces, quotes, a leading 'bot' copied from a URL."""
    t = (raw or "").strip().strip('"').strip("'").strip()
    if t.lower().startswith("bot") and t[3:4].isdigit():
        t = t[3:]
    return t


def token_problem(token: str) -> Optional[str]:
    """Explain (without revealing it) why a token can't be right; None if the format looks valid."""
    if not token:
        return "TELEGRAM_BOT_TOKEN is empty - set it with setx and open a NEW CMD window"
    if TOKEN_RE.match(token):
        return None
    if any("\u0590" <= ch <= "\u05ff" for ch in token):
        return "the token contains Hebrew letters - the example text was saved instead of the real token"
    if " " in token:
        return "the token contains spaces - copy it again from @BotFather (tap the token to copy)"
    if ":" not in token:
        return "the token has no ':' - it should look like 1234567890:AAH... (copy it again from @BotFather)"
    head, _, tail = token.partition(":")
    if not head.isdigit():
        return "the part before ':' must be digits only - copy the token again from @BotFather"
    return f"the part after ':' looks incomplete ({len(tail)} characters, expected about 35) - copy it again"


def format_post(content: dict, max_len: int) -> str:
    """Telegram-HTML post from build_content() output. Long posts drop claims, never the disclosure or link."""
    e = html.escape
    title = content.get("title", "")
    claims = [c["text"] for c in content.get("claims", [])]
    footer = []
    if content.get("store"):
        footer.append(f"🏬 {e(content['store'])}")
    footer.append(f'👉 <a href="{e(content.get("link", ""), quote=True)}">{e(content.get("cta") or "Link")}</a>')
    if content.get("page_url", "").startswith("https://"):
        footer.append(f'📈 <a href="{e(content["page_url"], quote=True)}">היסטוריית המחיר וההשוואה</a>')
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
                 min_interval: Optional[float] = None, watermark: Optional[str] = None,
                 allow_private_images: bool = False):
        e = env if env is not None else os.environ
        self.token = normalize_token(token or e.get("TELEGRAM_BOT_TOKEN", ""))
        self.channel = channel or e.get("TELEGRAM_CHANNEL", "")
        self.admin_chat = admin_chat or e.get("TELEGRAM_ADMIN_CHAT", "")
        self.session = session or requests.Session()
        self.min_interval = MIN_INTERVAL if min_interval is None else min_interval
        self._last = 0.0
        # logo watermark on product photos: env TELEGRAM_WATERMARK = path to a logo, or "0" to disable;
        # default: assets/logo.png when it exists
        from .branding import DEFAULT_LOGO
        wm = watermark if watermark is not None else e.get("TELEGRAM_WATERMARK", "")
        if wm in ("0", "off", "false", "no"):
            self.watermark_logo = None
        else:
            path = Path(wm) if wm else DEFAULT_LOGO
            self.watermark_logo = path if path.is_file() else None
        self.allow_private_images = allow_private_images

    @property
    def configured(self) -> bool:
        return bool(self.token and self.channel)

    def _api(self, method: str, payload: dict, retries: int = 2, files: Optional[dict] = None) -> dict:
        if not self.token:
            raise TelegramError("set TELEGRAM_BOT_TOKEN (from @BotFather)")
        for attempt in range(retries + 1):
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                url = f"{API_BASE()}/bot{self.token}/{method}"
                if files:
                    # multipart upload: nested values (reply_markup) must be JSON strings
                    form = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                            for k, v in payload.items()}
                    resp = self.session.post(url, data=form, files=files, timeout=60)
                else:
                    resp = self.session.post(url, json=payload, timeout=20)
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
        page = payload.get("page_url") or ""
        if page.startswith("https://"):
            content = {**content, "page_url": page}
        row = [{"text": content.get("cta") or "🛒", "url": link}] if link.startswith("http") else []
        if page.startswith("https://"):
            row.append({"text": "📈 היסטוריית מחיר", "url": page})
        button = {"inline_keyboard": [row]} if row else None
        image = payload.get("image") or ""
        if image.startswith("https://"):
            msg = {"chat_id": self.channel, "photo": image, "caption": format_post(content, CAPTION_LIMIT),
                   "parse_mode": "HTML"}
            if button:
                msg["reply_markup"] = button
            branded = self._branded_photo(image)
            if branded:
                try:
                    upload = {k: v for k, v in msg.items() if k != "photo"}
                    return self._api("sendPhoto", upload, files={"photo": ("mechir-emet.jpg", branded, "image/jpeg")})
                except TelegramError as exc:
                    log.info("branded sendPhoto failed (%s) - trying the original image URL", exc)
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

    def _branded_photo(self, image_url: str) -> Optional[bytes]:
        """Product photo with the channel logo, or None (then the original URL is posted)."""
        if not self.watermark_logo:
            return None
        from .branding import BrandingError, download_image, watermark
        try:
            raw = download_image(image_url, allow_private=self.allow_private_images)
            return watermark(raw, self.watermark_logo)
        except BrandingError as exc:
            log.info("watermark skipped: %s", exc)
            return None

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

    def find_chats(self) -> list[dict]:
        """Channels/groups the bot was recently added to or saw posts in (from getUpdates).

        Private channels have no @username - this is how you get their numeric id (-100...).
        Telegram keeps updates for about 24 hours, so add the bot (or post in the channel) shortly before.
        """
        updates = self._api("getUpdates", {"allowed_updates": ["my_chat_member", "channel_post", "message"]})
        found: dict[int, dict] = {}
        for u in updates or []:
            for key in ("my_chat_member", "channel_post", "message"):
                chat = (u.get(key) or {}).get("chat")
                if chat and chat.get("type") in ("channel", "supergroup", "group"):
                    entry = {"id": chat["id"], "title": chat.get("title", ""), "type": chat["type"],
                             "username": chat.get("username", "")}
                    status = ((u.get("my_chat_member") or {}).get("new_chat_member") or {}).get("status")
                    if status:
                        entry["bot_status"] = status
                    found[chat["id"]] = {**found.get(chat["id"], {}), **entry}
        return list(found.values())

    def check(self) -> dict:
        """Verify the token and the channel permissions (getMe + getChat)."""
        problem = token_problem(self.token)
        if problem:
            raise TelegramError(f"bad TELEGRAM_BOT_TOKEN: {problem}")
        try:
            me = self._api("getMe", {})
        except TelegramError as exc:
            if "404" in str(exc) or "401" in str(exc):
                raise TelegramError("Telegram rejected the token (wrong or revoked). In @BotFather: /mybots -> "
                                    "your bot -> API Token, copy it again, run setx, open a NEW CMD window") from None
            raise
        info = {"bot": me.get("username")}
        if self.channel:
            chat = self._api("getChat", {"chat_id": self.channel})
            info["channel"] = chat.get("title") or chat.get("username")
            admins = self._api("getChatAdministrators", {"chat_id": self.channel})
            info["bot_is_admin"] = any((a.get("user") or {}).get("id") == me.get("id") for a in admins)
        return info

"""Send events to n8n (or any webhook receiver).

Events
------
``scrape.completed``       run summary + top products (export format for n8n)
``deals.pending_approval`` new deals waiting for YOUR approval - internal notification
``crawl.errors``           failures worth looking at
``deal.publish``           an APPROVED deal with ready-made content - n8n posts it (Telegram, WordPress...)

Only ``deal.publish`` should lead to anything public, and it is sent only for
deals you approved (``py main.py approve <id>`` or in the dashboard).

The URL comes from ``--webhook`` / config / env ``N8N_WEBHOOK_URL``. If env
``WEBHOOK_SECRET`` is set every request carries ``X-Signature-SHA256`` =
HMAC-SHA256(secret, body) so n8n can verify it came from you.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional
from urllib.parse import urlparse

import requests

from .models import now_iso

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    """Any failure to deliver to a publishing destination (webhook, Telegram...)."""


class WebhookError(PublishError):
    pass


def webhook_url(explicit: Optional[str] = None) -> str:
    return explicit or os.environ.get("N8N_WEBHOOK_URL", "")


def send_event(url: str, event: str, data: dict, session: Optional[requests.Session] = None,
               timeout: float = 15.0, retries: int = 2, secret: Optional[str] = None) -> dict:
    """POST ``{"event", "sent_at", "data"}`` as JSON. Raises WebhookError on failure."""
    parts = urlparse(url or "")
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise WebhookError("webhook URL must be http(s)://... (set --webhook or N8N_WEBHOOK_URL)")
    body = json.dumps({"event": event, "sent_at": now_iso(), "data": data}, ensure_ascii=False, default=str).encode()
    headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "AffiliateProductBot-webhook/3.0"}
    secret = secret if secret is not None else os.environ.get("WEBHOOK_SECRET", "")
    if secret:
        headers["X-Signature-SHA256"] = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    sess = session or requests.Session()
    last = ""
    for attempt in range(retries + 1):
        try:
            resp = sess.post(url, data=body, headers=headers, timeout=timeout, allow_redirects=False)
            if 200 <= resp.status_code < 300:
                return {"status": resp.status_code}
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code < 500 and resp.status_code != 429:
                break
        except requests.RequestException as exc:
            last = str(exc)
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise WebhookError(f"webhook failed: {last}")

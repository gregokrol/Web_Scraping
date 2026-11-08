"""Dynamic (JavaScript-rendered) product pages via Playwright.

Use only for sites whose robots.txt / terms allow crawling. The same rules as
plain HTTP apply: robots.txt, per-host delay, SSRF checks (also for every
sub-request the page makes), honest User-Agent. If the page shows a CAPTCHA or
bot challenge we record it and stop - it is never bypassed.

Requires the optional dependency:  ``py -m pip install playwright`` and
``py -m playwright install chromium`` (or set ``PLAYWRIGHT_CHROMIUM_EXECUTABLE``).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from ..fetcher import FetchError
from ..models import Product
from ..security import is_safe_url
from .base import AdapterContext
from .web import WebPageAdapter

log = logging.getLogger(__name__)

_CHALLENGE = re.compile(r"captcha|are you a robot|verify you are human|access denied|cf-challenge|"
                        r"attention required|unusual traffic", re.I)


class BrowserPageAdapter(WebPageAdapter):
    name = "browser"
    source_type = "browser"

    def __init__(self, ctx: AdapterContext, wait_ms: int = 1500, nav_timeout_ms: int = 30000):
        super().__init__(ctx)
        self.wait_ms = wait_ms
        self.nav_timeout_ms = nav_timeout_ms
        self._pw = None
        self._browser = None

    def _ensure_browser(self):
        if self._browser is not None:
            return self._browser
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed: py -m pip install playwright && "
                               "py -m playwright install chromium") from exc
        self._pw = sync_playwright().start()
        exe = self.ctx.browser_path or os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
        self._browser = self._pw.chromium.launch(executable_path=exe, headless=True)
        return self._browser

    def render(self, url: str) -> Optional[tuple[str, str]]:
        fetcher = self.ctx.fetcher
        if not is_safe_url(url, fetcher.allow_private):
            fetcher.errors.append(FetchError(url=url, kind="unsafe_url", message="blocked"))
            return None
        if fetcher.host_status(url):
            fetcher.errors.append(FetchError(url=url, kind="host_skipped", message=fetcher.host_status(url) or ""))
            return None
        if not fetcher.allowed(url):
            fetcher.errors.append(FetchError(url=url, kind="robots", message="robots.txt disallows this URL"))
            return None
        fetcher._throttle(url)

        browser = self._ensure_browser()
        context = browser.new_context(user_agent=fetcher.user_agent, java_script_enabled=True)
        page = context.new_page()

        def guard(route):
            req = route.request
            if req.resource_type in ("image", "media", "font"):
                return route.abort()
            if not is_safe_url(req.url, fetcher.allow_private):
                return route.abort()
            return route.continue_()

        page.route("**/*", guard)
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
            if self.wait_ms:
                page.wait_for_timeout(self.wait_ms)
            status = resp.status if resp else 0
            title = page.title()
            html = page.content()
            final_url = page.url
        except Exception as exc:  # playwright TimeoutError / navigation errors
            kind = "timeout" if "Timeout" in type(exc).__name__ else "browser_error"
            fetcher.errors.append(FetchError(url=url, kind=kind, message=str(exc)[:300]))
            return None
        finally:
            context.close()

        fetcher.stats["requests"] += 1
        if status in (403, 429) or _CHALLENGE.search(title or ""):
            fetcher.errors.append(FetchError(url=url, kind=f"http_{status}" if status in (403, 429) else "bot_challenge",
                                             message="blocked or challenged - not bypassed", status=status))
            fetcher._host_failed(url, "browser_block")
            return None
        if status and status != 200:
            fetcher.errors.append(FetchError(url=url, kind=f"http_{status}", status=status))
            return None
        if not is_safe_url(final_url, fetcher.allow_private):
            fetcher.errors.append(FetchError(url=url, kind="unsafe_redirect", message=final_url))
            return None
        fetcher.stats["ok"] += 1
        return final_url, html

    def fetch(self, target: str) -> list[Product]:
        rendered = self.render(target)
        if rendered is None:
            return []
        final_url, html = rendered
        fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        return self.products_from_page(target, final_url, html, fetched_at)

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None

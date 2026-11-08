"""HTTP fetching that respects robots.txt, crawl delays and server back-pressure."""

from __future__ import annotations

import gzip
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "AffiliateProductBot/2.0 (+https://github.com/gregokrol/Web_Scraping)"


@dataclass
class Page:
    url: str  # final URL after redirects
    status: int
    text: str
    content_type: str = ""


class PoliteFetcher:
    """A requests.Session wrapper that behaves like a well-mannered crawler.

    * identifies itself honestly (configurable User-Agent)
    * obeys robots.txt (Disallow + Crawl-delay) per host
    * waits ``min_delay`` seconds between requests to the same host
    * retries 429/5xx with exponential back-off and honours Retry-After
    * optional on-disk cache so re-runs don't hit the site again
    """

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        min_delay: float = 2.0,
        timeout: float = 20.0,
        respect_robots: bool = True,
        max_retries: int = 3,
        cache_dir: Optional[str | Path] = None,
        session: Optional[requests.Session] = None,
    ):
        self.user_agent = user_agent
        self.min_delay = min_delay
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8,he;q=0.6",
        })
        retry = Retry(
            total=max_retries,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self._robots: dict[str, Optional[RobotFileParser]] = {}
        self._last_hit: dict[str, float] = {}

    # ------------------------------------------------------------------ robots
    def _robots_for(self, url: str) -> Optional[RobotFileParser]:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        rp = RobotFileParser(origin + "/robots.txt")
        try:
            resp = self.session.get(origin + "/robots.txt", timeout=self.timeout)
            if resp.status_code in (401, 403):
                rp.disallow_all = True
            elif resp.status_code >= 400:
                rp.allow_all = True
            else:
                rp.parse(resp.text.splitlines())
        except requests.RequestException as exc:
            log.warning("Could not read robots.txt for %s (%s) - treating as allowed", origin, exc)
            rp.allow_all = True
        self._robots[origin] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self._robots_for(url)
        return rp.can_fetch(self.user_agent, url) if rp else True

    def sitemaps_from_robots(self, url: str) -> list[str]:
        rp = self._robots_for(url)
        return list(rp.site_maps() or []) if rp else []

    def _delay_for(self, url: str) -> float:
        delay = self.min_delay
        if self.respect_robots:
            rp = self._robots_for(url)
            crawl_delay = rp.crawl_delay(self.user_agent) if rp else None
            if crawl_delay:
                delay = max(delay, float(crawl_delay))
        return delay

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        wait = self._last_hit.get(host, 0) + self._delay_for(url) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    # ------------------------------------------------------------------ cache
    def _cache_path(self, url: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".html")

    # ------------------------------------------------------------------ fetch
    def get(self, url: str) -> Optional[Page]:
        if urlparse(url).scheme not in ("http", "https"):
            log.warning("Skipping non-http URL: %s", url)
            return None
        cache = self._cache_path(url)
        if cache and cache.exists():
            return Page(url=url, status=200, text=cache.read_text(encoding="utf-8"), content_type="text/html")
        if not self.allowed(url):
            log.info("robots.txt disallows %s - skipped", url)
            return None

        self._throttle(url)
        try:
            resp = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            log.warning("Request failed for %s: %s", url, exc)
            return None

        if resp.status_code != 200:
            log.warning("HTTP %s for %s", resp.status_code, url)
            return None
        if resp.content[:2] == b"\x1f\x8b":  # gzipped file (e.g. sitemap.xml.gz)
            try:
                text = gzip.decompress(resp.content).decode("utf-8", "replace")
            except OSError as exc:
                log.warning("Bad gzip payload from %s: %s", url, exc)
                return None
        else:
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding
            text = resp.text
        page = Page(url=resp.url, status=resp.status_code, text=text,
                    content_type=resp.headers.get("Content-Type", ""))
        if cache:
            cache.write_text(page.text, encoding="utf-8")
        return page

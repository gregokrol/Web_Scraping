"""HTTP fetching that respects robots.txt, crawl delays and server back-pressure.

Reliability features (v3):

* TTL page cache (``cache_ttl_hours``); every Page carries ``fetched_at`` so prices
  from the cache are never presented as fresh
* per-URL error log (``fetcher.errors``) - timeout, HTTP status, robots, unsafe URL...
* 429: honours ``Retry-After`` (capped); longer waits put the host on cool-down
* 403 / repeated failures: per-host circuit breaker - we stop hitting a site that
  doesn't want us instead of retrying (bot protection is never bypassed)
* every request and every redirect hop is checked against private/internal
  addresses (SSRF protection) and robots.txt
* response size limit
"""

from __future__ import annotations

import email.utils
import gzip
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse
from .robots import RobotsParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .policy import CrawlPolicy
from .security import UnsafeURLError, check_url

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "AffiliateProductBot/3.0 (+https://github.com/gregokrol/Web_Scraping)"
CACHE_FORMAT_VERSION = 2


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class TermsBlockedError(PermissionError):
    """The site's terms forbid automated collection (see policy.py)."""


@dataclass
class Page:
    url: str  # final URL after redirects
    status: int
    text: str
    content_type: str = ""
    fetched_at: str = field(default_factory=lambda: _utcnow().isoformat())
    from_cache: bool = False


@dataclass
class FetchError:
    url: str
    kind: str            # timeout | http_403 | http_429 | http_<n> | robots | unsafe_url | network | too_large | host_skipped ...
    message: str = ""
    status: Optional[int] = None
    at: str = field(default_factory=lambda: _utcnow().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


class PoliteFetcher:
    """A requests.Session wrapper that behaves like a well-mannered crawler."""

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        min_delay: float = 2.0,
        timeout: float = 20.0,
        respect_robots: bool = True,
        max_retries: int = 2,
        cache_dir: Optional[str | Path] = None,
        session: Optional[requests.Session] = None,
        cache_ttl_hours: float = 12.0,
        use_cache: bool = True,
        allow_private: bool = False,
        max_bytes: int = 8 * 1024 * 1024,
        max_retry_after: float = 60.0,
        max_host_failures: int = 3,
        connect_timeout: float = 10.0,
        policy: Optional[CrawlPolicy] = None,
    ):
        self.user_agent = user_agent
        self.min_delay = min_delay
        self.timeout = timeout
        self.connect_timeout = min(connect_timeout, timeout)
        self.respect_robots = respect_robots
        self.cache_ttl_hours = cache_ttl_hours
        self.use_cache = use_cache and cache_dir is not None and bool(str(cache_dir))
        self.cache_dir = Path(cache_dir) if self.use_cache else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.allow_private = allow_private
        self.max_bytes = max_bytes
        self.max_retry_after = max_retry_after
        self.max_host_failures = max_host_failures
        self.policy = policy or CrawlPolicy()   # sites whose terms forbid scraping (not bypassed by ignore-robots)

        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8,he;q=0.6",
        })
        # urllib3 retries only transient server errors / connection problems.
        # 429 is handled by us so a huge Retry-After can't freeze the run.
        retry = Retry(
            total=max_retries, connect=max_retries, read=max_retries,
            backoff_factor=1.5, status_forcelist=(500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"), respect_retry_after_header=False,
            raise_on_status=False, redirect=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self._robots: dict[str, RobotsParser] = {}
        self._last_hit: dict[str, float] = {}
        self._host_delay: dict[str, float] = {}
        self._host_failures: dict[str, int] = {}
        self._host_blocked: dict[str, str] = {}
        self._cooldown_until: dict[str, float] = {}
        self.errors: list[FetchError] = []
        self.stats = {"requests": 0, "cache_hits": 0, "ok": 0, "failed": 0}

    # ------------------------------------------------------------------ errors
    def _error(self, url: str, kind: str, message: str = "", status: Optional[int] = None) -> None:
        self.errors.append(FetchError(url=url, kind=kind, message=message[:300], status=status))
        self.stats["failed"] += 1
        log.warning("%s: %s %s", kind, url, message)

    def errors_for(self, url: str) -> list[FetchError]:
        return [e for e in self.errors if e.url == url]

    # ------------------------------------------------------------------ robots
    def _robots_for(self, url: str) -> RobotsParser:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        rp = RobotsParser(origin + "/robots.txt")
        try:
            resp = self._request(origin + "/robots.txt", check_robots=False)
            if resp is None:
                rp.allow_all = True
            elif resp.status_code in (401, 403):
                rp.disallow_all = True
            elif resp.status_code >= 400:
                rp.allow_all = True
            else:
                rp.parse(self._read_text(resp).splitlines())
        except (requests.RequestException, UnsafeURLError, ValueError) as exc:
            log.warning("Could not read robots.txt for %s (%s) - treating as allowed", origin, exc)
            rp.allow_all = True
        self._robots[origin] = rp
        return rp

    def allowed(self, url: str) -> bool:
        try:
            check_url(url, self.allow_private)
        except UnsafeURLError:
            return False
        if self.policy.blocked_reason(url):
            return False
        if not self.respect_robots:
            return True
        return self._robots_for(url).can_fetch(self.user_agent, url)

    def sitemaps_from_robots(self, url: str) -> list[str]:
        return list(self._robots_for(url).site_maps() or [])

    def _delay_for(self, url: str) -> float:
        host = urlparse(url).netloc
        delay = max(self.min_delay, self._host_delay.get(host, 0))
        if self.respect_robots:
            crawl_delay = self._robots_for(url).crawl_delay(self.user_agent)
            if crawl_delay:
                delay = max(delay, float(crawl_delay))
        return delay

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        wait = self._last_hit.get(host, 0) + self._delay_for(url) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    # ------------------------------------------------------------------ host health
    def host_status(self, url: str) -> Optional[str]:
        host = urlparse(url).netloc
        if host in self._host_blocked:
            return self._host_blocked[host]
        if self._cooldown_until.get(host, 0) > time.monotonic():
            return "rate_limited_cooldown"
        return None

    def _host_failed(self, url: str, reason: str) -> None:
        host = urlparse(url).netloc
        self._host_failures[host] = self._host_failures.get(host, 0) + 1
        if self._host_failures[host] >= self.max_host_failures and host not in self._host_blocked:
            self._host_blocked[host] = f"circuit_open after {self._host_failures[host]} failures ({reason})"
            log.warning("Stopping requests to %s: %s", host, self._host_blocked[host])

    def _host_ok(self, url: str) -> None:
        self._host_failures[urlparse(url).netloc] = 0

    # ------------------------------------------------------------------ cache
    def _cache_path(self, url: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")

    def _cache_load(self, url: str) -> Optional[Page]:
        path = self._cache_path(url)
        if not path or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("v") != CACHE_FORMAT_VERSION:
                return None
            fetched = datetime.fromisoformat(data["fetched_at"])
        except (OSError, ValueError, KeyError):
            return None
        age_h = (_utcnow() - fetched).total_seconds() / 3600
        if self.cache_ttl_hours <= 0 or age_h >= self.cache_ttl_hours:
            return None
        self.stats["cache_hits"] += 1
        return Page(url=data["url"], status=200, text=data["text"], content_type=data.get("content_type", ""),
                    fetched_at=data["fetched_at"], from_cache=True)

    def _cache_store(self, requested_url: str, page: Page) -> None:
        path = self._cache_path(requested_url)
        if not path:
            return
        try:
            path.write_text(json.dumps({"v": CACHE_FORMAT_VERSION, "url": page.url, "fetched_at": page.fetched_at,
                                        "content_type": page.content_type, "text": page.text}), encoding="utf-8")
        except OSError as exc:
            log.debug("cache write failed: %s", exc)

    # ------------------------------------------------------------------ low level
    def _request(self, url: str, check_robots: bool = True, max_redirects: int = 5) -> Optional[requests.Response]:
        """GET with manual, validated redirects. Raises UnsafeURLError / RequestException."""
        current = url
        for _ in range(max_redirects + 1):
            check_url(current, self.allow_private)
            reason = self.policy.blocked_reason(current)
            if reason:
                raise TermsBlockedError(f"{current}: {reason}")
            if check_robots and self.respect_robots and not self._robots_for(current).can_fetch(self.user_agent, current):
                raise PermissionError(f"robots.txt disallows {current}")
            self.stats["requests"] += 1
            resp = self.session.get(current, timeout=(self.connect_timeout, self.timeout),
                                    allow_redirects=False, stream=True)
            if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                resp.close()
                if not location:
                    return resp
                current = urljoin(current, location)
                continue
            return resp
        raise requests.TooManyRedirects(f"more than {max_redirects} redirects")

    def _read_body(self, resp: requests.Response, max_bytes: int) -> bytes:
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            resp.close()
            raise ValueError(f"response too large ({declared} bytes)")
        chunks, size = [], 0
        for chunk in resp.iter_content(64 * 1024):
            size += len(chunk)
            if size > max_bytes:
                resp.close()
                raise ValueError(f"response larger than {max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def _read_text(self, resp: requests.Response, max_bytes: Optional[int] = None) -> str:
        raw = self._read_body(resp, max_bytes or self.max_bytes)
        if raw[:2] == b"\x1f\x8b":  # gzipped file (e.g. sitemap.xml.gz)
            return gzip.decompress(raw).decode("utf-8", "replace")
        encoding = resp.encoding
        if not encoding or encoding.lower() == "iso-8859-1":
            resp._content = raw
            encoding = resp.apparent_encoding or "utf-8"
        return raw.decode(encoding, "replace")

    @staticmethod
    def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        if value.strip().isdigit():
            return float(value.strip())
        try:
            dt = email.utils.parsedate_to_datetime(value)
            return max(0.0, (dt - datetime.now(dt.tzinfo or timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ fetch
    def get(self, url: str, max_bytes: Optional[int] = None) -> Optional[Page]:
        """Fetch a URL politely. Returns None on any failure (recorded in ``self.errors``)."""
        try:
            check_url(url, self.allow_private)
        except UnsafeURLError as exc:
            self._error(url, "unsafe_url", str(exc))
            return None

        reason = self.policy.blocked_reason(url)
        if reason:
            self._error(url, "blocked_by_terms", reason)
            return None

        if self.use_cache:
            cached = self._cache_load(url)
            if cached:
                return cached

        blocked = self.host_status(url)
        if blocked:
            self._error(url, "host_skipped", blocked)
            return None
        if self.respect_robots and not self.allowed(url):
            self._error(url, "robots", "robots.txt disallows this URL")
            return None

        for attempt in range(3):
            self._throttle(url)
            try:
                resp = self._request(url)
            except TermsBlockedError as exc:
                self._error(url, "blocked_by_terms", str(exc))
                return None
            except PermissionError as exc:
                self._error(url, "robots", str(exc))
                return None
            except UnsafeURLError as exc:
                self._error(url, "unsafe_redirect", str(exc))
                return None
            except requests.Timeout as exc:
                self._error(url, "timeout", str(exc))
                self._host_failed(url, "timeout")
                return None
            except requests.TooManyRedirects as exc:
                self._error(url, "too_many_redirects", str(exc))
                return None
            except requests.RequestException as exc:
                # urllib3 retries wrap read timeouts in ConnectionError(MaxRetryError(ReadTimeoutError))
                kind = "timeout" if "timed out" in str(exc).lower() else "network"
                self._error(url, kind, str(exc))
                self._host_failed(url, kind)
                return None

            status = resp.status_code
            if status == 429:
                wait = self._retry_after_seconds(resp.headers.get("Retry-After"))
                resp.close()
                host = urlparse(url).netloc
                self._host_delay[host] = max(self._host_delay.get(host, self.min_delay), self.min_delay) * 2
                if wait is not None and wait > self.max_retry_after:
                    self._cooldown_until[host] = time.monotonic() + wait
                    self._error(url, "http_429", f"rate limited; Retry-After {wait:.0f}s - host paused", 429)
                    return None
                if attempt < 2:
                    time.sleep(wait if wait is not None else min(5.0 * (attempt + 1), self.max_retry_after))
                    continue
                self._error(url, "http_429", "rate limited (gave up after retries)", 429)
                self._host_failed(url, "429")
                return None

            if status == 403:
                bot_wall = (resp.headers.get("cf-mitigated") == "challenge"
                            or "captcha" in resp.headers.get("Content-Type", "").lower())
                resp.close()
                self._error(url, "http_403", "forbidden - not retried, bot protection is never bypassed", 403)
                if bot_wall:
                    self._host_blocked[urlparse(url).netloc] = "bot_protection_challenge"
                else:
                    self._host_failed(url, "403")
                return None

            if status != 200:
                resp.close()
                self._error(url, f"http_{status}", "", status)
                if status >= 500:
                    self._host_failed(url, str(status))
                return None

            try:
                text = self._read_text(resp, max_bytes)
            except (ValueError, OSError, requests.RequestException) as exc:
                self._error(url, "too_large" if "large" in str(exc) else "read_error", str(exc))
                return None

            self._host_ok(url)
            self.stats["ok"] += 1
            page = Page(url=resp.url or url, status=200, text=text,
                        content_type=resp.headers.get("Content-Type", ""))
            self._cache_store(url, page)
            return page
        return None

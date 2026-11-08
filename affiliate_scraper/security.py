"""Input safety: block requests to internal / private network addresses (SSRF) and malformed URLs.

Scraped pages, sitemaps and redirects can point anywhere - including
``http://127.0.0.1``, router admin pages or cloud metadata endpoints
(``169.254.169.254``). Every URL the crawler requests is checked here first,
and again after each redirect.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from functools import lru_cache
from urllib.parse import urlparse

MAX_URL_LENGTH = 2048
_BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet")
_CONTROL_CHARS = re.compile(r"[\x00-\x20\x7f]")


class UnsafeURLError(ValueError):
    pass


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified or getattr(ip, "is_site_local", False))


@lru_cache(maxsize=1024)
def _resolve(host: str) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return ()
    return tuple(sorted({info[4][0] for info in infos}))


def check_url(url: str, allow_private: bool = False) -> None:
    """Raise UnsafeURLError if ``url`` must not be requested."""
    if not isinstance(url, str) or not url:
        raise UnsafeURLError("empty URL")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURLError("URL too long")
    if _CONTROL_CHARS.search(url):
        raise UnsafeURLError("URL contains whitespace or control characters")
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError(f"scheme '{parts.scheme}' not allowed (http/https only)")
    if parts.username or parts.password:
        raise UnsafeURLError("credentials in URL are not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURLError("URL has no host")
    try:
        parts.port  # raises ValueError on garbage ports
    except ValueError as exc:
        raise UnsafeURLError(f"bad port: {exc}") from None
    if allow_private:
        return
    if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise UnsafeURLError(f"internal hostname '{host}' blocked")
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    addresses = [str(literal)] if literal else list(_resolve(host))
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError:
            continue
        if not _ip_is_public(ip):
            raise UnsafeURLError(f"'{host}' resolves to non-public address {addr}")
    # If DNS fails we let the HTTP request fail normally (and get logged).


def is_safe_url(url: str, allow_private: bool = False) -> bool:
    try:
        check_url(url, allow_private)
        return True
    except UnsafeURLError:
        return False


def safe_regex(pattern: str | None, max_len: int = 500) -> re.Pattern | None:
    """Compile a user regex with basic sanity limits."""
    if not pattern:
        return None
    if len(pattern) > max_len:
        raise ValueError("regex too long")
    try:
        return re.compile(pattern, re.I)
    except re.error as exc:
        raise ValueError(f"invalid regex {pattern!r}: {exc}") from None

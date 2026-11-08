"""P0: bug fixes, validation, cache TTL, 403/429/timeout handling, error log, SSRF, CLI flags."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from affiliate_scraper.cli import main
from affiliate_scraper.fetcher import PoliteFetcher
from affiliate_scraper.models import Product
from affiliate_scraper.pipeline import Filters, dedupe
from affiliate_scraper.security import UnsafeURLError, check_url
from affiliate_scraper.validation import normalize_gtin, validate_product

from .conftest import Route, product_html


def fetcher(**kw):
    kw.setdefault("min_delay", 0)
    kw.setdefault("allow_private", True)
    return PoliteFetcher(**kw)


# ---------------------------------------------------------------- in-stock filter (bug fix)
def test_in_stock_only_excludes_unknown_and_preorder():
    f = Filters(in_stock_only=True)
    known = Product(url="https://s.example/a", name="a", availability="https://schema.org/InStock")
    unknown = Product(url="https://s.example/b", name="b")
    oos = Product(url="https://s.example/c", name="c", availability="OutOfStock")
    pre = Product(url="https://s.example/d", name="d", availability="PreOrder")
    hebrew = Product(url="https://s.example/e", name="e", availability="במלאי")
    assert [p.name for p in (known, unknown, oos, pre, hebrew) if f.accept(p)] == ["a", "e"]


# ---------------------------------------------------------------- validation
def _fresh(**kw):
    kw.setdefault("url", "https://s.example/p")
    kw.setdefault("name", "Thing")
    kw.setdefault("source_type", "json-ld")
    kw.setdefault("source", "json-ld")
    return Product(**kw)


def test_fully_valid_product_is_verified():
    p = validate_product(_fresh(price=10, currency="usd", availability="InStock", gtin="036000291452"))
    assert p.price_verified and p.validation_status == "verified"
    assert p.currency == "USD" and p.gtin == "00036000291452"


def test_invalid_values_are_cleared_and_flagged():
    p = validate_product(_fresh(price=-5, currency="XYZ", rating=7, gtin="1234567890128", availability="maybe"))
    assert p.price is None and p.currency == "" and p.rating is None
    assert {"price_out_of_range", "currency_invalid", "rating_out_of_range", "availability_unknown"} <= set(p.validation_issues)
    assert not p.price_verified and p.validation_status == "unverified"


def test_bad_gtin_checksum_rejected():
    assert normalize_gtin("4006381333931") == "04006381333931"   # valid EAN-13
    assert normalize_gtin("4006381333932") == ""                 # wrong check digit
    assert normalize_gtin("0000000000000") == ""


def test_stale_price_not_verified():
    old = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    p = validate_product(_fresh(price=10, currency="EUR", availability="InStock", fetched_at=old), max_age_hours=24)
    assert "stale" in p.validation_issues and not p.price_verified


def test_html_parsed_price_is_unverified():
    p = validate_product(_fresh(price=10, currency="EUR", source_type="html", source="html"))
    assert "price_from_html_parsing" in p.validation_issues and not p.price_verified


def test_unknown_stock_gives_partial_status():
    p = validate_product(_fresh(price=10, currency="EUR"))
    assert p.price_verified and p.validation_status == "partial"


def test_dedupe_same_store_gtin_prefers_verified():
    a = validate_product(_fresh(url="https://s.example/a", price=10, currency="X", gtin="4006381333931"))
    b = validate_product(_fresh(url="https://s.example/a?variant=1", price=10, currency="EUR", gtin="4006381333931"))
    c = validate_product(_fresh(url="https://other.example/a", price=11, currency="EUR", gtin="4006381333931"))
    out = dedupe([a, b, c])
    assert len(out) == 2 and out[0].price_verified and out[0].currency == "EUR"


# ---------------------------------------------------------------- security
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost:8080/", "http://10.1.2.3/x", "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "file:///etc/passwd", "ftp://example.com/",
    "http://user:pw@example.com/", "http://printer.local/", "javascript:alert(1)", "http://exa mple.com/",
])
def test_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeURLError):
        check_url(url)


def test_public_ip_literal_allowed():
    check_url("http://93.184.216.34/product")


class _FakeResp:
    def __init__(self, status, headers=None, body=b""):
        self.status_code, self.headers, self._body = status, headers or {}, body
        self.url, self.encoding = "", "utf-8"
        self.is_redirect = status in (301, 302, 303, 307, 308)

    def iter_content(self, n):
        yield self._body

    def close(self):
        pass


class _FakeSession:
    def __init__(self, responses):
        self.responses, self.headers, self.calls = list(responses), {}, []

    def mount(self, *a):
        pass

    def get(self, url, **kw):
        self.calls.append(url)
        return self.responses.pop(0)


def test_redirect_to_internal_address_is_blocked():
    sess = _FakeSession([_FakeResp(302, {"Location": "http://169.254.169.254/latest/meta-data/"})])
    f = PoliteFetcher(min_delay=0, respect_robots=False, session=sess)
    assert f.get("http://93.184.216.34/product") is None
    assert f.errors[-1].kind == "unsafe_redirect"
    assert sess.calls == ["http://93.184.216.34/product"]  # the internal URL was never requested


# ---------------------------------------------------------------- cache TTL
def test_cache_ttl_and_fetched_at(local_site, tmp_path):
    local_site.add("/p", product_html("A", "1.00"))
    url = local_site.base + "/p"
    f = fetcher(cache_dir=tmp_path, cache_ttl_hours=1)
    first = f.get(url)
    second = f.get(url)
    assert local_site.count("/p") == 1 and second.from_cache and second.fetched_at == first.fetched_at

    expired = fetcher(cache_dir=tmp_path, cache_ttl_hours=0)
    assert not expired.get(url).from_cache
    assert local_site.count("/p") == 2

    off = fetcher(cache_dir=tmp_path / "off", use_cache=False)
    off.get(url)
    assert not (tmp_path / "off").exists() or not any((tmp_path / "off").iterdir())


# ---------------------------------------------------------------- 403 / 429 / timeout
def test_403_not_retried_and_circuit_breaker(local_site):
    for i in range(5):
        local_site.add(f"/x{i}", "no", status=403)
    f = fetcher(max_host_failures=3)
    for i in range(5):
        assert f.get(f"{local_site.base}/x{i}") is None
    kinds = [e.kind for e in f.errors]
    assert kinds[:3] == ["http_403"] * 3 and kinds[3:] == ["host_skipped"] * 2
    assert sum(local_site.count(f"/x{i}") for i in range(5)) == 3  # stopped hitting the site


def test_429_short_retry_after_then_success(local_site):
    local_site.add("/r", [Route("slow down", status=429, headers={"Retry-After": "0"}), Route(product_html("A", "1"))])
    f = fetcher()
    assert f.get(local_site.base + "/r") is not None
    assert local_site.count("/r") == 2


def test_429_long_retry_after_pauses_host(local_site):
    local_site.add("/a", "slow down", status=429, headers={"Retry-After": "3600"})
    local_site.add("/b", product_html("B", "1"))
    f = fetcher(max_retry_after=30)
    assert f.get(local_site.base + "/a") is None and f.errors[-1].kind == "http_429"
    assert f.get(local_site.base + "/b") is None and f.errors[-1].kind == "host_skipped"
    assert local_site.count("/b") == 0


def test_timeout_is_logged(local_site):
    local_site.add("/slow", "x", delay=2.0)
    f = fetcher(timeout=0.5, max_retries=0)
    assert f.get(local_site.base + "/slow") is None
    assert f.errors[-1].kind == "timeout"


def test_response_size_limit(local_site):
    local_site.add("/big", "x" * 5000)
    f = fetcher(max_bytes=1000)
    assert f.get(local_site.base + "/big") is None and f.errors[-1].kind == "too_large"


# ---------------------------------------------------------------- CLI flags
def _cli(local_site, tmp_path, *extra):
    return main([*extra, "--delay", "0", "--allow-private-hosts", "-o", str(tmp_path / "out"), "--name", "run",
                 "--cache-dir", str(tmp_path / "cache")])


def test_dry_run_fetches_no_product_pages_and_writes_nothing(local_site, tmp_path, capsys):
    local_site.add("/products/a", product_html("A", "5"))
    rc = _cli(local_site, tmp_path, local_site.base + "/products/a", "--dry-run")
    assert rc == 0
    assert local_site.count("/products/a") == 0
    assert not (tmp_path / "out").exists()
    assert "allowed" in capsys.readouterr().out


def test_no_cache_and_ttl_flags(local_site, tmp_path):
    local_site.add("/products/a", product_html("A", "5"))
    url = local_site.base + "/products/a"
    assert _cli(local_site, tmp_path, url) == 0
    assert _cli(local_site, tmp_path, url) == 0
    assert local_site.count("/products/a") == 1              # second run served from cache
    assert _cli(local_site, tmp_path, url, "--no-cache") == 0
    assert _cli(local_site, tmp_path, url, "--cache-ttl-hours", "0") == 0
    assert local_site.count("/products/a") == 3


def test_error_log_per_url_written(local_site, tmp_path):
    local_site.add("/products/ok", product_html("OK", "5"))
    local_site.add("/products/gone", "x", status=410)
    rc = _cli(local_site, tmp_path, local_site.base + "/products/ok", local_site.base + "/products/gone",
              local_site.base + "/products/missing")
    assert rc == 0
    log = (tmp_path / "out" / "run_errors.csv").read_text(encoding="utf-8-sig")
    assert "/products/gone,http_410" in log and "/products/missing,http_404" in log


def test_in_stock_only_cli(local_site, tmp_path):
    local_site.add("/products/a", product_html("In", "5", availability="InStock"))
    local_site.add("/products/b", product_html("Unknown", "5", availability=""))
    rc = _cli(local_site, tmp_path, local_site.base + "/products/a", local_site.base + "/products/b",
              "--in-stock-only", "--formats", "json")
    assert rc == 0
    data = json.loads((tmp_path / "out" / "run.json").read_text(encoding="utf-8"))
    assert [d["name"] for d in data] == ["In"]


def test_unsafe_input_url_rejected(tmp_path):
    assert main(["file:///etc/passwd", "-o", str(tmp_path)]) == 2
    assert main(["http://169.254.169.254/", "-o", str(tmp_path)]) == 2


def test_bad_regex_rejected(tmp_path):
    assert main(["https://93.184.216.34/", "--include", "([", "--dry-run"]) == 2

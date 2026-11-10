"""Sites whose terms forbid scraping are refused - even with --ignore-robots."""

import json

from affiliate_scraper.cli import main
from affiliate_scraper.fetcher import PoliteFetcher
from affiliate_scraper.policy import CrawlPolicy


class NoNetwork:
    headers = {}

    def mount(self, *a):
        pass

    def get(self, url, **kw):
        raise AssertionError(f"must not request {url}")


def test_ksp_blocked_by_default_without_any_request():
    f = PoliteFetcher(min_delay=0, session=NoNetwork(), respect_robots=False)
    for url in ("https://ksp.co.il/web/item/12345", "https://www.ksp.co.il/", "https://m.ksp.co.il/x"):
        assert f.get(url) is None
        assert f.errors[-1].kind == "blocked_by_terms" and "6.7" in f.errors[-1].message
        assert not f.allowed(url)
    assert CrawlPolicy().blocked_reason("https://notksp.co.il/") is None


def test_redirect_into_blocked_site_is_refused():
    class Redirect(NoNetwork):
        def get(self, url, **kw):
            class R:
                status_code, is_redirect = 302, True
                headers = {"Location": "https://ksp.co.il/web/item/1"}

                def close(self):
                    pass
            return R()
    f = PoliteFetcher(min_delay=0, session=Redirect(), respect_robots=False)
    assert f.get("https://93.184.216.34/go") is None
    assert f.errors[-1].kind == "blocked_by_terms"


def test_permission_and_extra_blocks_from_sources_json(tmp_path):
    p = tmp_path / "sources.json"
    p.write_text(json.dumps({"permitted_domains": ["ksp.co.il"], "blocked_domains": {"shop.example": "terms s.4"},
                             "stores": []}), encoding="utf-8")
    pol = CrawlPolicy.load(p)
    assert pol.blocked_reason("https://ksp.co.il/x") is None            # written permission received
    assert pol.blocked_reason("https://www.shop.example/p") == "terms s.4"
    p.write_text(json.dumps({"blocked_domains": ["other.example"]}), encoding="utf-8")
    assert CrawlPolicy.load(p).blocked_reason("https://other.example/") == "listed in blocked_domains"


def test_cli_refuses_and_dry_run_shows_reason(tmp_path, capsys):
    assert main(["https://ksp.co.il/web/item/1", "--dry-run", "--cache-dir", ""]) == 0
    assert "BLOCKED by site terms" in capsys.readouterr().out
    rc = main(["https://ksp.co.il/web/item/1", "--ignore-robots", "--no-db", "--cache-dir", "", "-o", str(tmp_path)])
    assert rc == 1   # nothing fetched, even with --ignore-robots
    log = next(tmp_path.glob("*_errors.csv")).read_text(encoding="utf-8-sig")
    assert "blocked_by_terms" in log

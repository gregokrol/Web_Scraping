"""Run the full CLI against a local HTTP server - no internet needed."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from affiliate_scraper.cli import main
from affiliate_scraper.fetcher import PoliteFetcher


def product_html(name, price, rating):
    ld = {"@type": "Product", "name": name, "image": "/i.jpg",
          "offers": {"price": price, "priceCurrency": "USD", "availability": "InStock"},
          "aggregateRating": {"ratingValue": rating, "reviewCount": 100}}
    return f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head></html>'


ROUTES = {
    "/robots.txt": "User-agent: *\nDisallow: /private/\nSitemap: {base}/sitemap_index.xml\n",
    "/sitemap_index.xml": ('<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                           "<sitemap><loc>{base}/sitemap_products_1.xml</loc></sitemap></sitemapindex>"),
    "/sitemap_products_1.xml": ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                "<url><loc>{base}/products/a</loc></url>"
                                "<url><loc>{base}/products/b</loc></url>"
                                "<url><loc>{base}/private/products/c</loc></url>"
                                "<url><loc>{base}/about</loc></url></urlset>"),
    "/products/a": product_html("Alpha", "10.00", 4.8),
    "/products/b": product_html("Beta", "99.00", 3.9),
    "/private/products/c": product_html("Secret", "1.00", 5),
}


@pytest.fixture()
def site():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = ROUTES.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            data = body.replace("{base}", base).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/xml" if self.path.endswith(".xml") else "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield base, hits
    server.shutdown()


def test_robots_disallow_is_respected(site):
    base, hits = site
    f = PoliteFetcher(min_delay=0)
    assert f.get(base + "/private/products/c") is None
    assert "/private/products/c" not in hits
    assert f.get(base + "/products/a") is not None


def test_cli_sitemap_discovery(site, tmp_path):
    base, hits = site
    cfg = tmp_path / "aff.json"
    cfg.write_text(json.dumps({"programs": {"127.0.0.1": {"params": {"ref": "me"}}}}))
    rc = main([base, "--discover", "sitemap", "--delay", "0", "--cache-dir", "", "-c", str(cfg),
               "-o", str(tmp_path), "--name", "out", "--formats", "csv,json,html,md", "--min-rating", "4"])
    assert rc == 0
    data = json.loads((tmp_path / "out.json").read_text())
    assert [d["name"] for d in data] == ["Alpha"]  # Beta filtered by rating, Secret blocked by robots
    assert data[0]["affiliate_url"].endswith("/products/a?ref=me")
    assert "/private/products/c" not in hits
    html = (tmp_path / "out.html").read_text()
    assert 'rel="sponsored nofollow noopener"' in html and "Disclosure" in html
    assert (tmp_path / "out.csv").read_text(encoding="utf-8-sig").startswith("url,name")

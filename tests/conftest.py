"""Shared fixtures: a programmable local HTTP server (no internet needed)."""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class Route:
    def __init__(self, body="", status=200, headers=None, delay=0.0, content_type="text/html; charset=utf-8"):
        self.body, self.status, self.headers, self.delay, self.content_type = body, status, headers or {}, delay, content_type


class LocalSite:
    def __init__(self):
        self.routes: dict[str, object] = {}
        self.hits: list[str] = []
        self.posts: list[dict] = []
        self.post_status: list[int] = []   # queue of status codes for POSTs (default 200)
        self.base = ""

    def events(self, name=None):
        evs = [json.loads(p["body"]) for p in self.posts]
        return [e for e in evs if name is None or e["event"] == name]

    def add(self, path, body="", **kw):
        self.routes[path] = body if callable(body) or isinstance(body, list) else Route(body, **kw)

    def count(self, path):
        return self.hits.count(path)


def product_html(name, price, currency="USD", availability="InStock", rating=None, reviews=None, gtin="",
                 brand="", extra=None):
    ld = {"@type": "Product", "name": name, "image": "/i.jpg",
          "offers": {"price": price, "priceCurrency": currency, "availability": availability}}
    if rating is not None:
        ld["aggregateRating"] = {"ratingValue": rating, "reviewCount": reviews or 0}
    if gtin:
        ld["gtin13"] = gtin
    if brand:
        ld["brand"] = brand
    ld.update(extra or {})
    return f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head></html>'


@pytest.fixture()
def local_site():
    site = LocalSite()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            site.hits.append(self.path)
            route = site.routes.get(self.path)
            if isinstance(route, list):  # sequence of responses
                route = route.pop(0) if len(route) > 1 else route[0]
            if callable(route):
                route = route()
            if route is None:
                self.send_response(404)
                self.end_headers()
                return
            if route.delay:
                time.sleep(route.delay)
            data = route.body.replace("{base}", site.base).encode() if isinstance(route.body, str) else route.body
            self.send_response(route.status)
            self.send_header("Content-Type", route.content_type)
            self.send_header("Content-Length", str(len(data)))
            for k, v in route.headers.items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            site.posts.append({"path": self.path, "headers": dict(self.headers), "body": body})
            status = site.post_status.pop(0) if site.post_status else 200
            self.send_response(status)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    site.base = f"http://127.0.0.1:{server.server_port}"
    site.add("/robots.txt", "User-agent: *\nAllow: /\n", content_type="text/plain")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield site
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _chromium_path(monkeypatch):
    """Use the preinstalled Chromium in CI containers if present."""
    if not os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") and os.path.exists("/opt/pw-browsers/chromium"):
        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "/opt/pw-browsers/chromium")


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """Default DB / cache / output paths are relative - keep them out of the repository."""
    monkeypatch.chdir(tmp_path)

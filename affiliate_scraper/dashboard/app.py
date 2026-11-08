"""FastAPI dashboard - runs locally on 127.0.0.1 by default.

Security for a local tool without login:
* binds to 127.0.0.1 unless you choose otherwise
* Host-header allow-list (blocks DNS-rebinding attacks from web pages)
* every state-changing request needs the per-process token embedded in the page
  (a malicious site cannot read it, so it cannot trigger crawls/publishing - CSRF)
* manual crawls go through exactly the same polite, robots-respecting pipeline,
  one crawl at a time
"""

from __future__ import annotations

import csv
import io
import json
import secrets
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from ..db import Database
from ..security import UnsafeURLError, check_url

STATIC = Path(__file__).parent / "static"
OFFER_EXPORT_FIELDS = ["id", "title", "store", "domain", "price", "currency", "shipping_cost", "availability",
                       "price_verified", "validation_status", "deal_score", "deal_status", "affiliate_score",
                       "affiliate_status", "category", "url", "affiliate_url", "last_fetched_at", "group_id"]


def _default_crawl_runner(argv: list[str]) -> int:
    from .. import cli
    return cli.main(argv)


def create_app(db_path: str, automation_config: str = "automation.json", allowed_hosts: Optional[list[str]] = None,
               crawl_runner: Optional[Callable[[list[str]], int]] = None, token: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="Web_Scraping dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token or secrets.token_urlsafe(24)
    allowed = {h.lower() for h in (allowed_hosts or ["127.0.0.1", "localhost"])}
    jobs: dict[str, dict] = {}
    crawl_lock = threading.Lock()
    runner = crawl_runner or _default_crawl_runner

    def db() -> Database:
        return Database(db_path)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        hostname = host[1:].split("]")[0] if host.startswith("[") else host.rsplit(":", 1)[0]
        if hostname not in allowed:
            return PlainTextResponse("Host not allowed", status_code=400)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not secrets.compare_digest(request.headers.get("x-dashboard-token", ""), app.state.token):
                return PlainTextResponse("Missing or bad dashboard token", status_code=403)
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src * data:; style-src 'self' 'unsafe-inline'; "
                                                   "script-src 'self' 'unsafe-inline'; connect-src 'self'")
        return resp

    # ------------------------------------------------------------------ page
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        return page.replace("__DASHBOARD_TOKEN__", app.state.token)

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    # ------------------------------------------------------------------ overview
    @app.get("/api/summary")
    def summary():
        d = db()
        try:
            runs = [dict(r) for r in d.query("SELECT id, started_at, finished_at, command, targets, products, errors, "
                                             "status FROM crawl_runs ORDER BY id DESC LIMIT 15")]
            problems = []
            for r in d.query("SELECT id, started_at, status, errors_json FROM crawl_runs "
                             "WHERE status <> 'ok' OR errors > 0 ORDER BY id DESC LIMIT 5"):
                problems.append({"run": r["id"], "at": r["started_at"], "status": r["status"],
                                 "errors": json.loads(r["errors_json"] or "[]")[:20]})
            pending = d.query("SELECT COUNT(*) n FROM deal_alerts WHERE status='pending'")[0]["n"]
            return {"stats": d.stats(), "runs": runs, "problems": problems, "pending_alerts": pending,
                    "jobs": list(jobs.values())[-10:]}
        finally:
            d.close()

    # ------------------------------------------------------------------ offers / products
    @app.get("/api/offers")
    def offers(q: str = "", category: str = "", store: str = "", sort: str = "deal", min_deal: float = 0,
               verified_only: bool = False, limit: int = 200):
        order = {"deal": "deal_score IS NULL, deal_score DESC", "affiliate": "affiliate_score IS NULL, affiliate_score DESC",
                 "price": "price IS NULL, price ASC", "recent": "last_seen DESC"}.get(sort, "deal_score DESC")
        sql, params = "SELECT * FROM offers WHERE 1=1", []
        if q:
            sql += " AND (title LIKE ? OR store LIKE ?)"
            params += [f"%{q}%", f"%{q}%"]
        if category:
            sql += " AND category = ?"
            params.append(category)
        if store:
            sql += " AND (store = ? OR domain = ?)"
            params += [store, store]
        if min_deal:
            sql += " AND deal_score >= ?"
            params.append(min_deal)
        if verified_only:
            sql += " AND price_verified = 1"
        d = db()
        try:
            rows = d.query(f"{sql} ORDER BY {order} LIMIT ?", (*params, max(1, min(limit, 1000))))
            return [dict(r) for r in rows]
        finally:
            d.close()

    @app.get("/api/categories")
    def categories():
        d = db()
        try:
            cats = [r[0] for r in d.query("SELECT DISTINCT category FROM offers WHERE category <> '' ORDER BY 1")]
            stores = [r[0] for r in d.query("SELECT DISTINCT store FROM offers WHERE store <> '' ORDER BY 1")]
            return {"categories": cats, "stores": stores}
        finally:
            d.close()

    @app.get("/api/offers/{offer_id}/history")
    def offer_history(offer_id: int):
        d = db()
        try:
            offer = d.query("SELECT * FROM offers WHERE id=?", (offer_id,))
            if not offer:
                raise HTTPException(404, "offer not found")
            from ..history import check_discount_claim, stats_for_offer
            st = stats_for_offer(d, offer_id)
            o = dict(offer[0])
            obs = [dict(r) for r in d.query("SELECT observed_at, price, currency, availability, price_verified, source "
                                            "FROM price_history WHERE offer_id=? ORDER BY observed_at", (offer_id,))]
            group = []
            if o.get("group_id"):
                group = [dict(r) for r in d.query("SELECT id, store, price, currency, shipping_cost, price_verified, "
                                                  "availability, url, affiliate_url, match_confidence FROM offers "
                                                  "WHERE group_id=? ORDER BY price", (o["group_id"],))]
            return {"offer": o, "stats": st.to_dict(), "observations": obs, "same_product": group,
                    "discount_check": check_discount_claim(st, o.get("price"), o.get("list_price"))}
        finally:
            d.close()

    @app.get("/api/comparisons")
    def comparisons(limit: int = 100):
        d = db()
        try:
            rows = d.query("SELECT group_id, MIN(title) title, COUNT(*) n_offers, COUNT(DISTINCT domain) n_stores, "
                           "MIN(price) min_price, MAX(price) max_price, MIN(currency) currency, "
                           "MIN(match_confidence) confidence FROM offers WHERE group_id <> '' AND price_verified=1 "
                           "GROUP BY group_id, currency HAVING n_stores >= 2 ORDER BY (max_price - min_price) / max_price DESC "
                           "LIMIT ?", (limit,))
            out = []
            for r in rows:
                r = dict(r)
                prices = sorted(x[0] for x in d.query("SELECT price FROM offers WHERE group_id=? AND currency=? "
                                                      "AND price_verified=1", (r["group_id"], r["currency"])))
                mid = len(prices) // 2
                r["median_price"] = prices[mid] if len(prices) % 2 else round((prices[mid - 1] + prices[mid]) / 2, 2)
                r["savings_vs_median_pct"] = round((r["median_price"] - r["min_price"]) / r["median_price"] * 100, 1)
                r["uncertain"] = (r["confidence"] or 1) < 0.95
                out.append(r)
            return out
        finally:
            d.close()

    # ------------------------------------------------------------------ sources
    @app.get("/api/sources")
    def list_sources():
        d = db()
        try:
            return [dict(r) for r in d.query("SELECT * FROM sources ORDER BY domain")]
        finally:
            d.close()

    @app.post("/api/sources")
    def save_source(body: dict = Body(...)):
        domain = str(body.get("domain", "")).strip().lower().removeprefix("www.")
        if not domain or "/" in domain or " " in domain:
            raise HTTPException(400, "domain required, e.g. shop.example.com")
        trust = body.get("trust_score")
        if trust not in (None, "") and not (0 <= float(trust) <= 1):
            raise HTTPException(400, "trust_score must be 0..1")
        start_urls = [u.strip() for u in body.get("start_urls", []) if str(u).strip()]
        for u in start_urls:
            try:
                check_url(u)
            except UnsafeURLError as exc:
                raise HTTPException(400, f"bad start URL {u}: {exc}") from None
        d = db()
        try:
            sid = d.upsert_source(domain, name=body.get("name") or domain, type=body.get("type", "web"),
                                  trust_score=float(trust) if trust not in (None, "") else None,
                                  shipping_cost=float(body["shipping_cost"]) if body.get("shipping_cost") not in (None, "") else None,
                                  config_json=json.dumps({"start_urls": start_urls, "discover": body.get("discover", "none"),
                                                          "include": body.get("include", "")}),
                                  enabled=int(bool(body.get("enabled", True))))
            return {"id": sid}
        finally:
            d.close()

    @app.delete("/api/sources/{source_id}")
    def delete_source(source_id: int):
        d = db()
        try:
            with d.tx() as c:
                c.execute("UPDATE offers SET source_id=NULL WHERE source_id=?", (source_id,))
                n = c.execute("DELETE FROM sources WHERE id=?", (source_id,)).rowcount
            return {"deleted": n}
        finally:
            d.close()

    # ------------------------------------------------------------------ affiliate programs
    @app.get("/api/programs")
    def list_programs():
        d = db()
        try:
            return [dict(r) for r in d.query("SELECT * FROM affiliate_programs ORDER BY domain")]
        finally:
            d.close()

    @app.post("/api/programs")
    def save_program(body: dict = Body(...)):
        domain = str(body.get("domain", "")).strip().lower().removeprefix("www.")
        if not domain:
            raise HTTPException(400, "domain required")
        params = body.get("params") or {}
        template = str(body.get("template") or "")
        if template and "{url" not in template:
            raise HTTPException(400, "template must contain {url} or {url_encoded}")
        if template and not template.startswith(("https://", "http://")):
            raise HTTPException(400, "template must be an http(s) URL")
        rate = body.get("commission_rate")
        if rate not in (None, "") and not (0 <= float(rate) <= 1):
            raise HTTPException(400, "commission_rate is a fraction 0..1 (e.g. 0.05 = 5%)")
        d = db()
        try:
            d.upsert_program(domain, network=body.get("network", ""), params_json=json.dumps(params), template=template,
                             commission_rate=float(rate) if rate not in (None, "") else None,
                             commission_verified=int(bool(body.get("commission_verified")) and rate not in (None, "")),
                             commission_source=body.get("commission_source", ""), notes=body.get("notes", ""))
            return {"ok": True}
        finally:
            d.close()

    @app.delete("/api/programs/{domain}")
    def delete_program(domain: str):
        d = db()
        try:
            with d.tx() as c:
                n = c.execute("DELETE FROM affiliate_programs WHERE domain=?", (domain,)).rowcount
            return {"deleted": n}
        finally:
            d.close()

    # ------------------------------------------------------------------ alerts
    @app.get("/api/alerts")
    def alerts(status: str = ""):
        from ..alerts import list_alerts
        d = db()
        try:
            rows = list_alerts(d, status or None)
            for r in rows:
                r["content"] = json.loads(r.pop("content_json") or "{}")
            return rows
        finally:
            d.close()

    @app.post("/api/alerts/{alert_id}/{action}")
    def decide_alert(alert_id: int, action: str):
        from ..alerts import decide
        if action not in ("approve", "reject"):
            raise HTTPException(400, "action must be approve or reject")
        d = db()
        try:
            n = decide(d, [alert_id], action == "approve")
            if not n:
                raise HTTPException(409, "alert is not pending")
            return {"ok": True}
        finally:
            d.close()

    @app.post("/api/publish")
    def publish():
        from ..alerts import publish_approved
        from ..webhook import webhook_url
        cfg = {}
        if Path(automation_config).exists():
            cfg = json.loads(Path(automation_config).read_text(encoding="utf-8"))
        target = webhook_url(cfg.get("webhook_url"))
        tg_cfg = cfg.get("telegram") or {}
        if tg_cfg is True or (isinstance(tg_cfg, dict) and tg_cfg.get("enabled")):
            from ..telegram import TelegramPublisher
            tg = TelegramPublisher()
            if not tg.configured:
                raise HTTPException(400, "Telegram enabled but TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL not set")
            target = tg.send_event
        if not target:
            raise HTTPException(400, "no destination (automation.json: telegram / webhook_url, or N8N_WEBHOOK_URL)")
        d = db()
        try:
            return publish_approved(d, target)
        finally:
            d.close()

    @app.post("/api/detect")
    def detect(body: dict = Body(default={})):
        from ..alerts import DealRules, detect_deals
        d = db()
        try:
            return {"created": detect_deals(d, DealRules(min_deal_score=float(body.get("min_deal", 70))))}
        finally:
            d.close()

    # ------------------------------------------------------------------ manual crawl
    @app.post("/api/crawl")
    def crawl(body: dict = Body(...)):
        urls = [str(u).strip() for u in body.get("urls", []) if str(u).strip()]
        if body.get("source_id"):
            d = db()
            try:
                row = d.query("SELECT config_json FROM sources WHERE id=?", (int(body["source_id"]),))
            finally:
                d.close()
            if not row:
                raise HTTPException(404, "source not found")
            cfg = json.loads(row[0]["config_json"] or "{}")
            urls += cfg.get("start_urls", [])
            body.setdefault("discover", cfg.get("discover", "none"))
            if cfg.get("include"):
                body.setdefault("include", cfg["include"])
        if not urls:
            raise HTTPException(400, "no URLs to crawl (add start URLs to the source)")
        for u in urls:
            try:
                check_url(u)
            except UnsafeURLError as exc:
                raise HTTPException(400, f"refusing {u}: {exc}") from None
        discover = body.get("discover", "none")
        if discover not in ("none", "sitemap", "links"):
            raise HTTPException(400, "bad discover mode")
        if not crawl_lock.acquire(blocking=False):
            raise HTTPException(409, "a crawl is already running - one at a time keeps us polite")
        argv = [*urls, "--discover", discover, "--db", db_path, "--max-pages", str(int(body.get("max_pages", 30))),
                "--name", "dashboard_" + datetime.now().strftime("%Y%m%d_%H%M%S")]
        if body.get("include"):
            argv += ["--include", str(body["include"])]
        job = {"id": uuid.uuid4().hex[:8], "status": "running", "urls": urls,
               "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "exit_code": None}
        jobs[job["id"]] = job

        def work():
            try:
                job["exit_code"] = runner(argv)
                job["status"] = "done" if job["exit_code"] == 0 else "finished_with_errors"
            except Exception as exc:  # report, don't crash the server
                job["status"], job["error"] = "failed", str(exc)[:300]
            finally:
                job["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                crawl_lock.release()

        threading.Thread(target=work, daemon=True).start()
        return job

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        if job_id not in jobs:
            raise HTTPException(404, "no such job")
        return jobs[job_id]

    # ------------------------------------------------------------------ export
    @app.get("/api/export")
    def export(format: str = "csv"):
        d = db()
        try:
            rows = [dict(r) for r in d.query("SELECT * FROM offers ORDER BY deal_score IS NULL, deal_score DESC")]
        finally:
            d.close()
        if format == "json":
            return JSONResponse(rows, headers={"Content-Disposition": "attachment; filename=offers.json"})
        buf = io.StringIO()
        buf.write("﻿")  # Excel: Hebrew-safe UTF-8
        w = csv.DictWriter(buf, fieldnames=OFFER_EXPORT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=offers.csv"})

    return app

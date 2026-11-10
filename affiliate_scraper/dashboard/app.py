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

import contextlib
import csv
import io
import json
import os
import re
import secrets
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from ..db import Database
from ..security import UnsafeURLError, check_url
from ..utils import domain_matches

STATIC = Path(__file__).parent / "static"
OFFER_EXPORT_FIELDS = ["id", "title", "store", "domain", "price", "currency", "shipping_cost", "availability",
                       "price_verified", "validation_status", "deal_score", "deal_status", "affiliate_score",
                       "affiliate_status", "category", "url", "affiliate_url", "last_fetched_at", "group_id"]


TASK_NAME = "WebScrapingAffiliate"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_PROGRESS = re.compile(r"^\s*\[(\d+)/(\d+)\]")
_STAGE = re.compile(r"^=== scrape: (\S+)")
_FETCHING = re.compile(r"^Fetching (\d+) target")
_STORE_DONE = re.compile(r"^--- store done (\d+)/(\d+)")


def _default_crawl_runner(argv: list[str]) -> int:
    from .. import cli
    return cli.main(argv)


def _default_schedule_query(task_name: str = TASK_NAME) -> dict:
    """Is the daily task registered, and when does it run next? (CSV columns: name, next run, status)."""
    if os.name != "nt":
        return {"supported": False, "exists": False}
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", task_name, "/FO", "CSV", "/NH"], capture_output=True,
                           text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return {"supported": True, "exists": False}
    if r.returncode != 0 or not r.stdout.strip():
        return {"supported": True, "exists": False}
    row = next(csv.reader(io.StringIO(r.stdout.strip().splitlines()[0])), [])
    return {"supported": True, "exists": True, "next_run": row[1] if len(row) > 1 else "",
            "status": row[2] if len(row) > 2 else ""}


class _JobLog(io.TextIOBase):
    """Collects what a job prints (last lines + progress) so the page can show it live."""

    def __init__(self, job: dict):
        self.job, self._buf = job, ""
        self._lock = threading.Lock()             # a search writes from one thread per store

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        with self._lock:
            self._buf += s
            *lines, self._buf = self._buf.split("\n")
            for line in lines:
                self._line(line.rstrip("\r"))
        return len(s)

    def _line(self, line: str) -> None:
        if not line.strip():
            return
        job = self.job
        m = _PROGRESS.match(line)
        if m:
            job["done"], job["total"] = int(m.group(1)), int(m.group(2))
            job["fetched"] = job.get("fetched", 0) + 1     # pages read, summed over stores running side by side
            return                                  # per-page lines would flood the log
        m = _FETCHING.match(line)
        if m:
            job["targets"] = job.get("targets", 0) + int(m.group(1))
        m = _STORE_DONE.match(line)
        if m:
            job["stores_done"] = int(m.group(1))
        m = _STAGE.match(line)
        if m:
            job["stage"] = (re.sub(r"^https?://(www\.)?", "", m.group(1)).split("/")[0])
            job["stages_done"] = job.get("stages_started", 0)
            job["stages_started"] = job.get("stages_started", 0) + 1
            job["done"], job["total"] = 0, 0
        job["log"].append(line[:300])


def create_app(db_path: str, automation_config: str = "automation.json", allowed_hosts: Optional[list[str]] = None,
               crawl_runner: Optional[Callable[[list[str]], int]] = None, token: Optional[str] = None,
               project_dir: Optional[str] = None, on_shutdown: Optional[Callable[[], None]] = None,
               schedule_query: Optional[Callable[[], dict]] = None) -> FastAPI:
    app = FastAPI(title="Web_Scraping dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token or secrets.token_urlsafe(24)
    allowed = {h.lower() for h in (allowed_hosts or ["127.0.0.1", "localhost"])}
    jobs: dict[str, dict] = {}
    crawl_lock = threading.Lock()
    runner = crawl_runner or _default_crawl_runner
    query_schedule = schedule_query or _default_schedule_query
    project = Path(project_dir or Path.cwd())
    # for the desktop launcher: when did an open page last talk to us, and is a crawl running?
    app.state.last_ping = None
    app.state.busy = crawl_lock.locked

    def start_job(kind: str, argv: list[str], **meta) -> dict:
        """Run one CLI command in the background - one at a time, like the polite crawler expects."""
        if not crawl_lock.acquire(blocking=False):
            raise HTTPException(409, "a crawl is already running - one at a time keeps us polite")
        job = {"id": uuid.uuid4().hex[:8], "kind": kind, "status": "running", "log": deque(maxlen=200),
               "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "exit_code": None, **meta}
        jobs[job["id"]] = job

        def work():
            sink = _JobLog(job)
            try:
                with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(sink):
                    job["exit_code"] = runner(argv)
                job["status"] = "done" if job["exit_code"] == 0 else "finished_with_errors"
            except Exception as exc:  # report, don't crash the server
                job["status"], job["error"] = "failed", str(exc)[:300]
            finally:
                sink.write("\n")
                job["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                crawl_lock.release()

        threading.Thread(target=work, daemon=True).start()
        return job_view(job)

    def job_view(job: dict) -> dict:
        return {**job, "log": list(job["log"])[-40:]}

    def db() -> Database:
        return Database(db_path)

    def config_stores() -> list:
        """The stores in the automation's sources.json ([] when there is none or it cannot be read)."""
        from ..commands import load_automation
        from ..sources import load_stores
        try:
            src = load_automation(automation_config).get("sources_config")
            return load_stores(src) if src and Path(src).exists() else []
        except (OSError, ValueError, TypeError):
            return []

    def store_names(stores: Optional[list] = None) -> dict:
        """domain -> the name given in sources.json (shops' own site names are often whole slogans)."""
        return {s.domain: s.name for s in (config_stores() if stores is None else stores) if s.name}

    def joined_programs() -> list[str]:
        """Domains set up in the affiliate-programs tab."""
        d = db()
        try:
            return [r["domain"] for r in d.query("SELECT domain FROM affiliate_programs")]
        finally:
            d.close()

    def affiliate_sql(stores: Optional[list] = None) -> tuple[str, list]:
        """A WHERE clause keeping offers from stores with an affiliate program ("0" when no store is known to have one)."""
        from ..commands import affiliate_domains
        doms = affiliate_domains(config_stores() if stores is None else stores, joined_programs())
        if not doms:
            return "0", []
        like = lambda dm: "%." + dm.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return ("(" + " OR ".join("domain = ? OR domain LIKE ? ESCAPE '\\'" for _ in doms) + ")",
                [v for dm in doms for v in (dm, like(dm))])

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        hostname = host[1:].split("]")[0] if host.startswith("[") else host.rsplit(":", 1)[0]
        if hostname not in allowed:
            return PlainTextResponse("Host not allowed", status_code=400)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not secrets.compare_digest(request.headers.get("x-dashboard-token", ""), app.state.token):
                return PlainTextResponse("Missing or bad dashboard token", status_code=403)
        if request.url.path.startswith("/api/"):
            app.state.last_ping = time.monotonic()
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
        icon = STATIC / "app.ico"
        if icon.exists():
            return Response(icon.read_bytes(), media_type="image/x-icon")
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
            compared = d.query("SELECT COUNT(*) n FROM (SELECT group_id FROM offers WHERE group_id <> '' "
                               "AND price_verified=1 GROUP BY group_id HAVING COUNT(DISTINCT domain) >= 2)")[0]["n"]
            tracked = d.query("SELECT COUNT(*) n, COUNT(DISTINCT domain) stores FROM offers")[0]
            stats = d.stats()
        finally:
            d.close()
        from ..commands import affiliate_domains
        stores = config_stores()
        return {"stats": stats, "runs": runs, "problems": problems, "pending_alerts": pending,
                "compared_products": compared, "tracked_offers": tracked["n"], "tracked_stores": tracked["stores"],
                "jobs": [job_view(j) for j in list(jobs.values())[-10:]], "can_shutdown": on_shutdown is not None,
                "store_names": store_names(stores), "affiliate_domains": affiliate_domains(stores, joined_programs())}

    # ------------------------------------------------------------------ offers / products
    @app.get("/api/offers")
    def offers(q: str = "", category: str = "", store: str = "", sort: str = "deal", min_deal: float = 0,
               verified_only: bool = False, affiliate_only: bool = False, limit: int = 200):
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
        if affiliate_only:
            clause, extra = affiliate_sql()
            sql += f" AND {clause}"
            params += extra
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
                group = [dict(r) for r in d.query("SELECT id, store, domain, price, currency, shipping_cost, price_verified, "
                                                  "availability, url, affiliate_url, match_confidence FROM offers "
                                                  "WHERE group_id=? ORDER BY price", (o["group_id"],))]
            return {"offer": o, "stats": st.to_dict(), "observations": obs, "same_product": group,
                    "discount_check": check_discount_claim(st, o.get("price"), o.get("list_price"))}
        finally:
            d.close()

    @app.get("/api/comparisons")
    def comparisons(limit: int = 100, affiliate_only: bool = False):
        # affiliate_only: compare only the stores with an affiliate program (2+ of them must have the product)
        clause, extra = affiliate_sql() if affiliate_only else ("1", [])
        d = db()
        try:
            rows = d.query("SELECT group_id, MIN(title) title, COUNT(*) n_offers, COUNT(DISTINCT domain) n_stores, "
                           "MIN(price) min_price, MAX(price) max_price, MIN(currency) currency, "
                           "MIN(match_confidence) confidence FROM offers WHERE group_id <> '' AND price_verified=1 "
                           f"AND {clause} GROUP BY group_id, currency HAVING n_stores >= 2 "
                           "ORDER BY (max_price - min_price) / max_price DESC LIMIT ?", (*extra, limit))
            out = []
            for r in rows:
                r = dict(r)
                r["offers"] = [dict(o) for o in d.query(
                    "SELECT id, store, domain, price, currency, url, affiliate_url, image, availability FROM offers "
                    f"WHERE group_id=? AND currency=? AND price_verified=1 AND {clause} ORDER BY price",
                    (r["group_id"], r["currency"], *extra))]
                r["image"] = next((o["image"] for o in r["offers"] if o["image"]), "")
                prices = [o["price"] for o in r["offers"]]
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

    @app.get("/api/affiliate-stores")
    def affiliate_stores():
        """Every store in sources.json with what is known about its affiliate program, plus programs you set up
        for domains that are not in sources.json."""
        from ..commands import affiliate_info
        stores, joined = config_stores(), joined_programs()
        out = [{"domain": s.domain, "name": s.name or s.domain, "searchable": bool(s.enabled and (s.search_url or s.search_sitemap)),
                "in_sources": True, **affiliate_info(s, joined)} for s in stores]
        known = [s.domain for s in stores]
        for j in sorted(joined):
            if not any(domain_matches(k, j) or domain_matches(j, k) for k in known):
                out.append({"domain": j, "name": j, "searchable": False, "in_sources": False, "status": "", "url": "", "network": "",
                            "note": "", "checked": "", "joined": True, "has_program": True})
        return out

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
        from ..alerts import decide, withdraw
        if action not in ("approve", "reject", "withdraw"):
            raise HTTPException(400, "action must be approve, reject or withdraw")
        d = db()
        try:
            if action == "withdraw":
                from ..telegram import TelegramPublisher
                tg = TelegramPublisher()
                [res] = withdraw(d, [alert_id], tg.delete_message if tg.token else None)
                if not res["ok"]:
                    raise HTTPException(404, "no such deal")
                return {"ok": True, "telegram": res["telegram"]}
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
        argv = [*urls, "--discover", discover, "--db", db_path, "--max-pages", str(int(body.get("max_pages", 30))),
                "--name", "dashboard_" + datetime.now().strftime("%Y%m%d_%H%M%S")]
        if body.get("include"):
            argv.append(f"--include={body['include']}")  # "=" form: a regex may start with "-"
        return start_job("crawl", argv, urls=urls)

    @app.post("/api/run")
    def run_automation():
        """The full automation (every store in sources.json, cross-store comparison, deal detection)."""
        if not Path(automation_config).exists():
            raise HTTPException(400, f"no automation config at {automation_config}")
        return start_job("run", ["run", "--config", automation_config])

    # ------------------------------------------------------------------ product search
    @app.post("/api/search")
    def search(body: dict = Body(...)):
        """Look for ANY product on the stores' own search pages (``search_url`` in sources.json)."""
        from ..commands import _seconds_floor, affiliate_info, clean_query, load_automation, search_stores
        try:
            q = clean_query(body.get("q", ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        try:
            stores = search_stores(load_automation(automation_config))
        except (OSError, ValueError) as exc:
            raise HTTPException(400, f"cannot read {automation_config}: {exc}") from None
        if not stores:
            raise HTTPException(400, "no store can be searched - add a search_url to the stores in sources.json")
        affiliate_only = bool(body.get("affiliate_only"))
        argv = ["search", q, "--config", automation_config]
        if affiliate_only:
            joined = joined_programs()
            stores = [s for s in stores if affiliate_info(s, joined)["has_program"]]
            if not stores:
                raise HTTPException(400, "no_affiliate_store: none of the searchable stores has a known affiliate "
                                         "program - see the affiliate programs tab")
            argv += [f"--store={s.domain}" for s in stores]
        since = _seconds_floor(datetime.now(timezone.utc).isoformat())
        return start_job("search", argv, q=q, since=since, affiliate_only=affiliate_only,
                         stores=[{"domain": s.domain, "name": s.name or s.domain} for s in stores])

    @app.get("/api/search/{job_id}")
    def search_results(job_id: str):
        job = jobs.get(job_id)
        if not job or job["kind"] != "search":
            raise HTTPException(404, "no such search")
        domains = [s["domain"] for s in job["stores"]]
        names = {s["domain"]: s["name"] for s in job["stores"]}
        cols = ("id, title, store, domain, price, list_price, currency, url, affiliate_url, image, availability, "
                "price_verified, last_seen, group_id, deal_score")
        d = db()
        try:
            found = [dict(r) for r in d.query(f"SELECT {cols} FROM offers WHERE last_seen >= ? AND domain IN "
                                              f"({','.join('?' * len(domains))}) ORDER BY price IS NULL, price",
                                              (job["since"], *domains))]
            groups_ids = sorted({o["group_id"] for o in found if o["group_id"]})
            # an affiliate-only search compares against earlier prices from affiliate stores only
            clause, extra = affiliate_sql() if job.get("affiliate_only") else ("1", [])
            known = [dict(r) for r in d.query(f"SELECT {cols} FROM offers WHERE price_verified=1 AND {clause} AND "
                                              f"group_id IN ({','.join('?' * len(groups_ids))})",
                                              (*extra, *groups_ids))] if groups_ids else []
        finally:
            d.close()
        found_ids = {o["id"] for o in found}
        for o in found:
            o["earlier"] = False
        for o in known:
            o["earlier"] = o["id"] not in found_ids      # known from an earlier scan, not seen in this search
        groups, grouped = [], set()
        for gid in groups_ids:
            offers = {o["id"]: o for o in [*found, *known] if o["group_id"] == gid and o["price_verified"]
                      and o["price"] is not None}
            by_currency: dict[str, list] = {}
            for o in offers.values():
                by_currency.setdefault(o["currency"], []).append(o)
            for cur, os_ in by_currency.items():
                if len({o["domain"] for o in os_}) < 2:
                    continue
                os_.sort(key=lambda o: o["price"])
                groups.append({"group_id": gid, "title": next(o["title"] for o in os_ if not o["earlier"]),
                               "image": next((o["image"] for o in os_ if o["image"]), ""), "currency": cur,
                               "min_price": os_[0]["price"], "max_price": os_[-1]["price"], "offers": os_})
                grouped.update(o["id"] for o in os_)
        groups.sort(key=lambda g: (g["max_price"] - g["min_price"]) / g["max_price"] if g["max_price"] else 0,
                    reverse=True)
        from ..commands import relevance
        for o in [*found, *known]:
            o["relevance"] = relevance(job["q"], o["title"], o["url"])
        groups.sort(key=lambda g: -max(o["relevance"] for o in g["offers"]))   # stable: savings order within
        singles = sorted((o for o in found if o["id"] not in grouped),
                         key=lambda o: (-o["relevance"], o["price"] is None, o["price"] or 0))
        counts = {dm: 0 for dm in domains}
        for o in found:
            counts[o["domain"]] = counts.get(o["domain"], 0) + 1
        return {"id": job_id, "q": job["q"], "status": job["status"], "affiliate_only": bool(job.get("affiliate_only")),
                "groups": groups,
                "offers": [o for o in singles if o["relevance"] > 0],
                "others": [o for o in singles if o["relevance"] == 0],   # the store returned them; none of the words
                "stores": [{"domain": dm, "name": names[dm], "found": counts[dm]} for dm in domains]}

    @app.get("/api/jobs")
    def list_jobs():
        return [job_view(j) for j in list(jobs.values())[-10:]]

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        if job_id not in jobs:
            raise HTTPException(404, "no such job")
        return job_view(jobs[job_id])

    # ------------------------------------------------------------------ schedule / local helpers
    @app.get("/api/schedule")
    def schedule_status():
        return query_schedule()

    @app.post("/api/schedule")
    def schedule_create(body: dict = Body(...)):
        at = str(body.get("at", "")).strip()
        if not TIME_RE.match(at):
            raise HTTPException(400, "time must be HH:MM (24h), e.g. 07:43")
        code = runner(["schedule", "--every", "daily", "--at", at, "--config", automation_config,
                       "--hidden", "--create"])
        if code != 0:
            raise HTTPException(500, "Task Scheduler refused the task (see the log)")
        return query_schedule()

    @app.delete("/api/schedule")
    def schedule_remove():
        code = runner(["schedule", "--remove"])
        if code != 0:
            raise HTTPException(500, "could not remove the scheduled task")
        return query_schedule()

    @app.post("/api/open-output")
    def open_output():
        out = project / "output"
        out.mkdir(exist_ok=True)
        if not hasattr(os, "startfile"):
            raise HTTPException(400, "opening folders is only supported on Windows")
        os.startfile(str(out))  # noqa: S606 - a fixed local folder, never user input
        return {"ok": True}

    @app.get("/api/ping")
    def ping():
        return {"ok": True, "busy": crawl_lock.locked()}

    @app.post("/api/shutdown")
    def shutdown():
        if on_shutdown is None:
            raise HTTPException(404, "started from the command line - close it there")
        threading.Timer(0.3, on_shutdown).start()
        return {"ok": True}

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

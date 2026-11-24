"""Scrape hooks (SQLite history + scores) and extra subcommands.

Registered with cli.py so the classic ``py main.py <urls>`` stays unchanged.
"""

from __future__ import annotations

import argparse
import csv
import os
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, unquote

from . import cli
from .db import DEFAULT_DB, Database
from .history import check_discount_claim, detect_price_drop, is_lowest_in_90d, stats_for_offer
from .scoring import apply_scores
from .utils import domain_matches, parse_int, parse_price


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


# ===================================================================== scrape hooks
def _db_args(ap) -> None:
    g = ap.add_argument_group("database & scores (v3)")
    g.add_argument("--db", default=DEFAULT_DB, help="SQLite file for price history (default: %(default)s)")
    g.add_argument("--no-db", action="store_true", help="Do not store results / history")


class _DbHook:
    """Saves every observed offer + price observation, then computes Deal / Affiliate scores."""

    def __init__(self, args, stores, config, fetcher):
        self.args, self.stores, self.config, self.fetcher = args, stores, config, fetcher
        self.db = None if args.no_db else Database(args.db)
        self.run_id = None
        if self.db:
            self.db.sync_stores(stores)
            self.db.sync_programs(config)
            for domain, prog in self.db.programs_as_config().items():
                config.programs.setdefault(domain, prog)
            self.run_id = self.db.start_run("scrape " + " ".join(args.urls[:5]), len(args.urls) + len(args.feed))

    def post_process(self, products, comparisons) -> None:
        if self.db:
            self.db.save_products(products, self.run_id)   # history includes the current observation
        apply_scores(products, comparisons, db=self.db, stores=self.stores, affiliate_config=self.config)
        if self.db:
            self.db.update_scores(products)

    def finish(self, result) -> None:
        if not self.db:
            return
        if result is None:
            self.db.finish_run(self.run_id, 0, list(self.fetcher.errors), self.fetcher.stats, status="failed")
        else:
            status = "ok" if result.products or not result.errors else "no_products"
            self.db.finish_run(self.run_id, result.raw_count, result.errors, self.fetcher.stats, status=status)
        self.db.close()


cli._SCRAPE_ARG_HOOKS.append(_db_args)
cli._SCRAPE_POST_HOOKS.append(lambda args, stores, config, fetcher: _DbHook(args, stores, config, fetcher))


# ===================================================================== db
def _db_build(ap) -> None:
    ap.add_argument("action", choices=["stats", "runs", "errors"], nargs="?", default="stats")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--limit", type=int, default=10)


def _db_run(args) -> int:
    if not Path(args.db).exists():
        _err(f"No database at {args.db} yet - run a scrape first.")
        return 1
    db = Database(args.db)
    if args.action == "stats":
        for k, v in db.stats().items():
            print(f"{k:20} {v}")
    elif args.action == "runs":
        for r in db.query("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT ?", (args.limit,)):
            print(f"#{r['id']:<4} {r['started_at']}  {r['status']:<11} products={r['products']} errors={r['errors']}  {r['command']}")
    else:
        row = db.query("SELECT errors_json FROM crawl_runs WHERE errors > 0 ORDER BY id DESC LIMIT 1")
        for e in json.loads(row[0]["errors_json"]) if row else []:
            print(f"{e['kind']:20} {e['url']}  {e.get('message', '')}")
    db.close()
    return 0


cli.register_subcommand("db", "Database statistics, recent crawl runs and errors", _db_build, _db_run)


# ===================================================================== history
def _history_build(ap) -> None:
    ap.add_argument("target", help="Offer URL or GTIN/EAN barcode")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true")


def _history_run(args) -> int:
    db = Database(args.db)
    from .validation import normalize_gtin
    if args.target.startswith(("http://", "https://")):
        rows = db.query("SELECT id, url, store, title, currency FROM offers WHERE url=?", (args.target,))
    else:
        g = normalize_gtin(args.target)
        rows = db.query("SELECT o.id, o.url, o.store, o.title, o.currency FROM offers o JOIN products p "
                        "ON o.product_id=p.id WHERE p.gtin=?", (g,)) if g else []
    if not rows:
        _err("No offers found for that URL / barcode.")
        return 1
    out = []
    for r in rows:
        st = stats_for_offer(db, r["id"])
        latest = db.query("SELECT price, list_price FROM price_history WHERE offer_id=? AND price_verified=1 "
                          "ORDER BY observed_at DESC LIMIT 1", (r["id"],))
        disc = check_discount_claim(st, latest[0]["price"], latest[0]["list_price"]) if latest else {"status": "no_claim"}
        obs = [dict(h) for h in db.history(r["id"], verified_only=False)]
        out.append({"url": r["url"], "store": r["store"], "title": r["title"], "currency": r["currency"],
                    "stats": st.to_dict(), "price_drop": detect_price_drop(st), "lowest_90d": is_lowest_in_90d(st),
                    "discount_check": disc, "observations": [{k: o[k] for k in ("observed_at", "price", "currency",
                                                               "availability", "price_verified")} for o in obs]})
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for o in out:
            s = o["stats"]
            print(f"\n{o['title']}  [{o['store']}]\n{o['url']}")
            print(f"  observations: {s['n_obs']} over {s['days_covered']} days  status: {s['status']}")
            if s["sufficient"]:
                print(f"  current {s['current_price']} {o['currency']} | min30 {s['min_30d']} | min90 {s['min_90d']} | "
                      f"median90 {s['median_90d']}")
            print(f"  discount claim: {o['discount_check']['status']}  lowest in 90d: {o['lowest_90d']}")
            if o["price_drop"]:
                print(f"  PRICE DROP {o['price_drop']['drop_pct']}%: {o['price_drop']['from']} -> {o['price_drop']['to']}")
    db.close()
    return 0


cli.register_subcommand("history", "Price history, 30/90-day lows and discount check for a URL or barcode",
                        _history_build, _history_run)


# ===================================================================== deals
def _deals_build(ap) -> None:
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--sort", choices=["deal", "affiliate"], default="deal")
    ap.add_argument("--min-deal", type=float, default=0)
    ap.add_argument("--min-affiliate", type=float, default=0)
    ap.add_argument("--days", type=float, default=2, help="Only offers seen in the last N days (default 2)")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json", action="store_true")


def latest_offers(db: Database, days: float = 2, min_deal: float = 0, min_affiliate: float = 0,
                  sort: str = "deal", limit: int = 50) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    col = "deal_score" if sort == "deal" else "affiliate_score"
    rows = db.query(f"SELECT * FROM offers WHERE last_seen >= ? AND deal_score IS NOT NULL AND deal_score >= ? "
                    f"AND (affiliate_score IS NULL OR affiliate_score >= ?) ORDER BY {col} IS NULL, {col} DESC LIMIT ?",
                    (since, min_deal, min_affiliate, limit))
    return [dict(r) for r in rows]


def _deals_run(args) -> int:
    if not Path(args.db).exists():
        _err(f"No database at {args.db} yet - run a scrape first.")
        return 1
    db = Database(args.db)
    rows = latest_offers(db, args.days, args.min_deal, args.min_affiliate, args.sort, args.limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(f"{'deal':>5} {'aff':>5}  {'price':>12}  title")
        for r in rows:
            aff = f"{r['affiliate_score']:.0f}" if r["affiliate_score"] is not None else "n/a"
            print(f"{r['deal_score']:5.0f} {aff:>5}  {r['price']:>9,.2f} {r['currency']:3}  {r['title'][:60]}  [{r['store']}]")
        if not rows:
            _err("No scored deals yet (scores need verified prices + market comparison or price history).")
    db.close()
    return 0


cli.register_subcommand("deals", "List current deals from the database by Deal Score or Affiliate Score",
                        _deals_build, _deals_run)


# ===================================================================== import performance
def _perf_build(ap) -> None:
    ap.add_argument("csv_file", help="Network report CSV with columns: url or domain, clicks, conversions[, revenue, currency]")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--source", default="", help="Label, e.g. 'Awin report Oct 2026'")


def _perf_run(args) -> int:
    path = Path(args.csv_file)
    if not path.is_file():
        _err(f"File not found: {path}")
        return 2
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            low = {k.lower().strip(): (v or "").strip() for k, v in r.items() if k}
            clicks = parse_int(low.get("clicks"))
            if clicks is None or not (low.get("url") or low.get("domain")):
                continue
            rows.append({"url": low.get("url", ""), "domain": low.get("domain", "").removeprefix("www."),
                         "clicks": clicks, "conversions": parse_int(low.get("conversions") or low.get("sales")) or 0,
                         "revenue": parse_price(low.get("revenue") or low.get("commission")),
                         "currency": low.get("currency", ""), "period_start": low.get("period_start", ""),
                         "period_end": low.get("period_end", "")})
    db = Database(args.db)
    n = db.import_performance(rows, args.source or path.name)
    db.close()
    print(f"Imported {n} performance row(s).")
    return 0 if n else 1


cli.register_subcommand("import-performance", "Import real click/conversion data from your affiliate network report",
                        _perf_build, _perf_run)


# ===================================================================== P2: webhook export on scrape
def _webhook_args(ap) -> None:
    ap.add_argument("--webhook", help="POST a scrape.completed event (JSON) to this URL, e.g. your n8n webhook. "
                                      "Env N8N_WEBHOOK_URL works too with --webhook env")


class _WebhookHook:
    def __init__(self, args):
        self.args = args

    def post_process(self, products, comparisons):
        pass

    def finish(self, result):
        from .webhook import WebhookError, send_event, webhook_url
        if result is None or not self.args.webhook:
            return
        url = webhook_url(None if self.args.webhook == "env" else self.args.webhook)
        data = {"products": [p.to_dict() for p in result.products[:100]],
                "comparisons": [c.to_dict() for c in result.comparisons if c.n_stores >= 2][:50],
                "errors": [e.to_dict() for e in result.errors[:100]],
                "counts": {"products": len(result.products), "errors": len(result.errors)}}
        try:
            send_event(url, "scrape.completed", data)
            _err("Webhook: scrape.completed sent.")
        except WebhookError as exc:
            _err(f"Webhook failed: {exc}")


cli._SCRAPE_ARG_HOOKS.append(_webhook_args)
cli._SCRAPE_POST_HOOKS.append(lambda args, stores, config, fetcher: _WebhookHook(args) if args.webhook else None)


# ===================================================================== P2: alerts / approval / publish
def _alerts_build(ap) -> None:
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--status", choices=["pending", "approved", "rejected", "published", "failed", "publishing"])
    ap.add_argument("--detect", action="store_true", help="Detect new deals now (creates pending alerts)")
    ap.add_argument("--min-deal", type=float, default=70)
    ap.add_argument("--lang", choices=["he", "en"], default="he")
    ap.add_argument("--show", type=int, help="Print the generated content of this alert id")


def _alerts_run(args) -> int:
    from .alerts import DealRules, detect_deals, list_alerts
    db = Database(args.db)
    if args.detect:
        new = detect_deals(db, DealRules(min_deal_score=args.min_deal, language=args.lang))
        _err(f"{len(new)} new deal alert(s) created (status: pending - approve before publishing).")
    if args.show:
        row = db.query("SELECT content_json, status FROM deal_alerts WHERE id=?", (args.show,))
        if not row:
            _err("No such alert.")
            return 1
        content = json.loads(row[0]["content_json"])
        print(content["telegram"])
        print("\nclaims & their data basis:")
        for c in content["claims"]:
            print(f"  - {c['text']}  [{c['basis']}]")
        return 0
    rows = list_alerts(db, args.status)
    print(f"{'id':>4}  {'status':10} {'kind':11} {'deal':>5} {'aff':>5}  {'price':>10}  title")
    for r in rows:
        aff = f"{r['affiliate_score']:.0f}" if r["affiliate_score"] is not None else "n/a"
        deal = f"{r['deal_score']:.0f}" if r["deal_score"] is not None else "n/a"
        print(f"{r['id']:>4}  {r['status']:10} {r['kind']:11} {deal:>5} {aff:>5}  {r['price']:>10,.2f}  {r['title'][:55]}")
    db.close()
    return 0


def _ids_build(ap) -> None:
    ap.add_argument("ids", nargs="+", type=int)
    ap.add_argument("--db", default=DEFAULT_DB)


def _decide(approve: bool):
    def run(args) -> int:
        from .alerts import decide
        db = Database(args.db)
        n = decide(db, args.ids, approve)
        db.close()
        print(f"{n} alert(s) {'approved' if approve else 'rejected'}.")
        return 0 if n else 1
    return run


def _publish_build(ap) -> None:
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--telegram", action="store_true",
                    help="Post to your Telegram channel (env TELEGRAM_BOT_TOKEN + TELEGRAM_CHANNEL)")
    ap.add_argument("--webhook", help="n8n webhook URL (default: env N8N_WEBHOOK_URL)")
    ap.add_argument("--auto-approve", action="store_true",
                    help="Publish ALL pending alerts without manual review (not recommended)")


def _publish_run(args) -> int:
    from .alerts import publish_approved
    from .webhook import webhook_url
    if args.telegram:
        from .telegram import TelegramPublisher
        tg = TelegramPublisher()
        if not tg.configured:
            _err("error: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL (see RUN_ON_WINDOWS.md, Telegram section)")
            return 2
        target = tg.send_event
    else:
        target = webhook_url(args.webhook)
        if not target:
            _err("error: no destination - use --telegram, or --webhook / N8N_WEBHOOK_URL")
            return 2
    db = Database(args.db)
    res = publish_approved(db, target, auto_approve=args.auto_approve)
    db.close()
    print(f"published: {res['published']}  failed: {res['failed']}")
    return 0 if not res["failed"] else 1


cli.register_subcommand("alerts", "List / detect deal alerts and show generated promo content", _alerts_build, _alerts_run)
cli.register_subcommand("approve", "Approve deal alerts for publishing", _ids_build, _decide(True))
cli.register_subcommand("reject", "Reject deal alerts", _ids_build, _decide(False))
cli.register_subcommand("publish", "Post APPROVED deals to Telegram (--telegram) or n8n. Each deal is published once",
                        _publish_build, _publish_run)


def _tgcheck_build(ap) -> None:
    ap.add_argument("--test-admin", action="store_true", help="Also send a test message to TELEGRAM_ADMIN_CHAT")
    ap.add_argument("--test-channel", action="store_true",
                    help="Post a short test message IN THE CHANNEL (delete it afterwards)")
    ap.add_argument("--find-channel", action="store_true",
                    help="List channels the bot was added to, with their numeric id (for private channels)")


def _tgcheck_run(args) -> int:
    from .telegram import TelegramError, TelegramPublisher
    tg = TelegramPublisher()
    if args.find_channel:
        try:
            chats = tg.find_chats()
        except TelegramError as exc:
            _err(f"Telegram problem: {exc}")
            return 1
        if not chats:
            _err("No channels found. Add the bot as an ADMIN of your channel, post any message in the channel, "
                 "then run this again (Telegram keeps these events for about 24 hours).")
            return 1
        for c in chats:
            name = f"@{c['username']}" if c.get("username") else "(private)"
            print(f"{c['title']}  {name}  id={c['id']}  type={c['type']}  bot={c.get('bot_status', '?')}")
        print('\nUse it with:  setx TELEGRAM_CHANNEL "<id>"   (then open a new CMD window)')
        return 0
    try:
        info = tg.check()
        print(f"bot: @{info['bot']}")
        if "channel" in info:
            print(f"channel: {info['channel']}  bot is admin: {info['bot_is_admin']}")
            if not info["bot_is_admin"]:
                _err("Add the bot as an administrator of the channel with permission to post messages.")
                return 1
        else:
            _err("TELEGRAM_CHANNEL is not set.")
            return 1
        if args.test_admin:
            tg.notify_admin("✅ Web_Scraping connected to Telegram")
            print("test message sent to admin chat")
        if args.test_channel:
            tg._api("sendMessage", {"chat_id": tg.channel,
                                    "text": "🔧 בדיקת חיבור - אפשר למחוק את ההודעה הזו"})
            print("test message posted in the channel - delete it there")
        return 0
    except TelegramError as exc:
        _err(f"Telegram problem: {exc}")
        return 1


cli.register_subcommand("telegram-check", "Check the Telegram bot token, channel and admin rights",
                        _tgcheck_build, _tgcheck_run)


# ===================================================================== P2: automation run
def _run_build(ap) -> None:
    ap.add_argument("--config", default="automation.json", help="Automation settings (see automation.example.json)")
    ap.add_argument("--dry-run", action="store_true", help="Show the plan; fetch no product pages, write nothing")
    ap.add_argument("-v", "--verbose", action="count", default=0)


def load_automation(path: str) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("automation config must be a JSON object")
    return cfg


def automation_scrape_jobs(cfg: dict, limited: Optional[dict] = None) -> list[list[str]]:
    """Build one scrape argv per store / target group (each becomes a crawl_run). ``limited``: stores that said
    429 lately - scanned too, only more slowly (slow_delay)."""
    common = _common_args(cfg)
    jobs = []
    if cfg.get("sources_config") and Path(cfg["sources_config"]).exists():
        from .sources import load_stores
        for s in load_stores(cfg["sources_config"]):
            if not s.enabled:
                continue
            if s.type == "feed" and s.feed_url:
                slow = slow_delay(cfg, s.domain, limited, s.delay)
                jobs.append(["--feed", s.feed_url, "--name", f"auto_{s.domain}", *common,
                             *(["--delay", f"{slow:g}"] if slow else [])])
            elif s.start_urls:
                argv = [*s.start_urls, "--discover", s.discover or "none", "--name", f"auto_{s.domain}", *common]
                if s.include:
                    argv.append(f"--include={s.include}")  # "=" form: a regex may start with "-"
                if s.link_selector:
                    argv.append(f"--link-selector={s.link_selector}")
                # per-store politeness and limits (later options override the common ones)
                if s.delay is not None:
                    argv += ["--delay", str(s.delay)]
                slow = slow_delay(cfg, s.domain, limited, s.delay)
                if slow:
                    argv += ["--delay", f"{slow:g}"]
                if s.max_pages:
                    argv += ["--max-pages", str(int(s.max_pages))]
                if s.max_listing_pages:
                    argv += ["--max-listing-pages", str(int(s.max_listing_pages))]
                jobs.append(argv)
    if cfg.get("targets"):
        argv = [*cfg["targets"], "--name", "auto_targets", *common]
        if cfg.get("discover"):
            argv += ["--discover", cfg["discover"]]
        jobs.append(argv)
    for feed in cfg.get("feeds", []):
        jobs.append(["--feed", feed, "--name", "auto_feed", *common])
    for q in cfg.get("amazon_searches", []):
        jobs.append(["--amazon-search", str(q), "--name", "auto_amazon", *common])
    for a in cfg.get("amazon_asins", []):
        jobs.append(["--amazon-asin", str(a), "--name", "auto_amazon", *common])
    for q in cfg.get("aliexpress_searches", []):
        jobs.append(["--aliexpress-search", str(q), "--name", "auto_aliexpress", *common])
    for g in cfg.get("ebay_gtins", []):
        jobs.append(["--ebay-gtin", str(g), "--name", "auto_ebay", *common])
    return jobs


def _common_args(cfg: dict) -> list[str]:
    """Scrape options every automation / search job shares (database, politeness, output, configs)."""
    common = ["--db", cfg.get("db", DEFAULT_DB), "--delay", str(cfg.get("delay", 3)),
              "--max-pages", str(cfg.get("max_pages", 100)), "-o", cfg.get("out_dir", "output"),
              "--formats", ",".join(cfg.get("formats", ["csv", "json"])),
              "--max-age-hours", str(cfg.get("max_age_hours", 24))]
    if cfg.get("affiliate_config"):
        common += ["-c", cfg["affiliate_config"]]
    if cfg.get("sources_config"):
        common += ["--sources", cfg["sources_config"]]
    if cfg.get("render") == "js":
        common += ["--render", "js"]
    if cfg.get("no_cache"):
        common.append("--no-cache")
    if cfg.get("allow_private_hosts"):  # only for testing against your own local store
        common.append("--allow-private-hosts")
    return common


# ===================================================================== product search
SEARCH_PER_STORE = 12


def clean_query(query: str) -> str:
    """One line of plain words; raises ValueError for empty, too long or control-character input."""
    q = " ".join(str(query).split())
    if not 2 <= len(q) <= 100:
        raise ValueError("search words must be 2-100 characters")
    if any(ord(ch) < 32 for ch in q):
        raise ValueError("search words contain control characters")
    return q


def relevance(query: str, *texts: str) -> float:
    """Share of the search words found in a result's title / URL (0..1) - see :func:`discovery.relevance`."""
    from .discovery import relevance as _relevance
    return _relevance(clean_query(query), *texts)


def joined_programs(db_path: str) -> list[str]:
    """Domains you set up yourself in the app's affiliate-programs tab (the affiliate_programs table)."""
    if not db_path or not Path(db_path).exists():
        return []
    db = Database(db_path)
    try:
        return [r["domain"] for r in db.query("SELECT domain FROM affiliate_programs")]
    finally:
        db.close()


def affiliate_info(store, joined: list[str]) -> dict:
    """What is known about one store's affiliate program: the facts in sources.json + whether you set one up."""
    a = store.affiliate or {}
    mine = any(domain_matches(store.domain, j) for j in joined)
    return {"status": a.get("status", ""), "url": a.get("url", ""), "network": a.get("network", ""),
            "note": a.get("note", ""), "checked": a.get("checked", ""), "joined": mine,
            "has_program": mine or store.has_affiliate}


def affiliate_domains(stores: list, joined: list[str]) -> list[str]:
    """Domains of stores with an affiliate program - known from sources.json or set up by you."""
    return sorted({s.domain for s in stores if affiliate_info(s, joined)["has_program"]}
                  | {j.lower().removeprefix("www.") for j in joined})


def search_stores(cfg: dict, only: Optional[list[str]] = None, affiliate_only: bool = False) -> list:
    """Enabled stores from the automation's sources_config that have a search_url.

    ``affiliate_only`` keeps the stores with an affiliate program (see :func:`affiliate_info`)."""
    from .sources import load_stores
    src = cfg.get("sources_config")
    if not src or not Path(src).exists():
        return []
    stores = [s for s in load_stores(src)
              if s.enabled and s.search is not False and (s.search_url or s.search_sitemap)
              and (not only or s.domain in only)]
    if affiliate_only:
        joined = joined_programs(cfg.get("db", DEFAULT_DB))
        stores = [s for s in stores if affiliate_info(s, joined)["has_program"]]
    return stores


def recent_429(cfg: dict) -> dict:
    """{domain: count} of "429 too many requests" answers in the last 3 days (rate_limit.hours) - long enough that
    a store does not flip between a slow day and a fast day that brings the 429 back. Reads an existing
    database only - a dry run or a first run creates nothing."""
    path = Path(cfg.get("db", DEFAULT_DB))
    if not path.exists():
        return {}
    from .health import rate_limited_hosts
    db = Database(str(path))
    try:
        return rate_limited_hosts(db, hours=float((cfg.get("rate_limit") or {}).get("hours", 72)))
    finally:
        db.close()


def slow_delay(cfg: dict, domain: str, limited: Optional[dict] = None,
               store_delay: Optional[float] = None) -> Optional[float]:
    """The delay between requests for a store that said "429 too many requests" lately: much slower
    (``rate_limit.slow_factor`` x the normal delay, at least ``rate_limit.min_delay`` seconds). None = normal."""
    if not limited:
        return None
    bare = lambda d: (d or "").lower().removeprefix("www.")  # noqa: E731
    if bare(domain) not in {bare(d) for d in limited}:
        return None
    rl = cfg.get("rate_limit") or {}
    base = float(store_delay if store_delay is not None else cfg.get("delay", 3))   # this store's own delay
    return max(base * float(rl.get("slow_factor", 5)), float(rl.get("min_delay", 15)))


def search_jobs(cfg: dict, query: str, per_store: int = SEARCH_PER_STORE,
                only: Optional[list[str]] = None, affiliate_only: bool = False,
                limited: Optional[dict] = None) -> list[tuple[str, list[str]]]:
    """One scrape argv per store: its own search page -> the first ``per_store`` result products.
    ``limited``: stores that said 429 lately (health.rate_limited_hosts) - searched too, only more slowly."""
    q = quote_plus(clean_query(query))
    jobs = []
    for s in search_stores(cfg, only, affiliate_only):
        slow = slow_delay(cfg, s.domain, limited, s.delay)
        slow_args = ["--delay", f"{slow:g}"] if slow else []          # the last --delay wins
        if s.search_sitemap:
            # the store's robots.txt forbids its search page: look for the words in its product sitemaps
            argv = [s.search_sitemap, "--discover", "sitemap", f"--match={clean_query(query)}",
                    "--name", f"search_{s.domain}", *_common_args(cfg), *slow_args, "--max-pages", str(int(per_store))]
            include = s.search_include or s.include
            if include:
                argv.append(f"--include={include}")
            jobs.append((s.domain, argv))
            continue
        argv = [s.search_url.replace("{q}", q), "--discover", "links", "--name", f"search_{s.domain}",
                *_common_args(cfg), *slow_args, "--max-pages", str(int(per_store)), "--max-listing-pages", "1"]
        # with result-card selector the cards ARE the products; else the product-URL regex decides
        include = s.search_include or ("." if s.search_selector else s.include)
        if include:
            argv.append(f"--include={include}")
        if s.search_selector:
            argv.append(f"--link-selector={s.search_selector}")
        jobs.append((s.domain, argv))
    return jobs


def _search_build(ap) -> None:
    ap.add_argument("query", nargs="+", help="What to look for: product words, a model number or a barcode")
    ap.add_argument("--config", default="automation.json", help="Automation settings (stores, database, delay)")
    ap.add_argument("--per-store", type=int, default=SEARCH_PER_STORE,
                    help="Read this many results from each store (default %(default)s)")
    ap.add_argument("--store", action="append", default=[], help="Search only this store domain (repeatable)")
    ap.add_argument("--affiliate-only", action="store_true",
                    help="Only stores with an affiliate program (affiliate in sources.json, or one you set up)")
    ap.add_argument("--parallel", type=int, default=6,
                    help="Stores searched at the same time (default %(default)s; the delay between requests is per "
                         "store, so each store still sees one request every few seconds; 1 = one after another)")
    ap.add_argument("--dry-run", action="store_true", help="Show which search pages would be read")
    ap.add_argument("-v", "--verbose", action="count", default=0)


def _search_run(args) -> int:
    try:
        query = clean_query(" ".join(args.query))
        cfg = load_automation(args.config)
    except (OSError, ValueError) as exc:
        _err(f"error: {exc}")
        return 2
    if not 1 <= args.per_store <= 50:
        _err("error: --per-store must be 1-50")
        return 2
    if not 1 <= args.parallel <= 16:
        _err("error: --parallel must be 1-16")
        return 2
    limited = recent_429(cfg)       # stores that said "429 too many requests" lately: still searched, only slower
    jobs = search_jobs(cfg, query, args.per_store, args.store or None, args.affiliate_only, limited)
    slowed = [d for d, j in jobs if "--delay" in j[j.index("--delay") + 2:]]
    if slowed:
        _err("slower (said 429 'too many requests' lately): " + ", ".join(slowed))
    if not jobs:
        src = cfg.get("sources_config") or "sources.json"
        if args.affiliate_only and search_stores(cfg, args.store or None):
            _err(f"error: none of the searchable stores has a known affiliate program - see 'affiliate' in {src} "
                 "or set one up in the app's affiliate programs tab")
        else:
            _err(f"error: no store can be searched - add a search_url to the stores in {src}")
        return 2
    if args.dry_run:
        for domain, j in jobs:
            print(f"would search {domain}: {j[0]}")
        return 0

    started = datetime.now(timezone.utc).isoformat()
    finished = []
    lock = threading.Lock()

    def one(job):
        domain, argv = job
        _err("\n=== scrape: " + argv[0])
        try:
            return cli.scrape_main([*argv, *(["-v"] * args.verbose)])
        except Exception as exc:  # one broken store must not lose the others' results
            _err(f"error: {domain}: {exc}")
            return 1
        finally:
            with lock:
                finished.append(domain)
                _err(f"--- store done {len(finished)}/{len(jobs)}: {domain}")

    # every store is a different host, and PoliteFetcher's delay is per host - so running the stores side by side
    # is as polite as one after another, just faster. Each job has its own fetcher and database connection (WAL).
    with ThreadPoolExecutor(max_workers=min(args.parallel, len(jobs))) as pool:
        codes = list(pool.map(one, jobs))

    since = _seconds_floor(started)
    db = Database(cfg.get("db", DEFAULT_DB))
    try:
        compare_across_stores(db, cfg, since=since, with_known_matches=True)
        rows = db.query("SELECT domain, title, url, price, currency, group_id FROM offers WHERE last_seen >= ? "
                        f"AND domain IN ({','.join('?' * len(jobs))}) ORDER BY price IS NULL, price",
                        (since, *[d for d, _ in jobs]))
    finally:
        db.close()
    rows.sort(key=lambda r: -relevance(query, r["title"], r["url"]))   # stable: cheapest first within a level
    stores = {r["domain"] for r in rows}
    _err(f"\nSearch {query!r}: {len(rows)} product(s) in {len(stores)} of {len(jobs)} store(s).")
    for r in rows[:15]:
        price = f"{r['price']:,.2f} {r['currency']}" if r["price"] is not None else "-"
        _err(f"  {price:>14}  {r['domain']:<20} {(r['title'] or '')[:60]}")
    # a store without this product is a normal answer, not an error; 2 = bad arguments / config
    return 2 if all(c == 2 for c in codes) else 0


cli.register_subcommand("search", "Search the stores' own search pages for any product and compare prices",
                        _search_build, _search_run)


def _seconds_floor(iso: str) -> str:
    """offers.last_seen has whole seconds; compare against the run start without its microseconds."""
    return datetime.fromisoformat(iso).replace(microsecond=0).isoformat()


def compare_across_stores(db: Database, cfg: dict, since: str, with_known_matches: bool = False) -> list:
    """Match and re-score every offer seen since ``since`` together.

    The automation scrapes each store in its own job, and a job can only compare the offers it fetched
    itself - so on its own, no store was ever compared with another. This pass runs after all jobs.
    """
    from .affiliate import AffiliateConfig
    from .matching import compare_prices
    from .sources import load_stores

    products = db.recent_products(since, with_known_matches=with_known_matches)
    if not products:
        return []
    src = cfg.get("sources_config")
    stores = load_stores(src) if src and Path(src).exists() else []
    aff = cfg.get("affiliate_config")
    config = AffiliateConfig.load(aff) if aff and Path(aff).exists() else AffiliateConfig()
    for domain, prog in db.programs_as_config().items():
        config.programs.setdefault(domain, prog)
    comparisons = compare_prices(products, min_confidence=float(cfg.get("match_confidence", 0.75)))
    apply_scores(products, comparisons, db=db, stores=stores, affiliate_config=config)
    db.update_scores(products)
    shared = [c for c in comparisons if c.n_stores >= 2]
    _err(f"\nCross-store comparison: {len(products)} offer(s), {len(shared)} product(s) found in 2+ stores.")
    return comparisons


def _run_run(args) -> int:
    from .remote import RUN_LOCK_PORT, single_instance
    lock = None if getattr(args, "dry_run", False) else single_instance(RUN_LOCK_PORT)
    if lock is None and not getattr(args, "dry_run", False):
        # the scheduled run while a manual one is still going (or the other way round): two runs would hit the
        # stores twice as often and could publish the same deal twice
        _err("Another automation run is still going on this computer - this one is skipped.")
        return 0
    try:
        return _run_once(args)
    finally:
        if lock is not None:
            lock.close()


def _run_once(args) -> int:
    from .alerts import DealRules, detect_deals, notify_pending, publish_approved
    from .webhook import PublishError, send_event, webhook_url
    try:
        cfg = load_automation(args.config)
    except (OSError, ValueError) as exc:
        _err(f"error: cannot read {args.config}: {exc}")
        return 2
    limited = recent_429(cfg)       # stores that said "429 too many requests" lately: still scanned, only slower
    jobs = automation_scrape_jobs(cfg, limited)
    searches = [str(q) for q in cfg.get("searches", []) if str(q).strip()]
    if not jobs and not searches:
        _err("error: nothing to do - add searches (menu 19), stores (sources_config) or targets/feeds "
             "to the automation config")
        return 2
    if args.dry_run:
        for j in jobs:
            print("would run: py main.py " + " ".join(j))
            cli.scrape_main([*j, "--dry-run"])
        for q in searches:
            print(f"would search all stores for: {q}")
            cli.main(["search", q, "--config", args.config, "--dry-run"])
        return 0

    started = datetime.now(timezone.utc).isoformat()
    codes = []
    for j in jobs:
        _err("\n=== scrape: " + " ".join(j[:3]) + (" ..." if len(j) > 3 else ""))
        codes.append(cli.scrape_main([*j, *(["-v"] * args.verbose)]))
    # saved searches: every run looks the words up in every searchable store, so new products and prices are
    # found automatically - no product links needed
    for q in searches:
        _err(f"\n=== search: {q}")
        codes.append(cli.main(["search", q, "--config", args.config, *(["-v"] * args.verbose)]))

    db = Database(cfg.get("db", DEFAULT_DB))
    if len(jobs) > 1:
        compare_across_stores(db, cfg, since=_seconds_floor(started))
    channel_drops = []
    if _channels_names(cfg):
        _err("\n=== stores' Telegram channels")
        fetch_channels(cfg, db, float(cfg.get("delay", 3)))
        from .channels import new_drops
        channel_drops = new_drops(db)
    rules = DealRules.from_dict(cfg.get("deals"))
    rules.language = cfg.get("content", {}).get("language", rules.language)
    rules.disclosure = cfg.get("content", {}).get("disclosure") or rules.disclosure
    new = detect_deals(db, rules)
    _err(f"\n{len(new)} new deal(s) detected.")

    # notifications to YOU go to every configured destination; public posts to exactly ONE
    # (two publish targets could double-post when one of them fails and the deal is retried)
    url = webhook_url(cfg.get("webhook_url"))
    tg_cfg = cfg.get("telegram") or {}
    tg_enabled = tg_cfg is True or (isinstance(tg_cfg, dict) and tg_cfg.get("enabled"))
    tg = None
    if tg_enabled:
        from .telegram import TelegramPublisher
        tg = TelegramPublisher()
        if not tg.configured:
            _err("Telegram enabled in config but TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL are not set.")
            tg = None
    notify_targets = [t for t in ((lambda ev, d: send_event(url, ev, d)) if url else None,
                                  tg.send_event if (tg and tg.admin_chat) else None) if t]
    publish_target = tg.send_event if tg else (url or None)

    def notify_all(event, data):
        for t in notify_targets:
            try:
                t(event, data)
            except PublishError as exc:
                _err(f"Notification problem: {exc}")

    auto = cfg.get("require_approval", True) is False
    smart_cfg = cfg.get("auto_publish") or {}
    smart_ids: list[int] = []
    if auto:
        # automatic mode (testing): everything is approved; you remove what you don't want afterwards
        from .alerts import decide
        decide(db, [r["id"] for r in db.query("SELECT id FROM deal_alerts WHERE status='pending'")], True)
    elif smart_cfg.get("enabled"):
        # smart mode: only a big verified saving goes out by itself, a few per store a day; the rest wait for you
        from .alerts import smart_approve
        smart_ids = smart_approve(db, float(smart_cfg.get("min_savings_pct", 15)),
                                  int(smart_cfg.get("per_store_per_day", 2)))
        if smart_ids:
            _err(f"Smart approval: {len(smart_ids)} deal(s) with a big verified saving approved automatically.")
    if notify_targets:
        n = notify_pending(db, notify_all)
        failed = db.query("SELECT id, command, errors, errors_json FROM crawl_runs WHERE started_at >= ? "
                          "AND (status <> 'ok' OR errors > 0)", (started,))
        if failed:
            notify_all("crawl.errors", {"runs": [{"id": r["id"], "command": r["command"], "errors": r["errors"],
                                                  "sample": json.loads(r["errors_json"] or "[]")[:10]} for r in failed]})
        _err(f"Notified you: {n} pending deal(s), {len(failed)} run(s) with errors.")
    from .site import SiteConfig, build_site, page_url, publish_site
    site_cfg = SiteConfig.from_dict(cfg.get("site"))
    site_on = bool((cfg.get("site") or {}).get("enabled"))
    if site_on:
        # the website goes first, so the deal page exists when the Telegram post links to it
        try:
            info = build_site(db, site_cfg)
            _err(f"Website built: {info['deals']} deal page(s) in {info['out_dir']}")
            if site_cfg.publish_dir or site_cfg.publish_branch:
                _err(f"Website: {publish_site(site_cfg)}")
        except RuntimeError as exc:
            _err(f"Website problem: {exc}")
    if publish_target:
        res = publish_approved(db, publish_target, auto_approve=auto,
                               page_url_for=(lambda aid: page_url(site_cfg, aid)) if site_on else None)
        auto_ids = res["ids"] if auto else [i for i in res["ids"] if i in smart_ids]
        if auto_ids and tg and tg.admin_chat:
            res = {**res, "ids": auto_ids}
            head = (f"✅ פורסמו {len(auto_ids)} עסקאות אוטומטית" + (
                f" (חיסכון מאומת של {smart_cfg.get('min_savings_pct', 15):g}% ומעלה, עד "
                f"{smart_cfg.get('per_store_per_day', 2)} לחנות ביום):" if not auto else ":"))
            lines = [head]
            for aid in res["ids"][:20]:
                t = db.query("SELECT title, price, currency FROM deal_alerts WHERE id=?", (aid,))[0]
                lines.append(f"#{aid} {t['title'][:55]} - {t['price']} {t['currency']}")
            lines.append("להסרה מהערוץ ומהאתר: תפריט 24, או py main.py withdraw <מספר>")
            try:
                tg.notify_admin("\n".join(lines))
            except PublishError as exc:
                _err(f"Notification problem: {exc}")
        if res["published"] or res["failed"]:
            _err(f"Published to {'Telegram' if tg else 'n8n'}: {res}" + (" (auto-approved)" if auto else ""))
    else:
        _err("No publishing destination configured - deals wait in the dashboard / `py main.py alerts`.")
    # daily summary to share in WhatsApp / Facebook groups: once a day, to your private chat
    dcfg = cfg.get("digest") or {}
    if dcfg.get("daily", True) and tg and tg.admin_chat:
        from .digest import build_digest
        from .health import due, mark_sent
        if due(db, key="digest_sent_at"):
            text = build_digest(db, site_cfg, count=int(dcfg.get("count", 5)), hours=float(dcfg.get("hours", 48)))
            if text:
                try:
                    tg.notify_admin("📋 סיכום יומי לשיתוף - מעתיקים את ההודעה הבאה כמו שהיא:")
                    tg.notify_admin(text)
                    mark_sent(db, key="digest_sent_at")
                    _err("Daily summary to share sent to your Telegram chat.")
                except PublishError as exc:
                    _err(f"Notification problem: {exc}")
    channel_drops = auto_channel_posts(cfg, db, tg, channel_drops)
    # a product posted again cheaper in a store's channel: told to you right away, once
    if channel_drops and tg and tg.admin_chat:
        from .channels import drop_text, mark_drops_told
        told = []
        for r in channel_drops[:10]:
            try:
                tg.send_buttons(tg.admin_chat, drop_text(db, r),
                                [("✅ פרסם בערוץ", f"cpost:{r['id']}"), ("✖️ דלג", f"cskip:{r['id']}")])
                told.append(r["id"])
            except PublishError as exc:
                _err(f"Notification problem: {exc}")
                break
        mark_drops_told(db, told)
    # leads from the stores' channels: once a day to your private chat, for your own check
    if _channels_names(cfg) and tg and tg.admin_chat:
        from .channels import leads_text
        from .health import due, mark_sent
        if due(db, key="leads_sent_at"):
            text = leads_text(db)
            if text:
                try:
                    tg.send_long(tg.admin_chat, text)
                    mark_sent(db, key="leads_sent_at")
                except PublishError as exc:
                    _err(f"Notification problem: {exc}")
    # daily self-check: one report a day to your private chat (twice-daily runs still send one)
    if (cfg.get("health") or {}).get("daily", True) and tg and tg.admin_chat:
        from .health import due, format_report, mark_sent, run_health
        if due(db):
            report = format_report(run_health(db, cfg, tg))
            try:
                tg.notify_admin(report)
                mark_sent(db)
                _err("Daily self-check sent to your Telegram chat.")
            except PublishError as exc:
                _err(f"Notification problem: {exc}")
    db.close()
    return 0 if any(c == 0 for c in codes) else 1


cli.register_subcommand("run", "Full automation: scrape all sources, update history, detect deals, notify n8n",
                        _run_build, _run_run)


# ===================================================================== P2: Windows Task Scheduler
def _schedule_build(ap) -> None:
    ap.add_argument("--every", choices=["daily", "twice-daily", "hourly", "weekly"], default="daily",
                    help="twice-daily = every 12 hours from --at (e.g. 08:13 and 20:13)")
    ap.add_argument("--at", default="08:17", help="Start time HH:MM (default %(default)s)")
    ap.add_argument("--hours", type=int, default=6, help="Interval for --every hourly")
    ap.add_argument("--task-name", default="WebScrapingAffiliate")
    ap.add_argument("--config", default="automation.json")
    ap.add_argument("--create", action="store_true", help="Actually register the task (Windows only)")
    ap.add_argument("--remove", action="store_true", help="Delete the scheduled task (Windows only)")
    ap.add_argument("--hidden", action="store_true", help="Run without a CMD window (via a small .vbs wrapper)")


def _schedule_run(args) -> int:
    from .scheduler import (build_bat, build_schtasks_create, build_schtasks_delete, build_task_settings, build_vbs,
                            console_python, run_schtasks)
    project = Path.cwd()
    try:
        if args.remove:
            cmd = build_schtasks_delete(args.task_name)
            print(" ".join(cmd))
            return run_schtasks(cmd)
        bat = project / "run_automation.bat"
        vbs = project / "run_automation.vbs" if args.hidden else None
        cmd = build_schtasks_create(args.task_name, bat, args.every, args.at, args.hours, vbs_path=vbs)
    except ValueError as exc:
        _err(f"error: {exc}")
        return 2
    bat.write_text(build_bat(project, console_python(sys.executable), args.config), encoding="utf-8")
    print(f"Wrote {bat}")
    if vbs:
        vbs.write_text(build_vbs(bat), encoding="utf-16")  # wscript reads UTF-16 with BOM, any path works
        print(f"Wrote {vbs}")
    print("Task Scheduler command:\n  " + " ".join(cmd))
    if not args.create:
        print("\n(Not registered. Add --create to register it, or paste the command into CMD.)")
        return 0
    code = run_schtasks(cmd)
    if code == 0:
        # also run on battery, make up a run missed while the computer slept, never hang for days
        if run_schtasks(build_task_settings(args.task_name)) == 0:
            print("Task settings: runs on battery too, and a missed run starts as soon as the computer is on.")
        else:
            _err("warning: could not change the task settings - a laptop on battery or asleep at the time "
                 "may skip runs. Task Scheduler -> the task -> Conditions: untick 'Start only if on AC power'; "
                 "Settings: tick 'Run task as soon as possible after a scheduled start is missed'.")
    return code


cli.register_subcommand("schedule", "Create a Windows Task Scheduler task that runs the automation",
                        _schedule_build, _schedule_run)


# ===================================================================== P2: dashboard
def _dash_build(ap) -> None:
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--config", default="automation.json", help="Used for manual crawls / publishing settings")
    ap.add_argument("--open", action="store_true", help="Open the dashboard in your browser")


def _dash_run(args) -> int:
    try:
        import uvicorn

        from .dashboard.app import create_app
    except ImportError:
        _err("Dashboard needs FastAPI + uvicorn:  py -m pip install -r requirements.txt")
        return 2
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        _err(f"WARNING: binding to {args.host} exposes the dashboard to your network. It has no login.")
    app = create_app(args.db, automation_config=args.config, allowed_hosts=[args.host, "127.0.0.1", "localhost"])
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(f"Dashboard: {url}   (Ctrl+C to stop)")
    if args.open:
        import threading
        import webbrowser
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


cli.register_subcommand("dashboard", "Local web dashboard (http://127.0.0.1:8765)", _dash_build, _dash_run)


# ===================================================================== targets (edit automation.json safely)
def _targets_build(ap) -> None:
    ap.add_argument("action", choices=["add", "remove", "list"])
    ap.add_argument("urls", nargs="*", help="Product page URLs")
    ap.add_argument("--config", default="automation.json")
    ap.add_argument("--sources", default="sources.json", help="Used to check sites whose terms forbid scraping")


def _targets_run(args) -> int:
    from .policy import CrawlPolicy
    from .security import UnsafeURLError, check_url
    path = Path(args.config)
    if not path.exists():
        example = Path("automation.example.json")
        if not example.exists():
            _err(f"error: {path} not found")
            return 2
        path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        cfg = load_automation(str(path))
    except (OSError, ValueError) as exc:
        _err(f"error: {path} is not valid JSON ({exc}). Fix it in Notepad or copy automation.example.json again.")
        return 2
    targets = [str(t) for t in cfg.get("targets", [])]

    if args.action == "list":
        for i, t in enumerate(targets, 1):
            print(f"{i:3}. {t}")
        if not targets:
            print("(no product links yet)")
        return 0

    policy = CrawlPolicy.load(args.sources)
    changed = 0
    for url in args.urls:
        url = url.strip().strip('"')
        if args.action == "remove":
            if url.isdigit() and 1 <= int(url) <= len(targets):
                print(f"removed: {targets.pop(int(url) - 1)}")
                changed += 1
            elif url in targets:
                targets.remove(url)
                print(f"removed: {url}")
                changed += 1
            else:
                _err(f"not in the list: {url}")
            continue
        try:
            check_url(url)
        except UnsafeURLError as exc:
            _err(f"skipped (bad link): {url}  - {exc}")
            continue
        reason = policy.blocked_reason(url)
        if reason:
            _err(f"skipped (site terms): {url}  - {reason}")
            continue
        if url in targets:
            print(f"already in the list: {url}")
            continue
        targets.append(url)
        print(f"added: {url}")
        changed += 1
    if changed:
        cfg["targets"] = targets
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{len(targets)} product link(s) in {path}")
    return 0


cli.register_subcommand("targets", "Add / remove / list product links in automation.json (no manual JSON editing)",
                        _targets_build, _targets_run)


# ===================================================================== saved searches (automatic)
# Ready-made searches by category: chosen by an English name, so nothing Hebrew has to be typed into CMD.
# Specific product words, so the results are real products that can be compared and tracked over time.
SEARCH_PRESETS = {
    "electronics": ["אוזניות אלחוטיות", "AirPods", "רמקול בלוטוס", "שעון חכם", "Samsung Galaxy", "iPhone",
                    "מטען מהיר", "סוללה ניידת"],
    "appliances": ["שואב אבק רובוטי", "שואב אבק אלחוטי", "מכונת קפה", "סיר טיגון ללא שמן", "מיקרוגל", "מקרר",
                   "מכונת כביסה", "מזגן"],
    "computers": ["מחשב נייד", "מסך מחשב", "PlayStation 5", "בקר PS5", "עכבר גיימינג", "מקלדת אלחוטית", "iPad",
                  "כונן SSD"],
    "home": ["סט סירים", "מחבת", "מצעים", "שמיכה", "מגבות", "מקדחה", "סט כלי עבודה", "גריל גז"],
    "fashion": ["נעלי ריצה", "סניקרס", "ג'ינס", "מעיל", "תיק גב"],
    "baby": ["עגלת תינוק", "כיסא בטיחות", "בקבוק האכלה", "מוניטור תינוק", "טיולון"],
    "sport": ["נעלי ספורט", "משקולות", "הליכון", "אופניים", "מזרן יוגה"],
    "gaming": ["PlayStation 5", "Xbox Series", "Nintendo Switch", "אוזניות גיימינג", "מקלדת מכנית", "מסך גיימינג",
               "כיסא גיימינג", "כרטיס מסך"],
}


def _searches_build(ap) -> None:
    ap.add_argument("action", choices=["add", "remove", "list", "presets", "add-preset"])
    ap.add_argument("words", nargs="*", help="Search words, e.g. \"puzzle 1000\" (quote several words)")
    ap.add_argument("--config", default="automation.json")


def _searches_run(args) -> int:
    path = Path(args.config)
    if not path.exists():
        example = Path("automation.example.json")
        if not example.exists():
            _err(f"error: {path} not found")
            return 2
        path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        cfg = load_automation(str(path))
    except (OSError, ValueError) as exc:
        _err(f"error: {path} is not valid JSON ({exc})")
        return 2
    searches = [str(q) for q in cfg.get("searches", [])]
    if args.action == "presets":
        for name, words in SEARCH_PRESETS.items():
            print(f"{name:12} {', '.join(words)}")
        print("\nAdd one or more: py main.py searches add-preset electronics appliances")
        return 0
    if args.action == "add-preset":
        bad = [w for w in args.words if w.lower() not in SEARCH_PRESETS]
        if bad or not args.words:
            _err(f"error: choose from: {', '.join(SEARCH_PRESETS)}" + (f" (unknown: {', '.join(bad)})" if bad else ""))
            return 2
        args.words = [q for name in args.words for q in SEARCH_PRESETS[name.lower()]]
        args.action = "add"
    if args.action == "list":
        for i, q in enumerate(searches, 1):
            print(f"{i:3}. {q}")
        if not searches:
            print("(no automatic searches yet)")
        return 0
    changed = 0
    for w in args.words:
        if args.action == "remove":
            if w.isdigit() and 1 <= int(w) <= len(searches):
                print(f"removed: {searches.pop(int(w) - 1)}")
                changed += 1
            elif w in searches:
                searches.remove(w)
                print(f"removed: {w}")
                changed += 1
            else:
                _err(f"not in the list: {w}")
            continue
        try:
            q = clean_query(w)
        except ValueError as exc:
            _err(f"skipped: {w!r} - {exc}")
            continue
        if q in searches:
            print(f"already in the list: {q}")
            continue
        searches.append(q)
        print(f"added: {q}")
        changed += 1
    if changed:
        cfg["searches"] = searches
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{len(searches)} automatic search(es) in {path} - each run searches every store for them")
    return 0


cli.register_subcommand("searches", "Add / remove / list searches that every automation run performs in all stores",
                        _searches_build, _searches_run)


# ===================================================================== website
def _site_build(ap) -> None:
    ap.add_argument("action", choices=["setup", "build", "preview", "publish"],
                    help="setup = save the website settings; build = write the site folder; "
                         "preview = build and open it; publish = build and push")
    ap.add_argument("--config", default="automation.json")
    ap.add_argument("--same-repo", action="store_true",
                    help="setup: publish into the gh-pages branch of THIS project's repository (no second repository)")
    ap.add_argument("--publish-dir", help="setup: local clone of a separate GitHub Pages repository")
    ap.add_argument("--base-url", help="setup: the site address (default: from the clone's GitHub remote)")
    ap.add_argument("--telegram-url", help="setup: channel link (default: from TELEGRAM_CHANNEL)")


def _site_run(args) -> int:
    from .site import SiteConfig, build_site, publish_site
    try:
        cfg = load_automation(args.config)
    except (OSError, ValueError) as exc:
        _err(f"error: {exc}")
        return 2
    if args.action == "setup":
        return _site_setup(args, cfg)
    site_cfg = SiteConfig.from_dict(cfg.get("site"))
    db = Database(cfg.get("db", DEFAULT_DB))
    try:
        info = build_site(db, site_cfg)
    except RuntimeError as exc:
        _err(f"error: {exc}")
        return 2
    finally:
        db.close()
    index = Path(info["out_dir"]).resolve() / "index.html"
    print(f"Built {info['pages']} page(s), {info['deals']} deal(s): {index}")
    if args.action == "preview":
        import webbrowser
        webbrowser.open(index.as_uri())
    elif args.action == "publish":
        try:
            print(f"Website: {publish_site(site_cfg)}")
        except RuntimeError as exc:
            _err(f"error: {exc}")
            return 1
    return 0


def pages_url_from_remote(remote: str) -> str:
    """https://github.com/USER/REPO(.git) or git@github.com:USER/REPO.git -> https://USER.github.io/REPO/"""
    import re
    m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?/?$", remote.strip())
    if not m:
        return ""
    user, repo = m.group(1), m.group(2)
    return f"https://{user.lower()}.github.io/" if repo.lower() == f"{user.lower()}.github.io" else \
        f"https://{user.lower()}.github.io/{repo}/"


def _site_setup_same_repo(args, cfg: dict) -> int:
    import os
    import subprocess
    if not Path(".git").exists():
        _err("error: run this from the project folder (the one with main.py and .git)")
        return 2
    remote = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=False).stdout
    base = args.base_url or pages_url_from_remote(remote)
    if not base.startswith("https://"):
        _err("error: could not work out the site address - give --base-url https://<you>.github.io/<repo>/")
        return 2
    channel = os.environ.get("TELEGRAM_CHANNEL", "")
    tg = args.telegram_url or (f"https://t.me/{channel.lstrip('@')}" if channel.startswith("@") else "")
    site = dict(cfg.get("site") or {})
    site.update({"enabled": True, "base_url": base if base.endswith("/") else base + "/", "publish_branch": "gh-pages",
                 "publish_dir": "", "repo_dir": ".", "out_dir": site.get("out_dir", "site"),
                 "title": site.get("title", "מחיר אמת")})
    if tg:
        site["telegram_url"] = tg
    site.pop("_comment", None)
    cfg["site"] = site
    Path(args.config).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"website settings saved in {args.config}:")
    for k in ("base_url", "publish_branch", "telegram_url"):
        print(f"  {k}: {site.get(k, '(not set)')}")
    print("then: menu 22 (publish), and on GitHub: Settings -> Pages -> Deploy from a branch -> gh-pages / (root)")
    return 0


def _site_setup(args, cfg: dict) -> int:
    import os
    import subprocess
    if args.same_repo:
        return _site_setup_same_repo(args, cfg)
    if not args.publish_dir:
        _err("error: give --same-repo (simplest) or --publish-dir <the folder where you cloned the website repository>")
        return 2
    folder = Path(args.publish_dir)
    if not (folder / ".git").exists():
        _err(f"error: {folder} is not a git clone - run: git clone https://github.com/<you>/<repo>.git first")
        return 2
    from .site import _refuse_project_folder
    try:
        _refuse_project_folder(folder, "--publish-dir")
    except RuntimeError as exc:
        _err(f"error: {exc}")
        return 2
    base = args.base_url
    if not base:
        remote = subprocess.run(["git", "-C", str(folder), "remote", "get-url", "origin"], capture_output=True,
                                text=True, check=False).stdout
        base = pages_url_from_remote(remote)
    if not base.startswith("https://"):
        _err("error: could not work out the site address - give --base-url https://<you>.github.io/<repo>/")
        return 2
    channel = os.environ.get("TELEGRAM_CHANNEL", "")
    tg = args.telegram_url or (f"https://t.me/{channel.lstrip('@')}" if channel.startswith("@") else "")
    site = dict(cfg.get("site") or {})
    site.update({"enabled": True, "base_url": base if base.endswith("/") else base + "/",
                 "publish_dir": folder.resolve().as_posix(), "out_dir": site.get("out_dir", "site"),
                 "title": site.get("title", "מחיר אמת")})
    if tg:
        site["telegram_url"] = tg
    site.pop("_comment", None)
    cfg["site"] = site
    Path(args.config).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"website settings saved in {args.config}:")
    for k in ("base_url", "publish_dir", "telegram_url"):
        print(f"  {k}: {site.get(k, '(not set)')}")
    return 0


cli.register_subcommand("site", "Build / preview / publish the website made from your approved deals",
                        _site_build, _site_run)


# ===================================================================== automatic / manual approval + withdraw
def _approval_build(ap) -> None:
    ap.add_argument("mode", choices=["auto", "manual", "smart", "status"],
                    help="auto = every deal is published; manual = you approve every deal; smart = deals with a "
                         "big verified saving are published automatically (a few per store a day), the rest wait for you")
    ap.add_argument("--min-savings", type=float, default=None, help="smart: the saving that is published by itself (%%)")
    ap.add_argument("--per-store", type=int, default=None, help="smart: automatic deals per store per day")
    ap.add_argument("--config", default="automation.json")


def _approval_run(args) -> int:
    path = Path(args.config)
    try:
        cfg = load_automation(str(path))
    except (OSError, ValueError) as exc:
        _err(f"error: {exc}")
        return 2
    ap_cfg = dict(cfg.get("auto_publish") or {})
    if args.mode != "status":
        cfg["require_approval"] = args.mode != "auto"
        ap_cfg["enabled"] = args.mode == "smart"
        if args.min_savings is not None:
            ap_cfg["min_savings_pct"] = args.min_savings
        if args.per_store is not None:
            ap_cfg["per_store_per_day"] = args.per_store
        ap_cfg.setdefault("min_savings_pct", 15)
        ap_cfg.setdefault("per_store_per_day", 2)
        cfg["auto_publish"] = ap_cfg
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    if cfg.get("require_approval", True) is False:
        print("approval: AUTOMATIC - every deal is published right away; remove one with menu 24 / py main.py withdraw <id>")
    elif ap_cfg.get("enabled"):
        print(f"approval: SMART - a verified saving of {ap_cfg.get('min_savings_pct', 15):g}% or more is published by itself "
              f"(at most {ap_cfg.get('per_store_per_day', 2)} per store a day); every other deal waits for your "
              "✅ in Telegram")
    else:
        print("approval: MANUAL - every deal waits for your ✅ in Telegram (or menu 8)")
    return 0


def _withdraw_build(ap) -> None:
    ap.add_argument("ids", nargs="+", type=int)
    ap.add_argument("--config", default="automation.json", help="Used for the database and to rebuild the website")
    ap.add_argument("--db", help="Database file (default: from the automation config)")


def _withdraw_run(args) -> int:
    from .alerts import withdraw
    from .site import SiteConfig, build_site, publish_site
    from .telegram import TelegramPublisher
    cfg = {}
    if Path(args.config).exists():
        try:
            cfg = load_automation(args.config)
        except (OSError, ValueError):
            cfg = {}
    tg = TelegramPublisher()
    deleter = tg.delete_message if tg.token else None
    db = Database(args.db or cfg.get("db", DEFAULT_DB))
    try:
        results = withdraw(db, args.ids, deleter)
        for r in results:
            print(f"#{r['id']}: " + ("removed" if r["ok"] else "not found") + f" - Telegram: {r['telegram']}")
        site = cfg.get("site") or {}
        if site.get("enabled") and any(r["ok"] for r in results):
            site_cfg = SiteConfig.from_dict(site)
            try:
                build_site(db, site_cfg)
                print("website rebuilt" + (f" and {publish_site(site_cfg)}"
                                           if site_cfg.publish_dir or site_cfg.publish_branch else ""))
            except RuntimeError as exc:
                _err(f"Website problem: {exc}")
    finally:
        db.close()
    return 0 if all(r["ok"] for r in results) else 1


cli.register_subcommand("approval", "Switch between automatic publishing (testing) and manual approval",
                        _approval_build, _approval_run)
cli.register_subcommand("withdraw", "Remove published deals from the channel and the website",
                        _withdraw_build, _withdraw_run)


# ---------------------------------------------------------------- health
def _health_build(ap) -> None:
    ap.add_argument("--config", default="automation.json")
    ap.add_argument("--db", default=None)
    ap.add_argument("--telegram", action="store_true", help="also send the report to TELEGRAM_ADMIN_CHAT")
    ap.add_argument("--no-site-check", action="store_true", help="do not open the public website")


def _health_run(args) -> int:
    from .health import format_report, mark_sent, run_health
    from .telegram import TelegramPublisher
    from .webhook import PublishError
    cfg = {}
    if Path(args.config).exists():
        try:
            cfg = load_automation(args.config)
        except (OSError, ValueError) as exc:
            _err(f"warning: cannot read {args.config}: {exc}")
    tg = TelegramPublisher()
    db = Database(args.db or cfg.get("db", DEFAULT_DB))
    try:
        res = run_health(db, cfg, tg if tg.configured else None, check_site=not args.no_site_check)
        report = format_report(res)
        print(report)
        if args.telegram:
            if not tg.admin_chat:
                _err("TELEGRAM_ADMIN_CHAT is not set - the report was only printed here.")
            else:
                try:
                    tg.notify_admin(report)
                    mark_sent(db)
                    print("\nSent to your Telegram chat.")
                except PublishError as exc:
                    _err(f"Notification problem: {exc}")
    finally:
        db.close()
    return 0 if res["ok"] else 1


cli.register_subcommand("health", "Self-check: runs, store errors, published deals still true, Telegram, website",
                        _health_build, _health_run)


# ---------------------------------------------------------------- stores (sources.json)
def _stores_build(ap) -> None:
    ap.add_argument("action", choices=["list", "daily-off", "daily-on", "search-off", "search-on", "sale-add",
                                       "sale-remove", "find-sales", "update", "disable", "enable", "permit",
                                       "unpermit"])
    ap.add_argument("domains", nargs="*", help="Store domains, or 'all'. sale-add: a store's sale page URL(s)")
    ap.add_argument("--sources", default=None, help="Default: sources_config from automation.json, else sources.json")
    ap.add_argument("--config", default="automation.json")
    ap.add_argument("--pack", default="stores_il_sales.json", help="update: the checked sale pages to add")
    ap.add_argument("--i-have-written-permission", dest="written_permission", action="store_true",
                    help="permit: you confirm the site gave you WRITTEN permission for automated collection")
    ap.add_argument("--again", action="store_true", help="find-sales: also stores that already have a sale page")
    ap.add_argument("--delay", type=float, default=3.0, help=argparse.SUPPRESS)
    ap.add_argument("--allow-private-hosts", action="store_true", help=argparse.SUPPRESS)


def _stores_run(args) -> int:
    path = Path(args.sources or "sources.json")
    if not args.sources and Path(args.config).exists():
        try:
            path = Path(load_automation(args.config).get("sources_config") or path)
        except (OSError, ValueError):
            pass
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _err(f"error: cannot read {path}: {exc}")
        return 2
    stores = data.get("stores", [])
    if args.action == "list":
        for st in stores:
            sales = len(st.get("sale_urls") or [])
            daily = len(st.get("start_urls") or []) - sales
            off = len(st.get("_start_urls_off") or [])
            scan = f"daily scan: {daily} page(s)" if daily else ("daily scan: off" + (f" ({off} saved)" if off else ""))
            if sales:
                scan += f", sale: {sales}"
            search = "search: yes" if (st.get("search_url") or st.get("search_sitemap")) else "search: no"
            if search == "search: yes" and st.get("search") is False:
                search = "search: off"
            off_all = "  [DISABLED]" if st.get("enabled") is False else ""
            print(f"  {st.get('domain', '?'):24} {scan:28} {search}{off_all}")
        print("\nDaily scan = the store pages read in every run. Search = used by the automatic searches (menu 19).")
        return 0
    if args.action in ("sale-add", "sale-remove"):
        return _stores_sale(args, path, data)
    if args.action == "find-sales":
        return _stores_find_sales(args, path, data)
    if args.action == "update":
        return _stores_update(args, path, data)
    if args.action in ("permit", "unpermit"):
        return _stores_permit(args, path, data)
    want = {d.lower() for d in args.domains}
    if not want:
        _err("error: name the store domains, or 'all'")
        return 2
    changed = 0
    if args.action in ("disable", "enable"):
        # a disabled store is not scanned at all (no daily pages, no searches) - its settings stay
        for st in stores:
            if "all" in want or str(st.get("domain", "")).lower() in want:
                on = args.action == "enable"
                if (st.get("enabled") is not False) != on:
                    st["enabled"] = on
                    print(f"{'enabled' if on else 'disabled'}: {st['domain']}")
                    changed += 1
        known = {str(st.get("domain", "")).lower() for st in stores}
        for d in want - known - {"all"}:
            _err(f"not in {path}: {d}")
        if changed:
            _save_sources(path, data)
        else:
            print("nothing changed")
        return 0
    for st in stores:
        if "all" not in want and str(st.get("domain", "")).lower() not in want:
            continue
        sales = st.get("sale_urls") or []
        pages = [u for u in st.get("start_urls") or [] if u not in sales]
        if args.action == "daily-off" and pages:
            # category pages are kept (daily-on brings them back); sale pages stay on
            st["_start_urls_off"] = [*(st.get("_start_urls_off") or []), *pages]
            st["start_urls"] = [u for u in st.get("start_urls") or [] if u in sales]
            if not st["start_urls"]:
                st.pop("start_urls")
            print(f"daily scan off: {st['domain']}" + (" (sale pages stay on)" if sales else ""))
            changed += 1
        elif args.action == "daily-on" and st.get("_start_urls_off"):
            st["start_urls"] = [*(st.get("start_urls") or []), *st.pop("_start_urls_off")]
            print(f"daily scan on: {st['domain']}")
            changed += 1
        elif args.action == "search-off" and (st.get("search_url") or st.get("search_sitemap")) \
                and st.get("search") is not False:
            # the search address stays saved (search-on brings it back); daily and sale pages stay on
            st["search"] = False
            print(f"search off: {st['domain']}" + (" (sale pages stay on)" if sales else ""))
            changed += 1
        elif args.action == "search-on" and st.get("search") is False:
            st.pop("search")
            print(f"search on: {st['domain']}")
            changed += 1
    known = {str(st.get("domain", "")).lower() for st in stores}
    for d in want - known - {"all"}:
        _err(f"not in {path}: {d}")
    if changed:
        _save_sources(path, data)
    else:
        print("nothing changed")
    return 0


SALE_MAX_PAGES, SALE_MAX_LISTING_PAGES = 80, 5


def _save_sources(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _product_pattern(st: dict) -> str:
    return st.get("include") or st.get("search_include") or r"/products?/"


def _attach_sale(st: dict, url: str) -> bool:
    urls = st.setdefault("start_urls", [])
    if url in urls:
        return False
    urls.append(url)
    st.setdefault("sale_urls", []).append(url)
    st.setdefault("discover", "links")
    if not st.get("include") and st.get("search_include"):
        st["include"] = st["search_include"]        # which links on the page are products
    st.setdefault("max_pages", SALE_MAX_PAGES)
    st.setdefault("max_listing_pages", SALE_MAX_LISTING_PAGES)
    return True


def _store_home(st: dict) -> str:
    from urllib.parse import urlparse
    for key in ("search_url", "search_sitemap"):
        p = urlparse(str(st.get(key) or "").replace("{q}", "x"))
        if p.scheme in ("http", "https") and p.netloc:
            return f"{p.scheme}://{p.netloc}/"
    return f"https://www.{st['domain']}/"


def _stores_find_sales(args, path: Path, data: dict) -> int:
    """Open each store's home page and add its sale page automatically (see salefinder.py)."""
    from .fetcher import PoliteFetcher
    from .policy import CrawlPolicy
    from .salefinder import find_sale_page
    want = {d.lower() for d in args.domains} - {"all"}
    policy = CrawlPolicy.load(str(path))
    fetcher = PoliteFetcher(min_delay=args.delay, allow_private=args.allow_private_hosts, policy=policy,
                            use_cache=False)
    added = 0
    for st in data.get("stores", []):
        dom = str(st.get("domain", "")).lower()
        if (want and dom not in want) or st.get("enabled") is False:
            continue
        if st.get("sale_urls") and not args.again:
            print(f"  {dom:24} already has a sale page")
            continue
        home = _store_home(st)
        reason = policy.blocked_reason(home)
        if reason:
            print(f"  {dom:24} skipped - {reason}")
            continue
        try:
            found = find_sale_page(fetcher, home, dom, _product_pattern(st))
        except Exception as exc:  # noqa: BLE001 - one store's problem must not stop the others
            found = None
            _err(f"  {dom}: {exc}")
        if found and _attach_sale(st, found["url"]):
            added += 1
            print(f"  {dom:24} found: {found['url']}  ({found['products']} products on the page)")
            _save_sources(path, data)                # saved after each store: an interruption loses nothing
        elif found:
            print(f"  {dom:24} already in the list: {found['url']}")
        else:
            why = next((f"{e.kind}" for e in fetcher.errors_for(home)), "no sale link with products on the home page")
            print(f"  {dom:24} not found ({why})")
    print(f"\n{added} sale page(s) added. They are scanned in every automatic run (menu 6 / the schedule).")
    if added == 0:
        print("A store not found here: open its sale page in the browser and use menu 29 to paste the link.")
    return 0


def _stores_permit(args, path: Path, data: dict) -> int:
    """A site whose terms require written permission (policy.py, e.g. KSP) is scanned only after you
    confirm that you have it. The confirmation is recorded with the date; the document stays with you."""
    from .policy import DEFAULT_BLOCKED
    doms = [d.lower().removeprefix("www.") for d in args.domains]
    if not doms:
        _err("error: name the site, e.g. ksp.co.il")
        return 2
    permitted = [str(d).lower() for d in data.get("permitted_domains") or []]
    notes = data.setdefault("_permissions", {})
    if args.action == "unpermit":
        for d in doms:
            if d in permitted:
                permitted.remove(d)
                notes.pop(d, None)
                print(f"permission removed: {d} - it is blocked again")
        data["permitted_domains"] = permitted
        _save_sources(path, data)
        return 0
    if not args.written_permission:
        _err("error: add --i-have-written-permission to confirm that the site gave you WRITTEN permission "
             "(its terms require it). Without it the site stays blocked.")
        return 2
    for d in doms:
        if d not in permitted:
            permitted.append(d)
        notes[d] = {"confirmed_by_owner": datetime.now(timezone.utc).date().isoformat(),
                    "note": "the owner confirmed written permission; the document is kept by the owner",
                    "terms": DEFAULT_BLOCKED.get(d, "")}
        print(f"permitted: {d} (recorded {notes[d]['confirmed_by_owner']}). robots.txt and the delay still apply.")
    data["permitted_domains"] = permitted
    _save_sources(path, data)
    return 0


PACK_SETTABLE = {"link_selector", "include", "max_pages", "max_listing_pages", "discover"}


def _stores_update(args, path: Path, data: dict) -> int:
    """Add the sale pages that were checked and shipped with the code (stores_il_sales.json).
    Only adds: your own pages, settings and stores stay as they are."""
    try:
        pack = json.loads(Path(args.pack).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _err(f"error: cannot read {args.pack}: {exc}")
        return 2
    by_domain = {str(st.get("domain", "")).lower(): st for st in data.get("stores", [])}
    added = 0
    for entry in pack.get("stores", []):
        dom = str(entry.get("domain", "")).lower()
        st = by_domain.get(dom)
        if st is None:
            print(f"  {dom:24} not in your {path} - skipped")
            continue
        for k, v in (entry.get("set") or {}).items():
            if k in PACK_SETTABLE:
                st[k] = v
        new = [u for u in entry.get("sale_urls", []) if _attach_sale(st, u)]
        added += len(new)
        print(f"  {dom:24} " + (f"{len(new)} sale page(s) added" if new else "up to date"))
    for dom, why in (pack.get("not_added") or {}).items():
        print(f"  {dom:24} not added: {why}")
    if added:
        _save_sources(path, data)
    print(f"\n{added} sale page(s) added (checked {pack.get('checked', '?')}). They are scanned in every run.")
    return 0


def _stores_sale(args, path: Path, data: dict) -> int:
    """A store's own sale page ("מבצעים") becomes part of its daily scan: the listing is read and the
    product pages on it are checked - the same polite, robots.txt-respecting crawl as any other page."""
    from urllib.parse import urlparse

    from .policy import CrawlPolicy
    from .security import UnsafeURLError, check_url
    from .utils import domain_matches
    stores = data.get("stores", [])
    policy = CrawlPolicy.load(str(path))
    changed = 0
    for url in args.domains:
        url = url.strip()
        try:
            check_url(url)
        except UnsafeURLError as exc:
            _err(f"skipped: {url} - {exc}")
            continue
        host = (urlparse(url).hostname or "").lower()
        st = next((x for x in sorted(stores, key=lambda x: -len(str(x.get("domain", ""))))
                   if domain_matches(host, str(x.get("domain", "")))), None)
        if st is None:
            _err(f"skipped: {url} - {host} is not one of the stores in {path}")
            continue
        if args.action == "sale-remove":
            urls = st.get("start_urls") or []
            if url in urls:
                urls.remove(url)
                st["start_urls"] = urls
                sales = st.get("sale_urls") or []
                st["sale_urls"] = [u for u in sales if u != url]
                print(f"removed sale page: {st['domain']}")
                changed += 1
            else:
                _err(f"not in the list: {url}")
            continue
        reason = policy.blocked_reason(url)
        if reason:
            _err(f"skipped: {url} - {reason}")
            continue
        if not _attach_sale(st, url):
            print(f"already in the list: {st['domain']}")
            continue
        print(f"added sale page: {st['domain']} (up to {st['max_pages']} products per run)")
        changed += 1
    if changed:
        _save_sources(path, data)
    return 0 if changed or args.action == "sale-remove" else 1


cli.register_subcommand("stores", "Show the stores and turn their daily page scan / searches off / on (sources.json)",
                        _stores_build, _stores_run)


# ---------------------------------------------------------------- daily summary to share
def _digest_build(ap) -> None:
    ap.add_argument("--config", default="automation.json")
    ap.add_argument("--db", default=None)
    ap.add_argument("--count", type=int, default=None, help="How many deals (default 5)")
    ap.add_argument("--hours", type=float, default=None, help="Deals published in the last N hours (default 48)")
    ap.add_argument("--telegram", action="store_true", help="also send it to TELEGRAM_ADMIN_CHAT")
    ap.add_argument("--example", action="store_true", help="a clearly marked example with made-up items")


def _digest_run(args) -> int:
    from .digest import build_digest
    from .health import mark_sent
    from .site import SiteConfig
    from .telegram import TelegramPublisher
    from .webhook import PublishError
    cfg = {}
    if Path(args.config).exists():
        try:
            cfg = load_automation(args.config)
        except (OSError, ValueError) as exc:
            _err(f"warning: cannot read {args.config}: {exc}")
    dcfg = cfg.get("digest") or {}
    if args.example:
        from .digest import example_digest
        text = example_digest(SiteConfig.from_dict(cfg.get("site")))
        print(text)
        if args.telegram:
            tg = TelegramPublisher()
            if not tg.admin_chat:
                _err("TELEGRAM_ADMIN_CHAT is not set - open a new CMD window after setx.")
                return 1
            try:
                tg.notify_admin(text)
                print("\nExample sent to your Telegram chat.")
            except PublishError as exc:
                _err(f"Notification problem: {exc}")
                return 1
        return 0
    count = args.count or int(dcfg.get("count", 5))
    if not 1 <= count <= 10:
        _err("error: --count must be 1-10")
        return 2
    db = Database(args.db or cfg.get("db", DEFAULT_DB))
    try:
        text = build_digest(db, SiteConfig.from_dict(cfg.get("site")), count=count,
                            hours=args.hours or float(dcfg.get("hours", 48)))
        if not text:
            msg = "No deals to share right now (only active deals with a proven saving from the last 48 hours)."
            print(msg)
            if args.telegram:
                tg = TelegramPublisher()
                if tg.admin_chat:
                    try:     # so you can see it works - the daily automatic summary stays silent instead
                        tg.notify_admin("📋 סיכום יומי: אין כרגע עסקאות לשיתוף. נשלחות רק עסקאות פעילות עם "
                                        "חיסכון מוכח מ-48 השעות האחרונות. ההיסטוריה של המחירים נבנית בכל סריקה.")
                        print("Told your Telegram chat.")
                    except PublishError as exc:
                        _err(f"Notification problem: {exc}")
                else:
                    _err("TELEGRAM_ADMIN_CHAT is not set.")
            return 1
        out = Path(cfg.get("out_dir", "output"))
        out.mkdir(parents=True, exist_ok=True)
        (out / "daily_summary.txt").write_text(text + "\n", encoding="utf-8")
        print(text)
        print(f"\nSaved to {out / 'daily_summary.txt'} (open it in Notepad and copy).")
        if args.telegram:
            tg = TelegramPublisher()
            if not tg.admin_chat:
                _err("TELEGRAM_ADMIN_CHAT is not set - the summary was only saved here.")
            else:
                try:
                    tg.notify_admin(text)
                    mark_sent(db, key="digest_sent_at")
                    print("Sent to your Telegram chat - long-press the message, Copy, paste in the group.")
                except PublishError as exc:
                    _err(f"Notification problem: {exc}")
    finally:
        db.close()
    return 0


cli.register_subcommand("digest", "Today's best deals as one message to share in WhatsApp / Facebook groups",
                        _digest_build, _digest_run)


# ---------------------------------------------------------------- Telegram -> agent manager
LISTENER_VBS = "WebScrapingTelegramManager.vbs"


def _listen_build(ap) -> None:
    ap.add_argument("--config", default="automation.json")
    ap.add_argument("--install", action="store_true", help="start the listener automatically when Windows starts")
    ap.add_argument("--uninstall", action="store_true", help="stop starting it with Windows")
    ap.add_argument("-v", "--verbose", action="count", default=0)


def _listen_install(args, remove: bool) -> int:
    from .remote import startup_folder
    from .scheduler import build_vbs, console_python
    folder = startup_folder()
    if folder is None:
        _err("error: this works on Windows only (the Startup folder was not found)")
        return 2
    target = folder / LISTENER_VBS
    if remove:
        if target.exists():
            target.unlink()
            print(f"Removed {target}. The listener no longer starts with Windows.")
        else:
            print("It was not installed.")
        return 0
    project = Path.cwd()
    bat = project / "run_telegram_manager.bat"
    bat.write_text("\r\n".join(["@echo off", "REM Generated by: py main.py telegram-listen --install",
                                 f'cd /d "{project}"', "if not exist logs mkdir logs",
                                 f'"{console_python(sys.executable)}" main.py telegram-listen --config "{args.config}"'
                                 r' >> "logs\telegram_manager.log" 2>&1', ""]), encoding="utf-8")
    folder.mkdir(parents=True, exist_ok=True)
    target.write_text(build_vbs(bat), encoding="utf-16")
    print(f"Installed: {target}\nThe Telegram manager starts hidden every time you log in to Windows.")
    from .remote import single_instance
    probe = single_instance()
    if probe is None:
        print("It is already running now.")
        return 0
    probe.close()
    if os.name == "nt":
        import subprocess
        # start it now too, hidden, detached from this window (wscript runs the same starter as at login)
        subprocess.Popen(["wscript.exe", str(target)], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                         close_fds=True)
        print("Started now. In Telegram you should get: 'מנהל הסוכנים מחובר ומקשיב'.")
    else:
        print("To start it now: py main.py telegram-listen")
    return 0


def _listen_run(args) -> int:
    from .remote import Listener, listen_forever, single_instance
    from .telegram import TelegramPublisher
    if args.install or args.uninstall:
        return _listen_install(args, remove=args.uninstall)
    tg = TelegramPublisher()
    if not tg.token or not tg.admin_chat:
        _err("error: set TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT (setx), then open a new CMD window")
        return 2
    lock = single_instance()
    if lock is None:
        _err("The Telegram manager is already running on this computer.")
        return 0
    cfg = {}
    if Path(args.config).exists():
        try:
            cfg = load_automation(args.config)
        except (OSError, ValueError):
            cfg = {}
    db_path = cfg.get("db", DEFAULT_DB)

    def status() -> str:
        from .health import format_report, run_health
        db = Database(db_path)
        try:
            return format_report(run_health(db, cfg, tg))
        finally:
            db.close()

    def digest() -> Optional[str]:
        from .digest import build_digest
        from .site import SiteConfig
        db = Database(db_path)
        try:
            return build_digest(db, SiteConfig.from_dict(cfg.get("site")))
        finally:
            db.close()

    def get_offset() -> Optional[int]:
        db = Database(db_path)
        try:
            row = db.query("SELECT value FROM meta WHERE key='tg_offset'")
            return int(row[0]["value"]) if row else None
        finally:
            db.close()

    def save_offset(offset: int) -> None:
        db = Database(db_path)
        try:
            with db.tx() as c:
                c.execute("INSERT INTO meta(key, value) VALUES ('tg_offset', ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))
        finally:
            db.close()

    from functools import partial

    from .remote import DEFAULT_MANAGER_MODEL, run_claude
    model = (cfg.get("telegram_manager") or {}).get("model", DEFAULT_MANAGER_MODEL) or None
    def with_db(fn):
        def run(ident: int) -> str:
            db = Database(db_path)
            try:
                return fn(db, ident)
            finally:
                db.close()
        return run

    def deal_publish(db: Database, alert_id: int) -> str:
        """Approve one deal and publish the approved deals now: website first (the post links to it), then the channel."""
        from .alerts import decide, publish_approved
        from .site import SiteConfig, build_site, page_url, publish_site
        row = db.query("SELECT status, title FROM deal_alerts WHERE id=?", (alert_id,))
        if not row:
            return f"לא נמצאה עסקה #{alert_id}."
        if row[0]["status"] == "published":
            return f"עסקה #{alert_id} כבר פורסמה."
        decide(db, [alert_id], True)
        site = cfg.get("site") or {}
        site_cfg = SiteConfig.from_dict(site)
        if site.get("enabled"):
            try:
                build_site(db, site_cfg)
                if site_cfg.publish_dir or site_cfg.publish_branch:
                    publish_site(site_cfg)
            except RuntimeError as exc:
                log_note = f" (האתר לא עודכן: {exc})"
            else:
                log_note = ""
        else:
            log_note = ""
        res = publish_approved(db, tg.send_event,
                               page_url_for=(lambda aid: page_url(site_cfg, aid)) if site.get("enabled") else None)
        if alert_id in res["ids"]:
            return f"✅ עסקה #{alert_id} פורסמה בערוץ: {row[0]['title'][:70]}{log_note}"
        return f"❌ עסקה #{alert_id} אושרה אבל לא פורסמה ({res}). היא תפורסם בריצה הבאה.{log_note}"

    def deal_reject(db: Database, alert_id: int) -> str:
        from .alerts import decide
        decide(db, [alert_id], False)
        return f"✖️ עסקה #{alert_id} נדחתה ולא תפורסם."

    actions = {
        "cpost": with_db(lambda db, i: publish_claim(db, i, tg, publish=True)),
        "cskip": lambda i: "✖️ דילגתי. לא פורסם.",
        "dpub": with_db(deal_publish),
        "drej": with_db(deal_reject),
    }
    listener = Listener(tg, Path.cwd(), tg.admin_chat, runner=partial(run_claude, model=model),
                        status=status, digest=digest, actions=actions)
    listener.progress = lambda: [progress_line(p) for p in read_progress(since=listener.started_at, limit=8)]
    print("Telegram manager is listening. Write /help to your bot in Telegram. Ctrl+C stops it.")
    try:
        tg.notify_admin("🤖 מנהל הסוכנים מחובר ומקשיב. כתוב /help לרשימת הפקודות.")
    except Exception as exc:  # noqa: BLE001
        _err(f"warning: could not message you: {exc}")
    try:
        listen_forever(listener, get_offset, save_offset)
    except KeyboardInterrupt:
        print("Stopped.")
    except RuntimeError as exc:
        _err(f"error: {exc}")
        return 1
    finally:
        lock.close()
    return 0


cli.register_subcommand("telegram-listen", "Run the agent manager from Telegram: /manager <task> to your bot",
                        _listen_build, _listen_run)


# ---------------------------------------------------------------- stores' Telegram channels (leads)
def _channels_build(ap) -> None:
    ap.add_argument("action", choices=["list", "add", "remove", "fetch", "leads", "drops", "post", "auto"])
    ap.add_argument("names", nargs="*", help="Channel names, e.g. KSPcoil payngoil (or t.me/KSPcoil); post: the id")
    ap.add_argument("--publish", action="store_true", help="post: in the PUBLIC channel (default: a preview to you)")
    ap.add_argument("--config", default="automation.json")
    ap.add_argument("--telegram", action="store_true", help="leads: also send them to TELEGRAM_ADMIN_CHAT")
    ap.add_argument("--delay", type=float, default=3.0, help=argparse.SUPPRESS)


def _channels_names(cfg: dict) -> list[str]:
    return [str(c) for c in cfg.get("telegram_channels", []) if str(c).strip()]


def fetch_channels(cfg: dict, db: Database, delay: float = 3.0) -> int:
    """Read every configured channel's public page and store its product claims. Returns new claims."""
    from .alerts import DealRules
    from .channels import fetch_channel, save_claims
    from .fetcher import PoliteFetcher
    names = _channels_names(cfg)
    if not names:
        return 0
    fetcher = PoliteFetcher(min_delay=delay, use_cache=False)
    rules = DealRules.from_dict(cfg.get("deals"))
    new = 0
    for name in names:
        try:
            posts = fetch_channel(fetcher, name)
        except ValueError as exc:
            _err(f"channel {name}: {exc}")
            continue
        n = save_claims(db, posts, rules.exclude_keywords)
        new += n
        _err(f"channel {name}: {len(posts)} product(s) in the recent posts, {n} new")
    return new


def auto_channel_posts(cfg: dict, db: Database, tg, channel_drops: list[dict]) -> list[dict]:
    """A big drop in a store's own Telegram channel may be posted by itself (the ``channels_auto`` rule) - only
    when you chose automatic or smart approval; with manual approval every channel post waits for your button.
    A NEW post of a store's channel with a big stated discount ("₪X instead of ₪Y") may be posted too, under
    its own daily limit (channels.auto_new_post_candidates). Returns the drops that still need your decision."""
    from .telegram import agent_run
    from .webhook import PublishError
    if agent_run():
        return channel_drops                    # the agent manager never posts in the public channel
    not_manual = cfg.get("require_approval", True) is False or bool((cfg.get("auto_publish") or {}).get("enabled"))
    if not (tg and tg.channel and not_manual):
        return channel_drops
    from .channels import auto_new_post_candidates, auto_post_candidates, auto_rules, mark_auto_posted, store_name
    rules = auto_rules(cfg.get("channels_auto"))

    def post(r: dict, pct: int, why: str) -> bool:
        try:
            publish_claim(db, r["id"], tg, publish=True, auto=True)
        except (PublishError, ValueError) as exc:
            _err(f"Channel auto-post problem: {exc}")
            return False
        mark_auto_posted(db, [r["id"]])
        _err(f"Posted from the {store_name(r['channel'])} channel by itself: {r['title'][:60]} (-{pct}%)")
        if tg.admin_chat:
            try:
                tg.notify_admin(f"📢 פורסם בערוץ לבד (כלל ערוצי החנויות: {why}, לפי ערוץ "
                                f"{store_name(r['channel'])}):\n{r['title'][:100]}\n"
                                "המחיר לא נבדק אצלנו. אם זה לא מתאים, מחק את הפוסט בערוץ.")
            except PublishError:
                pass
        return True

    for r in auto_post_candidates(db, channel_drops, rules) if channel_drops else []:
        pct = round((r["prev_price"] - r["price"]) / r["prev_price"] * 100)
        if not post(r, pct, f"ירידה של {pct}%"):
            return channel_drops
        channel_drops = [d for d in channel_drops if d["id"] != r["id"]]
    for r in auto_new_post_candidates(db, rules):
        pct = round((r["was_price"] - r["price"]) / r["was_price"] * 100)
        if not post(r, pct, f"פוסט חדש, החנות כתבה {pct}% הנחה"):
            break
    return channel_drops


def publish_claim(db: Database, claim_id: int, tg, publish: bool, auto: bool = False) -> str:
    """Post a channel price: to the public channel (publish) or as a preview to you. Returns what happened."""
    from .channels import ensure_schema, post_text
    from .content import DISCLOSURE
    ensure_schema(db)
    rows = db.query("SELECT * FROM channel_claims WHERE id=?", (claim_id,))
    if not rows:
        raise ValueError(f"no channel product with id {claim_id}")
    r = dict(rows[0])
    target = tg.channel if publish else tg.admin_chat
    if not target:
        raise ValueError("set " + ("TELEGRAM_CHANNEL" if publish else "TELEGRAM_ADMIN_CHAT"))
    link = r["link"] or r["post_url"]
    tg.send_menu(target, post_text(db, r, DISCLOSURE["he"], auto=auto),
                 [("לפרטים ורכישה ›", link)] if link.startswith("https://") else [])
    if publish:                              # so the automatic rule does not post the same product again this week
        with db.tx() as c:
            c.execute("UPDATE channel_claims SET published_at=? WHERE id=?",
                      (datetime.now(timezone.utc).isoformat(), claim_id))
    return f"✅ פורסם בערוץ: {r['title'][:80]}" if publish else "תצוגה מקדימה נשלחה אליך."


def _channels_auto(args, path: Path, cfg: dict) -> int:
    """py main.py channels auto on|off|status - your choice: may a big drop in a store's channel be posted by itself?"""
    from .channels import auto_rules
    choice = (args.names or ["status"])[0]
    if choice not in ("on", "off", "status"):
        _err("error: py main.py channels auto on | off | status")
        return 2
    rules = auto_rules(cfg.get("channels_auto"))
    if choice != "status":
        if not path.exists():
            _err(f"error: {path} not found")
            return 2
        rules["enabled"] = choice == "on"
        cfg["channels_auto"] = rules
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    not_manual = cfg.get("require_approval", True) is False or bool((cfg.get("auto_publish") or {}).get("enabled"))
    if rules["enabled"]:
        print(f"channels auto: ON - a {rules['min_drop_pct']:g}%+ drop in a store's own channel (niche only, "
              f"vs its post from the last {rules['max_prev_age_days']:g} days) is posted by itself, at most "
              f"{rules['per_day']} a day, worded as the store's price."
              + (f"\nNew posts too: the store states {rules['min_claim_pct']:g}%+ off (\"instead of\"), niche only, "
                 f"at most {rules['new_per_day']} a day, worded as the store's own claim, not checked by us."
                 if rules.get("new_posts") else "\nNew posts: off (only drops).")
              + ("" if not_manual else "\nNOTE: approval is MANUAL, so nothing is posted by itself until you "
                                       "run: py main.py approval smart"))
    else:
        print("channels auto: OFF - every product from the stores' channels waits for your button.")
    return 0


def _channels_post(args, db: Database) -> int:
    """A channel price as a post: preview to you, or --publish to the public channel (your decision)."""
    from .telegram import TelegramPublisher
    from .webhook import PublishError
    if len(args.names) != 1 or not args.names[0].isdigit():
        _err("error: give the id from the drop message, e.g. py main.py channels post 12")
        return 2
    try:
        print(publish_claim(db, int(args.names[0]), TelegramPublisher(), args.publish))
    except ValueError as exc:
        _err(f"error: {exc}")
        return 2
    except PublishError as exc:
        _err(f"Telegram problem: {exc}")
        return 1
    return 0


def _channels_run(args) -> int:
    from .channels import channel_page, leads_text
    path = Path(args.config)
    cfg = load_automation(str(path)) if path.exists() else {}
    names = _channels_names(cfg)
    if args.action in ("add", "remove"):
        if not path.exists():
            _err(f"error: {path} not found")
            return 2
        for raw in args.names:
            try:
                name = channel_page(raw).rsplit("/", 1)[-1]
            except ValueError as exc:
                _err(f"skipped: {exc}")
                continue
            if args.action == "add" and name not in names:
                names.append(name)
                print(f"added: {name}")
            elif args.action == "remove" and name in names:
                names.remove(name)
                print(f"removed: {name}")
        cfg["telegram_channels"] = names
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    if args.action == "list":
        for n in names:
            print(f"  https://t.me/{n}")
        if not names:
            print("(no channels yet - e.g. py main.py channels add KSPcoil payngoil)")
        return 0
    db = Database(cfg.get("db", DEFAULT_DB))
    try:
        if args.action == "fetch":
            fetch_channels(cfg, db, args.delay)
            return 0
        if args.action == "drops":
            from .channels import drop_text, new_drops
            drops = new_drops(db)
            for r in drops:
                print(drop_text(db, r) + "\n")
            if not drops:
                print("No new price drops in the stores' channels.")
            return 0
        if args.action == "post":
            return _channels_post(args, db)
        if args.action == "auto":
            return _channels_auto(args, path, cfg)
        text = leads_text(db)
        if not text:
            print("No new products from the channels in the last 48 hours.")
            return 1
        print(text)
        if args.telegram:
            from .telegram import TelegramPublisher
            from .webhook import PublishError
            tg = TelegramPublisher()
            if tg.admin_chat:
                try:
                    tg.send_long(tg.admin_chat, text)
                    print("\nSent to your Telegram chat.")
                except PublishError as exc:
                    _err(f"Notification problem: {exc}")
        return 0
    finally:
        db.close()


cli.register_subcommand("channels", "Stores' public Telegram channels as leads (store-stated prices, not verified)",
                        _channels_build, _channels_run)


# ---------------------------------------------------------------- channel menu: browse by store
def _menu_build(ap) -> None:
    ap.add_argument("--config", default="automation.json")
    ap.add_argument("--publish", action="store_true",
                    help="post it in the PUBLIC channel and pin it (without this: a preview to your private chat)")
    ap.add_argument("--no-check", action="store_true", help=argparse.SUPPRESS)


def channel_menu(db: Database, site_cfg) -> tuple[str, list[tuple[str, str]]]:
    from .catalog import collect_catalog, store_slug
    catalog = collect_catalog(db)
    utm = "?utm_source=telegram&utm_medium=pinned&utm_campaign=store-menu"
    buttons = [(f"🏬 {catalog[d]['name']}", f"{site_cfg.base_url}stores/{store_slug(d)}.html{utm}")
               for d in sorted(catalog, key=lambda d: catalog[d]["name"])]
    buttons.append(("🔎 כל החנויות", f"{site_cfg.base_url}stores/index.html{utm}"))
    text = ("🏬 בחרו חנות, ואחר כך קטגוריה:\n"
            "בכל קטגוריה מופיעים 5 המוצרים הזולים ו-5 הפופולריים, במחירים שנבדקו ב-48 השעות האחרונות.\n\n"
            "הקישורים עשויים להיות קישורי שותפים. המחיר הקובע הוא באתר החנות.")
    return text, buttons


def _menu_run(args) -> int:
    from .site import SiteConfig
    from .telegram import TelegramPublisher
    from .webhook import PublishError
    cfg = load_automation(args.config) if Path(args.config).exists() else {}
    site_cfg = SiteConfig.from_dict(cfg.get("site"))
    if not site_cfg.base_url.startswith("https://"):
        _err("error: the website address is not set (py main.py site setup), the buttons would lead nowhere")
        return 2
    db = Database(cfg.get("db", DEFAULT_DB))
    try:
        text, buttons = channel_menu(db, site_cfg)
    finally:
        db.close()
    if len(buttons) < 2:
        _err("No store has checked products yet - run the automation first (menu 6), then build the site.")
        return 1
    if not args.no_check:
        # every button must open a real page: the store pages exist on the site only after "site publish"
        import requests
        missing = []
        for t, u in buttons:
            try:
                if requests.get(u.split("?")[0], timeout=20).status_code != 200:
                    missing.append(t)
            except requests.RequestException:
                missing.append(t)
        if missing:
            _err("error: these pages are not on the website yet: " + ", ".join(missing) + ". Run "
                 "'py main.py site publish', wait a minute or two for GitHub Pages, then try again.")
            return 1
    print(text + "\n\n" + "\n".join(f"[{t}] {u}" for t, u in buttons))
    tg = TelegramPublisher()
    target = tg.channel if args.publish else tg.admin_chat
    if not target:
        _err("error: set " + ("TELEGRAM_CHANNEL" if args.publish else "TELEGRAM_ADMIN_CHAT"))
        return 2
    try:
        tg.send_menu(target, text, buttons, pin=args.publish)
    except PublishError as exc:
        _err(f"Telegram problem: {exc}" + (" - the bot needs the 'pin messages' admin right" if args.publish else ""))
        return 1
    print("\nPosted in the channel and pinned." if args.publish else
          "\nPreview sent to your private chat. To post it in the channel and pin it: py main.py channel-menu --publish")
    return 0


cli.register_subcommand("channel-menu", "A pinned channel post with a button per store (store -> category -> top 5)",
                        _menu_build, _menu_run)


# ---------------------------------------------------------------- progress of the agent manager
PROGRESS_FILE = Path("agents_output") / "progress.jsonl"
PROGRESS_KINDS = {"plan": "📋 התוכנית", "start": "▶️ התחיל", "done": "✔️ סיים", "qa-pass": "🔍 QA: PASS",
                  "qa-fail": "🔍 QA: FAIL", "fix": "🔁 תיקון", "blocked": "⚠️ נתקע", "final": "🏁 הסתיים"}


def _progress_build(ap) -> None:
    ap.add_argument("kind", choices=sorted(PROGRESS_KINDS), help="the stage")
    ap.add_argument("message", nargs="+", help="short text, e.g. 'web-scanner: בדיקת דפי מבצעים'")
    ap.add_argument("--no-telegram", action="store_true", help="only write it down, do not message the owner")


def read_progress(since: Optional[str] = None, limit: int = 10) -> list[dict]:
    try:
        lines = PROGRESS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            entry = json.loads(ln)
        except ValueError:
            continue
        if not since or entry.get("at", "") >= since:
            out.append(entry)
    return out[-limit:]


def progress_line(entry: dict) -> str:
    return f"{PROGRESS_KINDS.get(entry.get('kind'), entry.get('kind'))}: {entry.get('message', '')}"


def _progress_run(args) -> int:
    """The agent manager reports each stage: written to agents_output/progress.jsonl and sent to the owner's
    private chat (allowed also while public actions are locked)."""
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="microseconds"), "kind": args.kind,
             "message": " ".join(args.message)[:300]}
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with PROGRESS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    text = progress_line(entry)
    print(text)
    if not args.no_telegram:
        from .telegram import TelegramPublisher
        from .webhook import PublishError
        tg = TelegramPublisher()
        if tg.token and tg.admin_chat:
            try:
                tg.notify_admin(text)
            except PublishError as exc:
                _err(f"(not sent to Telegram: {exc})")
    return 0


cli.register_subcommand("progress", "The agent manager reports a stage (plan, start, done, qa-pass, qa-fail, fix, "
                        "blocked, final) - to agents_output/progress.jsonl and your Telegram", _progress_build, _progress_run)


# ---------------------------------------------------------------- Instagram carousel images
def _carousel_build(ap) -> None:
    ap.add_argument("--ids", type=int, nargs="*", default=None, help="these published deals (default: today's best)")
    ap.add_argument("--count", type=int, default=5, help="how many deals (1-8, default 5)")
    ap.add_argument("--hours", type=float, default=48, help="deals published in the last N hours (default 48)")
    ap.add_argument("--no-product-images", action="store_true",
                    help="the category icon instead of the store's product photo (if you are unsure about image rights)")
    ap.add_argument("--telegram", action="store_true", help="send the slides and the caption to your private chat")
    ap.add_argument("--config", default="automation.json")


def _carousel_run(args) -> int:
    from .carousel import caption_text, render, slides_html
    from .digest import pick_deals
    from .site import SiteConfig, collect_deals
    if not 1 <= args.count <= 8:
        _err("error: --count must be 1-8 (an Instagram carousel has up to 10 slides, 2 are the cover and the end)")
        return 2
    cfg = load_automation(args.config) if Path(args.config).exists() else {}
    site_cfg = SiteConfig.from_dict(cfg.get("site"))
    db = Database(cfg.get("db", DEFAULT_DB))
    try:
        if args.ids:
            by_id = {d["id"]: d for d in collect_deals(db, site_cfg)}
            deals = [by_id[i] for i in args.ids if i in by_id and not by_id[i]["ended"]]
            missing = [i for i in args.ids if i not in by_id or by_id[i]["ended"]]
            if missing:
                _err(f"skipped (not published, or the price went back up): {', '.join(map(str, missing))}")
        else:
            deals = pick_deals(db, site_cfg, count=args.count, hours=args.hours)
    finally:
        db.close()
    if not deals:
        _err("No deals for a carousel right now: only published, active deals with a proven saving "
             f"from the last {args.hours:g} hours. (py main.py digest shows the same list.)")
        return 1
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out = Path(cfg.get("out_dir", "output")) / "carousel" / stamp
    pages = slides_html(deals, site_cfg.telegram_url, product_images=not args.no_product_images)
    try:
        files = render(pages, out)
    except RuntimeError as exc:
        _err(f"error: {exc}")
        return 2
    caption = caption_text(deals, site_cfg.telegram_url)
    (out / "caption.txt").write_text(caption + "\n", encoding="utf-8")
    print(f"{len(files)} slides (1080x1350) and caption.txt in {out}")
    if not args.no_product_images:
        print("Note: product photos belong to the stores. If unsure, run again with --no-product-images.")
    if args.telegram:
        from .telegram import TelegramPublisher
        from .webhook import PublishError
        tg = TelegramPublisher()
        if not tg.admin_chat:
            _err("TELEGRAM_ADMIN_CHAT is not set - the files are only saved here.")
            return 0
        try:
            tg.send_album(tg.admin_chat, files)
            tg.notify_admin("📸 קרוסלה לאינסטגרם (טיוטה, לא פורסם):\n\n" + caption)
            print("Sent to your Telegram chat.")
        except PublishError as exc:
            _err(f"Telegram problem: {exc}")
            return 1
    return 0


cli.register_subcommand("carousel", "Instagram carousel images (1080x1350) from verified deals - drafts for you",
                        _carousel_build, _carousel_run)


# ---------------------------------------------------------------- real screens of the website (video assets)
SCREEN_PAGES = {"home": "index.html", "stores": "stores/index.html", "search": "search.html?q=PS5"}


def _screens_build(ap) -> None:
    ap.add_argument("--size", default="1080x1920", help="WIDTHxHEIGHT of the screenshots (1080x1920 = phone / Reel)")
    ap.add_argument("--pages", nargs="*", default=None,
                    help="site paths (default: home, stores, search, the newest deal page and one category page)")
    ap.add_argument("--config", default="automation.json")


def _screens_run(args) -> int:
    """Screenshots of the REAL website (the built site folder) for videos and posts - no invented interface."""
    import re
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from .site import SiteConfig
    m = re.fullmatch(r"(\d{3,4})x(\d{3,4})", args.size)
    if not m:
        _err("error: --size must look like 1080x1920")
        return 2
    w, h = int(m.group(1)), int(m.group(2))
    cfg = load_automation(args.config) if Path(args.config).exists() else {}
    site_dir = Path(SiteConfig.from_dict(cfg.get("site")).out_dir)
    if not (site_dir / "index.html").exists():
        _err(f"error: no built website in {site_dir} - run 'py main.py site build' first")
        return 2
    pages = args.pages
    if not pages:
        pages = list(SCREEN_PAGES.values())
        deals = sorted((site_dir / "deals").glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
        cats = sorted((site_dir / "stores").glob("*/*.html"))
        pages += [f"deals/{deals[0].name}"] if deals else []
        pages += [f"stores/{cats[0].parent.name}/{cats[0].name}"] if cats else []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _err("error: Playwright is not installed: py -m pip install playwright && py -m playwright install chromium")
        return 2
    out = Path(cfg.get("out_dir", "output")) / "screens" / datetime.now().strftime("%Y-%m-%d_%H%M")
    out.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(site_dir)))
    server.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/"
    exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
    saved = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=exe, headless=True, args=["--no-sandbox"] if exe else [])
            page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
            for path in pages:
                page.goto(base + path.lstrip("/"), wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:  # noqa: BLE001 - render what loaded
                    pass
                name = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-") or "home"
                target = out / f"{name}.png"
                page.screenshot(path=str(target))
                saved.append(target)
            browser.close()
    finally:
        server.shutdown()
    for f in saved:
        print(f)
    print(f"\n{len(saved)} real screenshots ({w}x{h}) in {out}")
    return 0


cli.register_subcommand("screens", "Real screenshots of your website for videos and posts (default 1080x1920)",
                        _screens_build, _screens_run)


# ===================================================================== graphics the Telegram manager may run
def _send_to_owner(paths: list, caption: str, album: bool = False) -> int:
    """Files to YOUR private Telegram chat (never the channel). 0 = sent or not configured, 1 = failed."""
    from .telegram import TelegramPublisher
    from .webhook import PublishError
    tg = TelegramPublisher()
    if not tg.admin_chat:
        _err("TELEGRAM_ADMIN_CHAT is not set - the files are only saved here.")
        return 0
    try:
        if album:
            tg.send_album(tg.admin_chat, paths[:10], caption)
        else:
            for i, p in enumerate(paths):
                tg.send_file(tg.admin_chat, p, caption if i == 0 else "")
        print("Sent to your Telegram chat.")
        return 0
    except PublishError as exc:
        _err(f"Telegram problem: {exc}")
        return 1


def _profile_pic_build(ap) -> None:
    ap.add_argument("--logo", default="video/first-reel/assets/logo.png", help="the round logo (PNG)")
    ap.add_argument("--out", default="output/profile-pics", help="folder for the pictures (default %(default)s; brand/ keeps the approved ones)")
    ap.add_argument("--telegram", action="store_true", help="send them to your private chat")


def _profile_pic_run(args) -> int:
    from .media import MediaError, inside_project, profile_pics
    try:
        files = profile_pics(inside_project(Path.cwd(), args.logo), inside_project(Path.cwd(), args.out))
    except MediaError as exc:
        _err(f"error: {exc}")
        return 2
    for f in files:
        print(f)
    if args.telegram:
        return _send_to_owner([files[2], files[0], files[1]],
                              "🖼️ תמונות פרופיל לאינסטגרם (1080x1080): א - הלוגו המלא, ב - העגלה. "
                              "התצוגה מראה אותן בעיגול, בגדול ובגודל האמיתי. לא הועלה לשום מקום.")
    return 0


cli.register_subcommand("profile-pic", "Two Instagram profile pictures from the logo + a round preview",
                        _profile_pic_build, _profile_pic_run)


def _reel_build(ap) -> None:
    ap.add_argument("action", choices=["new", "check", "snapshot", "render"],
                    help="new = copy the first reel as a starting point; check / snapshot / render with HyperFrames")
    ap.add_argument("name", help="the reel folder inside video/ (e.g. week-48)")
    ap.add_argument("--at", default="", help="snapshot: seconds, e.g. 1,4,8,12,16")
    ap.add_argument("--telegram", action="store_true", help="snapshot / render: send the result to your private chat")


def _reel_run(args) -> int:
    from .media import MediaError, latest_snapshots, reel_dir, reel_new, reel_run
    root = Path.cwd()
    try:
        if args.action == "new":
            dst = reel_new(root, args.name)
            print(f"New reel project: {dst}\nEdit compositions/*.html and assets/, then: "
                  f"py main.py reel check {args.name}")
            return 0
        project = reel_dir(root, args.name)
        code, tail, mp4 = reel_run(args.action, project, args.at)
    except MediaError as exc:
        _err(f"error: {exc}")
        return 2
    print(tail)
    if code != 0:
        _err(f"{args.action} failed (exit {code}).")
        return 1
    if args.action == "render":
        print(f"Video: {mp4}")
        if args.telegram:
            return _send_to_owner([mp4], f"🎬 ריל {args.name} (טיוטה, לא פורסם). לפני העלאה: QA ובדיקת מחיר.")
    if args.action == "snapshot":
        frames = latest_snapshots(project)
        print("\n".join(map(str, frames)))
        if args.telegram and frames:
            return _send_to_owner(frames, f"🖼️ פריימים מהריל {args.name}", album=True)
    return 0


cli.register_subcommand("reel", "A HyperFrames reel in video/<name>: new, check, snapshot, render (needs Node.js)",
                        _reel_build, _reel_run)


# ---------------------------------------------------------------- fix posts already in the channel
def _fix_posts_build(ap) -> None:
    ap.add_argument("--apply", action="store_true", help="edit the posts in the PUBLIC channel (default: only list them)")
    ap.add_argument("--config", default="automation.json")


def _fix_posts_run(args) -> int:
    """Posts published before the 90-day rule was fixed still say "lowest in 90 days": list them, or with
    --apply rewrite each one in place ("lowest since <date>", or the line removed). Your decision to run it."""
    import json as _json
    from .alerts import posts_to_fix
    from .site import SiteConfig, page_url
    from .telegram import TelegramPublisher
    from .webhook import PublishError
    cfg = load_automation(args.config) if Path(args.config).exists() else {}
    site = cfg.get("site") or {}
    site_cfg = SiteConfig.from_dict(site)
    db = Database(cfg.get("db", DEFAULT_DB))
    try:
        todo = posts_to_fix(db, (lambda aid: page_url(site_cfg, aid)) if site.get("enabled") else None)
        if not todo:
            print("No post needs fixing.")
            return 0
        for t in todo:
            print(f"#{t['id']} (message {t['ref']['message_id']}): {t['title'][:70]}")
        if not args.apply:
            print(f"\n{len(todo)} post(s) say 'lowest in 90 days' without 90 days of history. "
                  "To rewrite them in the channel: py main.py fix-posts --apply")
            return 0
        tg = TelegramPublisher()
        done = failed = 0
        for t in todo:
            try:
                how = tg.edit_deal(t["ref"].get("chat_id") or tg.channel, t["ref"]["message_id"], t["payload"])
            except PublishError as exc:
                _err(f"#{t['id']}: {exc}")
                failed += 1
                continue
            with db.tx() as c:            # the saved text matches the channel now, so it is not fixed twice
                c.execute("UPDATE deal_alerts SET content_json=? WHERE id=?",
                          (_json.dumps(t["content"], ensure_ascii=False), t["id"]))
            print(f"#{t['id']}: fixed ({how})")
            done += 1
        print(f"\n{done} fixed, {failed} failed.")
        return 0 if not failed else 1
    finally:
        db.close()


cli.register_subcommand("fix-posts", "Rewrite channel posts that say 'lowest in 90 days' without 90 days of history",
                        _fix_posts_build, _fix_posts_run)

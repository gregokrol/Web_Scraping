"""Scrape hooks (SQLite history + scores) and extra subcommands.

Registered with cli.py so the classic ``py main.py <urls>`` stays unchanged.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import cli
from .db import DEFAULT_DB, Database
from .history import check_discount_claim, detect_price_drop, is_lowest_in_90d, stats_for_offer
from .scoring import apply_scores
from .utils import parse_int, parse_price


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


def automation_scrape_jobs(cfg: dict) -> list[list[str]]:
    """Build one scrape argv per store / target group (each becomes a crawl_run)."""
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
    jobs = []
    if cfg.get("sources_config") and Path(cfg["sources_config"]).exists():
        from .sources import load_stores
        for s in load_stores(cfg["sources_config"]):
            if not s.enabled:
                continue
            if s.type == "feed" and s.feed_url:
                jobs.append(["--feed", s.feed_url, "--name", f"auto_{s.domain}", *common])
            elif s.start_urls:
                argv = [*s.start_urls, "--discover", s.discover or "none", "--name", f"auto_{s.domain}", *common]
                if s.include:
                    argv += ["--include", s.include]
                # per-store politeness (later options override the common ones)
                if s.delay is not None:
                    argv += ["--delay", str(s.delay)]
                if s.max_pages is not None:
                    argv += ["--max-pages", str(s.max_pages)]
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


def _run_run(args) -> int:
    from .alerts import DealRules, detect_deals, notify_pending, publish_approved
    from .webhook import PublishError, send_event, webhook_url
    try:
        cfg = load_automation(args.config)
    except (OSError, ValueError) as exc:
        _err(f"error: cannot read {args.config}: {exc}")
        return 2
    jobs = automation_scrape_jobs(cfg)
    if not jobs:
        _err("error: nothing to do - add stores (sources_config) or targets/feeds to the automation config")
        return 2
    if args.dry_run:
        for j in jobs:
            print("would run: py main.py " + " ".join(j))
            cli.scrape_main([*j, "--dry-run"])
        return 0

    started = datetime.now(timezone.utc).isoformat()
    codes = []
    for j in jobs:
        _err("\n=== scrape: " + " ".join(j[:3]) + (" ..." if len(j) > 3 else ""))
        codes.append(cli.scrape_main([*j, *(["-v"] * args.verbose)]))

    db = Database(cfg.get("db", DEFAULT_DB))
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

    if notify_targets:
        n = notify_pending(db, notify_all)
        failed = db.query("SELECT id, command, errors, errors_json FROM crawl_runs WHERE started_at >= ? "
                          "AND (status <> 'ok' OR errors > 0)", (started,))
        if failed:
            notify_all("crawl.errors", {"runs": [{"id": r["id"], "command": r["command"], "errors": r["errors"],
                                                  "sample": json.loads(r["errors_json"] or "[]")[:10]} for r in failed]})
        _err(f"Notified you: {n} pending deal(s), {len(failed)} run(s) with errors.")
    if publish_target:
        auto = cfg.get("require_approval", True) is False
        res = publish_approved(db, publish_target, auto_approve=auto)
        if res["published"] or res["failed"]:
            _err(f"Published to {'Telegram' if tg else 'n8n'}: {res}" + (" (auto-approved)" if auto else ""))
    else:
        _err("No publishing destination configured - deals wait in the dashboard / `py main.py alerts`.")
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


def _schedule_run(args) -> int:
    from .scheduler import build_bat, build_schtasks_create, build_schtasks_delete, run_schtasks
    project = Path.cwd()
    try:
        if args.remove:
            cmd = build_schtasks_delete(args.task_name)
            print(" ".join(cmd))
            return run_schtasks(cmd)
        bat = project / "run_automation.bat"
        cmd = build_schtasks_create(args.task_name, bat, args.every, args.at, args.hours)
    except ValueError as exc:
        _err(f"error: {exc}")
        return 2
    bat.write_text(build_bat(project, sys.executable, args.config), encoding="utf-8")
    print(f"Wrote {bat}")
    print("Task Scheduler command:\n  " + " ".join(cmd))
    if not args.create:
        print("\n(Not registered. Add --create to register it, or paste the command into CMD.)")
        return 0
    return run_schtasks(cmd)


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

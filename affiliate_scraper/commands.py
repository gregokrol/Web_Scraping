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

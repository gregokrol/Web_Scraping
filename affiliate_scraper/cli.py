"""Command-line interface.

The classic form still works exactly as in v2::

    py main.py https://shop.example/products/blue-mug -c affiliate_config.json

Examples
--------
Discover products from a site's sitemap, keep verified in-stock ones::

    py main.py https://shop.example --discover sitemap --max-pages 200 ^
        --min-rating 4.2 --in-stock-only --verified-only -c affiliate_config.json --top 30

Official sources (preferred)::

    py main.py --feed https://merchant.example/google_feed.xml
    py main.py --feed C:\\feeds\\awin_feed.csv
    py main.py --ebay-gtin 0885909950805          (needs EBAY_CLIENT_ID / EBAY_CLIENT_SECRET)

Dynamic site that allows crawling (needs Playwright)::

    py main.py https://shop.example/p/1 --render js

Check what would happen without fetching product pages or writing anything::

    py main.py https://shop.example --discover sitemap --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .affiliate import AffiliateConfig
from .discovery import DEFAULT_PRODUCT_PATTERN, from_listing_pages, from_sitemaps
from .exporters import DEFAULT_DISCLOSURE, EXPORTERS, export_run
from .fetcher import DEFAULT_USER_AGENT, PoliteFetcher
from .pipeline import SORT_KEYS, Filters, classify_target, run_scrape
from .security import UnsafeURLError, check_url, safe_regex
from .sources import load_stores

SUBCOMMANDS: dict[str, str] = {}  # name -> help; filled by register_subcommand()
_HANDLERS: dict = {}


def register_subcommand(name: str, help_text: str, builder, handler) -> None:
    SUBCOMMANDS[name] = help_text
    _HANDLERS[name] = (builder, handler)


def build_parser() -> argparse.ArgumentParser:
    sub_help = "\n".join(f"  py main.py {k:<10} {v}" for k, v in SUBCOMMANDS.items())
    ap = argparse.ArgumentParser(
        prog="affiliate-scraper",
        description="Collect public product data, compare prices across stores and build affiliate-ready lists.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__ + ("\nOther commands (py main.py <command> --help):\n" + sub_help if sub_help else ""),
    )
    ap.add_argument("urls", nargs="*", help="Product page URLs, or site/category URLs when using --discover")
    ap.add_argument("-f", "--from-file", help="Text file with one URL per line")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    g = ap.add_argument_group("sources")
    g.add_argument("--sources", help="Store settings JSON (see sources.example.json)")
    g.add_argument("--feed", action="append", default=[], help="Product feed URL or file (Google XML / CSV). Repeatable")
    g.add_argument("--ebay-search", action="append", default=[], help="eBay Browse API keyword search. Repeatable")
    g.add_argument("--ebay-gtin", action="append", default=[], help="eBay Browse API search by barcode. Repeatable")
    g.add_argument("--render", choices=["static", "js"], default="static",
                   help="js = render pages with Playwright (only for sites that allow crawling)")
    g.add_argument("--browser-path", help="Chromium executable for --render js (or env PLAYWRIGHT_CHROMIUM_EXECUTABLE)")

    g = ap.add_argument_group("discovery")
    g.add_argument("--discover", choices=["none", "sitemap", "links"], default="none",
                   help="sitemap: read the site's XML sitemaps; links: follow links on the given listing pages")
    g.add_argument("--include", default=DEFAULT_PRODUCT_PATTERN,
                   help="Regex a URL must match to be treated as a product page (default: %(default)s)")
    g.add_argument("--max-pages", type=int, default=50, help="Max product pages to fetch (default: %(default)s)")

    g = ap.add_argument_group("politeness, cache & safety")
    g.add_argument("--delay", type=float, default=2.0, help="Seconds between requests to the same host (default: %(default)s)")
    g.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent header (identify your bot honestly)")
    g.add_argument("--ignore-robots", action="store_true",
                   help="Do NOT check robots.txt. Only for sites you own or have written permission to crawl.")
    g.add_argument("--cache-dir", default=".cache", help="Cache fetched pages here ('' to disable)")
    g.add_argument("--no-cache", action="store_true", help="Always download fresh pages (cache neither read nor written)")
    g.add_argument("--cache-ttl-hours", type=float, default=12.0, help="Max age of cached pages (default: %(default)s)")
    g.add_argument("--max-age-hours", type=float, default=24.0,
                   help="Prices older than this are marked stale / unverified (default: %(default)s)")
    g.add_argument("--timeout", type=float, default=20.0)
    g.add_argument("--allow-private-hosts", action="store_true",
                   help="Allow localhost / private network URLs (only for testing your own local store)")
    g.add_argument("--dry-run", action="store_true",
                   help="Show what would be fetched (robots / safety checks) - no product pages, no files, no DB")

    g = ap.add_argument_group("filters & ranking")
    g.add_argument("--min-rating", type=float)
    g.add_argument("--min-reviews", type=int)
    g.add_argument("--min-price", type=float)
    g.add_argument("--max-price", type=float)
    g.add_argument("--min-discount", type=float, help="Minimum claimed discount %% vs list price")
    g.add_argument("--in-stock-only", action="store_true", help="Only products the store confirms are in stock")
    g.add_argument("--verified-only", action="store_true", help="Only products with a verified, fresh price")
    g.add_argument("--keyword", action="append", default=[], help="Keep products mentioning this word (repeatable)")
    g.add_argument("--sort", choices=list(SORT_KEYS), default="score")
    g.add_argument("--top", type=int, help="Keep only the top N products after sorting")
    g.add_argument("--match-confidence", type=float, default=0.75,
                   help="Min confidence to treat offers from different stores as the same product (default: %(default)s)")

    g = ap.add_argument_group("affiliate & output")
    g.add_argument("-c", "--config", help="Affiliate programs JSON (see affiliate_config.example.json)")
    g.add_argument("-o", "--out-dir", default="output")
    g.add_argument("--name", help="Output file base name (default: products_<timestamp>)")
    g.add_argument("--formats", default="csv,json,html", help=f"Comma list of: {','.join(EXPORTERS)}")
    g.add_argument("--title", default="Top Picks", help="Title for HTML/Markdown pages")
    g.add_argument("--disclosure", default=DEFAULT_DISCLOSURE, help="Affiliate disclosure text for HTML/Markdown")
    g.add_argument("-v", "--verbose", action="count", default=0)
    for hook in _SCRAPE_ARG_HOOKS:
        hook(ap)
    return ap


_SCRAPE_ARG_HOOKS: list = []      # extra scrape options added by later modules (db, scoring)
_SCRAPE_POST_HOOKS: list = []     # factories (args, stores, config, fetcher) -> object with post_process()/finish()


def setup_logging(verbose: int) -> None:
    logging.basicConfig(level=logging.WARNING - 10 * min(verbose, 2), format="%(levelname)s %(name)s: %(message)s")


def make_fetcher(args) -> PoliteFetcher:
    return PoliteFetcher(
        user_agent=args.user_agent,
        min_delay=args.delay,
        timeout=args.timeout,
        respect_robots=not args.ignore_robots,
        cache_dir=None if args.no_cache else (args.cache_dir or None),
        use_cache=not args.no_cache,
        cache_ttl_hours=args.cache_ttl_hours,
        allow_private=args.allow_private_hosts,
    )


def collect_targets(args, fetcher: PoliteFetcher, err) -> list[str] | None:
    urls = list(args.urls)
    if args.from_file:
        try:
            urls += [ln.strip() for ln in Path(args.from_file).read_text(encoding="utf-8-sig").splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#")]
        except OSError as exc:
            err(f"error: cannot read {args.from_file}: {exc}")
            return None
    for u in urls:
        try:
            check_url(u, args.allow_private_hosts)
        except UnsafeURLError as exc:
            err(f"error: refusing URL {u!r}: {exc}")
            return None
    try:
        safe_regex(args.include)
    except ValueError as exc:
        err(f"error: --include: {exc}")
        return None

    if args.discover == "sitemap":
        found: list[str] = []
        for site in urls:
            found += from_sitemaps(fetcher, site, include=args.include, limit=args.max_pages - len(found))
            if len(found) >= args.max_pages:
                break
        urls = found
    elif args.discover == "links":
        urls = from_listing_pages(fetcher, urls, include=args.include, limit=args.max_pages)
    else:
        urls = urls[: args.max_pages]
    urls += [f"feed:{f}" for f in args.feed]
    urls += [f"ebay:search:{q}" for q in args.ebay_search] + [f"ebay:gtin:{g}" for g in args.ebay_gtin]
    return urls


def scrape_main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    def err(msg: str) -> None:
        print(msg, file=sys.stderr)

    if not (args.urls or args.from_file or args.feed or args.ebay_search or args.ebay_gtin):
        parser.print_usage(sys.stderr)
        err("error: give at least one URL, --from-file, --feed or --ebay-search/--ebay-gtin")
        return 2

    formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    unknown = set(formats) - set(EXPORTERS)
    if unknown:
        err(f"error: unknown format(s): {', '.join(sorted(unknown))}")
        return 2
    for name, value in (("--delay", args.delay), ("--cache-ttl-hours", args.cache_ttl_hours),
                        ("--max-age-hours", args.max_age_hours), ("--timeout", args.timeout)):
        if value is None or value < 0:
            err(f"error: {name} must be >= 0")
            return 2
    if not 0 < args.match_confidence <= 1:
        err("error: --match-confidence must be between 0 and 1")
        return 2

    try:
        config = AffiliateConfig.load(args.config)
    except (OSError, ValueError) as exc:
        err(f"error: cannot read config {args.config}: {exc}")
        return 2
    try:
        stores = load_stores(args.sources)
    except (OSError, ValueError, TypeError) as exc:
        err(f"error: cannot read sources {args.sources}: {exc}")
        return 2

    if args.ignore_robots:
        err("WARNING: robots.txt checks disabled - make sure you are allowed to crawl these sites.")

    fetcher = make_fetcher(args)
    targets = collect_targets(args, fetcher, err)
    if targets is None:
        return 2

    if args.dry_run:
        err(f"DRY RUN - {len(targets)} target(s); nothing will be downloaded (except robots.txt/sitemaps) or written.")
        for t in targets:
            adapter, norm = classify_target(t, stores, args.render)
            if norm.startswith(("http://", "https://")):
                status = "allowed" if fetcher.allowed(norm) else "BLOCKED (robots.txt or unsafe URL)"
            else:
                status = "local/API source"
            print(f"  [{adapter:7}] {status:38} {t}")
        if fetcher.errors:
            err(f"{len(fetcher.errors)} problem(s) during discovery:")
            for e in fetcher.errors[:20]:
                err(f"  {e.kind:18} {e.url} {e.message}")
        return 0

    err(f"Fetching {len(targets)} target(s)...")
    filters = Filters(
        min_rating=args.min_rating, min_reviews=args.min_reviews,
        min_price=args.min_price, max_price=args.max_price, min_discount=args.min_discount,
        in_stock_only=args.in_stock_only, verified_only=args.verified_only, keywords=tuple(args.keyword),
    )

    def progress(i: int, n: int, url: str) -> None:
        err(f"  [{i}/{n}] {url}")

    hooks = [h for h in (hook(args, stores, config, fetcher) for hook in _SCRAPE_POST_HOOKS) if h]

    def post_process(products, comparisons):
        for h in hooks:
            h.post_process(products, comparisons)

    try:
        result = run_scrape(targets, fetcher, config, filters, sort_by=args.sort, limit=args.top, progress=progress,
                            stores=stores, render=args.render, max_age_hours=args.max_age_hours,
                            browser_path=args.browser_path, match_confidence=args.match_confidence,
                            post_process=post_process if hooks else None)
    except BaseException:
        for h in hooks:
            h.finish(None)
        raise
    for h in hooks:
        h.finish(result)

    name = args.name or f"products_{datetime.now():%Y%m%d_%H%M%S}"
    if not result.products:
        err("No products found (pages may lack structured data, be blocked by robots.txt, or be filtered out).")
        if result.errors:
            from .exporters import errors_to_csv
            Path(args.out_dir).mkdir(parents=True, exist_ok=True)
            path = errors_to_csv(result.errors, Path(args.out_dir) / f"{name}_errors.csv")
            err(f"Error log: {path}")
        return 1

    paths = export_run(result, args.out_dir, name, formats, title=args.title, disclosure=args.disclosure)

    verified = sum(p.price_verified for p in result.products)
    err(f"\n{len(result.products)} product(s), {verified} with verified price, "
        f"{sum(1 for c in result.comparisons if c.n_stores >= 2)} cross-store match group(s), "
        f"{len(result.errors)} error(s):")
    for p in result.products[:10]:
        price = f"{p.price:,.2f} {p.currency}" if p.price is not None else "-"
        flag = "" if p.price_verified else " (unverified)"
        deal = f"D{p.deal_score:>3.0f}" if p.deal_score is not None else "D  -"
        err(f"  {p.score:5.1f} {deal}  {price:>14}{flag}  {p.name[:55]}")
    err("\nWrote:\n" + "\n".join(f"  {p}" for p in paths))
    return 0


def _safe_console() -> None:
    """Windows: redirected output (Task Scheduler logs) uses the legacy code page, and a product
    name with an emoji would crash the run. Write UTF-8 to files/pipes and never fail on a character."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _safe_console()
    _load_plugins()
    if argv and argv[0] in _HANDLERS:
        builder, handler = _HANDLERS[argv[0]]
        parser = argparse.ArgumentParser(prog=f"affiliate-scraper {argv[0]}", description=SUBCOMMANDS[argv[0]])
        builder(parser)
        args = parser.parse_args(argv[1:])
        setup_logging(getattr(args, "verbose", 0))
        return handler(args)
    return scrape_main(argv)


_PLUGINS_LOADED = False


def _load_plugins() -> None:
    """Import modules that register subcommands / scrape hooks (kept separate to keep this file readable)."""
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED:
        return
    _PLUGINS_LOADED = True
    import importlib
    for mod in ("affiliate_scraper.commands",):
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError as exc:
            if exc.name != mod:
                raise


if __name__ == "__main__":
    raise SystemExit(main())

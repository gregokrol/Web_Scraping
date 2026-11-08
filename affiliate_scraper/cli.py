"""Command-line interface.

Examples
--------
Scrape specific product pages::

    python main.py https://shop.example/products/blue-mug https://shop.example/products/red-mug

Discover products from a site's sitemap, keep the best-rated in-stock ones, export everything::

    python main.py https://shop.example --discover sitemap --max-pages 200 \\
        --min-rating 4.2 --min-reviews 20 --in-stock-only \\
        --config affiliate_config.json --formats csv,json,html,md --top 30

Follow links from a category page::

    python main.py https://shop.example/collections/coffee --discover links --include "/products/"
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
from .exporters import DEFAULT_DISCLOSURE, EXPORTERS, export
from .fetcher import DEFAULT_USER_AGENT, PoliteFetcher
from .pipeline import Filters, scrape


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="affiliate-scraper",
        description="Collect public product data (schema.org / OpenGraph) and build affiliate-ready product lists.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("urls", nargs="*", help="Product page URLs, or site/category URLs when using --discover")
    ap.add_argument("-f", "--from-file", help="Text file with one URL per line")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    g = ap.add_argument_group("discovery")
    g.add_argument("--discover", choices=["none", "sitemap", "links"], default="none",
                   help="sitemap: read the site's XML sitemaps; links: follow links on the given listing pages")
    g.add_argument("--include", default=DEFAULT_PRODUCT_PATTERN,
                   help="Regex a URL must match to be treated as a product page (default: %(default)s)")
    g.add_argument("--max-pages", type=int, default=50, help="Max product pages to fetch (default: %(default)s)")

    g = ap.add_argument_group("politeness")
    g.add_argument("--delay", type=float, default=2.0, help="Seconds between requests to the same host (default: %(default)s)")
    g.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent header (identify your bot honestly)")
    g.add_argument("--ignore-robots", action="store_true",
                   help="Do NOT check robots.txt. Only for sites you own or have written permission to crawl.")
    g.add_argument("--cache-dir", default=".cache", help="Cache fetched pages here ('' to disable)")
    g.add_argument("--timeout", type=float, default=20.0)

    g = ap.add_argument_group("filters & ranking")
    g.add_argument("--min-rating", type=float)
    g.add_argument("--min-reviews", type=int)
    g.add_argument("--min-price", type=float)
    g.add_argument("--max-price", type=float)
    g.add_argument("--min-discount", type=float, help="Minimum discount %% vs list price")
    g.add_argument("--in-stock-only", action="store_true")
    g.add_argument("--keyword", action="append", default=[], help="Keep products mentioning this word (repeatable)")
    g.add_argument("--sort", choices=["score", "rating", "reviews", "discount", "price", "none"], default="score")
    g.add_argument("--top", type=int, help="Keep only the top N products after sorting")

    g = ap.add_argument_group("affiliate & output")
    g.add_argument("-c", "--config", help="Affiliate programs JSON (see affiliate_config.example.json)")
    g.add_argument("-o", "--out-dir", default="output")
    g.add_argument("--name", help="Output file base name (default: products_<timestamp>)")
    g.add_argument("--formats", default="csv,json,html", help=f"Comma list of: {','.join(EXPORTERS)}")
    g.add_argument("--title", default="Top Picks", help="Title for HTML/Markdown pages")
    g.add_argument("--disclosure", default=DEFAULT_DISCLOSURE, help="Affiliate disclosure text for HTML/Markdown")
    g.add_argument("-v", "--verbose", action="count", default=0)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING - 10 * min(args.verbose, 2),
                        format="%(levelname)s %(name)s: %(message)s")

    urls = list(args.urls)
    if args.from_file:
        urls += [ln.strip() for ln in Path(args.from_file).read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
    if not urls:
        build_parser().print_usage(sys.stderr)
        print("error: give at least one URL (or --from-file)", file=sys.stderr)
        return 2

    formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    unknown = set(formats) - set(EXPORTERS)
    if unknown:
        print(f"error: unknown format(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    if args.ignore_robots:
        print("WARNING: robots.txt checks disabled - make sure you are allowed to crawl these sites.", file=sys.stderr)

    fetcher = PoliteFetcher(
        user_agent=args.user_agent,
        min_delay=args.delay,
        timeout=args.timeout,
        respect_robots=not args.ignore_robots,
        cache_dir=args.cache_dir or None,
    )

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
    print(f"Fetching {len(urls)} page(s)...", file=sys.stderr)

    try:
        config = AffiliateConfig.load(args.config)
    except (OSError, ValueError) as exc:
        print(f"error: cannot read config {args.config}: {exc}", file=sys.stderr)
        return 2

    filters = Filters(
        min_rating=args.min_rating, min_reviews=args.min_reviews,
        min_price=args.min_price, max_price=args.max_price, min_discount=args.min_discount,
        in_stock_only=args.in_stock_only, keywords=tuple(args.keyword),
    )

    def progress(i: int, n: int, url: str) -> None:
        print(f"  [{i}/{n}] {url}", file=sys.stderr)

    products = scrape(urls, fetcher, config, filters, sort_by=args.sort, limit=args.top, progress=progress)
    if not products:
        print("No products found (pages may lack structured data, be blocked by robots.txt, or be filtered out).",
              file=sys.stderr)
        return 1

    name = args.name or f"products_{datetime.now():%Y%m%d_%H%M%S}"
    paths = export(products, args.out_dir, name, formats, title=args.title, disclosure=args.disclosure)

    print(f"\n{len(products)} product(s):", file=sys.stderr)
    for p in products[:10]:
        price = f"{p.price:,.2f} {p.currency}" if p.price is not None else "-"
        rating = f"{p.rating:.1f}★" if p.rating is not None else "-"
        print(f"  {p.score:5.1f}  {rating:>5}  {price:>14}  {p.name[:60]}", file=sys.stderr)
    print("\nWrote:", *[f"  {p}" for p in paths], sep="\n", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

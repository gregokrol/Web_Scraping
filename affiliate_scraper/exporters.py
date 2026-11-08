"""Write products to CSV / JSON / HTML / Markdown."""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from .models import Product

DEFAULT_DISCLOSURE = (
    "Disclosure: this page contains affiliate links. If you buy through them we may earn "
    "a commission at no extra cost to you. Prices and availability are accurate as of the "
    "date shown and are subject to change."
)


def to_csv(products: list[Product], path: Path) -> Path:
    # utf-8-sig so Excel shows Hebrew / non-Latin text correctly
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=Product.field_names())
        writer.writeheader()
        for p in products:
            writer.writerow(p.to_dict())
    return path


def to_json(products: list[Product], path: Path) -> Path:
    path.write_text(json.dumps([p.to_dict() for p in products], ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _price(p: Product) -> str:
    if p.price is None:
        return ""
    return f"{p.price:,.2f} {p.currency}".strip()


def _stars(rating: float | None) -> str:
    if rating is None:
        return ""
    full = int(round(rating))
    return "★" * full + "☆" * (5 - full)


def to_markdown(products: list[Product], path: Path, title: str = "Top Picks",
                disclosure: str = DEFAULT_DISCLOSURE) -> Path:
    lines = [f"# {title}", "", f"> {disclosure}", ""]
    for i, p in enumerate(products, 1):
        link = p.affiliate_url or p.url
        lines.append(f"## {i}. [{p.name}]({link})")
        if p.image:
            lines.append(f"\n[![{p.name}]({p.image})]({link})\n")
        meta = []
        if p.brand:
            meta.append(f"**Brand:** {p.brand}")
        if p.price is not None:
            price = f"**Price:** {_price(p)}"
            if p.discount_pct:
                price += f" ~~{p.list_price:,.2f}~~ (-{p.discount_pct:.0f}%)"
            if not p.price_verified:
                price += " _(unverified - check the store)_"
            price += f" _(as of {p.fetched_at[:10]})_"
            meta.append(price)
        if p.rating is not None:
            meta.append(f"**Rating:** {_stars(p.rating)} {p.rating:.1f}/5"
                        + (f" ({p.review_count:,} reviews)" if p.review_count else ""))
        if meta:
            lines.append(" · ".join(meta))
        if p.description:
            lines.append(f"\n{p.description}")
        lines.append(f"\n[Check price →]({link})\n")
    lines.append(f"\n_Last updated: {products[0].scraped_at[:10] if products else ''}_")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --bg:#f7f7f8; --card:#fff; --text:#1d1d1f; --muted:#6e6e73; --accent:#0a7cff; --deal:#d70015; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#111; --card:#1c1c1e; --text:#f5f5f7; --muted:#a1a1a6; }} }}
  body {{ margin:0; font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif; background:var(--bg); color:var(--text); }}
  main {{ max-width:1100px; margin:0 auto; padding:24px 16px; }}
  .disclosure {{ font-size:.85rem; color:var(--muted); border-left:3px solid var(--accent); padding:8px 12px; margin-bottom:24px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:16px; }}
  .card {{ background:var(--card); border-radius:14px; overflow:hidden; display:flex; flex-direction:column; box-shadow:0 1px 3px rgba(0,0,0,.08); }}
  .card img {{ width:100%; aspect-ratio:1; object-fit:contain; background:#fff; }}
  .body {{ padding:14px; display:flex; flex-direction:column; gap:6px; flex:1; }}
  .brand {{ font-size:.75rem; text-transform:uppercase; color:var(--muted); letter-spacing:.04em; }}
  .name {{ font-weight:600; line-height:1.3; }}
  .rating {{ color:#f5a623; font-size:.9rem; }} .rating span {{ color:var(--muted); }}
  .price {{ font-size:1.15rem; font-weight:700; }} .price s {{ color:var(--muted); font-weight:400; font-size:.9rem; }}
  .badge {{ background:var(--deal); color:#fff; border-radius:6px; padding:1px 6px; font-size:.75rem; margin-left:6px; }}
  .cta {{ margin-top:auto; text-align:center; background:var(--accent); color:#fff; text-decoration:none; padding:10px; border-radius:10px; font-weight:600; }}
  .warn {{ color:var(--deal); font-size:.8rem; font-weight:600; }}
  .asof {{ color:var(--muted); font-size:.75rem; }}
  footer {{ color:var(--muted); font-size:.8rem; margin-top:24px; }}
</style>
</head>
<body><main>
<h1>{title}</h1>
<p class="disclosure">{disclosure}</p>
<div class="grid">
{cards}
</div>
<footer>Last updated: {updated}</footer>
</main></body></html>
"""


def _card(p: Product) -> str:
    e = html.escape
    link = e(p.affiliate_url or p.url)
    parts = ['<article class="card">']
    if p.image:
        parts.append(f'<a href="{link}" rel="sponsored nofollow noopener" target="_blank">'
                     f'<img src="{e(p.image)}" alt="{e(p.name)}" loading="lazy"></a>')
    parts.append('<div class="body">')
    if p.brand:
        parts.append(f'<div class="brand">{e(p.brand)}</div>')
    parts.append(f'<div class="name">{e(p.name)}</div>')
    if p.rating is not None:
        reviews = f" ({p.review_count:,})" if p.review_count else ""
        parts.append(f'<div class="rating">{_stars(p.rating)} <span>{p.rating:.1f}{reviews}</span></div>')
    if p.price is not None:
        deal = (f' <s>{p.list_price:,.2f}</s><span class="badge">-{p.discount_pct:.0f}%</span>'
                if p.discount_pct else "")
        parts.append(f'<div class="price">{e(_price(p))}{deal}</div>')
        if not p.price_verified:
            why = "stale" if "stale" in p.validation_issues else "unverified"
            parts.append(f'<div class="warn">Price {why} - check on the store</div>')
        parts.append(f'<div class="asof">Price as of {e(p.fetched_at[:16].replace("T", " "))} UTC</div>')
        if p.shipping_cost is not None:
            parts.append(f'<div class="asof">{"Free shipping" if p.shipping_cost == 0 else "Shipping " + format(p.shipping_cost, ",.2f")}</div>')
    parts.append(f'<a class="cta" href="{link}" rel="sponsored nofollow noopener" target="_blank">Check price</a>')
    parts.append("</div></article>")
    return "".join(parts)


def to_html(products: list[Product], path: Path, title: str = "Top Picks",
            disclosure: str = DEFAULT_DISCLOSURE) -> Path:
    path.write_text(_HTML_TEMPLATE.format(
        title=html.escape(title),
        disclosure=html.escape(disclosure),
        cards="\n".join(_card(p) for p in products),
        updated=products[0].scraped_at[:10] if products else "",
    ), encoding="utf-8")
    return path


EXPORTERS = {"csv": to_csv, "json": to_json, "md": to_markdown, "html": to_html}


COMPARISON_FIELDS = ["group_id", "title", "brand", "model", "gtin", "method", "confidence", "uncertain",
                     "n_offers", "n_stores", "n_compared", "currency", "min_price", "median_price", "max_price",
                     "spread", "savings_vs_median_pct", "savings_vs_max_pct", "min_total", "best_store",
                     "best_offer_url", "partial", "partial_reasons"]


def comparisons_to_csv(comparisons, path: Path) -> Path:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COMPARISON_FIELDS, extrasaction="ignore")
        w.writeheader()
        for c in comparisons:
            w.writerow(c.to_dict(include_offers=False))
    return path


def comparisons_to_json(comparisons, path: Path) -> Path:
    path.write_text(json.dumps([c.to_dict() for c in comparisons], ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def errors_to_csv(errors, path: Path) -> Path:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["url", "kind", "status", "message", "at"])
        w.writeheader()
        for e in errors:
            w.writerow(e.to_dict())
    return path


def export(products: list[Product], out_dir: str | Path, basename: str, formats: list[str],
           title: str = "Top Picks", disclosure: str = DEFAULT_DISCLOSURE) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for fmt in formats:
        fn = EXPORTERS[fmt]
        path = out / f"{basename}.{fmt}"
        if fmt in ("md", "html"):
            written.append(fn(products, path, title=title, disclosure=disclosure))
        else:
            written.append(fn(products, path))
    return written


def export_run(result, out_dir: str | Path, basename: str, formats: list[str], title: str = "Top Picks",
               disclosure: str = DEFAULT_DISCLOSURE) -> list[Path]:
    """Export products + cross-store comparisons (when 2+ stores) + per-URL error log."""
    out = Path(out_dir)
    written = export(result.products, out, basename, formats, title, disclosure)
    multi = [c for c in result.comparisons if c.n_stores >= 2]
    if multi:
        if "csv" in formats:
            written.append(comparisons_to_csv(multi, out / f"{basename}_comparison.csv"))
        if "json" in formats:
            written.append(comparisons_to_json(multi, out / f"{basename}_comparison.json"))
    if result.errors:
        written.append(errors_to_csv(result.errors, out / f"{basename}_errors.csv"))
    return written

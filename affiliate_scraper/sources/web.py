"""Public product pages fetched over plain HTTP."""

from __future__ import annotations

import logging

from ..extractors import extract_products
from ..fetcher import FetchError
from ..models import Product
from ..utils import domain_of
from .base import SourceAdapter, store_for

log = logging.getLogger(__name__)


class WebPageAdapter(SourceAdapter):
    name = "web"
    source_type = "web"

    def fetch(self, target: str) -> list[Product]:
        page = self.ctx.fetcher.get(target)
        if page is None:
            return []
        return self.products_from_page(target, page.url, page.text, page.fetched_at)

    def products_from_page(self, target: str, url: str, html: str, fetched_at: str) -> list[Product]:
        store = store_for(domain_of(url), self.ctx.stores)
        products = extract_products(html, url, selectors=store.selectors if store else None, fetched_at=fetched_at)
        if not products:
            log.info("No product data on %s", target)
            self.ctx.fetcher.errors.append(
                FetchError(url=target, kind="no_product_data", message="page has no product structured data"))
        return self.apply_store_defaults(products)

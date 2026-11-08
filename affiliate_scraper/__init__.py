"""Polite, structured-data product scraper for affiliate marketing.

The package collects *publicly available* product information (name, price,
rating, image, availability...) from pages that publish it as structured data
(schema.org JSON-LD, microdata, OpenGraph), turns product links into affiliate
links, ranks products by how "promotable" they are and exports CSV / JSON /
HTML / Markdown ready for publishing.
"""

from .models import Product

__all__ = ["Product"]
__version__ = "3.0.0"

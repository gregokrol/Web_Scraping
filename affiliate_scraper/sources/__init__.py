"""Source adapters: every way of getting product offers into the system.

Order of preference (most reliable / most clearly authorised first):

1. ``api``      – official APIs (eBay Browse API, ...), credentials from environment variables
2. ``feed``     – product feeds supplied by merchants / affiliate networks (Google Merchant XML, CSV)
3. ``web``      – public product pages: JSON-LD > microdata > OpenGraph > configured CSS selectors
4. ``browser``  – same as ``web`` but rendered with Playwright, for dynamic sites that allow crawling
"""

from .base import AdapterContext, SourceAdapter, StoreConfig, load_stores
from .browser import BrowserPageAdapter
from .ebay import EbayBrowseAdapter
from .feed import ProductFeedAdapter
from .web import WebPageAdapter

__all__ = ["AdapterContext", "SourceAdapter", "StoreConfig", "load_stores", "WebPageAdapter",
           "BrowserPageAdapter", "ProductFeedAdapter", "EbayBrowseAdapter"]

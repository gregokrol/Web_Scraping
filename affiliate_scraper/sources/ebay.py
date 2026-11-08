"""Official eBay Browse API adapter (search by keyword or GTIN).

Credentials come ONLY from environment variables - never from code or config files:

    set EBAY_CLIENT_ID=...
    set EBAY_CLIENT_SECRET=...
    set EBAY_MARKETPLACE=EBAY_US          (optional)
    set EBAY_CAMPAIGN_ID=...              (optional, eBay Partner Network -> affiliate links)
    set EBAY_ENV=sandbox                  (optional)

Targets: ``ebay:search:<keywords>`` or ``ebay:gtin:<barcode>``.

Docs: https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search
"""

from __future__ import annotations

import base64
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from ..fetcher import FetchError
from ..models import Product
from ..utils import parse_price
from .base import AdapterContext, SourceAdapter

log = logging.getLogger(__name__)

SCOPE = "https://api.ebay.com/oauth/api_scope"
def ebay_condition(text: str) -> str:
    t = (text or "").lower()
    if "parts" in t or "not working" in t:
        return "damaged"
    if "refurbished" in t:
        return "refurbished"
    if "used" in t or "pre-owned" in t or "open box" in t:
        return "used"
    if t.startswith("new") or t == "brand new":
        return "new"
    return ""


class EbayCredentialsMissing(RuntimeError):
    pass


class EbayBrowseAdapter(SourceAdapter):
    name = "ebay"
    source_type = "api"

    def __init__(self, ctx: AdapterContext, session: Optional[requests.Session] = None, limit: int = 20,
                 env: Optional[dict] = None):
        super().__init__(ctx)
        e = env if env is not None else os.environ
        self.client_id = e.get("EBAY_CLIENT_ID", "")
        self.client_secret = e.get("EBAY_CLIENT_SECRET", "")
        self.marketplace = e.get("EBAY_MARKETPLACE", "EBAY_US")
        self.campaign_id = e.get("EBAY_CAMPAIGN_ID", "")
        host = "api.sandbox.ebay.com" if e.get("EBAY_ENV", "").lower() == "sandbox" else "api.ebay.com"
        self.token_url = f"https://{host}/identity/v1/oauth2/token"
        self.search_url = f"https://{host}/buy/browse/v1/item_summary/search"
        self.session = session or requests.Session()
        self.limit = max(1, min(limit, 200))
        self._token: Optional[str] = None
        self._token_expiry = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        if not (self.client_id and self.client_secret):
            raise EbayCredentialsMissing("set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET environment variables")
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        resp = self.session.post(self.token_url, timeout=20,
                                 headers={"Authorization": f"Basic {basic}",
                                          "Content-Type": "application/x-www-form-urlencoded"},
                                 data={"grant_type": "client_credentials", "scope": SCOPE})
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 7200))
        return self._token

    def fetch(self, target: str) -> list[Product]:
        _, _, rest = target.partition(":")
        mode, _, value = rest.partition(":")
        params = {"limit": str(self.limit)}
        if mode == "gtin":
            params["gtin"] = value
        elif mode == "search":
            params["q"] = value
        else:
            self.ctx.fetcher.errors.append(FetchError(url=target, kind="bad_target",
                                                      message="use ebay:search:<words> or ebay:gtin:<code>"))
            return []
        try:
            headers = {"Authorization": f"Bearer {self._get_token()}", "X-EBAY-C-MARKETPLACE-ID": self.marketplace}
            if self.campaign_id:
                headers["X-EBAY-C-ENDUSERCTX"] = f"affiliateCampaignId={self.campaign_id}"
            resp = self.session.get(self.search_url, params=params, headers=headers, timeout=20)
        except EbayCredentialsMissing as exc:
            self.ctx.fetcher.errors.append(FetchError(url=target, kind="credentials_missing", message=str(exc)))
            return []
        except requests.RequestException as exc:
            self.ctx.fetcher.errors.append(FetchError(url=target, kind="api_error", message=str(exc)))
            return []
        if resp.status_code != 200:
            self.ctx.fetcher.errors.append(FetchError(url=target, kind=f"http_{resp.status_code}",
                                                      status=resp.status_code, message=resp.text[:200]))
            return []
        fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        products = []
        for item in resp.json().get("itemSummaries", []):
            price = item.get("price") or {}
            shipping = None
            for opt in item.get("shippingOptions", []) or []:
                cost = parse_price((opt.get("shippingCost") or {}).get("value"))
                if cost is not None:
                    shipping = cost if shipping is None else min(shipping, cost)
            seller = item.get("seller") or {}
            products.append(Product(
                url=item.get("itemWebUrl", ""),
                affiliate_url=item.get("itemAffiliateWebUrl", ""),
                name=item.get("title", ""),
                price=parse_price(price.get("value")),
                currency=price.get("currency", ""),
                shipping_cost=shipping,
                image=(item.get("image") or {}).get("imageUrl", ""),
                condition=ebay_condition(item.get("condition", "")),
                sku=item.get("itemId", ""),
                gtin=value if mode == "gtin" else "",
                store=f"eBay: {seller.get('username', '')}".strip(": "),
                # seller feedback is NOT a product rating, so rating/review_count stay empty
                availability="InStock",  # Browse API only returns purchasable listings
                source="ebay-api", source_type="api", fetched_at=fetched_at,
            ))
        return [p for p in products if p.url and p.name]

"""Official Amazon Creators API adapter (successor of PA-API 5.0).

Credentials come ONLY from environment variables (Associates Central -> Tools -> Creators API):

    setx AMAZON_CREDENTIAL_ID "..."
    setx AMAZON_CREDENTIAL_SECRET "..."
    setx AMAZON_CREDENTIAL_VERSION "2.1"        (shown next to your credentials: 2.1/2.2/2.3/3.1/3.2/3.3)
    setx AMAZON_PARTNER_TAG "yourtag-20"
    setx AMAZON_MARKETPLACE "www.amazon.com"    (optional; e.g. www.amazon.de, www.amazon.co.uk)

Targets: ``amazon:search:<keywords>`` or ``amazon:asin:<ASIN>[,<ASIN>...]``.

Protocol (taken from Amazon's official Python SDK, ``creatorsapi_python_sdk``):
* OAuth2 client-credentials token. v2.x -> Cognito endpoint, form body, scope ``creatorsapi/default``,
  header ``Authorization: Bearer <token>, Version <v>``; v3.x -> Login with Amazon, JSON body,
  scope ``creatorsapi::default``, header ``Authorization: Bearer <token>``
* ``POST https://creatorsapi.amazon/catalog/v1/searchItems`` / ``getItems`` with header ``x-marketplace``
  and a lowerCamelCase JSON body.

Amazon's terms restrict how long product data may be stored and require prices to be shown
with their time. This adapter therefore marks offers so that NO long-term price history is
stored for them (see db.save_products), and prices older than --max-age-hours become
unverified. Check the current Associates / Creators API terms yourself.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from ..fetcher import FetchError
from ..models import Product
from ..utils import parse_price
from .base import AdapterContext, SourceAdapter

log = logging.getLogger(__name__)

API_HOST = "https://creatorsapi.amazon"
TOKEN_ENDPOINTS = {
    "2.1": "https://creatorsapi.auth.us-east-1.amazoncognito.com/oauth2/token",
    "2.2": "https://creatorsapi.auth.eu-south-2.amazoncognito.com/oauth2/token",
    "2.3": "https://creatorsapi.auth.us-west-2.amazoncognito.com/oauth2/token",
    "3.1": "https://api.amazon.com/auth/o2/token",
    "3.2": "https://api.amazon.co.uk/auth/o2/token",
    "3.3": "https://api.amazon.co.jp/auth/o2/token",
}
RESOURCES = [
    "itemInfo.title", "itemInfo.byLineInfo", "itemInfo.externalIds", "itemInfo.manufactureInfo",
    "itemInfo.productInfo", "itemInfo.classifications", "images.primary.large", "customerReviews.count",
    "customerReviews.starRating", "offersV2.listings.price", "offersV2.listings.availability",
    "offersV2.listings.condition", "offersV2.listings.merchantInfo", "offersV2.listings.isBuyBoxWinner",
]
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
NO_HISTORY_SOURCE = "amazon-api"   # db.save_products skips long-term price history for this source


class AmazonCredentialsMissing(RuntimeError):
    pass


def _dv(node: Optional[dict]) -> str:
    """displayValue of a single-valued attribute."""
    return str((node or {}).get("displayValue") or "").strip()


def _first(node: Optional[dict]) -> str:
    values = (node or {}).get("displayValues") or []
    return str(values[0]).strip() if values else ""


def amazon_availability(listing: dict) -> str:
    av = listing.get("availability") or {}
    text = f"{av.get('type', '')} {av.get('message', '')}".lower()
    if any(k in text for k in ("out_of_stock", "out of stock", "unavailable", "not available")):
        return "OutOfStock"
    if any(k in text for k in ("preorder", "pre-order", "pre_order")):
        return "PreOrder"
    if any(k in text for k in ("in_stock", "in stock", "now")):
        return "InStock"
    return ""   # unknown - never guessed


def amazon_condition(listing: dict) -> str:
    value = str((listing.get("condition") or {}).get("value") or "").lower()
    return {"new": "new", "used": "used", "refurbished": "refurbished", "collectible": "used"}.get(value, "")


class AmazonCreatorsAdapter(SourceAdapter):
    name = "amazon"
    source_type = "api"

    def __init__(self, ctx: AdapterContext, session: Optional[requests.Session] = None, item_count: int = 10,
                 env: Optional[dict] = None):
        super().__init__(ctx)
        e = env if env is not None else os.environ
        self.credential_id = e.get("AMAZON_CREDENTIAL_ID", "")
        self.credential_secret = e.get("AMAZON_CREDENTIAL_SECRET", "")
        self.version = e.get("AMAZON_CREDENTIAL_VERSION", "")
        self.partner_tag = e.get("AMAZON_PARTNER_TAG", "")
        self.marketplace = e.get("AMAZON_MARKETPLACE", "www.amazon.com")
        self.session = session or requests.Session()
        self.item_count = max(1, min(item_count, 10))
        self._token: Optional[str] = None
        self._token_expiry = 0.0
        self._last_call = 0.0

    # ------------------------------------------------------------------ auth
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        missing = [n for n, v in (("AMAZON_CREDENTIAL_ID", self.credential_id),
                                  ("AMAZON_CREDENTIAL_SECRET", self.credential_secret),
                                  ("AMAZON_CREDENTIAL_VERSION", self.version),
                                  ("AMAZON_PARTNER_TAG", self.partner_tag)) if not v]
        if missing:
            raise AmazonCredentialsMissing("set environment variables: " + ", ".join(missing))
        endpoint = TOKEN_ENDPOINTS.get(self.version)
        if not endpoint:
            raise AmazonCredentialsMissing(f"unsupported AMAZON_CREDENTIAL_VERSION {self.version!r} "
                                           f"(expected one of {', '.join(TOKEN_ENDPOINTS)})")
        lwa = self.version.startswith("3.")
        body = {"grant_type": "client_credentials", "client_id": self.credential_id,
                "client_secret": self.credential_secret,
                "scope": "creatorsapi::default" if lwa else "creatorsapi/default"}
        if lwa:
            resp = self.session.post(endpoint, json=body, headers={"Content-Type": "application/json"}, timeout=20)
        else:
            resp = self.session.post(endpoint, data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
        if resp.status_code != 200:
            raise requests.HTTPError(f"token request failed: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3600)) - 30
        return self._token

    def _headers(self) -> dict:
        token = self._get_token()
        auth = f"Bearer {token}" if self.version.startswith("3.") else f"Bearer {token}, Version {self.version}"
        return {"Authorization": auth, "x-marketplace": self.marketplace, "Content-Type": "application/json",
                "Accept": "application/json"}

    def _call(self, operation: str, body: dict) -> dict:
        # Amazon throttles per account (roughly 1 request/second for new accounts) - stay below it
        wait = self._last_call + max(1.1, self.ctx.fetcher.min_delay) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
        resp = self.session.post(f"{API_HOST}/catalog/v1/{operation}", json=body, headers=self._headers(), timeout=25)
        if resp.status_code != 200:
            raise requests.HTTPError(f"HTTP {resp.status_code}: {resp.text[:300]}", response=resp)
        return resp.json()

    # ------------------------------------------------------------------ fetch
    def fetch(self, target: str) -> list[Product]:
        _, _, rest = target.partition(":")
        mode, _, value = rest.partition(":")
        value = value.strip()
        try:
            if mode == "search" and value:
                data = self._call("searchItems", {"keywords": value, "partnerTag": self.partner_tag,
                                                  "itemCount": self.item_count, "resources": RESOURCES})
                items = (data.get("searchResult") or {}).get("items") or []
            elif mode == "asin" and value:
                asins = [a.strip().upper() for a in value.split(",") if a.strip()]
                bad = [a for a in asins if not ASIN_RE.match(a)]
                if bad:
                    raise ValueError(f"invalid ASIN(s): {', '.join(bad)}")
                data = self._call("getItems", {"itemIds": asins[:10], "partnerTag": self.partner_tag,
                                               "resources": RESOURCES})
                items = (data.get("itemsResult") or {}).get("items") or []
            else:
                raise ValueError("use amazon:search:<keywords> or amazon:asin:<ASIN,...>")
        except AmazonCredentialsMissing as exc:
            self.ctx.fetcher.errors.append(FetchError(url=target, kind="credentials_missing", message=str(exc)))
            return []
        except ValueError as exc:
            self.ctx.fetcher.errors.append(FetchError(url=target, kind="bad_target", message=str(exc)))
            return []
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            kind = f"http_{status}" if status else "api_error"
            self.ctx.fetcher.errors.append(FetchError(url=target, kind=kind, status=status, message=str(exc)[:300]))
            return []
        for err in (data.get("errors") or [])[:5]:
            self.ctx.fetcher.errors.append(FetchError(url=target, kind="api_item_error",
                                                      message=f"{err.get('code')}: {err.get('message')}"))
        fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        return [p for p in (self._to_product(i, fetched_at) for i in items) if p]

    def _to_product(self, item: dict, fetched_at: str) -> Optional[Product]:
        asin = item.get("asin", "")
        info = item.get("itemInfo") or {}
        title = _dv(info.get("title"))
        if not asin or not title:
            return None
        listings = (item.get("offersV2") or {}).get("listings") or []
        listing = next((x for x in listings if x.get("isBuyBoxWinner")), listings[0] if listings else {})
        price = (listing.get("price") or {})
        money = price.get("money") or {}
        basis = ((price.get("savingBasis") or {}).get("money") or {})
        byline = info.get("byLineInfo") or {}
        ext = info.get("externalIds") or {}
        manu = info.get("manufactureInfo") or {}
        prod = info.get("productInfo") or {}
        reviews = item.get("customerReviews") or {}
        merchant = (listing.get("merchantInfo") or {}).get("name", "")
        images = ((item.get("images") or {}).get("primary") or {})
        image = ((images.get("large") or images.get("medium") or {}).get("url") or "")
        rating = (reviews.get("starRating") or {}).get("value")
        return Product(
            url=f"https://{self.marketplace}/dp/{asin}",
            affiliate_url=item.get("detailPageURL", ""),   # already carries your partner tag
            name=title,
            brand=_dv(byline.get("brand")) or _dv(byline.get("manufacturer")),
            model=_dv(manu.get("model")) or _dv(manu.get("itemPartNumber")),
            gtin=_first(ext.get("eans")) or _first(ext.get("upcs")),
            sku=asin,
            category=_dv((info.get("classifications") or {}).get("productGroup")),
            color=_dv(prod.get("color")),
            size=_dv(prod.get("size")),
            price=parse_price(money.get("amount")),
            list_price=parse_price(basis.get("amount")),
            currency=money.get("currency", ""),
            availability=amazon_availability(listing),
            condition=amazon_condition(listing),
            image=image,
            rating=float(rating) if rating is not None else None,
            review_count=int(reviews["count"]) if reviews.get("count") is not None else None,
            store=f"Amazon ({merchant})" if merchant else "Amazon",
            source=NO_HISTORY_SOURCE, source_type="api", fetched_at=fetched_at,
        )

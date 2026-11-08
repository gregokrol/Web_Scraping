"""Official AliExpress Affiliate API adapter (AliExpress Portals / Open Platform).

Credentials come ONLY from environment variables:

    setx ALIEXPRESS_APP_KEY "..."
    setx ALIEXPRESS_APP_SECRET "..."
    setx ALIEXPRESS_TRACKING_ID "..."        (your Portals tracking id - required, else results are empty)
    setx ALIEXPRESS_CURRENCY "USD"           (optional)
    setx ALIEXPRESS_LANGUAGE "HE"            (optional, default HE)
    setx ALIEXPRESS_SHIP_TO "IL"             (optional, default IL)

Targets: ``aliexpress:search:<keywords>`` or ``aliexpress:id:<product_id>[,<id>...]``.

Protocol (same as the widely used ``python-aliexpress-api`` SDK): POST to
``https://api-sg.aliexpress.com/sync`` with system parameters in the query string and
business parameters in the form body; ``sign`` = upper-case MD5 of
``secret + concat(sorted key+value of all parameters) + secret``. HMAC-SHA256 is supported
with ``ALIEXPRESS_SIGN_METHOD=sha256``.

The API returns a ready affiliate link (``promotion_link``) and the commission rate for each
product - the commission comes from the network itself, so it counts as verified.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import requests

from ..fetcher import FetchError
from ..models import Product
from ..utils import parse_price
from .base import AdapterContext, SourceAdapter

log = logging.getLogger(__name__)

GATEWAY = "https://api-sg.aliexpress.com/sync"
SEARCH_METHOD = "aliexpress.affiliate.product.query"
DETAIL_METHOD = "aliexpress.affiliate.productdetail.get"
RESPONSE_KEYS = {SEARCH_METHOD: "aliexpress_affiliate_product_query_response",
                 DETAIL_METHOD: "aliexpress_affiliate_productdetail_get_response"}
FIELDS = ("product_id,product_title,product_detail_url,promotion_link,product_main_image_url,"
          "target_sale_price,target_sale_price_currency,target_original_price,target_original_price_currency,"
          "sale_price,sale_price_currency,original_price,commission_rate,first_level_category_name,"
          "second_level_category_name,shop_url")


class AliExpressCredentialsMissing(RuntimeError):
    pass


def sign_request(params: dict, secret: str, method: str = "md5") -> str:
    joined = "".join(f"{k}{params[k]}" for k in sorted(params))
    if method == "sha256":
        return hmac.new(secret.encode(), joined.encode(), hashlib.sha256).hexdigest().upper()
    return hashlib.md5(f"{secret}{joined}{secret}".encode()).hexdigest().upper()


def _pct(value) -> Optional[float]:
    """'7.0%' -> 0.07"""
    if value in (None, ""):
        return None
    try:
        return round(float(str(value).strip().rstrip("%")) / 100, 4)
    except ValueError:
        return None


class AliExpressAffiliateAdapter(SourceAdapter):
    name = "aliexpress"
    source_type = "api"

    def __init__(self, ctx: AdapterContext, session: Optional[requests.Session] = None, page_size: int = 20,
                 env: Optional[dict] = None):
        super().__init__(ctx)
        e = env if env is not None else os.environ
        self.app_key = e.get("ALIEXPRESS_APP_KEY", "")
        self.app_secret = e.get("ALIEXPRESS_APP_SECRET", "")
        self.tracking_id = e.get("ALIEXPRESS_TRACKING_ID", "")
        self.currency = e.get("ALIEXPRESS_CURRENCY", "USD").upper()
        self.language = e.get("ALIEXPRESS_LANGUAGE", "HE").upper()
        self.ship_to = e.get("ALIEXPRESS_SHIP_TO", "IL").upper()
        self.sign_method = "sha256" if e.get("ALIEXPRESS_SIGN_METHOD", "md5").lower() in ("sha256", "hmac-sha256") else "md5"
        self.session = session or requests.Session()
        self.page_size = max(1, min(page_size, 50))
        self._last_call = 0.0

    def _call(self, method: str, business: dict) -> dict:
        missing = [n for n, v in (("ALIEXPRESS_APP_KEY", self.app_key), ("ALIEXPRESS_APP_SECRET", self.app_secret),
                                  ("ALIEXPRESS_TRACKING_ID", self.tracking_id)) if not v]
        if missing:
            raise AliExpressCredentialsMissing("set environment variables: " + ", ".join(missing))
        wait = self._last_call + max(1.0, self.ctx.fetcher.min_delay) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
        system = {"app_key": self.app_key, "method": method, "timestamp": str(int(time.time() * 1000)),
                  "sign_method": self.sign_method, "format": "json", "v": "2.0"}
        business = {k: str(v) for k, v in business.items() if v not in (None, "")}
        system["sign"] = sign_request({**system, **business}, self.app_secret, self.sign_method)
        resp = self.session.post(f"{GATEWAY}?{urlencode(system)}", data=business, timeout=25,
                                 headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        if resp.status_code != 200:
            raise requests.HTTPError(f"HTTP {resp.status_code}: {resp.text[:300]}", response=resp)
        data = resp.json()
        if "error_response" in data:
            err = data["error_response"]
            raise requests.RequestException(f"API error {err.get('code')}: {err.get('msg') or err.get('sub_msg')}")
        result = (data.get(RESPONSE_KEYS[method]) or {}).get("resp_result") or {}
        if result.get("resp_code") not in (200, "200"):
            raise requests.RequestException(f"resp_code {result.get('resp_code')}: {result.get('resp_msg')}")
        return result.get("result") or {}

    def fetch(self, target: str) -> list[Product]:
        _, _, rest = target.partition(":")
        mode, _, value = rest.partition(":")
        value = value.strip()
        common = {"target_currency": self.currency, "target_language": self.language,
                  "tracking_id": self.tracking_id, "fields": FIELDS}
        try:
            if mode == "search" and value:
                result = self._call(SEARCH_METHOD, {**common, "keywords": value, "page_size": self.page_size,
                                                    "page_no": 1, "ship_to_country": self.ship_to,
                                                    "sort": "LAST_VOLUME_DESC"})
            elif mode == "id" and value:
                ids = [i.strip() for i in value.split(",") if i.strip().isdigit()]
                if not ids:
                    raise ValueError("product ids must be numbers")
                result = self._call(DETAIL_METHOD, {**common, "product_ids": ",".join(ids[:50]),
                                                    "country": self.ship_to})
            else:
                raise ValueError("use aliexpress:search:<keywords> or aliexpress:id:<product_id,...>")
        except AliExpressCredentialsMissing as exc:
            self.ctx.fetcher.errors.append(FetchError(url=target, kind="credentials_missing", message=str(exc)))
            return []
        except ValueError as exc:
            self.ctx.fetcher.errors.append(FetchError(url=target, kind="bad_target", message=str(exc)))
            return []
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            self.ctx.fetcher.errors.append(FetchError(url=target, kind=f"http_{status}" if status else "api_error",
                                                      status=status, message=str(exc)[:300]))
            return []

        products_node = result.get("products") or {}
        items = products_node.get("product", []) if isinstance(products_node, dict) else products_node
        fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        out = []
        for it in items or []:
            pid = str(it.get("product_id", ""))
            title = it.get("product_title", "")
            if not pid or not title:
                continue
            price = parse_price(it.get("target_sale_price") or it.get("sale_price"))
            currency = it.get("target_sale_price_currency") or it.get("sale_price_currency") or ""
            original = parse_price(it.get("target_original_price") or it.get("original_price"))
            out.append(Product(
                url=f"https://www.aliexpress.com/item/{pid}.html",
                affiliate_url=it.get("promotion_link", ""),
                name=title, sku=pid, price=price, currency=currency,
                list_price=original if (original and price and original > price) else None,
                image=it.get("product_main_image_url", ""),
                category=it.get("second_level_category_name") or it.get("first_level_category_name") or "",
                # returned by the affiliate API only for products currently listed for promotion
                availability="InStock" if price is not None else "",
                store="AliExpress",
                commission_rate=_pct(it.get("commission_rate")),
                source="aliexpress-api", source_type="api", fetched_at=fetched_at,
            ))
        if not out and not self.ctx.fetcher.errors_for(target):
            self.ctx.fetcher.errors.append(FetchError(url=target, kind="no_results",
                                                      message="no products (check ALIEXPRESS_TRACKING_ID)"))
        return out

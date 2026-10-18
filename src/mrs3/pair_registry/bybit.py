"""Bybit reference for the pair registry: linear instruments and risk-limit tiers (public API)."""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

import httpx

from .common import decimal_text, integer
from .store import RegistryError, RegistrySettings

BASE_URL = "https://api.bybit.com/v5/market"


def _pages(feed: str, params: Mapping[str, str]) -> list[dict]:
    items: list[dict] = []
    cursor = ""
    for _page in range(100):
        query = {**params, **({"cursor": cursor} if cursor else {})}
        try:
            response = httpx.get(f"{BASE_URL}/{feed}", params=query, timeout=30.0)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RegistryError(f"Bybit {feed} request failed: {error}") from error
        if not isinstance(payload, dict) or payload.get("retCode") != 0 or not isinstance(payload.get("result"), dict):
            raise RegistryError(f"Bybit {feed} answered an error")
        page = payload["result"].get("list")
        if not isinstance(page, list):
            raise RegistryError(f"Bybit {feed} list is missing")
        items.extend(item for item in page if isinstance(item, dict))
        cursor = payload["result"].get("nextPageCursor") or ""
        if not cursor:
            return items
    raise RegistryError(f"Bybit {feed}: too many pages")


def fetch_bybit() -> tuple[list[dict], list[dict]]:
    """All linear instruments and all linear risk-limit tiers."""
    return (_pages("instruments-info", {"category": "linear", "limit": "1000"}),
            _pages("risk-limit", {"category": "linear"}))


def _nested(item: Mapping, group: str, key: str) -> object:
    value = item.get(group)
    return value.get(key) if isinstance(value, Mapping) else None


def normalize_instruments(items: Sequence[Mapping], settings: RegistrySettings) -> list[dict]:
    """Trading USDT linear perpetuals of the configured symbol types, minus the exclude list."""
    records = []
    for item in items:
        symbol = str(item.get("symbol") or "")
        if (item.get("contractType") != "LinearPerpetual" or item.get("status") != "Trading"
                or item.get("settleCoin") != "USDT" or item.get("symbolType") not in settings.bybit_symbol_types
                or symbol in settings.bybit_exclude or not symbol):
            continue
        records.append({
            "symbol": symbol, "api_symbol": symbol, "display_name": item.get("displayName") or symbol,
            "base_asset": item.get("baseCoin"), "symbol_type": item.get("symbolType"),
            "contract_type": item.get("contractType"), "settle_coin": item.get("settleCoin"), "status": "TRADING",
            "exchange_status": item.get("status"), "listing_ms": integer(item.get("launchTime")),
            "delivery_ms": integer(item.get("deliveryTime")), "api_open": None, "api_close": None,
            "tick_size": decimal_text(_nested(item, "priceFilter", "tickSize")),
            "min_price": decimal_text(_nested(item, "priceFilter", "minPrice")),
            "max_price": decimal_text(_nested(item, "priceFilter", "maxPrice")),
            "price_scale": integer(item.get("priceScale")),
            "qty_step": decimal_text(_nested(item, "lotSizeFilter", "qtyStep")),
            "min_order_qty": decimal_text(_nested(item, "lotSizeFilter", "minOrderQty")),
            "max_order_qty": decimal_text(_nested(item, "lotSizeFilter", "maxOrderQty")),
            "max_market_order_qty": decimal_text(_nested(item, "lotSizeFilter", "maxMktOrderQty")),
            "min_notional": decimal_text(_nested(item, "lotSizeFilter", "minNotionalValue")),
            "min_leverage": decimal_text(_nested(item, "leverageFilter", "minLeverage")),
            "max_leverage": decimal_text(_nested(item, "leverageFilter", "maxLeverage")),
            "leverage_step": decimal_text(_nested(item, "leverageFilter", "leverageStep")),
            "funding_interval_min": integer(item.get("fundingInterval")),
            "upper_funding_rate": decimal_text(item.get("upperFundingRate")),
            "lower_funding_rate": decimal_text(item.get("lowerFundingRate")),
            "maker_fee": None, "taker_fee": None, "full_name": item.get("fullName") or None,
        })
    return records


def normalize_risk_limits(items: Sequence[Mapping], symbols: Iterable[str]) -> list[dict]:
    """Risk-limit tiers of the given symbols, ordered by symbol and tier."""
    wanted = set(symbols)
    tiers = [{
        "symbol": str(item["symbol"]), "tier_id": integer(item.get("id")),
        "risk_limit_value": decimal_text(item.get("riskLimitValue")),
        "initial_margin": decimal_text(item.get("initialMargin")),
        "maintenance_margin": decimal_text(item.get("maintenanceMargin")),
        "max_leverage": decimal_text(item.get("maxLeverage")),
        "mm_deduction": decimal_text(item.get("mmDeduction")) or "0",
        "is_lowest": str(item.get("isLowestRisk")) in {"1", "True", "true"},
    } for item in items if item.get("symbol") in wanted]
    return sorted(tiers, key=lambda tier: (tier["symbol"], tier["tier_id"]))

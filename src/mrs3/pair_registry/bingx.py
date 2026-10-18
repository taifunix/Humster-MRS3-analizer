"""BingX reference for the pair registry: the full TradFi contract list (public API).

Human ticker rule: docs/specs/2026-10-18-pair-registry.md, «BingX names and data».
"""

from __future__ import annotations

from decimal import Decimal
import re
from typing import Mapping, Sequence

from .common import decimal_text, integer
from .store import RegistryError, RegistrySettings

SYMBOL_TYPES = {"NCSK": "stock", "NCSI": "index", "NCCO": "commodity"}
_TAIL = re.compile(r"(?P<base>[A-Z0-9]+?)(?:2USD|USDT|2[A-Z]{3})")
_CORE = re.compile(r"[A-Z0-9]+")


def human_ticker(symbol: str) -> str | None:
    """`NCSKAMD2USD-USDT` -> `AMDUSDT`: strip the 4-letter prefix, `-USDT` and the quote tail."""
    if not isinstance(symbol, str) or not symbol.endswith("-USDT") or len(symbol) <= 9:
        return None
    core = symbol[4:-5]
    if not _CORE.fullmatch(core):
        return None
    match = _TAIL.fullmatch(core)
    return f"{match.group('base') if match else core}USDT"


def fetch_bingx() -> list[dict]:
    from ..bingx_downloader import BingXClient

    return BingXClient().contracts()


def _step(precision: object) -> str | None:
    value = integer(precision)
    return None if value is None else decimal_text(Decimal(1).scaleb(-value))


def _flag(value: object) -> bool | None:
    if value is None or value == "":
        return None
    return str(value).strip().lower() == "true"


def unreadable_symbols(items: Sequence[Mapping], settings: RegistrySettings) -> list[str]:
    """Contracts of the configured prefixes that the ticker rule cannot read (left out of the refresh, reported)."""
    return sorted(str(item.get("symbol")) for item in items
                  if str(item.get("symbol") or "")[:4] in settings.bingx_prefixes
                  and human_ticker(str(item.get("symbol") or "")) is None)


def normalize_contracts(items: Sequence[Mapping], settings: RegistrySettings) -> list[dict]:
    """TradFi contracts (configured prefixes): status 1 trades, any other status is stored as SUSPENDED."""
    records, owners = [], {}
    for item in items:
        api_symbol = str(item.get("symbol") or "")
        prefix = api_symbol[:4]
        if prefix not in settings.bingx_prefixes:
            continue
        ticker = human_ticker(api_symbol)
        if ticker is None or ticker in settings.bingx_exclude:
            continue
        owners.setdefault(ticker, []).append(api_symbol)
        status = item.get("status")
        records.append({
            "symbol": ticker, "api_symbol": api_symbol, "display_name": item.get("displayName") or api_symbol,
            "base_asset": ticker[:-4], "symbol_type": SYMBOL_TYPES.get(prefix, prefix),
            "contract_type": "LinearPerpetual", "settle_coin": item.get("currency") or "USDT",
            "status": "TRADING" if status == 1 else "SUSPENDED", "exchange_status": str(status),
            "listing_ms": integer(item.get("launchTime")), "delivery_ms": None,
            "api_open": _flag(item.get("apiStateOpen")), "api_close": _flag(item.get("apiStateClose")),
            "tick_size": _step(item.get("pricePrecision")), "min_price": None, "max_price": None,
            "price_scale": integer(item.get("pricePrecision")), "qty_step": _step(item.get("quantityPrecision")),
            # tradeMinQuantity is min notional / live price: not reference data (it moves with the price)
            "min_order_qty": None, "max_order_qty": None,
            "max_market_order_qty": None, "min_notional": decimal_text(item.get("tradeMinUSDT")),
            "min_leverage": None, "max_leverage": None, "leverage_step": None, "funding_interval_min": None,
            "upper_funding_rate": None, "lower_funding_rate": None,
            "maker_fee": decimal_text(item.get("makerFeeRate")), "taker_fee": decimal_text(item.get("takerFeeRate")),
            "full_name": None,
        })
    collisions = {ticker: sorted(symbols) for ticker, symbols in owners.items() if len(symbols) > 1}
    if collisions:
        listed = "; ".join(f"{ticker}: {', '.join(symbols)}" for ticker, symbols in sorted(collisions.items()))
        raise RegistryError(f"BingX ticker collision ({listed})")
    return records

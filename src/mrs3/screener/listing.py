"""Resolve UTC listing dates: liquidity registry -> dates.xlsx -> Bybit fallback.

Bybit's documented HTTP rate limit is 600 requests per 5-second window per
IP; exceeding it returns "403, access too frequent" and bans the IP for at
least 10 minutes, lifted automatically
(https://bybit-exchange.github.io/docs/v5/rate-limit). The public,
no-API-key `instruments-info` endpoint has no more specific documented limit
(https://bybit-exchange.github.io/docs/v5/market/instrument), so the general
IP limit is what applies here. `_throttle()` below keeps this module's own
request rate at a small fraction of that threshold, with a wide safety
margin, and it does so across separate `resolve_listing_dates` calls too
(the IP ban is keyed off a rolling window, not a "call"), not just within
one.
"""

from __future__ import annotations

from pathlib import Path
import threading
import time

import httpx
import pandas as pd

from mrs3.loader import InputError, load_listing_dates

from .errors import ScreenerEvaluationError
from .registry import read_registry_listing_dates

_BYBIT_INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"

# <=4 req/s, i.e. <=20 requests per Bybit's 5-second window — about a 30x
# margin under the documented 600-per-5s per-IP ban threshold.
_MIN_REQUEST_INTERVAL_SECONDS = 0.25

_throttle_lock = threading.Lock()
_last_request_monotonic: float | None = None


def _throttle() -> None:
    global _last_request_monotonic
    with _throttle_lock:
        now = time.monotonic()
        if _last_request_monotonic is not None:
            wait = _MIN_REQUEST_INTERVAL_SECONDS - (now - _last_request_monotonic)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
        _last_request_monotonic = now


def resolve_listing_dates(
    symbols: tuple[str, ...],
    *,
    registry_path: Path | None,
    dates_path: Path | None,
) -> dict[str, pd.Timestamp]:
    symbols = tuple(symbol.strip().upper() for symbol in symbols)
    listing: dict[str, pd.Timestamp] = {}
    if registry_path is not None and registry_path.exists():
        listing.update(read_registry_listing_dates(registry_path, symbols))
    still_missing = any(symbol not in listing for symbol in symbols)
    if still_missing and dates_path is not None and dates_path.exists():
        # Unlike read_registry_listing_dates, load_listing_dates (shared with
        # other callers in mrs3.loader) validates/parses the WHOLE file, not
        # just rows for `symbols` — an unrelated malformed row elsewhere in a
        # shared dates.xlsx can still fail this call even though none of our
        # requested symbols depend on it. Only calling it when the registry
        # left symbols unresolved narrows, but does not eliminate, that
        # exposure; a full fix would mean changing the shared loader used
        # elsewhere in the project, which is out of scope here.
        try:
            dates_from_xlsx = load_listing_dates(dates_path)
        except InputError as exc:
            raise ScreenerEvaluationError(f"invalid dates.xlsx: {exc}") from exc
        # load_listing_dates only dedupes case-sensitively (loader.py strips
        # but does not upper-case before its own duplicate check), so two
        # rows differing only in case would otherwise silently pick whichever
        # comes first here instead of surfacing the data-entry mistake. Scope
        # this check to symbols the registry didn't already resolve — like
        # read_registry_listing_dates, an unrelated duplicate elsewhere in a
        # shared dates.xlsx (including one for a symbol the registry already
        # settled) must not block resolving the symbols actually still needed
        # from this file.
        wanted = {symbol for symbol in symbols if symbol not in listing}
        normalized_dates: dict[str, pd.Timestamp] = {}
        for symbol, value in dates_from_xlsx.items():
            key = symbol.strip().upper()
            if key not in wanted:
                continue
            if key in normalized_dates:
                raise ScreenerEvaluationError(
                    f"dates.xlsx has case-insensitive duplicate symbols: {key}"
                )
            normalized_dates[key] = value
        for key, value in normalized_dates.items():
            listing.setdefault(key, value)
    for symbol in symbols:
        if symbol not in listing:
            listing[symbol] = _fetch_bybit_launch_time(symbol)
    return {symbol: listing[symbol] for symbol in symbols}


def _fetch_bybit_launch_time(symbol: str) -> pd.Timestamp:
    _throttle()
    try:
        response = httpx.get(
            _BYBIT_INSTRUMENTS_URL,
            params={"category": "linear", "symbol": symbol},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise ScreenerEvaluationError(
            f"Bybit instruments-info request failed for {symbol}: {exc}"
        ) from exc
    if response.status_code == 403:
        raise ScreenerEvaluationError(
            "Bybit blocked this IP for sending requests too frequently (HTTP 403) "
            "— wait at least 10 minutes before retrying"
        )
    try:
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ScreenerEvaluationError(
            f"Bybit instruments-info request failed for {symbol}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ScreenerEvaluationError(
            f"Bybit instruments-info returned an unexpected response shape for {symbol}"
        )
    if payload.get("retCode") != 0:
        raise ScreenerEvaluationError(
            f"Bybit instruments-info returned retCode={payload.get('retCode')} for {symbol}"
        )
    result = payload.get("result")
    items = result.get("list") if isinstance(result, dict) else None
    if not isinstance(items, list) or not items:
        raise ScreenerEvaluationError(f"listing date not found on Bybit for symbol: {symbol}")
    matching = next(
        (item for item in items if isinstance(item, dict) and item.get("symbol") == symbol),
        None,
    )
    if matching is None:
        raise ScreenerEvaluationError(f"listing date not found on Bybit for symbol: {symbol}")
    launch_time = matching.get("launchTime")
    if launch_time is None:
        raise ScreenerEvaluationError(f"listing date not found on Bybit for symbol: {symbol}")
    try:
        launch_time_ms = int(launch_time)
    except (TypeError, ValueError) as exc:
        raise ScreenerEvaluationError(
            f"invalid Bybit launchTime for {symbol}: {launch_time!r}"
        ) from exc
    if launch_time_ms <= 0:
        raise ScreenerEvaluationError(f"listing date not found on Bybit for symbol: {symbol}")
    try:
        return pd.Timestamp(launch_time_ms, unit="ms", tz="UTC")
    except ValueError as exc:
        # Covers pandas' OutOfBoundsDatetime (a ValueError subclass) for an
        # absurd-but-numeric launchTime.
        raise ScreenerEvaluationError(
            f"invalid Bybit launchTime for {symbol}: {launch_time!r}"
        ) from exc

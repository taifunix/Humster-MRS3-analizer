"""Download BingX 1m candles into daily Bybit-format CSV files for the tester.

Contract: docs/specs/2026-10-10-bingx-data-downloader.md. Data lives in
``<bot_root>/tester/data/bingx/<api_symbol>/<TICKER>USDT<YYYY-MM-DD>_1m.csv``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import time
from typing import Callable, Iterable, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://open-api.bingx.com"
CONTRACTS_PATH = "/openApi/swap/v2/quote/contracts"
KLINES_PATH = "/openApi/swap/v3/quote/klines"
CSV_HEADER = ("timestamp", "open", "high", "low", "close", "volume", "buy_volume", "sell_volume", "trades")
DOWNLOAD_MODES = ("incremental", "overwrite")
MINUTE_MS = 60_000
DAY_MS = 86_400_000
# Daily data appears ~6 h after 00:00 UTC; 3 h margin on top (spec: «Доступность дневных данных»).
AVAILABILITY_LAG = timedelta(hours=9)
# BingX gateway codes that mean "retry later": rate limit, system busy, internal error.
RETRYABLE_CODES = frozenset({100410, 100500, 109500})
_BOM = b"\xef\xbb\xbf"
_TICKER = re.compile(r"^[A-Z0-9]+$")


class BingXDownloadError(RuntimeError):
    """Raised for one pair/day failure or invalid downloader settings."""


@dataclass(frozen=True, slots=True)
class BingXContract:
    ticker: str
    api_symbol: str
    file_ticker: str
    launch_ms: int


@dataclass(frozen=True, slots=True)
class BingXSettings:
    bot_root: Path
    download_mode: str = "incremental"
    start_date: date | None = None
    end_date: date | None = None
    pairs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DownloadError:
    pair: str
    day: date | None
    message: str


@dataclass(slots=True)
class DownloadReport:
    downloaded: int = 0
    skipped_valid: int = 0
    redownloaded: int = 0
    errors: list[DownloadError] = field(default_factory=list)
    contracts: dict[str, BingXContract] = field(default_factory=dict)

    def summary_lines(self) -> list[str]:
        lines = [
            f"скачано новых дней: {self.downloaded}",
            f"пропущено уже валидных дней: {self.skipped_valid}",
            f"перекачано повреждённых дней: {self.redownloaded}",
            f"ошибки по дням/парам: {len(self.errors)}",
        ]
        for error in self.errors:
            where = f"{error.pair} {error.day.isoformat()}" if error.day else error.pair
            lines.append(f"  {where}: {error.message}")
        return lines


# -- settings ---------------------------------------------------------------


def _optional_date(section: Mapping[str, object], key: str) -> date | None:
    value = section.get(key)
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise BingXDownloadError(f"bingx.{key} must be a YYYY-MM-DD date") from None


def load_bingx_settings(config_path: Path) -> BingXSettings:
    """Read the ``bingx`` block and ``tester_runner.bot_root`` from config.local.json."""
    config_path = Path(config_path).resolve()
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BingXDownloadError(f"cannot read {config_path.name}: {error}") from None
    if not isinstance(document, dict):
        raise BingXDownloadError("config.local.json must be an object")
    runner = document.get("tester_runner")
    if not isinstance(runner, dict) or not isinstance(runner.get("bot_root"), str) or not runner["bot_root"]:
        raise BingXDownloadError("tester_runner.bot_root is required")
    bot_root = Path(runner["bot_root"])
    if not bot_root.is_absolute():
        bot_root = config_path.parent / bot_root
    section = document.get("bingx", {})
    if not isinstance(section, dict):
        raise BingXDownloadError("bingx must be an object")
    unknown = sorted(set(section) - {"start_date", "end_date", "pairs", "download_mode"})
    if unknown:
        raise BingXDownloadError("bingx contains unknown keys: " + ", ".join(unknown))
    mode = section.get("download_mode", "incremental")
    if mode not in DOWNLOAD_MODES:
        raise BingXDownloadError("bingx.download_mode must be incremental or overwrite")
    pairs = section.get("pairs", [])
    if not isinstance(pairs, list) or not all(isinstance(item, str) for item in pairs):
        raise BingXDownloadError("bingx.pairs must be a list of tickers")
    return BingXSettings(
        bot_root=bot_root.resolve(),
        download_mode=mode,
        start_date=_optional_date(section, "start_date"),
        end_date=_optional_date(section, "end_date"),
        pairs=tuple(pairs),
    )


def bingx_data_root(bot_root: Path) -> Path:
    return Path(bot_root) / "tester" / "data" / "bingx"


def load_account_keys(bot_root: Path, account: str = "bingx") -> tuple[str, str] | None:
    """Return ``(api_key, api_secret)`` from the tester account file, or None if absent."""
    path = Path(bot_root) / "settings_accounts" / f"{account}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        raise BingXDownloadError(f"cannot read account file {path.name}") from None
    key = document.get("api_key") if isinstance(document, dict) else None
    secret = document.get("api_secret") if isinstance(document, dict) else None
    if not isinstance(key, str) or not isinstance(secret, str) or not key or not secret:
        return None
    return key, secret


# -- HTTP client ------------------------------------------------------------


def _default_fetch_json(url: str, headers: dict[str, str]) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "mrs3-bingx-downloader", **headers})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


class BingXClient:
    """Minimal BingX swap REST client with bounded retries and `code` checks."""

    def __init__(
        self,
        *,
        fetch_json: Callable[[str, dict[str, str]], object] = _default_fetch_json,
        sleep: Callable[[float], None] = time.sleep,
        retries: int = 4,
        backoff_seconds: float = 1.0,
        request_delay_seconds: float = 0.2,
        credentials: tuple[str, str] | None = None,
    ) -> None:
        self._fetch_json = fetch_json
        self._sleep = sleep
        self._retries = retries
        self._backoff = backoff_seconds
        self._delay = request_delay_seconds
        self._credentials = credentials

    def _get(self, path: str, params: Mapping[str, object], *, signed: bool = False) -> object:
        query_params = dict(params)
        headers: dict[str, str] = {}
        if signed:
            if self._credentials is None:
                raise BingXDownloadError("BingX API keys are required for this request")
            key, secret = self._credentials
            query_params["timestamp"] = int(time.time() * 1000)
            query = urllib.parse.urlencode(sorted(query_params.items()))
            signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query += f"&signature={signature}"
            headers["X-BX-APIKEY"] = key
        else:
            query = urllib.parse.urlencode(list(query_params.items()))
        url = f"{BASE_URL}{path}?{query}"
        last_error: Exception | None = None
        for attempt in range(self._retries):
            if attempt:
                self._sleep(self._backoff * 2 ** (attempt - 1))
            elif self._delay:
                self._sleep(self._delay)
            try:
                payload = self._fetch_json(url, headers)
            except urllib.error.HTTPError as error:
                if error.code != 429 and error.code < 500:
                    raise BingXDownloadError(f"HTTP {error.code} from {path}") from None
                last_error = error
                continue
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError) as error:
                last_error = error
                continue
            if not isinstance(payload, dict):
                raise BingXDownloadError(f"unexpected response from {path}")
            if payload.get("code") in RETRYABLE_CODES:
                last_error = BingXDownloadError(f"BingX code {payload.get('code')}: {payload.get('msg', '')}")
                continue
            if payload.get("code") != 0:
                raise BingXDownloadError(f"BingX code {payload.get('code')}: {payload.get('msg', '')}")
            return payload.get("data")
        raise BingXDownloadError(f"request to {path} failed after {self._retries} attempts: {last_error}")

    def contracts(self) -> list[dict[str, object]]:
        data = self._get(CONTRACTS_PATH, {})
        if not isinstance(data, list):
            raise BingXDownloadError("unexpected contracts response")
        return data

    def klines(self, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, object]]:
        data = self._get(
            KLINES_PATH,
            {"symbol": symbol, "interval": "1m", "startTime": start_ms, "endTime": end_ms, "limit": 1440},
        )
        if not isinstance(data, list):
            raise BingXDownloadError("unexpected klines response")
        return data


# -- symbols and days -------------------------------------------------------


def normalize_ticker(value: str) -> str:
    """``panw`` / ``PANWUSDT`` -> ``PANW``."""
    ticker = str(value).strip().upper()
    if ticker.endswith("USDT"):
        ticker = ticker[: -len("USDT")]
    if not ticker or not _TICKER.fullmatch(ticker):
        raise BingXDownloadError(f"invalid ticker: {value!r}")
    return ticker


def resolve_contracts(
    pairs: Iterable[str], contracts: Sequence[Mapping[str, object]]
) -> tuple[dict[str, BingXContract], dict[str, str]]:
    """Map user tickers to BingX contracts by ``displayName == <TICKER>-USDT``."""
    resolved: dict[str, BingXContract] = {}
    errors: dict[str, str] = {}
    for raw in pairs:
        try:
            ticker = normalize_ticker(raw)
        except BingXDownloadError as error:
            errors[str(raw)] = str(error)
            continue
        if ticker in resolved or ticker in errors:
            continue
        display = f"{ticker}-USDT"
        matches = [item for item in contracts if item.get("displayName") == display]
        if len(matches) != 1:
            errors[ticker] = f"ticker not found on BingX: {display}"
            continue
        match = matches[0]
        try:
            launch_ms = int(match.get("launchTime") or 0)
        except (TypeError, ValueError):
            launch_ms = 0
        symbol = match.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            errors[ticker] = f"ticker not found on BingX: {display}"
            continue
        resolved[ticker] = BingXContract(ticker, symbol, display.replace("-", ""), launch_ms)
    return resolved, errors


def last_available_day(now: datetime) -> date:
    return (now.astimezone(timezone.utc) - AVAILABILITY_LAG).date() - timedelta(days=1)


def _day_start_ms(day: date) -> int:
    return int(datetime.combine(day, dt_time(), timezone.utc).timestamp() * 1000)


def _first_minute_ms(day: date, launch_ms: int) -> int:
    launch_minute = -(-launch_ms // MINUTE_MS) * MINUTE_MS
    return max(_day_start_ms(day), launch_minute)


def planned_days(start: date, end: date, launch_ms: int, last_available: date) -> list[date]:
    """Requested days from the listing day up to the last available day."""
    if launch_ms > 0:
        start = max(start, datetime.fromtimestamp(launch_ms / 1000, timezone.utc).date())
    end = min(end, last_available)
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)] if start <= end else []


def day_csv_path(data_root: Path, contract: BingXContract, day: date) -> Path:
    return Path(data_root) / contract.api_symbol / f"{contract.file_ticker}{day.isoformat()}_1m.csv"


# -- CSV --------------------------------------------------------------------


def _number(text: str) -> Decimal:
    value = Decimal(text)
    if not value.is_finite():
        raise InvalidOperation
    return value


def _validate_rows(rows: Sequence[Sequence[str]], day: date, launch_ms: int) -> str | None:
    first = _first_minute_ms(day, launch_ms)
    expected = (_day_start_ms(day) + DAY_MS - first) // MINUTE_MS
    if expected <= 0:
        return "day is before listing"
    if len(rows) != expected:
        return f"expected {expected} rows, got {len(rows)}"
    for index, row in enumerate(rows):
        if len(row) != len(CSV_HEADER):
            return f"row {index + 1} has {len(row)} columns"
        try:
            stamp = int(row[0])
            opened, high, low, close, volume = (_number(value) for value in row[1:6])
        except (ValueError, InvalidOperation):
            return f"row {index + 1} has an empty or invalid value"
        if str(stamp) != row[0] or stamp != first + index * MINUTE_MS:
            return f"row {index + 1} timestamp is not the expected minute"
        if high < max(opened, close, low) or low > min(opened, close) or volume < 0:
            return f"row {index + 1} has inconsistent OHLCV"
        if row[6:] != ["0", "0", "0"]:
            return f"row {index + 1} buy_volume/sell_volume/trades must be 0"
    return None


def validate_day_csv(path: Path, day: date, launch_ms: int) -> str | None:
    """Return None for a complete valid day file, otherwise the reason it is invalid."""
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        return f"cannot read file: {error}"
    if not raw.startswith(_BOM):
        return "missing UTF-8 BOM"
    try:
        text = raw[len(_BOM):].decode("utf-8")
    except UnicodeDecodeError:
        return "not UTF-8"
    if "\r" in text:
        return "line endings must be LF"
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0] != ",".join(CSV_HEADER):
        return "invalid header"
    return _validate_rows([line.split(",") for line in lines[1:]], day, launch_ms)


def _rows_from_klines(klines: Sequence[Mapping[str, object]], day: date) -> list[list[str]]:
    start = _day_start_ms(day)
    rows = []
    for candle in sorted(klines, key=lambda item: int(item.get("time", 0))):
        stamp = int(candle.get("time", 0))
        if start <= stamp < start + DAY_MS:
            rows.append(
                [str(stamp)] + [str(candle.get(name, "")) for name in ("open", "high", "low", "close", "volume")] + ["0", "0", "0"]
            )
    return rows


def write_day_csv(path: Path, rows: Sequence[Sequence[str]], day: date, launch_ms: int) -> None:
    """Write via a sibling ``.tmp`` file and replace the final CSV only after validation."""
    reason = _validate_rows(rows, day, launch_ms)
    if reason is not None:
        raise BingXDownloadError(f"incomplete or invalid day: {reason}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    body = "\n".join([",".join(CSV_HEADER), *(",".join(row) for row in rows)]) + "\n"
    try:
        with temporary.open("wb") as handle:
            handle.write(_BOM + body.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        reason = validate_day_csv(temporary, day, launch_ms)
        if reason is not None:
            raise BingXDownloadError(f"written file failed validation: {reason}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# -- orchestration ----------------------------------------------------------


def download_pairs(
    *,
    bot_root: Path,
    pairs: Iterable[str],
    start: date,
    end: date,
    mode: str = "incremental",
    client: BingXClient | None = None,
    now: datetime | None = None,
    log: Callable[[str], None] = lambda _message: None,
) -> DownloadReport:
    """Download or top up daily CSV files; one failing pair/day never stops the rest."""
    if mode not in DOWNLOAD_MODES:
        raise BingXDownloadError("download mode must be incremental or overwrite")
    if start > end:
        raise BingXDownloadError("start date must not be after end date")
    client = client or BingXClient()
    report = DownloadReport()
    pairs = list(pairs)
    try:
        contracts = client.contracts()
    except Exception as error:
        report.errors.extend(DownloadError(str(pair), None, str(error)) for pair in pairs)
        return report
    resolved, unresolved = resolve_contracts(pairs, contracts)
    report.contracts = resolved
    for pair, message in unresolved.items():
        report.errors.append(DownloadError(pair, None, message))
        log(f"{pair}: {message}")
    data_root = bingx_data_root(bot_root)
    last_day = last_available_day(now or datetime.now(timezone.utc))
    for ticker, contract in resolved.items():
        for day in planned_days(start, end, contract.launch_ms, last_day):
            path = day_csv_path(data_root, contract, day)
            damaged = path.exists() and validate_day_csv(path, day, contract.launch_ms) is not None
            if path.exists() and not damaged and mode == "incremental":
                report.skipped_valid += 1
                continue
            try:
                day_start = _day_start_ms(day)
                klines = client.klines(contract.api_symbol, day_start, day_start + DAY_MS - 1)
                write_day_csv(path, _rows_from_klines(klines, day), day, contract.launch_ms)
            except Exception as error:  # One failing day must never stop the remaining pairs/days.
                report.errors.append(DownloadError(ticker, day, str(error) or type(error).__name__))
                log(f"{ticker} {day.isoformat()}: {error}")
                continue
            if damaged:
                report.redownloaded += 1
            else:
                report.downloaded += 1
            log(f"{ticker} {day.isoformat()}: ok")
    return report

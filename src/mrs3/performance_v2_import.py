"""Lock-first transactional publication for the unified Performance v2 store."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import csv
import json
from pathlib import Path
from typing import Callable, Mapping
from uuid import uuid4

import duckdb
import pandas as pd

from .performance import PerformanceParseError, report_range
from .performance_v2_html import PerformanceV2HtmlError, ParsedPerformanceV2Report, parse_current_performance_v2_html
from .loader import load_listing_dates
from .performance_v2_input import (
    PerformanceV2InputError,
    PreparedV2Entry,
    PreparedV2Input,
    _shift_from_multiplier,
    create_v2_parser_staging,
    read_performance_v2_inbox,
    remove_v2_parser_staging,
)
from .performance_v2_store import (
    PerformanceV2Config,
    PerformanceV2StoreError,
    initialize_performance_v2,
    performance_v2_database_path,
    require_performance_v2,
    PerformanceV2WriterLock,
)


class PerformanceV2ImportError(RuntimeError):
    """Raised when a v2 publication cannot be committed safely."""


class PerformanceV2LockedError(PerformanceV2ImportError):
    """Raised when another process owns the v2 DuckDB writer lock."""


ProgressCallback = Callable[[str, int, int], object]
_APPEND_BATCH_ROWS = 20_000
_WARMUP_HOURS = 120
_INTERVAL_MISSING = object()
_ACTION_COLUMNS = (
    "result_id", "action_index", "timestamp_utc", "symbol", "order_id", "action",
    "size", "post_size", "post_side", "pnl", "fee", "balance", "raw_action_json",
)
_EQUITY_COLUMNS = ("result_id", "sample_index", "timestamp_utc", "wallet", "equity")


@dataclass(frozen=True, slots=True)
class _StoredResult:
    result_id: int
    strategy_id: int
    report_start_utc: object
    report_end_utc: object
    exchange: object
    commission_rate: object
    initial_balance: object
    final_balance: object
    total_pnl: object
    total_pnl_pct: object
    max_drawdown: object
    max_drawdown_pct: object
    total_fees: object
    total_trades: object
    reported_start_utc: object
    reported_end_utc: object
    listing_date_utc: object
    listing_date_raw: object
    listing_date_source: object
    effective_start_utc: object
    effective_end_utc: object
    warmup_hours: object
    excluded_trade_count: object
    exclusion_reason: object


@dataclass(frozen=True, slots=True, init=False)
class PerformanceV2ImportRequest:
    inbox: Path
    report_root: Path
    config: PerformanceV2Config
    mode: str
    replacement_strategy_ids: Mapping[str, int]
    expected_strategy_identities: Mapping[str, object] | None
    strategy_root: Path | None
    clear_retest_on_success: bool
    test_start: str | None
    test_end: str | None
    listing_dates_path: Path | None
    listing_dates_root: Path | None

    def __init__(
        self,
        inbox: Path | None = None,
        report_root: Path | None = None,
        config: PerformanceV2Config | None = None,
        *,
        inbox_path: Path | None = None,
        tester_report_root: Path | None = None,
        mode: str = "ADD",
        replacement_strategy_ids: Mapping[str, int] | None = None,
        strategy_id_mapping: Mapping[str, int] | None = None,
        expected_strategy_identities: Mapping[str, object] | None = None,
        strategy_root: Path | None = None,
        clear_retest_on_success: bool = False,
        test_start: str | None = None,
        test_end: str | None = None,
        listing_dates_path: Path | None = None,
        listing_dates_root: Path | None = None,
    ) -> None:
        if inbox is None:
            inbox = inbox_path
        if report_root is None:
            report_root = tester_report_root
        if inbox is None or report_root is None or not isinstance(config, PerformanceV2Config):
            raise ValueError("v2 import requires inbox, report root and PerformanceV2Config")
        if replacement_strategy_ids is not None and strategy_id_mapping is not None:
            raise ValueError("replacement strategy mapping was specified twice")
        mapping = replacement_strategy_ids if replacement_strategy_ids is not None else strategy_id_mapping
        if mapping is None:
            mapping = {}
        if not isinstance(mapping, Mapping) or any(
            not isinstance(name, str) or not name.strip() or isinstance(strategy_id, bool) or not isinstance(strategy_id, int)
            for name, strategy_id in mapping.items()
        ):
            raise ValueError("replacement_strategy_ids must be a mapping")
        if mode not in {"ADD", "REPLACE"}:
            raise ValueError("v2 import mode must be ADD or REPLACE")
        if mode != "REPLACE" and (
            replacement_strategy_ids is not None
            or strategy_id_mapping is not None
            or expected_strategy_identities is not None
        ):
            raise ValueError("replacement identity controls require REPLACE mode")
        if type(clear_retest_on_success) is not bool:
            raise ValueError("clear_retest_on_success must be boolean")
        if (test_start is None) != (test_end is None):
            raise ValueError("test_start and test_end must be supplied together")
        if test_start is not None:
            try:
                parsed_start = date.fromisoformat(test_start)
                parsed_end = date.fromisoformat(test_end)  # type: ignore[arg-type]
            except (TypeError, ValueError) as error:
                raise ValueError("test_start and test_end must be ISO dates") from error
            if parsed_start.isoformat() != test_start or parsed_end.isoformat() != test_end or parsed_end < parsed_start:
                raise ValueError("test_start and test_end must be ISO dates")
        object.__setattr__(self, "inbox", Path(inbox))
        object.__setattr__(self, "report_root", Path(report_root))
        object.__setattr__(self, "config", config)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "replacement_strategy_ids", dict(mapping))
        object.__setattr__(self, "expected_strategy_identities", expected_strategy_identities)
        object.__setattr__(self, "strategy_root", None if strategy_root is None else Path(strategy_root))
        object.__setattr__(self, "clear_retest_on_success", clear_retest_on_success)
        object.__setattr__(self, "test_start", test_start)
        object.__setattr__(self, "test_end", test_end)
        object.__setattr__(
            self,
            "listing_dates_root",
            None if listing_dates_root is None else Path(listing_dates_root).resolve(),
        )
        if listing_dates_path is not None:
            listing_path = Path(listing_dates_path)
            if listing_path.is_absolute() or ".." in listing_path.parts:
                raise ValueError("listing dates path must be relative to a trusted input root")
            listing_dates_path = listing_path
        object.__setattr__(self, "listing_dates_path", listing_dates_path)

    @property
    def inbox_path(self) -> Path:
        return self.inbox

    @property
    def tester_report_root(self) -> Path:
        return self.report_root

    @property
    def strategy_id_mapping(self) -> Mapping[str, int]:
        return self.replacement_strategy_ids


@dataclass(frozen=True, slots=True)
class PerformanceV2ImportResult:
    import_id: str
    status: str
    imported_count: int = 0
    skipped_count: int = 0
    rejected_count: int = 0
    database_path: Path | None = None
    audit_path: Path | None = None
    phases: Mapping[str, float] = field(default_factory=dict)
    failure_report_path: Path | None = None
    failure_report_xlsx_path: Path | None = None
    failure_count: int = 0
    excluded_trade_count: int = 0

    @property
    def committed(self) -> bool:
        return self.status == "COMMITTED"

    @property
    def imported(self) -> int:
        return self.imported_count

    @property
    def skipped(self) -> int:
        return self.skipped_count

    @property
    def rejected(self) -> int:
        return self.rejected_count


def _parse_staged_report(path: Path, limits: PerformanceV2Config) -> ParsedPerformanceV2Report:
    return parse_current_performance_v2_html(path.read_bytes(), limits)


def _append_rows(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    columns: tuple[str, ...],
    rows: list[tuple[object, ...]],
) -> None:
    if rows:
        connection.append(
            table,
            pd.DataFrame.from_records(
                [
                    tuple(
                        format(value.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP), "f")
                        if isinstance(value, Decimal)
                        else value
                        for value in row
                    )
                    for row in rows
                ],
                columns=columns,
            ),
        )


def _parse_reports(
    staging: Path,
    prepared: PreparedV2Input,
    config: PerformanceV2Config,
    progress: ProgressCallback | None = None,
    *,
    parse_errors: list[str | None] | None = None,
) -> tuple[ParsedPerformanceV2Report | None, ...]:
    paths = tuple(staging / "reports" / entry.report_path.name for entry in prepared.entries)
    workers = min(config.workers, len(paths))
    if progress is not None:
        progress("PARSING", 0, len(paths))
    if workers == 1:
        parsed = []
        for path in paths:
            try:
                parsed.append(_parse_staged_report(path, config))
            except Exception as error:
                parsed.append(None)
                if parse_errors is not None:
                    parse_errors[len(parsed) - 1] = f"{type(error).__name__}: {str(error).strip()}"[:512]
            if progress is not None:
                progress("PARSING", len(parsed), len(paths))
        return tuple(parsed)
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_parse_staged_report, path, config): index for index, path in enumerate(paths)}
        parsed: list[ParsedPerformanceV2Report | None] = [None] * len(paths)
        for completed, future in enumerate(as_completed(futures), start=1):
            try:
                parsed[futures[future]] = future.result()
            except Exception as error:
                if parse_errors is not None:
                    parse_errors[futures[future]] = f"{type(error).__name__}: {str(error).strip()}"[:512]
            if progress is not None:
                progress("PARSING", completed, len(paths))
    return tuple(parsed)


def _listing_datetime(value: object) -> datetime:
    """Normalize the existing loader's UTC timestamp without accepting naive values."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("listing date has no UTC offset")
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        return _listing_datetime(to_pydatetime())
    raise ValueError("listing date is invalid")


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")


def _warmup_report(
    entry: PreparedV2Entry,
    report: ParsedPerformanceV2Report,
    listing_dates: Mapping[str, object],
) -> tuple[ParsedPerformanceV2Report | None, dict[str, object] | None]:
    """Trim one report to its listing warm-up range, preserving parser types."""
    try:
        reported_start, reported_end = report_range(report.metrics)
    except PerformanceParseError:
        return None, {"strategy_name": entry.strategy_name, "reason": "INVALID_REPORT_RANGE"}
    reported_start = reported_start.astimezone(timezone.utc)
    reported_end = reported_end.astimezone(timezone.utc)
    # ``report_end`` is an inclusive UTC endpoint (the existing parser accepts
    # an event exactly at that instant).  Anything later is outside this
    # report and must not be used to flush an otherwise open lifecycle.
    report_end_inclusive = reported_end
    raw_listing = listing_dates.get(entry.identity.symbol)
    if raw_listing is None:
        return None, {
            "strategy_name": entry.strategy_name,
            "symbol": entry.identity.symbol,
            "reported_start": reported_start.isoformat(),
            "reported_end": reported_end.isoformat(),
            "reason": "LISTING_MISSING",
        }
    try:
        listing = _listing_datetime(raw_listing)
    except (TypeError, ValueError):
        return None, {
            "strategy_name": entry.strategy_name,
            "symbol": entry.identity.symbol,
            "reported_start": reported_start.isoformat(),
            "reported_end": reported_end.isoformat(),
            "listing_raw": str(raw_listing),
            "reason": "LISTING_INVALID",
        }
    effective_start = max(reported_start, listing + timedelta(hours=_WARMUP_HOURS))
    if effective_start >= report_end_inclusive:
        return None, {
            "strategy_name": entry.strategy_name,
            "symbol": entry.identity.symbol,
            "reported_start": reported_start.isoformat(),
            "reported_end": reported_end.isoformat(),
            "effective_start": effective_start.isoformat(),
            "effective_end": reported_end.isoformat(),
            "listing_raw": str(raw_listing),
            "listing_normalized": listing.isoformat(),
            "reason": "EFFECTIVE_RANGE_EMPTY",
        }

    for previous, current in zip(report.actions, report.actions[1:], strict=False):
        if previous.timestamp_utc > current.timestamp_utc:
            return None, {
                "strategy_name": entry.strategy_name,
                "symbol": entry.identity.symbol,
                "reason": "ACTIONS_OUT_OF_ORDER",
            }
    for label, series in (("WALLET", report.wallet_series), ("EQUITY", report.equity_series)):
        if any(previous[0] > current[0] for previous, current in zip(series, series[1:], strict=False)):
            return None, {
                "strategy_name": entry.strategy_name,
                "symbol": entry.identity.symbol,
                "reason": f"{label}_OUT_OF_ORDER",
            }

    # Group actions by complete position lifecycles.  A pyramided position has
    # multiple opens and partial closes, so a single boolean is not enough.
    retained: list[object] = []
    current_trade: list[object] = []
    active_trade = False
    trade_excluded = False
    excluded_trades = 0
    retained_trade_count = 0
    trade_results: list[Decimal] = []
    for action in report.actions:
        action_name = action.action.casefold()
        if action.timestamp_utc > report_end_inclusive:
            # Actions after the inclusive report end cannot complete a trade
            # inside this report.  Leave the lifecycle open so it is dropped
            # below instead of flushing an opening fee as a fake result.
            break
        if action_name not in {"opened", "increased", "decreased", "closed"}:
            return None, {
                "strategy_name": entry.strategy_name,
                "symbol": entry.identity.symbol,
                "reported_start": reported_start.isoformat(),
                "reported_end": reported_end.isoformat(),
                "effective_start": effective_start.isoformat(),
                "effective_end": reported_end.isoformat(),
                "listing_raw": str(raw_listing),
                "listing_normalized": listing.isoformat(),
                "action": action.action,
                "reason": "UNKNOWN_ACTION",
            }
        is_open = action_name in {"opened", "increased"}
        is_close = action_name in {"decreased", "closed"}
        if is_open:
            if action_name == "opened" and active_trade:
                return None, {
                    "strategy_name": entry.strategy_name,
                    "symbol": entry.identity.symbol,
                    "reported_start": reported_start.isoformat(),
                    "reported_end": reported_end.isoformat(),
                    "effective_start": effective_start.isoformat(),
                    "effective_end": reported_end.isoformat(),
                    "listing_raw": str(raw_listing),
                    "listing_normalized": listing.isoformat(),
                    "action": action.action,
                    "reason": "INVALID_ACTION_STATE",
                }
            if action_name == "increased" and not active_trade:
                return None, {
                    "strategy_name": entry.strategy_name,
                    "symbol": entry.identity.symbol,
                    "reported_start": reported_start.isoformat(),
                    "reported_end": reported_end.isoformat(),
                    "effective_start": effective_start.isoformat(),
                    "effective_end": reported_end.isoformat(),
                    "listing_raw": str(raw_listing),
                    "listing_normalized": listing.isoformat(),
                    "action": action.action,
                    "reason": "INVALID_ACTION_STATE",
                }
            if not active_trade:
                active_trade = True
                current_trade = []
                trade_excluded = False
            if action.timestamp_utc < effective_start:
                trade_excluded = True
            if not trade_excluded and action.timestamp_utc <= report_end_inclusive:
                current_trade.append(action)
        else:
            if not active_trade:
                return None, {
                    "strategy_name": entry.strategy_name,
                    "symbol": entry.identity.symbol,
                    "reported_start": reported_start.isoformat(),
                    "reported_end": reported_end.isoformat(),
                    "effective_start": effective_start.isoformat(),
                    "effective_end": reported_end.isoformat(),
                    "listing_raw": str(raw_listing),
                    "listing_normalized": listing.isoformat(),
                    "action": action.action,
                    "reason": "INVALID_ACTION_STATE",
                }
            if action.timestamp_utc < effective_start:
                trade_excluded = True
            if not trade_excluded:
                current_trade.append(action)
        # A flat post-size is the authoritative lifecycle boundary.  This
        # handles both full ``closed`` rows and a full ``decreased`` row.
        if active_trade and is_close and action.post_size == 0:
            trade_pnl = sum((item.pnl - item.fee for item in current_trade), Decimal("0"))
            if trade_excluded:
                excluded_trades += 1
            else:
                retained.extend(current_trade)
                trade_results.append(trade_pnl)
                retained_trade_count += 1
            current_trade = []
            active_trade = False
            trade_excluded = False
    # An open position at the report boundary is not a complete round trip.
    # Count an excluded one for diagnostics, but never publish its fee/PnL.
    if active_trade:
        # Every position still open at the report boundary is dropped. This
        # applies equally to warm-up and no-trim reports, but an already
        # counted excluded lifecycle must not be counted twice.
        excluded_trades += 1
    actions = tuple(retained)
    if not actions:
        return None, {
            "strategy_name": entry.strategy_name,
            "symbol": entry.identity.symbol,
            "reported_start": reported_start.isoformat(),
            "reported_end": reported_end.isoformat(),
            "effective_start": effective_start.isoformat(),
            "effective_end": reported_end.isoformat(),
            "listing_raw": str(raw_listing),
            "listing_normalized": listing.isoformat(),
            "excluded_trade_count": excluded_trades,
            "reason": "NO_EFFECTIVE_TRADE",
        }
    # When no warm-up is required and no partial lifecycle was dropped, tester
    # metrics and samples are authoritative.  A dropped open-at-end lifecycle
    # still requires the same reconciliation as a trimmed report.
    if effective_start == reported_start and excluded_trades == 0:
        return replace(
            report,
            reported_start_utc=reported_start,
            reported_end_utc=reported_end,
            listing_date_utc=listing,
            listing_date_raw=str(raw_listing),
            listing_date_source="configured_listing_dates_path",
            effective_start_utc=effective_start,
            effective_end_utc=reported_end,
            warmup_hours=_WARMUP_HOURS,
            excluded_trade_count=0,
            exclusion_reason=None,
        ), None

    # Recompute the action-derived financial totals from retained lifecycles,
    # but preserve every parsed action field.  In particular, do not invent a
    # balance series from action deltas: wallet/equity are tester observations
    # and must remain empty when the source did not provide them.
    # Anchor the researched balance to the source's first pre-action balance;
    # this removes all warm-up lifecycle deltas, including excluded fees/PnL.
    source_first = report.actions[0]
    baseline = source_first.balance - source_first.pnl + source_first.fee
    fees = sum((action.fee for action in actions), Decimal("0"))
    total_pnl = sum((action.pnl for action in actions), Decimal("0")) - fees
    final = baseline + total_pnl

    def rebuild_series(series: tuple[tuple[datetime, Decimal], ...]) -> tuple[tuple[datetime, Decimal], ...]:
        if not series:
            return ()
        # Values are tester observations, not a synthetic balance projection.
        # Only timestamps outside the retained report window are discarded.
        return tuple(item for item in series if effective_start <= item[0] <= report_end_inclusive)

    wallet = rebuild_series(report.wallet_series)
    equity = rebuild_series(report.equity_series)
    values = [value for _timestamp, value in equity or wallet]
    max_drawdown: Decimal | None = None
    if values:
        # Seed the peak at the baseline so the first retained sample can
        # legitimately establish drawdown below starting capital.
        peak = baseline
        max_drawdown = Decimal("0")
        for value in values:
            peak = max(peak, value)
            max_drawdown = max(max_drawdown, peak - value)
    gross_profit = sum((value for value in trade_results if value > 0), Decimal("0"))
    # Gross loss is the positive magnitude of losing trade PnL, matching the
    # Performance v2 window calculator and profit-factor convention.
    gross_loss = -sum((value for value in trade_results if value < 0), Decimal("0"))
    # Keep only metrics backed by the retained action/series set.  Parser
    # values such as Sharpe/expectancy are not recomputable after warm-up and
    # must not survive with their untrimmed values.
    metrics = {
        "Report range": f"{effective_start.date().isoformat()} - {reported_end.date().isoformat()}",
        "Initial balance": _format_decimal(baseline),
        "Final balance": _format_decimal(final),
        "Total PnL": _format_decimal(total_pnl),
        "Total PnL, %": _format_decimal((total_pnl / baseline * Decimal("100")) if baseline else Decimal("0")),
        "Total Trades": str(retained_trade_count),
        "Total transactions (buy/sell)": str(len(actions)),
        "Total fees": _format_decimal(fees),
        "Max Drawdown": _format_decimal(max_drawdown) if max_drawdown is not None else "N/A",
        "Max Drawdown, %": _format_decimal((max_drawdown / baseline * Decimal("100")) if baseline and max_drawdown is not None else Decimal("0")) if max_drawdown is not None else "N/A",
        "Win Trades": str(sum(1 for value in trade_results if value > 0)),
        "Los Trades": str(sum(1 for value in trade_results if value < 0)),
        "Gross profit": _format_decimal(gross_profit),
        "Gross loss": _format_decimal(gross_loss),
    }
    timestamps = [action.timestamp_utc for action in actions]
    timestamps.extend(timestamp for timestamp, _value in wallet)
    timestamps.extend(timestamp for timestamp, _value in equity)
    inventory = replace(
        report.inventory,
        trade_row_count=len(actions),
        wallet_sample_count=len(wallet),
        equity_sample_count=len(equity),
        minimum_timestamp=min(timestamps),
        maximum_timestamp=max(timestamps),
    )
    return replace(
        report,
        metrics=metrics,
        actions=actions,
        wallet_series=wallet,
        equity_series=equity,
        inventory=inventory,
        reported_start_utc=reported_start,
        reported_end_utc=reported_end,
        listing_date_utc=listing,
        listing_date_raw=str(raw_listing),
        listing_date_source="configured_listing_dates_path",
        effective_start_utc=effective_start,
        effective_end_utc=reported_end,
        warmup_hours=_WARMUP_HOURS,
        excluded_trade_count=excluded_trades,
        exclusion_reason=None,
    ), None


def _prepare_listing_ranges(
    request: PerformanceV2ImportRequest,
    prepared: PreparedV2Input,
    parsed: tuple[ParsedPerformanceV2Report | None, ...],
) -> tuple[tuple[ParsedPerformanceV2Report | None, ...], list[dict[str, object]]]:
    path = request.listing_dates_path
    if path is None:
        path = prepared.listing_dates_path
    if path is None:
        # A listing date is required for every run mode. Publishing an
        # untrimmed report would make the warm-up contract depend on mode.
        failures = [
            {
                "strategy_name": entry.strategy_name,
                "symbol": entry.identity.symbol,
                "reason": "LISTING_MISSING",
            }
            for entry, report in zip(prepared.entries, parsed, strict=True)
            if report is not None
        ]
        return (None,) * len(parsed), failures
    path = Path(path)
    if ".." in path.parts:
        raise PerformanceV2ImportError("listing dates path contains parent traversal")
    if path.is_absolute():
        raise PerformanceV2ImportError("listing dates path must be relative to a trusted input root")
    trusted_roots = [(request.listing_dates_root or request.inbox.parent).resolve()]
    inbox_root = request.inbox.parent.resolve()
    if inbox_root not in trusted_roots:
        trusted_roots.append(inbox_root)
    resolved_path: Path | None = None
    found_non_regular = False
    if not path.is_absolute():
        for trusted_root in trusted_roots:
            candidate = trusted_root / path
            if candidate.is_symlink():
                raise PerformanceV2ImportError("listing dates path cannot use a symlink")
            if not candidate.is_file():
                found_non_regular = found_non_regular or candidate.exists()
                continue
            try:
                resolved_candidate = candidate.resolve(strict=True)
                resolved_candidate.relative_to(trusted_root)
            except (OSError, ValueError) as error:
                raise PerformanceV2ImportError("listing dates path is outside the trusted input root") from error
            resolved_path = resolved_candidate
            break
    if resolved_path is None:
        detail = "not a regular file" if found_non_regular else "not found"
        raise PerformanceV2ImportError(f"listing dates path is {detail}")
    path = resolved_path
    failures: list[dict[str, object]] = []
    try:
        loaded = load_listing_dates(path)
        listing_dates: Mapping[str, object] = loaded
    except Exception as error:
        raise PerformanceV2ImportError("listing dates file could not be loaded") from error
    filtered: list[ParsedPerformanceV2Report | None] = []
    for entry, report in zip(prepared.entries, parsed, strict=True):
        if report is None:
            filtered.append(None)
            continue
        try:
            result, failure = _warmup_report(entry, report, listing_dates)
        except Exception as error:
            result, failure = None, {
                "strategy_name": entry.strategy_name,
                "symbol": entry.identity.symbol,
                "reason": "WARMUP_FAILED",
                "error": str(error),
            }
        filtered.append(result)
        if failure is not None:
            failure["listing_dates_path"] = str(path)
            failures.append(failure)
    return tuple(filtered), failures


def _write_failure_reports(
    config: PerformanceV2Config,
    rows: list[dict[str, object]],
    *,
    import_id: str,
    status: str,
) -> tuple[Path, Path | None]:
    root = config.database_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    csv_path = root / f"performance_v2_failures_{import_id}_{stamp}.csv"
    xlsx_path = root / f"performance_v2_failures_{import_id}_{stamp}.xlsx"
    # Keep this artifact schema stable for operators and downstream tooling.
    # These are all fields emitted by parser, warm-up, validation and abort
    # paths; missing values remain blank in a given row.
    keys = [
        "reason", "strategy_name", "symbol", "import_id", "outcome",
        "reported_start", "reported_end", "effective_start", "effective_end",
        "listing_raw", "listing_normalized", "listing_dates_path", "action",
        "excluded_trade_count", "error",
    ]
    def clean(value: object) -> str:
        if value is None:
            return ""
        return "".join(" " if ord(char) < 32 else char for char in str(value)).strip()
    rows = [
        {key: clean(row.get(key, "")) for key in keys} | {"import_id": import_id, "outcome": status}
        for row in rows
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    try:
        pd.DataFrame(rows, columns=keys).to_excel(xlsx_path, index=False)
    except Exception:
        # Keep the CSV path usable when the optional Excel writer is not
        # available or the second artifact cannot be written.
        try:
            xlsx_path.unlink()
        except OSError:
            pass
        return csv_path, None
    return csv_path, xlsx_path


def _decimal_metric(metrics: Mapping[str, str], *names: str, default: Decimal | None = None) -> Decimal | None:
    for name in names:
        value = metrics.get(name)
        if value is None or value.strip().casefold() in {"", "n/a", "na", "undefined"}:
            continue
        try:
            parsed = Decimal(value.strip())
        except (InvalidOperation, TypeError, ValueError) as error:
            raise PerformanceV2ImportError(f"metric {name!r} is not a finite Decimal") from error
        if not parsed.is_finite():
            raise PerformanceV2ImportError(f"metric {name!r} is not a finite Decimal")
        return parsed
    return default


def _int_metric(metrics: Mapping[str, str], *names: str) -> int | None:
    value = _decimal_metric(metrics, *names)
    if value is None:
        return None
    if value != value.to_integral_value():
        raise PerformanceV2ImportError(f"metric {names[0]!r} is not an integer")
    return int(value)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_report(
    entry: PreparedV2Entry,
    report: ParsedPerformanceV2Report,
    prepared: PreparedV2Input | None = None,
    request: PerformanceV2ImportRequest | None = None,
    *,
    check_range: bool = True,
) -> None:
    basic = report.settings.get("basic")
    if not isinstance(basic, Mapping) or str(basic.get("symbol", "")).strip() != entry.identity.symbol:
        raise PerformanceV2ImportError(f"report symbol does not match strategy {entry.strategy_name!r}")
    configured_start = getattr(prepared, "test_start", None)
    configured_end = getattr(prepared, "test_end", None)
    if request is not None and request.test_start is not None and (request.test_start, request.test_end) != (configured_start, configured_end):
        raise PerformanceV2ImportError("request test range does not match the prepared inbox")
    if check_range and configured_start is not None and configured_end is not None:
        try:
            reported_start, reported_end = report_range(report.metrics)
        except PerformanceParseError as error:
            raise PerformanceV2ImportError(f"report period is invalid for strategy {entry.strategy_name!r}") from error
        if (reported_start.date().isoformat(), reported_end.date().isoformat()) != (configured_start, configured_end):
            raise PerformanceV2ImportError(f"report range does not match configured batch for strategy {entry.strategy_name!r}")


def _load_existing(
    connection: duckdb.DuckDBPyConnection,
    names: tuple[str, ...] | None = None,
    *,
    typed_prefixes: tuple[tuple[object, ...], ...] = (),
) -> tuple[dict[str, tuple[object, ...]], dict[int, list[tuple[object, ...]]], dict[int, _StoredResult]]:
    clauses: list[str] = []
    strategy_args: list[object] = []
    if names:
        placeholders = ",".join("?" for _ in names)
        clauses.append(f"strategy_name in ({placeholders})")
        strategy_args.extend(names)
    for prefix in typed_prefixes:
        if len(prefix) != 5:
            raise ValueError("typed prefix must contain five fields")
        clauses.append("(symbol = ? and side = ? and timeframe = ? and close_ma_len = ? and order_count = ?)")
        strategy_args.extend(prefix)
    if not clauses:
        return {}, {}, {}
    strategy_sql = f"""select strategy_name, strategy_id, symbol, side, timeframe, close_ma_len,
                       order_count, analysis_run_id, candidate_identity, lifecycle_status,
                       current_result_id from strategies where {' or '.join(clauses)}"""
    strategies = {str(row[0]): row for row in connection.execute(strategy_sql, strategy_args).fetchall()}
    ids = tuple(int(row[1]) for row in strategies.values())
    if not ids:
        return strategies, {}, {}
    id_placeholders = ",".join("?" for _ in ids)
    orders: dict[int, list[tuple[object, ...]]] = {}
    for row in connection.execute(
        f"""select strategy_id, order_id, open_ma_len, open_multiplier, shift_bp, lot_x,
                   analysis_run_id, plateau_id, base_point_trades
               from strategy_orders where strategy_id in ({id_placeholders})
               order by strategy_id, order_id""",
        list(ids),
    ).fetchall():
        orders.setdefault(int(row[0]), []).append(row)
    result_ids = tuple(int(row[10]) for row in strategies.values() if row[10] is not None)
    results: dict[int, tuple[object, ...]] = {}
    if result_ids:
        result_placeholders = ",".join("?" for _ in result_ids)
        results = {
            int(row[0]): _StoredResult(*row)
            for row in connection.execute(
                f"""select result_id, strategy_id, report_start_utc, report_end_utc, exchange,
                           commission_rate, initial_balance, final_balance, total_pnl,
                           total_pnl_pct, max_drawdown, max_drawdown_pct, total_fees, total_trades,
                           reported_start_utc, reported_end_utc, listing_date_utc, listing_date_raw,
                           listing_date_source, effective_start_utc, effective_end_utc, warmup_hours,
                           excluded_trade_count, exclusion_reason
                       from strategy_results where result_id in ({result_placeholders})""",
                list(result_ids),
            ).fetchall()
        }
    return strategies, orders, results


def _quantized_lot(value: object) -> Decimal:
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
        if not decimal.is_finite():
            raise PerformanceV2ImportError("lot_x is not finite")
        with localcontext() as context:
            context.prec = 40
            normalized = decimal.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP)
        if normalized.copy_abs().adjusted() > 25:
            raise PerformanceV2ImportError("lot_x exceeds DECIMAL(38,12) precision")
        return normalized.copy_abs() if normalized.is_zero() else normalized
    except PerformanceV2ImportError:
        raise
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PerformanceV2ImportError("lot_x exceeds DECIMAL(38,12) precision") from error


def _typed_key(entry: PreparedV2Entry) -> tuple[object, ...]:
    identity = entry.identity
    orders = tuple(sorted(
        (int(order.open_ma_len), int(order.shift_bp), _quantized_lot(order.lot_x))
        for order in identity.orders
    ))
    return (
        str(identity.symbol).strip(),
        str(identity.side).strip(),
        str(identity.timeframe).strip(),
        int(identity.close_ma_len),
        int(identity.order_count),
        orders,
    )


def _stored_typed_key(row: tuple[object, ...], orders: list[tuple[object, ...]]) -> tuple[object, ...] | None:
    try:
        if any(row[index] is None for index in (2, 3, 4, 5, 6)):
            return None
        order_count = int(row[6])
        if len(orders) != order_count:
            return None
        settings = tuple(sorted(
            (
                int(order[2]),
                int(order[4]),
                _quantized_lot(order[5]),
            )
            for order in orders
        ))
        if any(
            _shift_from_multiplier(Decimal(str(order[3])), str(row[3])) != int(order[4])
            for order in orders
        ):
            return None
    except (TypeError, ValueError, IndexError, PerformanceV2ImportError, PerformanceV2InputError):
        return None
    return (str(row[2]).strip(), str(row[3]).strip(), str(row[4]).strip(), int(row[5]), order_count, settings)


def _expected_identity_matches(entry: PreparedV2Entry, expected: object) -> bool:
    if not isinstance(expected, Mapping):
        return False
    required = {"symbol", "side", "timeframe", "close_ma_len", "order_count", "orders"}
    if not required <= set(expected):
        return False
    identity = entry.identity
    fields = {
        "symbol": str(identity.symbol).strip(),
        "side": str(identity.side).strip(),
        "timeframe": str(identity.timeframe).strip(),
        "close_ma_len": int(identity.close_ma_len),
        "order_count": int(identity.order_count),
    }
    for field, actual in fields.items():
        if field in expected:
            value = expected[field]
            if isinstance(actual, int) and type(value) is not int:
                return False
            if value != actual:
                return False
    raw_orders = expected["orders"]
    if not isinstance(raw_orders, (list, tuple)):
        return False
    if len(raw_orders) != fields["order_count"] or any(
        not isinstance(order, Mapping) for order in raw_orders
    ):
        return False
    try:
        if any(
            ("open_ma_len" in order and "open_ma" in order and order["open_ma_len"] != order["open_ma"])
            or
            type(order.get("open_ma_len", order.get("open_ma"))) is not int
            or type(order.get("shift_bp")) is not int
            for order in raw_orders
        ):
            return False
        expected_orders = tuple(sorted(
            (
                order.get("open_ma_len", order.get("open_ma")),
                order["shift_bp"],
                _quantized_lot(order["lot_x"]),
            )
            for order in raw_orders
        ))
    except (KeyError, TypeError, ValueError, PerformanceV2ImportError):
        return False
    return expected_orders == _typed_key(entry)[-1]


def _utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _comparison_interval(
    report: ParsedPerformanceV2Report,
    stored: _StoredResult | None | object = _INTERVAL_MISSING,
) -> tuple[datetime, datetime] | None:
    try:
        report_start, report_end = report_range(report.metrics)
    except PerformanceParseError:
        return None
    if stored is _INTERVAL_MISSING:
        raw_start = report.effective_start_utc or report.reported_start_utc or report_start
        raw_end = report.effective_end_utc or report.reported_end_utc or report_end
        listing_value = report.listing_date_utc
    elif stored is None:
        return None
    else:
        raw_start = stored.effective_start_utc or stored.reported_start_utc or stored.report_start_utc
        raw_end = stored.effective_end_utc or stored.reported_end_utc or stored.report_end_utc
        # The incoming listing is the configured listing date.  Falling back
        # to the stored value supports direct callers and legacy rows.
        listing_value = report.listing_date_utc or stored.listing_date_utc
    start = _utc_timestamp(raw_start)
    end = _utc_timestamp(raw_end)
    if start is None or end is None or listing_value is None:
        return None
    try:
        listing = _listing_datetime(listing_value)
    except (TypeError, ValueError):
        return None
    listing = _utc_timestamp(listing)
    if listing is None:
        return None
    start = max(start, listing + timedelta(hours=_WARMUP_HOURS))
    return (start, end) if start < end else None


def _interval_relation(
    incoming: tuple[datetime, datetime] | None,
    current: tuple[datetime, datetime] | None,
) -> str:
    if (
        incoming is None
        or current is None
        or any(value is None for value in (*incoming, *current))
    ):
        return "UNKNOWN"
    incoming_start, incoming_end = incoming
    current_start, current_end = current
    if incoming_start <= current_start and incoming_end >= current_end:
        return "SUPERSET" if incoming_start < current_start or incoming_end > current_end else "EQUAL"
    return "SKIP"


def _plateau_facts(connection: duckdb.DuckDBPyConnection, prepared: PreparedV2Input) -> dict[tuple[str, str], tuple[object, ...]]:
    if not prepared.plateaus:
        return {}
    clauses = " or ".join("(analysis_run_id = ? and plateau_id = ?)" for _ in prepared.plateaus)
    values = [value for fact in prepared.plateaus for value in (fact.analysis_run_id, fact.plateau_id)]
    return {
        (str(row[0]), str(row[1])): row
        for row in connection.execute(
            f"select analysis_run_id, plateau_id, plateau_point_count, plateau_total_trades from analysis_plateaus where {clauses}",
            values,
        ).fetchall()
    }


def _publish(
    connection: duckdb.DuckDBPyConnection,
    request: PerformanceV2ImportRequest,
    prepared: PreparedV2Input,
    parsed: tuple[ParsedPerformanceV2Report | None, ...],
    import_id: str,
    *,
    failure_reasons: Mapping[str, str] | None = None,
) -> tuple[int, int, int]:
    # The writer lock is held by the caller.  Start the transaction before any
    # read so key indexes and decisions cannot go stale between validation and
    # publication.
    connection.execute("begin")
    try:
        names = tuple(entry.strategy_name for entry in prepared.entries)
        incoming_keys = {_typed_key(entry) for entry in prepared.entries}
        incoming_prefixes = tuple(sorted(key[:5] for key in incoming_keys))
        existing, existing_orders, existing_results = _load_existing(
            connection, names, typed_prefixes=incoming_prefixes
        )
        existing_plateaus = _plateau_facts(connection, prepared)
        for fact in prepared.plateaus:
            old = existing_plateaus.get((fact.analysis_run_id, fact.plateau_id))
            if old is not None and (int(old[2]), int(old[3])) != (fact.plateau_point_count, fact.plateau_total_trades):
                raise PerformanceV2ImportError(f"typed plateau mismatch for {fact.plateau_id!r}")

        active_by_key: dict[tuple[object, ...], list[tuple[object, ...]]] = {}
        stored_keys: dict[int, tuple[object, ...] | None] = {}
        for name, row in existing.items():
            strategy_id = int(row[1])
            key = _stored_typed_key(row, existing_orders.get(strategy_id, []))
            stored_keys[strategy_id] = key
            if str(row[9]) == "ACTIVE":
                if key is None:
                    try:
                        base = tuple(str(row[index]).strip() for index in (2, 3, 4)) + (
                            int(row[5]), int(row[6])
                        )
                    except (TypeError, ValueError, IndexError):
                        base = None
                    if base in incoming_prefixes:
                        raise PerformanceV2ImportError(
                            f"active strategy {row[0]!r} has invalid typed configuration"
                        )
                elif key in incoming_keys:
                    active_by_key.setdefault(key, []).append(row)
        if any(len(rows) > 1 for rows in active_by_key.values()):
            raise PerformanceV2ImportError("multiple ACTIVE strategies share a typed key")

        if request.mode == "REPLACE":
            if set(request.replacement_strategy_ids) != set(names):
                raise PerformanceV2ImportError("REPLACE requires an explicit strategy mapping for every strategy")
            if request.expected_strategy_identities is not None and (
                set(request.expected_strategy_identities) != set(names)
            ):
                raise PerformanceV2ImportError(
                    "REPLACE requires an expected typed identity for every strategy"
                )
            for name, strategy_id in request.replacement_strategy_ids.items():
                row = existing.get(name)
                if row is None or int(row[1]) != int(strategy_id) or str(row[9]) != "ACTIVE":
                    raise PerformanceV2ImportError(f"replacement mapping does not match active strategy {name!r}")

        valid: list[tuple[int, PreparedV2Entry, ParsedPerformanceV2Report]] = []
        decisions: list[tuple[str, PreparedV2Entry, ParsedPerformanceV2Report | None, tuple[object, ...] | None, PreparedV2Entry]] = []
        rejected = 0
        for index, (entry, report) in enumerate(zip(prepared.entries, parsed, strict=True)):
            if report is None:
                decisions.append(("REJECTED", entry, None, None, entry))
                rejected += 1
            else:
                # The same range was already validated before listing/warm-up
                # preparation; avoid repeating that check inside publication.
                _validate_report(entry, report, prepared, request, check_range=False)
                valid.append((index, entry, report))

        # Validate every incoming name, including entries later reduced as
        # same-key aliases.  Otherwise an alias could hide a name collision.
        for _index, entry, _report in valid:
            name_row = existing.get(entry.strategy_name)
            if name_row is not None and stored_keys.get(int(name_row[1])) != _typed_key(entry):
                raise PerformanceV2ImportError(f"typed strategy mismatch for existing {entry.strategy_name!r}")

        groups: dict[tuple[object, ...], list[tuple[int, PreparedV2Entry, ParsedPerformanceV2Report]]] = {}
        for item in valid:
            groups.setdefault(_typed_key(item[1]), []).append(item)
        representatives: dict[tuple[object, ...], tuple[int, PreparedV2Entry, ParsedPerformanceV2Report]] = {}
        for key, members in groups.items():
            if request.mode == "REPLACE" and len(members) > 1:
                raise PerformanceV2ImportError("REPLACE requires one entry per typed key")
            if len(members) == 1:
                representatives[key] = members[0]
                continue
            intervals = {item[0]: _comparison_interval(item[2]) for item in members}
            if any(interval is None for interval in intervals.values()):
                raise PerformanceV2ImportError("same-batch typed-key interval is invalid")
            containing = [
                item for item in members
                if all(
                    intervals[item[0]][0] <= intervals[peer[0]][0]
                    and intervals[item[0]][1] >= intervals[peer[0]][1]
                    for peer in members
                )
            ]
            if not containing:
                raise PerformanceV2ImportError("same-batch typed-key intervals are incomparable")
            # ``members`` retains manifest order, so equal maxima are stable.
            representatives[key] = containing[0]

        resolved: dict[tuple[object, ...], tuple[str, tuple[object, ...] | None]] = {}
        for key, (_index, entry, report) in representatives.items():
            name_row = existing.get(entry.strategy_name)
            if name_row is not None and stored_keys.get(int(name_row[1])) != key:
                raise PerformanceV2ImportError(f"typed strategy mismatch for existing {entry.strategy_name!r}")
            active_rows = active_by_key.get(key, [])
            row: tuple[object, ...] | None
            if request.mode == "REPLACE":
                row = name_row
                if row is None:
                    raise PerformanceV2ImportError(f"REPLACE target {entry.strategy_name!r} does not exist")
                if len(active_rows) != 1 or int(active_rows[0][1]) != int(row[1]):
                    raise PerformanceV2ImportError(f"REPLACE target {entry.strategy_name!r} has a typed-key collision")
                if row[10] is None or int(row[10]) not in existing_results:
                    raise PerformanceV2ImportError(f"REPLACE target {entry.strategy_name!r} has no current result")
                current = existing_results[int(row[10])]
                incoming_interval = _comparison_interval(report)
                current_interval = _comparison_interval(report, current)
                if incoming_interval is None or current_interval is None:
                    raise PerformanceV2ImportError(
                        f"REPLACE target {entry.strategy_name!r} has an invalid effective period"
                    )
                incoming_duration = incoming_interval[1] - incoming_interval[0]
                current_duration = current_interval[1] - current_interval[0]
                if incoming_interval[1] < current_interval[1] or incoming_duration < current_duration:
                    raise PerformanceV2ImportError(
                        f"REPLACE target {entry.strategy_name!r} has a shorter effective period"
                    )
                if request.expected_strategy_identities and entry.strategy_name in request.expected_strategy_identities:
                    expected = request.expected_strategy_identities[entry.strategy_name]
                    if not _expected_identity_matches(entry, expected):
                        raise PerformanceV2ImportError("typed strategy mismatch in replacement mapping")
                resolved[key] = ("REPLACE", row)
                continue
            # An active canonical key wins over a retired alias with the same
            # incoming name; the latter cannot shadow a valid dedup target.
            row = active_rows[0] if active_rows else name_row
            if row is None:
                resolved[key] = ("ADD", None)
                continue
            if str(row[9]) != "ACTIVE" or row[10] is None:
                raise PerformanceV2ImportError(f"existing strategy {entry.strategy_name!r} has no current result")
            current = existing_results.get(int(row[10]))
            if current is None:
                raise PerformanceV2ImportError(f"existing strategy {entry.strategy_name!r} has no current result")
            relation = _interval_relation(_comparison_interval(report), _comparison_interval(report, current))
            if relation == "UNKNOWN":
                raise PerformanceV2ImportError(
                    f"existing strategy {entry.strategy_name!r} has an invalid comparison interval"
                )
            resolved[key] = ("REPLACE" if relation == "SUPERSET" else "SKIPPED", row)

        for index, entry, report in valid:
            key = _typed_key(entry)
            representative = representatives[key]
            action, old = resolved[key]
            decisions.append((action if index == representative[0] else "SKIPPED", entry, report, old, representative[1]))

        skipped = sum(1 for decision, _entry, _report, _old, _rep in decisions if decision == "SKIPPED")
        now = _utc_now()
        existing_run = connection.execute(
            "select import_run_id from import_runs where source_inbox_sha256 = ?",
            [prepared.inbox_snapshot_sha256],
        ).fetchone()
        if existing_run is None:
            run_id = int(connection.execute(
                """insert into import_runs (source_inbox_sha256, expected_report_count, imported_count,
                   skipped_count, rejected_count, status, started_at_utc)
                   values (?, ?, 0, 0, 0, 'RUNNING', ?) returning import_run_id""",
                [prepared.inbox_snapshot_sha256, len(prepared.entries), now],
            ).fetchone()[0])
        else:
            run_id = int(existing_run[0])
            connection.execute(
                """update import_runs set expected_report_count = ?, imported_count = 0,
                   skipped_count = 0, rejected_count = 0, status = 'RUNNING', started_at_utc = ?,
                   finished_at_utc = null where import_run_id = ?""",
                [len(prepared.entries), now, run_id],
            )
        imported = 0
        action_rows: list[tuple[object, ...]] = []
        equity_rows: list[tuple[object, ...]] = []
        result_files: dict[str, tuple[str, str, int, int, int, str]] = {}
        written_results: list[tuple[int, int, int]] = []
        status_priority = {"REJECTED": 0, "SKIPPED": 1, "IMPORTED": 2, "REPLACED": 2}
        published_plateaus = {
            (entry.analysis_run_id, order.plateau_id)
            for decision, entry, _report, _old, _representative in decisions
            if decision == "ADD"
            for order in entry.identity.orders
        }
        if published_plateaus:
            for fact in prepared.plateaus:
                if (fact.analysis_run_id, fact.plateau_id) not in published_plateaus:
                    continue
                connection.execute(
                    """insert into analysis_plateaus (analysis_run_id, plateau_id, plateau_point_count, plateau_total_trades)
                       values (?, ?, ?, ?) on conflict (analysis_run_id, plateau_id) do nothing""",
                    [fact.analysis_run_id, fact.plateau_id, fact.plateau_point_count, fact.plateau_total_trades],
                )
        for decision, entry, report, old, _representative in decisions:
            if report is None:
                reason = (failure_reasons or {}).get(entry.strategy_name, "INVALID_REPORT")
                record = (entry.report_path.name, report_hash(entry), entry.report_path.stat().st_size, 0, 0, f"REJECTED:{reason}")
                result_files.setdefault(record[1], record)
                continue
            if decision == "SKIPPED":
                record = (entry.report_path.name, report_hash(entry), entry.report_path.stat().st_size, len(report.actions), len(report.equity_series), "SKIPPED")
                previous = result_files.get(record[1])
                if previous is None or status_priority[record[5].split(":", 1)[0]] > status_priority[previous[5].split(":", 1)[0]]:
                    result_files[record[1]] = record
                continue
            if len(report.wallet_series) != len(report.equity_series):
                raise PerformanceV2ImportError(
                    f"wallet/equity sample count mismatch for {entry.strategy_name!r}"
                )
            if any(
                wallet[0] != equity[0]
                for wallet, equity in zip(report.wallet_series, report.equity_series, strict=True)
            ):
                raise PerformanceV2ImportError(
                    f"wallet/equity timestamps are misaligned for {entry.strategy_name!r}"
                )
            strategy_id: int
            if decision == "ADD":
                if int(entry.identity.order_count) != len(entry.identity.orders):
                    raise PerformanceV2ImportError(
                        f"typed order count mismatch for {entry.strategy_name!r}"
                    )
                strategy_id = int(connection.execute(
                    """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
                       order_count, analysis_run_id, candidate_identity, lifecycle_status,
                       created_at_utc, updated_at_utc) values (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                       returning strategy_id""",
                    [entry.strategy_name, entry.identity.symbol, entry.identity.side, entry.identity.timeframe,
                     entry.identity.close_ma_len, entry.identity.order_count, entry.analysis_run_id,
                     entry.candidate_identity, now, now],
                ).fetchone()[0])
                for order in entry.identity.orders:
                    connection.execute(
                        """insert into strategy_orders (strategy_id, order_id, open_ma_len, open_multiplier,
                           shift_bp, lot_x, analysis_run_id, plateau_id, base_point_trades)
                           values (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [strategy_id, order.order_id, order.open_ma_len, order.open_multiplier,
                         order.shift_bp, _quantized_lot(order.lot_x), entry.analysis_run_id,
                         order.plateau_id, order.base_point_trades],
                    )
            else:
                strategy_id = int(old[1])  # type: ignore[index]
                result_id = int(old[10])  # type: ignore[index]
                # Keep the existing result identity for v4 databases, whose
                # strategy_id uniqueness permits one current result per strategy.
                for table in ("strategy_actions", "strategy_equity", "window_metrics"):
                    connection.execute(f"delete from {table} where result_id = ?", [result_id])
            values = _result_values(entry, report, prepared.commission_contract, now)
            if decision == "ADD":
                result_id = int(connection.execute(
                    """insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange,
                       commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
                       max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc,
                       reported_start_utc, reported_end_utc, listing_date_utc, listing_date_raw,
                       listing_date_source, effective_start_utc, effective_end_utc, warmup_hours,
                       excluded_trade_count, exclusion_reason)
                       values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) returning result_id""",
                    [strategy_id, *values],
                ).fetchone()[0])
                connection.execute("update strategies set current_result_id = ?, updated_at_utc = ? where strategy_id = ?", [result_id, now, strategy_id])
            else:
                connection.execute(
                    """update strategy_results set report_start_utc = ?, report_end_utc = ?, exchange = ?,
                       commission_rate = ?, initial_balance = ?, final_balance = ?, total_pnl = ?, total_pnl_pct = ?,
                       max_drawdown = ?, max_drawdown_pct = ?, total_fees = ?, total_trades = ?, imported_at_utc = ?,
                       reported_start_utc = ?, reported_end_utc = ?, listing_date_utc = ?, listing_date_raw = ?,
                       listing_date_source = ?, effective_start_utc = ?, effective_end_utc = ?, warmup_hours = ?,
                       excluded_trade_count = ?, exclusion_reason = ? where result_id = ?""",
                    [*values, result_id],
                )
                connection.execute(
                    "update strategies set updated_at_utc = ? where strategy_id = ?",
                    [now, strategy_id],
                )
            for action in report.actions:
                action_rows.append((result_id, action.action_index, action.timestamp_utc, action.symbol,
                                    action.order_id, action.action, action.size, action.post_size, action.post_side,
                                    action.pnl, action.fee, action.balance, None))
            if len(action_rows) >= _APPEND_BATCH_ROWS:
                _append_rows(connection, "strategy_actions", _ACTION_COLUMNS, action_rows)
                action_rows.clear()
            for sample_index, (timestamp, wallet) in enumerate(report.wallet_series):
                equity = report.equity_series[sample_index][1]
                equity_rows.append((result_id, sample_index, timestamp, wallet, equity))
            if len(equity_rows) >= _APPEND_BATCH_ROWS:
                _append_rows(connection, "strategy_equity", _EQUITY_COLUMNS, equity_rows)
                equity_rows.clear()
            status = "REPLACED" if decision == "REPLACE" else "IMPORTED"
            record = (entry.report_path.name, report_hash(entry), entry.report_path.stat().st_size, len(report.actions), len(report.equity_series), status)
            previous = result_files.get(record[1])
            if previous is None or status_priority[status] >= status_priority[previous[5].split(":", 1)[0]]:
                result_files[record[1]] = record
            written_results.append((result_id, len(report.actions), len(report.equity_series)))
            imported += 1

        _append_rows(connection, "strategy_actions", _ACTION_COLUMNS, action_rows)
        _append_rows(connection, "strategy_equity", _EQUITY_COLUMNS, equity_rows)

        connection.executemany(
            """insert into import_files (import_run_id, source_filename, source_html_sha256, source_size_bytes,
               action_count, equity_sample_count, status) values (?, ?, ?, ?, ?, ?, ?)
               on conflict (import_run_id, source_html_sha256) do update set source_filename = excluded.source_filename,
               source_size_bytes = excluded.source_size_bytes, action_count = excluded.action_count,
               equity_sample_count = excluded.equity_sample_count, status = excluded.status""",
             [[run_id, name, digest, size, actions, equity, status] for name, digest, size, actions, equity, status in result_files.values()],
        )
        for result_id, expected_actions, expected_equity in written_results:
            actual_actions = int(connection.execute("select count(*) from strategy_actions where result_id = ?", [result_id]).fetchone()[0])
            actual_equity = int(connection.execute("select count(*) from strategy_equity where result_id = ?", [result_id]).fetchone()[0])
            if (actual_actions, actual_equity) != (expected_actions, expected_equity):
                raise PerformanceV2ImportError("result child readback count mismatch")
        # RETEST is removed only after all replacement readbacks pass.  Since
        # this remains in the same transaction, any later failure preserves
        # both the old result and its tag.
        if request.clear_retest_on_success and request.mode == "REPLACE":
            replaced_ids = [
                int(old[1])
                for decision, _entry, _report, old, _rep in decisions
                if decision == "REPLACE" and old is not None
            ]
            if replaced_ids:
                placeholders = ",".join("?" for _ in replaced_ids)
                connection.execute(
                    f"delete from strategy_tags where tag = 'RETEST' and strategy_id in ({placeholders})",
                    replaced_ids,
                )
        status = "FAILED" if imported == 0 and rejected == len(prepared.entries) and rejected > 0 else "COMMITTED"
        connection.execute(
            """update import_runs set imported_count = ?, skipped_count = ?, rejected_count = ?,
               status = ?, finished_at_utc = ? where import_run_id = ?""",
            [imported, skipped, rejected, status, _utc_now(), run_id],
        )
        connection.execute("commit")
        return imported, skipped, rejected
    except Exception as error:
        try:
            connection.execute("rollback")
        except duckdb.Error:
            pass
        if isinstance(error, PerformanceV2ImportError):
            raise
        raise PerformanceV2ImportError(str(error) or "Performance v2 transaction failed") from error


def _result_values(entry: PreparedV2Entry, report: ParsedPerformanceV2Report, contract: Mapping[str, str], now: datetime) -> list[object]:
    try:
        start, end = report_range(report.metrics)
    except PerformanceParseError as error:
        raise PerformanceV2ImportError("report period is invalid") from error
    exchange = report.settings.get("exchange")
    exchange_name = entry.exchange_name
    if isinstance(exchange, Mapping) and isinstance(exchange.get("name"), str) and exchange["name"].strip():
        exchange_name = exchange["name"].strip()
    reported_start = (report.reported_start_utc or start).astimezone(timezone.utc)
    reported_end = (report.reported_end_utc or end).astimezone(timezone.utc)
    effective_start = (report.effective_start_utc or reported_start).astimezone(timezone.utc)
    effective_end = (report.effective_end_utc or reported_end).astimezone(timezone.utc)
    return [
        effective_start,
        effective_end,
        exchange_name,
        Decimal(contract["TakerFee"]),
        _decimal_metric(report.metrics, "Initial balance", default=Decimal("0")),
        _decimal_metric(report.metrics, "Final balance", default=Decimal("0")),
        _decimal_metric(report.metrics, "Total PnL"),
        _decimal_metric(report.metrics, "Total PnL, %", "Total PnL %"),
        _decimal_metric(report.metrics, "Max Drawdown", "Max drawdown"),
        _decimal_metric(report.metrics, "Max Drawdown, %", "Max Drawdown %", "Max drawdown, %"),
        _decimal_metric(report.metrics, "Total fees", "Total Fees"),
        _int_metric(report.metrics, "Total Trades"),
        now,
        reported_start,
        reported_end,
        report.listing_date_utc,
        report.listing_date_raw,
        report.listing_date_source,
        effective_start,
        effective_end,
        report.warmup_hours,
        report.excluded_trade_count,
        report.exclusion_reason,
    ]


def report_hash(entry: PreparedV2Entry) -> str:
    return entry.report_sha256


def _write_audit(
    config: PerformanceV2Config,
    request: PerformanceV2ImportRequest,
    result: PerformanceV2ImportResult | None,
    error: Exception | None,
    source_hash: str | None,
    failure_report_paths: tuple[Path, Path | None] | None = None,
) -> Path:
    path = config.database_root.resolve() / "import_audit.v2.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "owner": "mrs3.performance_v2_import",
        "status": result.status if result is not None else "FAILED",
        "mode": request.mode,
        "source_inbox_sha256": source_hash,
        "imported_count": result.imported_count if result is not None else 0,
        "skipped_count": result.skipped_count if result is not None else 0,
        "rejected_count": result.rejected_count if result is not None else 1,
        "failure_count": result.failure_count if result is not None else 1,
        "excluded_trade_count": result.excluded_trade_count if result is not None else 0,
        "error": str(error) if error is not None else None,
        "database_path": str(performance_v2_database_path(config)),
        "failure_report_paths": [
            str(path) for path in failure_report_paths if path is not None
        ] if failure_report_paths else [],
    }
    path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    return path


def import_performance_v2(
    request: PerformanceV2ImportRequest,
    *,
    progress: ProgressCallback | None = None,
) -> PerformanceV2ImportResult:
    if not isinstance(request, PerformanceV2ImportRequest):
        raise TypeError("request must be PerformanceV2ImportRequest")
    config = request.config
    try:
        target = performance_v2_database_path(config)
    except (ValueError, TypeError) as error:
        raise PerformanceV2ImportError("invalid Performance v2 target") from error
    if not target.is_file() or target.stat().st_size == 0:
        raise PerformanceV2ImportError(
            "Performance v2 target does not exist or does not have schema version 2"
        )
    connection: duckdb.DuckDBPyConnection | None = None
    staging: Path | None = None
    lock_acquired = False
    writer_lock: PerformanceV2WriterLock | None = None
    prepared: PreparedV2Input | None = None
    result: PerformanceV2ImportResult | None = None
    failure: Exception | None = None
    failure_rows: list[dict[str, object]] = []
    import_id = uuid4().hex
    failure_report_paths: tuple[Path, Path | None] | None = None
    try:
        writer_lock = PerformanceV2WriterLock(target.parent)
        try:
            writer_lock.__enter__()
        except PerformanceV2StoreError as error:
            if "root does not exist" in str(error).casefold():
                raise PerformanceV2ImportError(
                    "Performance v2 target does not exist or does not have schema version 2"
                ) from error
            raise PerformanceV2LockedError("Performance v2 database writer is busy") from error
        try:
            connection = duckdb.connect(str(target))
        except duckdb.Error as error:
            if "lock" in str(error).casefold():
                raise PerformanceV2LockedError("Performance v2 database is locked") from error
            raise PerformanceV2ImportError("Performance v2 target does not have schema version 2") from error
        try:
            require_performance_v2(connection)
        except (PerformanceV2StoreError, duckdb.Error) as error:
            raise PerformanceV2ImportError("Performance v2 target does not have schema version 2") from error
        # Migrate additive v2 columns only after the existing target has passed
        # the lock and schema gates; this cannot create a missing database.
        try:
            initialize_performance_v2(connection)
        except (PerformanceV2StoreError, duckdb.Error) as error:
            raise PerformanceV2ImportError("Performance v2 target migration failed") from error
        lock_acquired = True
        prepared = read_performance_v2_inbox(
            request.inbox,
            request.report_root,
            config=config,
            strategy_root=request.strategy_root,
        )
        if request.test_start is not None and (request.test_start, request.test_end) != (prepared.test_start, prepared.test_end):
            raise PerformanceV2ImportError("request test range does not match the prepared inbox")
        staging = create_v2_parser_staging(config.database_root, prepared)
        parse_errors: list[str | None] = [None] * len(prepared.entries)
        parsed = _parse_reports(staging, prepared, config, progress, parse_errors=parse_errors)
        parse_failures: list[dict[str, object]] = []
        validation_failures: list[dict[str, object]] = []
        validated: list[ParsedPerformanceV2Report | None] = []
        for index, (entry, report) in enumerate(zip(prepared.entries, parsed, strict=True)):
            if report is None:
                validated.append(None)
                failure_row = {
                    "strategy_name": entry.strategy_name,
                    "symbol": entry.identity.symbol,
                    "reason": "INVALID_REPORT",
                }
                if parse_errors[index]:
                    failure_row["error"] = parse_errors[index]
                parse_failures.append(failure_row)
                continue
            try:
                _validate_report(entry, report, prepared, request)
            except PerformanceV2ImportError as error:
                validated.append(None)
                validation_failures.append({
                    "strategy_name": entry.strategy_name,
                    "symbol": entry.identity.symbol,
                    "reason": "INVALID_REPORT",
                    "error": str(error),
                })
            else:
                validated.append(report)
        parsed, listing_failures = _prepare_listing_ranges(request, prepared, tuple(validated))
        failure_rows = parse_failures + listing_failures + validation_failures
        if progress is not None:
            progress("PUBLISHING", len(parsed), len(parsed))
        failure_reasons = {
            str(row["strategy_name"]): str(row.get("reason", "INVALID_REPORT"))
            for row in failure_rows
            if row.get("strategy_name")
        }
        imported, skipped, rejected = _publish(
            connection,
            request,
            prepared,
            parsed,
            import_id,
            failure_reasons=failure_reasons,
        )
        status = "FAILED" if imported == 0 and rejected == len(prepared.entries) and rejected > 0 else "COMMITTED"
        if failure_rows:
            try:
                failure_report_paths = _write_failure_reports(
                    request.config, failure_rows, import_id=import_id, status=status
                )
            except Exception:
                # The database transaction is already committed.  Report I/O
                # must not turn a successful publication into a false FAILED.
                failure_report_paths = None
        result = PerformanceV2ImportResult(
            import_id,
            status,
            imported,
            skipped,
            rejected,
            target,
            None,
            {},
            failure_report_paths[0] if failure_report_paths else None,
            failure_report_paths[1] if failure_report_paths else None,
            len(failure_rows),
            sum(
                int(row.get("excluded_trade_count", 0) or 0)
                for row in failure_rows
            ) + sum(
                report.excluded_trade_count
                for report in parsed
                if report is not None
            ),
        )
    except Exception as error:
        failure = (
            error
            if isinstance(error, PerformanceV2ImportError)
            else PerformanceV2ImportError(str(error))
            if isinstance(error, PerformanceV2InputError)
            else PerformanceV2ImportError("Performance v2 import failed")
        )
        if failure is not error:
            failure.__cause__ = error
        if failure_report_paths is None and (failure_rows or connection is not None):
            try:
                report_rows = failure_rows or [{
                    "strategy_name": "",
                    "reason": "IMPORT_ABORTED",
                    "error": str(failure),
                }]
                failure_report_paths = _write_failure_reports(
                    request.config, report_rows, import_id=import_id, status="FAILED"
                )
            except Exception:
                failure_report_paths = None
    finally:
        if connection is not None:
            connection.close()
        if staging is not None:
            try:
                remove_v2_parser_staging(staging)
            except Exception as error:
                if failure is None:
                    failure = PerformanceV2ImportError("v2 staging cleanup failed")
                    failure.__cause__ = error
        if lock_acquired and not isinstance(failure, PerformanceV2LockedError):
            try:
                audit_path = _write_audit(
                    config,
                    request,
                    result,
                    failure,
                    prepared.inbox_snapshot_sha256 if prepared else None,
                    failure_report_paths,
                )
                if result is not None:
                    result = replace(result, audit_path=audit_path)
            except Exception as error:
                if failure is None:
                    failure = PerformanceV2ImportError("v2 audit write failed")
                    failure.__cause__ = error
        if writer_lock is not None:
            try:
                writer_lock.__exit__(None, None, None)
            except Exception as error:
                if failure is None:
                    failure = PerformanceV2ImportError("Performance v2 writer lock release failed")
                    failure.__cause__ = error
    if failure is not None:
        raise failure
    if result is None:
        raise PerformanceV2ImportError("Performance v2 import produced no result")
    return result


__all__ = [
    "PerformanceV2ImportError",
    "PerformanceV2ImportLockedError",
    "PerformanceV2ImportRequest",
    "PerformanceV2ImportResult",
    "PerformanceV2LockedError",
    "import_performance_v2",
]

# Compatibility spelling for callers that describe the typed lock exception as
# an import-specific error.
PerformanceV2ImportLockedError = PerformanceV2LockedError

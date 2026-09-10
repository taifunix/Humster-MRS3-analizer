"""Read-only, immutable input snapshots for the portfolio optimizer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import duckdb

from ..performance_v2_selection import (
    SelectionConfig,
    SelectionRequest,
    _ab_metrics_from_windows,
    _consistency_summary,
    _consistency_windows,
    _return_30d,
    _selection_windows,
    _trade_rate_30d,
    load_selection_candidates,
    parse_selection_request,
)
from ..performance_v2_store import require_performance_v2
from ..performance_v2_windows import (
    METRICS_VERSION,
    WindowMetrics,
    _Action,
    _Equity,
    _calculate,
)
from .canonical import (
    CanonicalList,
    CanonicalEnvelope,
    DecimalValue,
    TimestampValue,
    TypedValue,
    Unknown,
    canonical_digest_v1,
    canonical_json_v1,
    decimal_value,
    list_value,
    timestamp_value,
    typed_value,
    unknown_value,
)


SOURCE_SNAPSHOT_UNAVAILABLE = "SOURCE_SNAPSHOT_UNAVAILABLE"
TICK_REPLAY_AVAILABLE = "AVAILABLE"
TICK_REPLAY_UNAVAILABLE = "UNAVAILABLE"

PAIR_UNSELECTED = "PAIR_UNSELECTED"
DIRECTION_DISABLED = "DIRECTION_DISABLED"
USER_RANK_MISSING = "USER_RANK_MISSING"
USER_RANK_DUPLICATE = "USER_RANK_DUPLICATE"
USER_RANK_CUTOFF = "USER_RANK_CUTOFF"


class PortfolioInputError(RuntimeError):
    """A fail-closed source snapshot error with a stable code."""

    def __init__(self, message: str, *, code: str = SOURCE_SNAPSHOT_UNAVAILABLE) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ReplayAvailability:
    status: str
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.status == TICK_REPLAY_AVAILABLE


@dataclass(frozen=True, slots=True)
class DecisionCampaign:
    decision_campaign_id: str
    execution_campaign_id: str
    content_digest: str


@dataclass(frozen=True, slots=True)
class SnapshotIdentity:
    """Explicit upstream identity required for a reproducible input digest."""

    optimizer_config_hash: str
    algorithm_versions: Mapping[str, str]
    seed: int
    upstream_selection_window: Mapping[str, Any] | str
    account_currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.optimizer_config_hash, str) or not self.optimizer_config_hash.strip():
            raise ValueError("optimizer_config_hash is required")
        if not isinstance(self.algorithm_versions, Mapping) or not self.algorithm_versions:
            raise ValueError("algorithm_versions are required")
        if any(not isinstance(key, str) or not key or not isinstance(value, str) or not value for key, value in self.algorithm_versions.items()):
            raise ValueError("algorithm_versions must contain non-empty strings")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValueError("seed must be a non-negative integer")
        if not isinstance(self.upstream_selection_window, (str, Mapping)) or not self.upstream_selection_window:
            raise ValueError("upstream_selection_window is required")
        if not isinstance(self.account_currency, str) or not self.account_currency.strip():
            raise ValueError("account_currency is required")
        object.__setattr__(self, "optimizer_config_hash", self.optimizer_config_hash.strip())
        object.__setattr__(self, "algorithm_versions", MappingProxyType(dict(sorted(self.algorithm_versions.items()))))
        if isinstance(self.upstream_selection_window, Mapping):
            object.__setattr__(self, "upstream_selection_window", _frozen(dict(self.upstream_selection_window)))
        else:
            object.__setattr__(self, "upstream_selection_window", self.upstream_selection_window.strip())
        object.__setattr__(self, "account_currency", self.account_currency.strip())


@dataclass(frozen=True, slots=True)
class PerformanceInputSnapshot:
    source_database_path: Path
    source_schema_version: str
    database_kind: str
    database_instance_id: str
    requests: tuple[SelectionRequest, ...]
    candidates: tuple[Mapping[str, Any], ...]
    actions: tuple[Mapping[str, Any], ...]
    equity: tuple[Mapping[str, Any], ...]
    window_metrics: tuple[WindowMetrics, ...]
    provenance: Mapping[str, tuple[Mapping[str, Any], ...]]
    payload: Mapping[str, Any]
    digest: str
    canonical_json: str
    read_at_utc: datetime
    decision_replay: str = TICK_REPLAY_AVAILABLE
    tick_replay: str = TICK_REPLAY_UNAVAILABLE

    @property
    def canonical_digest(self) -> str:
        return self.digest

    @property
    def content_digest(self) -> str:
        return self.digest

    @property
    def canonical_content(self) -> str:
        """JSON content suitable for durable PortfolioStore persistence."""
        return self.canonical_json

    @property
    def decision_replay_available(self) -> bool:
        return self.decision_replay == TICK_REPLAY_AVAILABLE

    @property
    def tick_replay_available(self) -> bool:
        return self.tick_replay == TICK_REPLAY_AVAILABLE


@dataclass(frozen=True, slots=True)
class _SourceSeries:
    result_id: int
    report_start_utc: datetime
    report_end_utc: datetime
    actions: tuple[Mapping[str, Any], ...]
    equity: tuple[Mapping[str, Any], ...]


def _utc(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        result = datetime.fromisoformat(text)
    else:
        raise PortfolioInputError("source timestamp is not ISO-8601")
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _fetch_records(connection: duckdb.DuckDBPyConnection, sql: str, parameters: Sequence[object] = ()) -> tuple[dict[str, Any], ...]:
    cursor = connection.execute(sql, list(parameters))
    names = tuple(item[0] for item in cursor.description)
    return tuple(dict(zip(names, row)) for row in cursor.fetchall())


def _in_clause(ids: Sequence[int]) -> str:
    return ",".join("?" for _ in ids)


def _records_for_ids(connection: duckdb.DuckDBPyConnection, table: str, column: str, ids: Sequence[int]) -> tuple[dict[str, Any], ...]:
    if not ids:
        return ()
    # Table and column names are constants at every call site.
    return _fetch_records(
        connection,
        f"select * from {table} where {column} in ({_in_clause(ids)})",
        ids,
    )


def _source_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PortfolioInputError(f"invalid source {field}", code="INVALID_SOURCE_VALUE")
    return value


def _request(value: SelectionRequest | Mapping[str, object]) -> SelectionRequest:
    if isinstance(value, SelectionRequest):
        return value
    if isinstance(value, Mapping):
        try:
            return parse_selection_request(value)
        except Exception as error:
            raise PortfolioInputError("invalid selection request", code="INVALID_REQUEST") from error
    raise PortfolioInputError("selection requests must be typed mappings", code="INVALID_REQUEST")


def _request_payload(request: SelectionRequest, *, currency: str) -> Mapping[str, Any]:
    return {
        "identity": typed_value("selection_request", f"{request.symbol}:{request.side}", unit="1"),
        "symbol": request.symbol,
        "side": request.side,
        "stages": typed_value(
            "stage_sequence",
            {str(index): _canonical_record(asdict(stage), currency=currency) for index, stage in enumerate(request.stages)},
            unit="1",
        ),
    }


def _coerce_identity(
    identity: SnapshotIdentity | None,
    *,
    optimizer_config_hash: str | None,
    algorithm_versions: Mapping[str, str] | None,
    seed: int | None,
    upstream_selection_window: Mapping[str, Any] | str | None,
    selection_window: Mapping[str, Any] | str | None,
    account_currency: str | None,
) -> SnapshotIdentity:
    if identity is not None:
        if any(value is not None for value in (optimizer_config_hash, algorithm_versions, seed, upstream_selection_window, selection_window, account_currency)):
            raise PortfolioInputError("identity must be supplied either as one object or explicit fields", code="INVALID_IDENTITY")
        return identity
    if upstream_selection_window is None:
        upstream_selection_window = selection_window
    if any(value is None for value in (optimizer_config_hash, algorithm_versions, seed, upstream_selection_window, account_currency)):
        raise PortfolioInputError("optimizer identity facts are required", code="MISSING_IDENTITY")
    try:
        return SnapshotIdentity(
            optimizer_config_hash,
            algorithm_versions,
            seed,
            upstream_selection_window,
            account_currency,
        )
    except (TypeError, ValueError) as error:
        raise PortfolioInputError("invalid optimizer identity facts", code="INVALID_IDENTITY") from error


def _to_decimal(value: object) -> Decimal:
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise PortfolioInputError("source decimal is not finite")
    return result


def _series_for_calculation(series: _SourceSeries) -> tuple[tuple[_Action, ...], tuple[_Equity, ...]]:
    actions = tuple(
        _Action(
            int(row["action_index"]),
            _utc(row["timestamp_utc"]),
            str(row["action"]).casefold(),
            _to_decimal(row["post_size"]),
            _to_decimal(row["pnl"]),
            _to_decimal(row["fee"]),
        )
        for row in series.actions
    )
    equity = tuple(
        _Equity(
            int(row["sample_index"]),
            _utc(row["timestamp_utc"]),
            _to_decimal(row["wallet"]),
            _to_decimal(row["equity"]),
        )
        for row in series.equity
    )
    return actions, equity


_DECIMAL_FIELD_TYPES: dict[str, tuple[str, str]] = {
    # Performance source monetary values use the explicit identity currency.
    "initial_balance": ("money", "currency"),
    "final_balance": ("money", "currency"),
    "total_pnl": ("money", "currency"),
    "max_drawdown": ("money", "currency"),
    "total_fees": ("money", "currency"),
    "pnl_without_best_trade": ("money", "currency"),
    "pnl_30d_pct": ("percent", "%"),
    "pnl": ("money", "currency"),
    "fee": ("money", "currency"),
    "balance": ("money", "currency"),
    "wallet": ("money", "currency"),
    "equity": ("money", "currency"),
    "commission_rate": ("rate", "1"),
    "open_multiplier": ("multiplier", "1"),
    "lot_x": ("lot", "1"),
    "size": ("quantity", "contracts"),
    "post_size": ("quantity", "contracts"),
    "growth_factor": ("factor", "1"),
    "daily_log_return": ("log_return", "1"),
    "return_dd_ratio": ("ratio", "1"),
    "profit_factor": ("ratio", "1"),
    "holding_seconds": ("duration", "seconds"),
    "time_in_market_pct": ("percent", "%"),
}
for _field in (
    "total_pnl_pct", "max_drawdown_pct", "return_pct", "daily_growth_pct", "fees_pct", "win_rate_pct",
    "ab_pnl_change_30d_pct", "ab_return_b_pct", "ab_return_a_30d_pct", "ab_return_b_30d_pct",
    "ab_win_rate_b_pct", "ab_drawdown_b_pct", "best_trade_profit_share_pct", "pnl_without_best_trade_pct",
    "robust_pnl_30d_pct", "worst_drawdown_pct", "rank_weight_coverage_pct",
    "ab_return_floor_pct", "ab_win_rate_floor_pct", "best_trade_max_profit_share_pct",
):
    _DECIMAL_FIELD_TYPES[_field] = ("percent", "%")
for _field in (
    "trades_30d", "ab_trade_rate_a_30d", "ab_trade_rate_b_30d", "ab_calendar_days_a", "ab_calendar_days_b",
    "holding_p95_minutes", "holding_median_minutes", "ab_holding_p95_minutes", "worst_holding_p95_minutes",
    "risk_scale", "dd5_proxy", "scaled_lot_sum", "capital_proxy", "capital_efficiency",
    "ab_stability_ratio", "final_score",
    "ab_return_divisor", "ab_trade_rate_divisor", "plateau_points_pareto_pnl_multiplier",
):
    _DECIMAL_FIELD_TYPES[_field] = ("scalar", "1")
for _field in (
    "rank_quality_robust_pnl", "rank_quality_worst_drawdown", "rank_quality_ab_stability",
    "rank_quality_worst_holding", "rank_quality_first_shift", "rank_quality_minimum_plateau_points",
    "rank_quality_close_ma", "rank_weight_robust_pnl", "rank_weight_worst_drawdown",
    "rank_weight_ab_stability", "rank_weight_worst_holding", "rank_weight_first_shift",
    "rank_weight_minimum_plateau_points", "rank_weight_close_ma", "final_rank", "auto_score",
):
    _DECIMAL_FIELD_TYPES[_field] = ("scalar", "1")
for _field in ("first_shift_bp",):
    _DECIMAL_FIELD_TYPES[_field] = ("basis_points", "bp")
for _order in range(1, 5):
    _DECIMAL_FIELD_TYPES[f"order_{_order}_shift_bp"] = ("basis_points", "bp")
    _DECIMAL_FIELD_TYPES[f"order_{_order}_open_ma_len"] = ("integer", "1")
    _DECIMAL_FIELD_TYPES[f"order_{_order}_plateau_point_count"] = ("integer", "1")
for _order in range(1, 5):
    _DECIMAL_FIELD_TYPES[f"order_{_order}_open_multiplier"] = ("multiplier", "1")
    _DECIMAL_FIELD_TYPES[f"order_{_order}_lot_x"] = ("lot", "1")


def _decimal_type(field: str, currency: str) -> tuple[str, str]:
    try:
        type_tag, unit = _DECIMAL_FIELD_TYPES[field]
    except KeyError as error:
        raise PortfolioInputError(
            f"unknown Decimal snapshot field: {field}",
            code="UNKNOWN_DECIMAL_FIELD",
        ) from error
    return type_tag, currency if unit == "currency" else unit


def _canonical_value(value: Any, field: str = "", *, currency: str = "USDT") -> Any:
    if isinstance(value, (CanonicalList, DecimalValue, TimestampValue, TypedValue, Unknown)):
        return value
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        timestamp = value if isinstance(value, datetime) else datetime.combine(value, datetime.min.time())
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        precision = "microseconds" if timestamp.microsecond else "seconds"
        return timestamp_value(timestamp, precision=precision)
    if isinstance(value, Decimal):
        scale = max(0, -value.as_tuple().exponent)
        type_tag, unit = _decimal_type(field, currency)
        return typed_value(type_tag, decimal_value(value, scale=scale, unit=unit), unit=unit)
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item, str(key), currency=currency) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return typed_value(
            "sequence",
            {str(index): _canonical_value(item, field, currency=currency) for index, item in enumerate(value)},
            unit="1",
        )
    if hasattr(value, "item") and not isinstance(value, (str, bytes, bytearray)):
        try:
            return _canonical_value(value.item(), field, currency=currency)
        except (ValueError, TypeError):
            pass
    if type(value).__name__ == "NAType":
        return unknown_value("MISSING_SOURCE_VALUE")
    if isinstance(value, float):
        if not math.isfinite(value):
            return unknown_value("NONFINITE_SOURCE_VALUE")
        if field.endswith(("_open_ma_len", "_plateau_point_count")) or field == "final_rank":
            if not value.is_integer():
                raise PortfolioInputError(f"non-integral source value: {field}", code="INVALID_SOURCE_VALUE")
            return typed_value("integer", int(value), unit="1")
        scale = max(0, -Decimal(str(value)).as_tuple().exponent)
        type_tag, unit = _decimal_type(field, currency)
        return typed_value(type_tag, decimal_value(str(value), scale=scale, unit=unit), unit=unit)
    try:
        # pandas.NA and numpy NaN are explicit UNKNOWN typed facts.
        missing = value is ... or bool(value is not None and value != value)
    except (TypeError, ValueError):
        missing = False
    if missing:
        return unknown_value("MISSING_SOURCE_VALUE")
    if isinstance(value, (bytes, bytearray)):
        return typed_value("bytes", bytes(value).hex(), unit="hex")
    return value


def _canonical_record(row: Mapping[str, Any], identity: str | None = None, *, currency: str = "USDT") -> Mapping[str, Any]:
    try:
        result = {str(key): _canonical_value(value, str(key), currency=currency) for key, value in row.items()}
        if identity is not None:
            result["identity"] = typed_value("record", identity, unit="1")
        return result
    except PortfolioInputError:
        raise
    except Exception as error:
        raise PortfolioInputError("failed to assemble canonical record", code="SNAPSHOT_ASSEMBLY_FAILED") from error


def _metric_record(metric: WindowMetrics) -> Mapping[str, Any]:
    return {field.name: getattr(metric, field.name) for field in fields(metric)}


def _refresh_candidate_metrics(
    candidates: list[dict[str, Any]],
    metrics: Mapping[tuple[int, datetime, datetime, str], WindowMetrics],
    selection_config: SelectionConfig,
) -> None:
    """Replace cache-derived candidate values with metrics from frozen series."""
    for candidate in candidates:
        try:
            result_id = int(candidate["result_id"])
            report_start = _utc(candidate["report_start_utc"])
            report_end = _utc(candidate["report_end_utc"])
            full = metrics[(result_id, report_start, report_end, METRICS_VERSION)]
        except (KeyError, TypeError, ValueError) as error:
            raise PortfolioInputError("invalid source candidate metric identity", code="INVALID_SOURCE_VALUE") from error

        pnl_30d = _return_30d(full, report_start, report_end)
        trade_rate = _trade_rate_30d(full, report_start, report_end)
        candidate.update(
            total_trades=full.trade_count,
            trades_30d=trade_rate,
            pnl_30d_pct=pnl_30d,
            profit_factor=full.profit_factor,
            win_rate_pct=full.win_rate_pct,
        )
        drawdown = candidate.get("max_drawdown_pct")
        risk_scale = Decimal(5) / drawdown if isinstance(drawdown, Decimal) and drawdown > 0 else None
        candidate["risk_scale"] = risk_scale
        candidate["dd5_proxy"] = pnl_30d * risk_scale if pnl_30d is not None and risk_scale is not None else None

        split = report_end - timedelta(days=selection_config.ab_final_days)
        if split > report_start:
            metrics_a = metrics[(result_id, report_start, split, METRICS_VERSION)]
            metrics_b = metrics[(result_id, split, report_end, METRICS_VERSION)]
            candidate.update(_ab_metrics_from_windows(metrics_a, metrics_b, report_start, report_end))
        else:
            candidate.update({
                "ab_pnl_change_30d_pct": None,
                "ab_return_b_pct": None,
                "ab_return_a_30d_pct": None,
                "ab_calendar_days_a": None,
                "ab_calendar_days_b": None,
                "ab_return_b_30d_pct": None,
                "ab_win_rate_b_pct": None,
                "ab_trade_rate_a_30d": None,
                "ab_trade_rate_b_30d": None,
                "ab_drawdown_b_pct": None,
            })

        consistency_windows = _consistency_windows(report_start, report_end)
        quarter_metrics = [
            metrics[(result_id, start, end, METRICS_VERSION)]
            for start, end in consistency_windows
        ]
        positive_quarters = _consistency_summary(quarter_metrics, consistency_windows)
        candidate["positive_quarter_count"] = positive_quarters[0]
        candidate["positive_quarter_available_count"] = positive_quarters[1]
        candidate["positive_quarter_status"] = positive_quarters[2]

        a = candidate["ab_return_a_30d_pct"]
        b = candidate["ab_return_b_30d_pct"]
        candidate["robust_pnl_30d_pct"] = min(a, b) if a is not None and b is not None else None
        candidate["ab_stability_ratio"] = min(a, b) / max(a, b) if a is not None and b is not None and a > 0 and b > 0 else None
        full_dd = candidate.get("max_drawdown_pct")
        b_dd = candidate.get("ab_drawdown_b_pct")
        candidate["worst_drawdown_pct"] = max(full_dd, b_dd) if isinstance(full_dd, Decimal) and isinstance(b_dd, Decimal) and full_dd >= 0 and b_dd >= 0 else None
        capital_proxy = candidate.get("capital_proxy")
        candidate["capital_efficiency"] = (
            candidate["dd5_proxy"] / capital_proxy
            if isinstance(candidate["dd5_proxy"], Decimal) and isinstance(capital_proxy, Decimal) and capital_proxy > 0
            else None
        )


def _frozen(value: Any) -> Any:
    if isinstance(value, CanonicalList):
        return CanonicalList(tuple(_frozen(item) for item in value.items), value.ordering)
    if isinstance(value, TypedValue):
        return TypedValue(value.type_tag, _frozen(value.value), value.unit_tag)
    if isinstance(value, dict):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_frozen(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(_frozen(item) for item in sorted(value, key=repr))
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    return value


def _selection_provenance(
    connection: duckdb.DuckDBPyConnection,
    requests: Sequence[SelectionRequest],
    strategy_ids: Sequence[int],
) -> dict[str, tuple[dict[str, Any], ...]]:
    pairs = tuple(dict.fromkeys((request.symbol, request.side) for request in requests))
    clauses = " or ".join("(symbol = ? and side = ?)" for _ in pairs)
    parameters = [part for pair in pairs for part in pair]
    runs = _fetch_records(connection, f"select * from selection_runs where {clauses}", parameters) if pairs else ()
    run_ids = tuple(str(row["selection_run_id"]) for row in runs)
    results = _records_for_ids(connection, "selection_results", "strategy_id", strategy_ids)
    if run_ids:
        results = tuple(
            row for row in results if str(row["selection_run_id"]) in run_ids
        )
    imports = _fetch_records(
        connection,
        f"select * from selection_review_imports where selection_run_id in ({_in_clause(run_ids)})",
        run_ids,
    ) if run_ids else ()
    import_ids = tuple(str(row["review_import_id"]) for row in imports)
    review_rows = _fetch_records(
        connection,
        f"select * from selection_review_rows where review_import_id in ({_in_clause(import_ids)})",
        import_ids,
    ) if import_ids else ()
    tags = _records_for_ids(connection, "strategy_tags", "strategy_id", strategy_ids)
    return {
        "selection_runs": runs,
        "selection_results": results,
        "selection_review_imports": imports,
        "selection_review_rows": review_rows,
        "strategy_tags": tags,
    }


def _current_review_facts(
    connection: duckdb.DuckDBPyConnection,
    requests: Sequence[SelectionRequest],
    *,
    allow_missing_pair: bool = False,
) -> tuple[
    dict[tuple[str, str, int, int], dict[str, Any]],
    set[tuple[str, str, int, int]],
    tuple[dict[str, str], ...],
]:
    """Return status and User Rank from the latest imported review per pair/side."""
    facts: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    selected: set[tuple[str, str, int, int]] = set()
    lineage: list[dict[str, str]] = []

    def latest_unique(query: str, parameters: Sequence[object], field: str) -> str:
        rows = connection.execute(query, list(parameters)).fetchall()
        if not rows:
            if field == "run" and allow_missing_pair:
                return ""
            raise PortfolioInputError(
                f"current selection {field} is ambiguous",
                code=SOURCE_SNAPSHOT_UNAVAILABLE,
            )
        if rows[0][1] is None or (len(rows) > 1 and rows[1][1] == rows[0][1]):
            raise PortfolioInputError(
                f"current selection {field} is ambiguous",
                code=SOURCE_SNAPSHOT_UNAVAILABLE,
            )
        value = rows[0][0]
        if not isinstance(value, str) or not value.strip():
            raise PortfolioInputError(
                f"invalid source {field}",
                code="INVALID_SOURCE_VALUE",
            )
        return value

    pairs = tuple(dict.fromkeys((request.symbol, request.side) for request in requests))
    for symbol, side in pairs:
        run_id = latest_unique(
            """select selection_run_id, created_at_utc from selection_runs
               where symbol = ? and side = ?
               order by created_at_utc desc""",
            [symbol, side],
            "run",
        )
        if not run_id:
            continue
        review_id = latest_unique(
            """select review_import_id, imported_at_utc from selection_review_imports
               where selection_run_id = ?
               order by imported_at_utc desc""",
            [run_id],
            "review",
        )
        selected_results: dict[int, int] = {}
        for strategy_id, result_id in connection.execute(
            "select strategy_id, result_id_at_selection from selection_results where selection_run_id = ?",
            [run_id],
        ).fetchall():
            strategy_key = _source_integer(strategy_id, "strategy_id")
            result_key = _source_integer(result_id, "result_id")
            if strategy_key in selected_results:
                raise PortfolioInputError(
                    "duplicate current selection result identity",
                    code=SOURCE_SNAPSHOT_UNAVAILABLE,
                )
            selected_results[strategy_key] = result_key
            selected.add((symbol, side, strategy_key, result_key))
        rows = connection.execute(
            "select strategy_id, user_status, user_rank from selection_review_rows where review_import_id = ?",
            [review_id],
        ).fetchall()
        review_statuses: dict[int, object] = {}
        for strategy_id, user_status, user_rank in rows:
            strategy_key = _source_integer(strategy_id, "strategy_id")
            if strategy_key in review_statuses:
                raise PortfolioInputError(
                    "duplicate current review row identity",
                    code=SOURCE_SNAPSHOT_UNAVAILABLE,
                )
            review_statuses[strategy_key] = (user_status, user_rank)
        facts.update(
            {
                (symbol, side, strategy_id, selected_results[strategy_id]): {
                    "user_status": user_status,
                    "user_rank": user_rank,
                    "selection_run_id": run_id,
                    "review_import_id": review_id,
                }
                for strategy_id, (user_status, user_rank) in review_statuses.items()
                if strategy_id in selected_results and isinstance(user_status, str)
            }
        )
        lineage.append({
            "symbol": symbol,
            "side": side,
            "selection_run_id": run_id,
            "review_import_id": review_id,
        })
    return facts, selected, tuple(lineage)


def _provenance_identity(name: str, row: Mapping[str, Any]) -> str:
    if name == "selection_runs":
        return str(row["selection_run_id"])
    if name == "selection_results":
        return f"{row['selection_run_id']}:{row['strategy_id']}"
    if name == "selection_review_imports":
        return str(row["review_import_id"])
    if name == "selection_review_rows":
        return f"{row['review_import_id']}:{row['strategy_id']}"
    if name == "strategy_tags":
        return f"{row['strategy_id']}:{row['tag']}"
    if name == "admission_lineage":
        return f"{row['symbol']}:{row['side']}"
    raise PortfolioInputError(f"unknown provenance relation: {name}", code="INVALID_PROVENANCE")


def _assemble(factory: Any) -> Any:
    try:
        return factory()
    except PortfolioInputError:
        raise
    except Exception as error:
        raise PortfolioInputError("failed to assemble portfolio snapshot", code="SNAPSHOT_ASSEMBLY_FAILED") from error


def read_performance_snapshot(
    database: str | Path,
    requests: Sequence[SelectionRequest | Mapping[str, object]] | SelectionRequest | Mapping[str, object],
    *,
    selection_config: SelectionConfig = SelectionConfig(),
    identity: SnapshotIdentity | None = None,
    optimizer_config_hash: str | None = None,
    algorithm_versions: Mapping[str, str] | None = None,
    seed: int | None = None,
    upstream_selection_window: Mapping[str, Any] | str | None = None,
    selection_window: Mapping[str, Any] | str | None = None,
    account_currency: str | None = None,
    read_at_utc: datetime | None = None,
) -> PerformanceInputSnapshot:
    """Read one immutable source snapshot without opening a write connection.

    Identity facts are mandatory; ``read_at_utc`` defaults only to the current
    aware UTC instant and is always frozen into the digest.
    """
    snapshot_identity = _coerce_identity(
        identity,
        optimizer_config_hash=optimizer_config_hash,
        algorithm_versions=algorithm_versions,
        seed=seed,
        upstream_selection_window=upstream_selection_window,
        selection_window=selection_window,
        account_currency=account_currency,
    )
    if read_at_utc is None:
        read_at = datetime.now(timezone.utc)
    elif read_at_utc.tzinfo is None or read_at_utc.utcoffset() is None:
        raise PortfolioInputError("read_at_utc must be timezone-aware", code="INVALID_READ_TIME")
    else:
        read_at = read_at_utc.astimezone(timezone.utc)
    request_values = (requests,) if isinstance(requests, (SelectionRequest, Mapping)) else requests
    parsed_values = tuple(_request(item) for item in request_values)
    requests_by_pair: dict[tuple[str, str], SelectionRequest] = {}
    for item in parsed_values:
        pair = (item.symbol, item.side)
        previous = requests_by_pair.setdefault(pair, item)
        if previous != item:
            raise PortfolioInputError("conflicting selection requests for symbol and side", code="INVALID_REQUEST")
    parsed = tuple(requests_by_pair.values())
    if not parsed:
        raise PortfolioInputError("at least one selection request is required", code="INVALID_REQUEST")
    path = Path(database).resolve()
    candidates_by_identity: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    strategy_ids: set[int] = set()
    result_ids: set[int] = set()
    source_rows: dict[int, _SourceSeries] = {}
    provenance: dict[str, tuple[dict[str, Any], ...]] = {}
    strategy_rows: tuple[dict[str, Any], ...] = ()
    result_rows: tuple[dict[str, Any], ...] = ()
    order_rows: tuple[dict[str, Any], ...] = ()
    plateau_rows: tuple[dict[str, Any], ...] = ()
    schema_markers: dict[str, str] = {}
    try:
        with duckdb.connect(str(path), read_only=True) as connection:
            connection.execute("begin transaction")
            require_performance_v2(connection)
            schema_markers = dict(connection.execute("select key, value from schema_info").fetchall())
            for request in parsed:
                # This is intentionally the only selection API call and is always cache-only.
                frame = load_selection_candidates(connection, request, selection_config, cache_only=True)
                for row in frame.to_dict(orient="records"):
                    candidate = dict(row)
                    try:
                        symbol = candidate["symbol"]
                        side = candidate["side"]
                        if not isinstance(symbol, str) or not isinstance(side, str):
                            raise ValueError("invalid candidate pair identity")
                        identity_key = (
                            symbol,
                            side,
                            _source_integer(candidate["strategy_id"], "strategy_id"),
                            _source_integer(candidate["result_id"], "result_id"),
                        )
                    except (KeyError, TypeError, ValueError) as error:
                        raise PortfolioInputError("invalid source candidate identity", code="INVALID_SOURCE_VALUE") from error
                    candidates_by_identity.setdefault(identity_key, candidate)
            review_facts, selected, admission_lineage = _current_review_facts(connection, parsed)
            candidates = []
            for candidate in candidates_by_identity.values():
                symbol = candidate["symbol"]
                side = candidate["side"]
                strategy_id = _source_integer(candidate["strategy_id"], "strategy_id")
                result_id = _source_integer(candidate["result_id"], "result_id")
                candidate_key = (symbol, side, strategy_id, result_id)
                strategy_key = (symbol, side, strategy_id)
                fact = review_facts.get(candidate_key)
                if fact is None:
                    prior_facts = [
                        value for key, value in review_facts.items() if key[:3] == strategy_key
                    ]
                    if any(value["user_status"] == "FINALIST" for value in prior_facts):
                        raise PortfolioInputError(
                            "current finalist result is stale",
                            code=SOURCE_SNAPSHOT_UNAVAILABLE,
                        )
                    if any(key[:3] == strategy_key for key in selected):
                        if prior_facts:
                            continue
                        raise PortfolioInputError(
                            "current selection status is unavailable",
                            code=SOURCE_SNAPSHOT_UNAVAILABLE,
                        )
                    continue
                if fact["user_status"] != "FINALIST":
                    continue
                try:
                    start = _utc(candidate["report_start_utc"])
                    end = _utc(candidate["report_end_utc"])
                    if end <= start:
                        raise ValueError("invalid report period")
                except (KeyError, TypeError, ValueError) as error:
                    raise PortfolioInputError(
                        "current finalist period is unavailable",
                        code=SOURCE_SNAPSHOT_UNAVAILABLE,
                    ) from error
                candidates.append(candidate)
            strategy_ids.update(int(row["strategy_id"]) for row in candidates)
            result_ids.update(int(row["result_id"]) for row in candidates)
            strategy_rows = _records_for_ids(connection, "strategies", "strategy_id", tuple(strategy_ids))
            result_rows = _records_for_ids(connection, "strategy_results", "result_id", tuple(result_ids))
            order_rows = _records_for_ids(connection, "strategy_orders", "strategy_id", tuple(strategy_ids))
            plateau_keys = tuple((row["analysis_run_id"], row["plateau_id"]) for row in order_rows)
            if plateau_keys:
                plateau_rows = _fetch_records(
                    connection,
                    "select * from analysis_plateaus where " + " or ".join("(analysis_run_id = ? and plateau_id = ?)" for _ in plateau_keys),
                    [part for key in plateau_keys for part in key],
                )
            action_rows = _records_for_ids(connection, "strategy_actions", "result_id", tuple(result_ids))
            equity_rows = _records_for_ids(connection, "strategy_equity", "result_id", tuple(result_ids))
            provenance = _selection_provenance(connection, parsed, tuple(strategy_ids))
            provenance["admission_lineage"] = admission_lineage
            for result in result_rows:
                result_id = int(result["result_id"])
                source_rows[result_id] = _SourceSeries(
                    result_id,
                    _utc(result["report_start_utc"]),
                    _utc(result["report_end_utc"]),
                    tuple(row for row in action_rows if int(row["result_id"]) == result_id),
                    tuple(row for row in equity_rows if int(row["result_id"]) == result_id),
                )
            connection.execute("commit")
    except PortfolioInputError:
        raise
    except Exception as error:
        raise PortfolioInputError(SOURCE_SNAPSHOT_UNAVAILABLE) from error

    try:
        metrics: dict[tuple[int, datetime, datetime, str], WindowMetrics] = {}
        for series in source_rows.values():
            actions, equity = _series_for_calculation(series)
            for start, end in _selection_windows(series.report_start_utc, series.report_end_utc, selection_config):
                key = (series.result_id, start, end, METRICS_VERSION)
                metrics[key] = _calculate(
                    series.result_id,
                    start,
                    end,
                    METRICS_VERSION,
                    series.report_start_utc,
                    series.report_end_utc,
                    actions,
                    equity,
                )
        _refresh_candidate_metrics(candidates, metrics, selection_config)

        candidate_records = tuple(
            _frozen(dict(row)) for row in sorted(candidates, key=lambda row: (int(row["strategy_id"]), int(row["result_id"])))
        )
        action_records = tuple(
            _frozen(dict(row))
            for series in sorted(source_rows.values(), key=lambda item: item.result_id)
            for row in sorted(series.actions, key=lambda item: (item["timestamp_utc"], item["action_index"]))
        )
        equity_records = tuple(
            _frozen(dict(row))
            for series in sorted(source_rows.values(), key=lambda item: item.result_id)
            for row in sorted(series.equity, key=lambda item: (item["timestamp_utc"], item["sample_index"]))
        )
        ordered_metrics = tuple(metrics[key] for key in sorted(metrics, key=lambda item: (item[0], item[1], item[2], item[3])))
    except PortfolioInputError:
        raise
    except Exception as error:
        raise PortfolioInputError("invalid source value during snapshot assembly", code="INVALID_SOURCE_VALUE") from error
    raw_payload = _assemble(lambda: {
        "identity": {
            "optimizer_config_hash": typed_value("sha256", snapshot_identity.optimizer_config_hash, unit="hex"),
            "algorithm_versions": _canonical_record(snapshot_identity.algorithm_versions, currency=snapshot_identity.account_currency),
            "seed": snapshot_identity.seed,
            "upstream_selection_window": _canonical_value(snapshot_identity.upstream_selection_window, "upstream_selection_window", currency=snapshot_identity.account_currency),
            "account_currency": snapshot_identity.account_currency,
            "selection_config": _canonical_record(asdict(selection_config), currency=snapshot_identity.account_currency),
        },
        "source": {
            "schema_version": schema_markers.get("schema_version"),
            "database_kind": schema_markers.get("database_kind"),
            "database_instance_id": schema_markers.get("database_instance_id"),
        },
        "requests": list_value([_request_payload(item, currency=snapshot_identity.account_currency) for item in parsed], ordering="identity"),
        "candidates": list_value(
            [_canonical_record(item, f"{item['strategy_id']}:{item['result_id']}", currency=snapshot_identity.account_currency) for item in candidate_records],
            ordering="identity",
        ),
        "results": list_value(
            [_canonical_record(row, str(row["result_id"]), currency=snapshot_identity.account_currency) for row in result_rows],
            ordering="identity",
        ),
        "strategies": list_value(
            [_canonical_record(row, str(row["strategy_id"]), currency=snapshot_identity.account_currency) for row in strategy_rows],
            ordering="identity",
        ),
        "strategy_orders": list_value(
            [_canonical_record(row, f"{row['strategy_id']}:{row['order_id']}", currency=snapshot_identity.account_currency) for row in order_rows],
            ordering="identity",
        ),
        "analysis_plateaus": list_value(
            [_canonical_record(row, f"{row['analysis_run_id']}:{row['plateau_id']}", currency=snapshot_identity.account_currency) for row in plateau_rows],
            ordering="identity",
        ),
        "actions": list_value(
            [
                {
                    "identity": typed_value("result", str(series.result_id), unit="1"),
                    "result_id": series.result_id,
                    "items": list_value(
                        [
                            _canonical_record({**dict(row), "source_ordinal": row["action_index"]}, f"{row['result_id']}:{row['action_index']}", currency=snapshot_identity.account_currency)
                            for row in sorted(series.actions, key=lambda item: (item["timestamp_utc"], item["action_index"]))
                        ],
                        ordering="timestamp_ordinal",
                    ),
                }
                for series in sorted(source_rows.values(), key=lambda item: item.result_id)
            ],
            ordering="identity",
        ),
        "equity": list_value(
            [
                {
                    "identity": typed_value("result", str(series.result_id), unit="1"),
                    "result_id": series.result_id,
                    "items": list_value(
                        [
                            _canonical_record({**dict(row), "source_ordinal": row["sample_index"]}, f"{row['result_id']}:{row['sample_index']}", currency=snapshot_identity.account_currency)
                            for row in sorted(series.equity, key=lambda item: (item["timestamp_utc"], item["sample_index"]))
                        ],
                        ordering="timestamp_ordinal",
                    ),
                }
                for series in sorted(source_rows.values(), key=lambda item: item.result_id)
            ],
            ordering="identity",
        ),
        "window_metrics": list_value(
            [_canonical_record(_metric_record(metric), f"{metric.result_id}:{metric.requested_start_utc.isoformat()}:{metric.requested_end_utc.isoformat()}", currency=snapshot_identity.account_currency) for metric in ordered_metrics],
            ordering="identity",
        ),
        "provenance": {
            name: list_value([_canonical_record(row, _provenance_identity(name, row), currency=snapshot_identity.account_currency) for row in rows], ordering="identity")
            for name, rows in provenance.items()
        },
        "read_at_utc": timestamp_value(read_at, precision="microseconds"),
    })
    try:
        canonical_payload = _frozen({key: _canonical_value(value, key, currency=snapshot_identity.account_currency) for key, value in raw_payload.items()})
        envelope = CanonicalEnvelope(
            schema_id="portfolio_optimizer_input_snapshot",
            schema_version=1,
            type_tag="performance_v4_snapshot",
            unit_tag="1",
            payload=canonical_payload,
        )
        digest = canonical_digest_v1(envelope)
        canonical_json = canonical_json_v1(envelope)
    except PortfolioInputError:
        raise
    except Exception as error:
        raise PortfolioInputError("failed to assemble canonical snapshot", code="SNAPSHOT_ASSEMBLY_FAILED") from error
    frozen_provenance = _frozen({name: tuple(_frozen(dict(row)) for row in rows) for name, rows in provenance.items()})
    return PerformanceInputSnapshot(
        path,
        schema_markers.get("schema_version", ""),
        schema_markers.get("database_kind", ""),
        schema_markers.get("database_instance_id", ""),
        parsed,
        candidate_records,
        action_records,
        equity_records,
        ordered_metrics,
        frozen_provenance,
        canonical_payload,
        digest,
        canonical_json,
        read_at,
    )


def _pair(value: object) -> tuple[str, str]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise PortfolioInputError("pair must contain symbol and side", code="INVALID_REQUEST")
    symbol, side = value
    if not isinstance(symbol, str) or not symbol.strip() or not isinstance(side, str):
        raise PortfolioInputError("pair must contain symbol and side", code="INVALID_REQUEST")
    side = side.strip().upper()
    if side not in {"LONG", "SHORT"}:
        raise PortfolioInputError("pair side is invalid", code="INVALID_REQUEST")
    return symbol.strip(), side


def _maximum(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PortfolioInputError("effective maximum must be a non-negative integer", code="INVALID_REQUEST")
    return value


def _user_rank(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PortfolioInputError("invalid current User Rank", code="INVALID_SOURCE_VALUE")
    return value


def _source_decimal(value: object, field: str, *, minimum: Decimal | None = None, exclusive_minimum: bool = False) -> Decimal:
    if value is None:
        raise PortfolioInputError(
            f"current finalist {field} is unavailable",
            code=SOURCE_SNAPSHOT_UNAVAILABLE,
        )
    if isinstance(value, bool):
        raise PortfolioInputError(f"invalid source {field}", code="INVALID_SOURCE_VALUE")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PortfolioInputError(f"invalid source {field}", code="INVALID_SOURCE_VALUE") from error
    if not result.is_finite():
        raise PortfolioInputError(f"invalid source {field}", code="INVALID_SOURCE_VALUE")
    if minimum is not None and (result <= minimum if exclusive_minimum else result < minimum):
        raise PortfolioInputError(f"invalid source {field}", code="INVALID_SOURCE_VALUE")
    return result


def _source_timestamp(value: object, field: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise PortfolioInputError(
                f"current finalist {field} is unavailable",
                code=SOURCE_SNAPSHOT_UNAVAILABLE,
            )
        return None
    try:
        return _utc(value).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError) as error:
        raise PortfolioInputError(f"invalid source {field}", code="INVALID_SOURCE_VALUE") from error


def _source_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortfolioInputError(f"invalid source {field}", code="INVALID_SOURCE_VALUE")
    return value


def _finalist_provenance(
    facts: Mapping[tuple[str, str, int, int], Mapping[str, Any]],
    strategy: Mapping[str, Any],
    result: Mapping[str, Any],
    key: tuple[str, str, int, int],
    runs: Mapping[str, Mapping[str, Any]],
    reviews: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    fact = facts[key]
    run_id = _source_text(fact.get("selection_run_id"), "selection_run_id")
    review_id = _source_text(fact.get("review_import_id"), "review_import_id")
    run = runs.get(run_id)
    review = reviews.get(review_id)
    if run is None or review is None:
        raise PortfolioInputError(
            "current finalist provenance is unavailable",
            code=SOURCE_SNAPSHOT_UNAVAILABLE,
        )
    return {
        "selection_run_id": run_id,
        "review_import_id": review_id,
        "database_instance_id": _source_text(run.get("database_instance_id"), "database_instance_id"),
        "selection_contract_version": _source_text(run.get("selection_contract_version"), "selection_contract_version"),
        "request_sha256": _source_text(run.get("request_sha256"), "request_sha256"),
        "config_sha256": _source_text(run.get("config_sha256"), "config_sha256"),
        "run_workbook_sha256": _source_text(run.get("workbook_sha256"), "workbook_sha256"),
        "run_created_at_utc": _source_timestamp(run.get("created_at_utc"), "selection_run.created_at_utc"),
        "review_workbook_sha256": _source_text(review.get("workbook_sha256"), "workbook_sha256"),
        "review_imported_at_utc": _source_timestamp(review.get("imported_at_utc"), "review_imported_at_utc"),
        "analysis_run_id": _source_text(strategy.get("analysis_run_id"), "analysis_run_id"),
        "candidate_identity": _source_text(strategy.get("candidate_identity"), "candidate_identity"),
        "strategy_created_at_utc": _source_timestamp(strategy.get("created_at_utc"), "strategy.created_at_utc"),
        "strategy_updated_at_utc": _source_timestamp(strategy.get("updated_at_utc"), "strategy.updated_at_utc"),
        "result_imported_at_utc": _source_timestamp(result.get("imported_at_utc"), "imported_at_utc"),
    }


def _finalist_order(row: Mapping[str, Any]) -> dict[str, Any]:
    order_id = _source_integer(row.get("order_id"), "order_id")
    open_ma_len = _source_integer(row.get("open_ma_len"), "open_ma_len")
    shift_bp = _source_integer(row.get("shift_bp"), "shift_bp")
    if order_id <= 0 or open_ma_len <= 0 or shift_bp < 0:
        raise PortfolioInputError("invalid source strategy order", code="INVALID_SOURCE_VALUE")
    return {
        "order_id": order_id,
        "open_ma_len": open_ma_len,
        "open_multiplier": _source_decimal(row.get("open_multiplier"), "open_multiplier", minimum=Decimal("0"), exclusive_minimum=True),
        "shift_bp": shift_bp,
        "lot_x": _source_decimal(row.get("lot_x"), "lot_x", minimum=Decimal("0"), exclusive_minimum=True),
    }


def apply_finalist_cutoff(
    candidates: Sequence[Mapping[str, Any]],
    *,
    selected_pairs: Sequence[tuple[str, str]] | set[tuple[str, str]],
    maximums: Mapping[tuple[str, str], int],
) -> tuple[dict[str, Any], ...]:
    """Annotate current FINALIST rows with the pair/side cutoff decision."""
    limits = {_pair(pair): _maximum(value) for pair, value in maximums.items()}
    selected = {_pair(pair) for pair in selected_pairs}
    rows: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[int]] = {}
    for candidate in candidates:
        if candidate.get("user_status") != "FINALIST":
            continue
        row = dict(candidate)
        pair = _pair((row.get("symbol"), row.get("side")))
        row["symbol"], row["side"] = pair
        row["user_rank"] = _user_rank(row.get("user_rank"))
        row["effective_maximum"] = limits.get(pair, 0)
        rows.append(row)
        groups.setdefault(pair, []).append(len(rows) - 1)

    for pair, indexes in groups.items():
        limit = limits.get(pair, 0)
        if pair not in selected:
            status, reason = "EXCLUDED", PAIR_UNSELECTED
        elif limit == 0:
            status, reason = "EXCLUDED", DIRECTION_DISABLED
        elif len(indexes) <= limit:
            status, reason = "SELECTED", "WITHIN_MAXIMUM"
            for index in indexes:
                rows[index]["selection_status"] = status
                rows[index]["selection_reason"] = reason
            continue
        else:
            ranks = [rows[index]["user_rank"] for index in indexes]
            if any(rank is None for rank in ranks):
                status, reason = "EXCLUDED", USER_RANK_MISSING
                for index in indexes:
                    rows[index]["selection_status"] = status
                    rows[index]["selection_reason"] = reason
                continue
            if len(set(ranks)) != len(ranks):
                status, reason = "EXCLUDED", USER_RANK_DUPLICATE
                for index in indexes:
                    rows[index]["selection_status"] = status
                    rows[index]["selection_reason"] = reason
                continue
            winners = {index for _, index in sorted(zip(ranks, indexes))[:limit]}
            for index in indexes:
                rows[index]["selection_status"] = "SELECTED" if index in winners else "EXCLUDED"
                rows[index]["selection_reason"] = "USER_RANK_SELECTED" if index in winners else USER_RANK_CUTOFF
            continue
        for index in indexes:
            rows[index]["selection_status"] = status
            rows[index]["selection_reason"] = reason
    return tuple(rows)


def read_current_finalists(
    database: str | Path,
    pairs: Sequence[tuple[str, str]],
    include_series: bool = True,
) -> tuple[dict[str, Any], ...]:
    """Read current exact User Status=FINALIST facts without source writes.

    ``include_series`` is kept opt-in for callers that build an execution
    snapshot.  Metadata-only consumers such as Panel readiness can avoid
    touching the potentially very large action/equity tables.
    """
    pair_values = tuple(dict.fromkeys(_pair(pair) for pair in pairs))
    if not pair_values:
        return ()
    requests = tuple(
        parse_selection_request({"symbol": symbol, "side": side, "stages": []})
        for symbol, side in pair_values
    )
    path = Path(database).resolve()
    try:
        with duckdb.connect(str(path), read_only=True) as connection:
            connection.execute("begin transaction")
            require_performance_v2(connection)
            facts, selected, _ = _current_review_facts(connection, requests, allow_missing_pair=True)
            scoped_selected = {key for key in selected if key[:2] in pair_values}
            missing_review = sorted(key for key in scoped_selected if key not in facts)
            if missing_review:
                raise PortfolioInputError(
                    "current selection review is incomplete",
                    code=SOURCE_SNAPSHOT_UNAVAILABLE,
                )
            finalist_keys = [
                key for key, fact in facts.items()
                if key[:2] in pair_values and fact.get("user_status") == "FINALIST"
            ]
            strategy_ids = tuple(dict.fromkeys(key[2] for key in finalist_keys))
            result_ids = tuple(dict.fromkeys(key[3] for key in finalist_keys))
            strategy_rows = _records_for_ids(connection, "strategies", "strategy_id", strategy_ids)
            by_strategy = {int(row["strategy_id"]): row for row in strategy_rows}
            result_rows = _records_for_ids(connection, "strategy_results", "result_id", result_ids)
            by_result = {int(row["result_id"]): row for row in result_rows}
            order_rows = _records_for_ids(connection, "strategy_orders", "strategy_id", strategy_ids)
            actions_by_result: dict[int, tuple[Mapping[str, Any], ...]] = {}
            equities_by_result: dict[int, tuple[Mapping[str, Any], ...]] = {}
            if include_series:
                # Bulk reads keep the optimizer on the read-only snapshot path
                # and avoid one query per finalist when constructing proxy
                # paths.  Freeze only the fields consumed by the optimizer;
                # full DB rows can contain large or irrelevant payloads.
                action_rows = _records_for_ids(connection, "strategy_actions", "result_id", result_ids)
                equity_rows = _records_for_ids(connection, "strategy_equity", "result_id", result_ids)

                def _public_series_row(row: Mapping[str, Any], fields: tuple[str, ...]) -> Mapping[str, Any]:
                    value = {field: row[field] for field in fields if field in row}
                    if value.get("timestamp_utc") is not None:
                        value["timestamp_utc"] = _utc(value["timestamp_utc"]).isoformat().replace("+00:00", "Z")
                    return value

                for result_id in result_ids:
                    actions_by_result[int(result_id)] = tuple(
                        _frozen(_public_series_row(row, ("result_id", "action_index", "timestamp_utc")))
                        for row in sorted(
                            (item for item in action_rows if int(item.get("result_id")) == int(result_id)),
                            key=lambda item: (item.get("timestamp_utc"), item.get("action_index")),
                        )
                    )
                    equities_by_result[int(result_id)] = tuple(
                        _frozen(_public_series_row(row, ("result_id", "sample_index", "timestamp_utc", "equity")))
                        for row in sorted(
                            (item for item in equity_rows if int(item.get("result_id")) == int(result_id)),
                            key=lambda item: (item.get("timestamp_utc"), item.get("sample_index")),
                        )
                    )
            orders_by_strategy: dict[int, list[dict[str, Any]]] = {}
            for order in order_rows:
                strategy_id = _source_integer(order.get("strategy_id"), "strategy_id")
                orders_by_strategy.setdefault(strategy_id, []).append(order)
            run_ids = tuple(dict.fromkeys(
                _source_text(facts[key].get("selection_run_id"), "selection_run_id")
                for key in finalist_keys
            ))
            review_ids = tuple(dict.fromkeys(
                _source_text(facts[key].get("review_import_id"), "review_import_id")
                for key in finalist_keys
            ))
            run_rows = _records_for_ids(connection, "selection_runs", "selection_run_id", run_ids)
            review_rows = _records_for_ids(connection, "selection_review_imports", "review_import_id", review_ids)
            runs = {str(row["selection_run_id"]): row for row in run_rows}
            reviews = {str(row["review_import_id"]): row for row in review_rows}
            result: list[dict[str, Any]] = []
            pair_order = {pair: index for index, pair in enumerate(pair_values)}
            for key in finalist_keys:
                symbol, side, strategy_id, result_id = key
                source = by_strategy.get(strategy_id)
                if source is None or source.get("current_result_id") != result_id:
                    raise PortfolioInputError(
                        "current finalist result is stale",
                        code=SOURCE_SNAPSHOT_UNAVAILABLE,
                    )
                source_result = by_result.get(result_id)
                if source_result is None or source_result.get("strategy_id") != strategy_id:
                    raise PortfolioInputError(
                        "current finalist result is unavailable",
                        code=SOURCE_SNAPSHOT_UNAVAILABLE,
                    )
                if source.get("lifecycle_status") != "ACTIVE" or (source.get("symbol"), source.get("side")) != (symbol, side):
                    raise PortfolioInputError(
                        "current finalist source identity is unavailable",
                        code=SOURCE_SNAPSHOT_UNAVAILABLE,
                    )
                orders = orders_by_strategy.get(strategy_id, [])
                expected_order_count = _source_integer(source.get("order_count"), "order_count")
                if expected_order_count <= 0 or len(orders) != expected_order_count:
                    raise PortfolioInputError(
                        "current finalist strategy geometry is unavailable",
                        code=SOURCE_SNAPSHOT_UNAVAILABLE,
                    )
                ordered_orders = tuple(
                    _finalist_order(order)
                    for order in sorted(orders, key=lambda item: _source_integer(item.get("order_id"), "order_id"))
                )
                source_result = dict(source_result)
                provenance = _finalist_provenance(facts, source, source_result, key, runs, reviews)
                total_pnl = _source_decimal(source_result.get("total_pnl"), "total_pnl")
                total_fees = _source_decimal(source_result.get("total_fees"), "total_fees", minimum=Decimal("0"))
                max_drawdown = _source_decimal(source_result.get("max_drawdown"), "max_drawdown", minimum=Decimal("0"))
                max_drawdown_pct = _source_decimal(source_result.get("max_drawdown_pct"), "max_drawdown_pct", minimum=Decimal("0"))
                initial_balance = _source_decimal(source_result.get("initial_balance"), "initial_balance", minimum=Decimal("0.00000001"))
                report_start = _source_timestamp(source_result.get("report_start_utc"), "report_start_utc")
                report_end = _source_timestamp(source_result.get("report_end_utc"), "report_end_utc")
                if report_end <= report_start:
                    raise PortfolioInputError(
                        "current finalist report period is invalid",
                        code="INVALID_SOURCE_VALUE",
                    )
                recovery_factor: Decimal | dict[str, str]
                if max_drawdown > 0:
                    with localcontext() as context:
                        context.prec = max(
                            32,
                            len(total_pnl.as_tuple().digits) + len(max_drawdown.as_tuple().digits) + 8,
                        )
                        recovery_factor = total_pnl / max_drawdown
                    if not recovery_factor.is_finite():
                        raise PortfolioInputError(
                            "invalid source recovery_factor",
                            code="INVALID_SOURCE_VALUE",
                        )
                else:
                    recovery_factor = {
                        "status": "UNKNOWN",
                        "reason": "MAX_DRAWDOWN_NOT_POSITIVE",
                    }
                result_row = {
                    "strategy_id": strategy_id,
                    "result_id": result_id,
                    "strategy_name": _source_text(source.get("strategy_name"), "strategy_name"),
                    "symbol": symbol,
                    "side": side,
                    "user_status": "FINALIST",
                    "user_rank": _user_rank(facts[key].get("user_rank")),
                    "timeframe": _source_text(source.get("timeframe"), "timeframe"),
                    "report_start_utc": report_start,
                    "report_end_utc": report_end,
                    "effective_start_utc": _source_timestamp(source_result.get("effective_start_utc"), "effective_start_utc", required=False),
                    "effective_end_utc": _source_timestamp(source_result.get("effective_end_utc"), "effective_end_utc", required=False),
                    "imported_at_utc": _source_timestamp(source_result.get("imported_at_utc"), "imported_at_utc"),
                    "reported_start_utc": _source_timestamp(source_result.get("reported_start_utc"), "reported_start_utc", required=False),
                    "reported_end_utc": _source_timestamp(source_result.get("reported_end_utc"), "reported_end_utc", required=False),
                    "total_pnl": total_pnl,
                    "total_pnl_basis": "PERSISTED_NET_PNL",
                    "initial_balance": initial_balance,
                    "result_initial_balance": initial_balance,
                    "source_initial_balance": initial_balance,
                    "total_fees": total_fees,
                    "max_drawdown": max_drawdown,
                    "max_drawdown_pct": max_drawdown_pct,
                    "recovery_factor": recovery_factor,
                    "strategy_orders": ordered_orders,
                    "selection_run_id": provenance["selection_run_id"],
                    "review_import_id": provenance["review_import_id"],
                    "source_provenance": provenance,
                }
                if include_series:
                    result_row.update({
                        "actions": actions_by_result.get(result_id, ()),
                        "equity": equities_by_result.get(result_id, ()),
                        "action_series": actions_by_result.get(result_id, ()),
                        "equity_series": equities_by_result.get(result_id, ()),
                    })
                result.append(result_row)
            connection.execute("commit")
    except PortfolioInputError:
        raise
    except Exception as error:
        raise PortfolioInputError(SOURCE_SNAPSHOT_UNAVAILABLE) from error
    return tuple(sorted(result, key=lambda row: (pair_order[(row["symbol"], row["side"])], row["strategy_id"], row["result_id"])))


def read_and_select_finalists(
    database: str | Path,
    *,
    selected_pairs: Sequence[tuple[str, str]] | set[tuple[str, str]],
    maximums: Mapping[tuple[str, str], int],
) -> tuple[dict[str, Any], ...]:
    """Read current FINALIST facts and apply the package-owned cutoff."""
    pairs = tuple(dict.fromkeys((*maximums, *selected_pairs)))
    return apply_finalist_cutoff(
        read_current_finalists(database, pairs),
        selected_pairs=selected_pairs,
        maximums=maximums,
    )


def tick_replay_availability(
    binary_identity: str | None,
    tick_identity: str | None,
    artifacts: Sequence[str | Path],
) -> ReplayAvailability:
    """Exact replay requires identities and every referenced artifact on disk."""
    if not binary_identity or not tick_identity:
        return ReplayAvailability(TICK_REPLAY_UNAVAILABLE, "TICK_OR_BINARY_IDENTITY_MISSING")
    if not artifacts or any(not Path(item).is_file() for item in artifacts):
        return ReplayAvailability(TICK_REPLAY_UNAVAILABLE, "TICK_OR_BINARY_ARTIFACT_MISSING")
    return ReplayAvailability(TICK_REPLAY_AVAILABLE)


def fresh_decision_campaign(execution_campaign_id: str, reference_facts: Mapping[str, Any]) -> DecisionCampaign:
    if not isinstance(execution_campaign_id, str) or not execution_campaign_id:
        raise ValueError("execution_campaign_id must be non-empty")
    payload = {"execution_campaign_id": execution_campaign_id, "reference_facts": _canonical_value(reference_facts, "reference_facts")}
    digest = canonical_digest_v1(
        CanonicalEnvelope(
            schema_id="portfolio_decision_campaign",
            schema_version=1,
            type_tag="decision_campaign",
            unit_tag="1",
            payload=payload,
        )
    )
    return DecisionCampaign(f"decision-{digest}", execution_campaign_id, digest)


new_decision_campaign = fresh_decision_campaign


@dataclass(frozen=True, slots=True)
class CommonPretestPeriodResult:
    status: str
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    daily_paths: Mapping[str, tuple[Mapping[str, Any], ...]] = MappingProxyType({})
    coverage_pct: Mapping[str, Decimal] = MappingProxyType({})
    exclusions: tuple[Mapping[str, Any], ...] = ()
    evidence: Mapping[str, Any] = MappingProxyType({})
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.status == "PASS"

    @property
    def start(self) -> datetime | None:
        return self.start_utc

    @property
    def end(self) -> datetime | None:
        return self.end_utc


def _day_floor(value: datetime) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _day_ceil(value: datetime) -> datetime:
    floor = _day_floor(value)
    return floor if value == floor else floor + timedelta(days=1)


def _period_row_key(row: Mapping[str, Any]) -> str:
    return (
        f"{str(row.get('symbol', '')).upper()}:{str(row.get('side', '')).upper()}"
        f":{row.get('strategy_id', '')}:{row.get('result_id', '')}"
    )


def _period_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    def identifier(value: Any) -> tuple[int, Any]:
        if isinstance(value, bool):
            return 1, str(value)
        if isinstance(value, int):
            return 0, value
        return 1, str(value)

    return (
        str(row.get("symbol", "")).upper(),
        str(row.get("side", "")).upper(),
        identifier(row.get("strategy_id", "")),
        identifier(row.get("result_id", "")),
    )


def _period_interval(row: Mapping[str, Any]) -> tuple[datetime, datetime] | None:
    effective_start = row.get("effective_start_utc")
    effective_end = row.get("effective_end_utc")
    # An effective interval is usable only as a complete, valid pair.  A
    # malformed or partial immutable range falls back to the persisted report
    # range; never mix endpoints from the two ranges.
    if effective_start is not None and effective_end is not None:
        try:
            start_value, end_value = _utc(effective_start), _utc(effective_end)
        except (PortfolioInputError, TypeError, ValueError):
            start_value = end_value = None
        if start_value is not None and end_value is not None and end_value > start_value:
            return start_value, end_value
    try:
        start_value, end_value = _utc(row.get("report_start_utc")), _utc(row.get("report_end_utc"))
    except (PortfolioInputError, TypeError, ValueError):
        return None
    return (start_value, end_value) if end_value > start_value else None


def _daily_path_for_row(
    row: Mapping[str, Any], start: datetime, end: datetime, max_gap_days: int
) -> tuple[tuple[Mapping[str, Any], ...], Decimal, str | None, Mapping[str, Any]]:
    """Build a sparse CURRENT_RESULT step path and diagnostic evidence.

    ``max_gap_days`` remains in the signature for compatibility.  Current
    result observations are sparse evidence, so coverage and gap are reported
    but do not gate retention.
    """
    raw = row.get("equity", row.get("equity_series", row.get("equity_path", ())))
    samples: list[tuple[datetime, Decimal]] = []
    for item in raw if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else ():
        if isinstance(item, Mapping):
            timestamp, value = item.get("timestamp_utc", item.get("timestamp")), item.get("equity", item.get("value"))
        elif isinstance(item, Sequence) and len(item) == 2:
            timestamp, value = item
        else:
            continue
        try:
            samples.append((_utc(timestamp), _to_decimal(value)))
        except (PortfolioInputError, TypeError, ValueError):
            return (), Decimal(0), "INVALID_EQUITY_SAMPLE", MappingProxyType({})
    samples.sort(key=lambda item: item[0])
    if any(left[0] >= right[0] for left, right in zip(samples, samples[1:])):
        return (), Decimal(0), "INVALID_EQUITY_SAMPLE", MappingProxyType({
            "path_policy": "SPARSE_OBSERVATION_FORWARD_FILL",
        })
    actions = row.get("actions", row.get("action_series", ()))
    action_points: list[datetime] = []
    for action in actions if isinstance(actions, Sequence) and not isinstance(actions, (str, bytes)) else ():
        timestamp = action.get("timestamp_utc", action.get("timestamp")) if isinstance(action, Mapping) else None
        try:
            action_points.append(_utc(timestamp))
        except (PortfolioInputError, TypeError, ValueError):
            return (), Decimal(0), "INVALID_ACTION_SAMPLE", MappingProxyType({})
    calendar_days = (end - start).days
    days = tuple(start + timedelta(days=index) for index in range(calendar_days + 1))
    if calendar_days <= 0:
        return (), Decimal(0), "COMMON_PERIOD_TOO_SHORT", MappingProxyType({"calendar_days": 0})

    # Last real sample per UTC day; samples outside the interval remain
    # eligible only for the explicit start seed.
    in_window = [(timestamp, value) for timestamp, value in samples if start <= timestamp < end]
    real_days = {_day_floor(timestamp) for timestamp, _value in in_window}
    prior = next(((timestamp, value) for timestamp, value in reversed(samples) if timestamp <= start), None)
    actions_in_window = [timestamp for timestamp in action_points if start <= timestamp < end]
    actions_at_or_before_start = any(timestamp <= start for timestamp in action_points)
    initial = row.get("initial_balance", row.get("source_initial_balance"))
    seed_source = "NONE"
    seeded = False
    seed_value: Decimal | None = None
    # A future observation on the common-start day is not a start seed.  Keep
    # it in the observed-day diagnostics, but reserve the first endpoint for a
    # real sample at the boundary or an explicit seed.
    if prior is not None:
        seed_source = "EQUITY_OBSERVATION"
        seeded = prior[0] != start
    elif actions_at_or_before_start:
        return (), Decimal(0), "INVALID_START_SEED", MappingProxyType({
            "path_policy": "SPARSE_OBSERVATION_FORWARD_FILL",
            "calendar_days": calendar_days, "in_window_observation_count": len(in_window),
            "in_window_action_count": len(actions_in_window), "seed_source": "NONE", "seeded": False,
        })
    elif initial is not None:
        try:
            seed_value = _to_decimal(initial)
        except (PortfolioInputError, TypeError, ValueError):
            return (), Decimal(0), "INVALID_INITIAL_BALANCE", MappingProxyType({})
        if seed_value <= 0:
            return (), Decimal(0), "INVALID_INITIAL_BALANCE", MappingProxyType({
                "path_policy": "SPARSE_OBSERVATION_FORWARD_FILL",
                "calendar_days": calendar_days,
                "in_window_observation_count": len(in_window),
                "in_window_action_count": len(actions_in_window),
                "seed_source": "NONE",
                "seeded": False,
            })
        seed_source = "INITIAL_BALANCE"
        seeded = True
    elif not in_window and not actions_in_window:
        return (), Decimal(0), "DAILY_PATH_REQUIRES_SEED", MappingProxyType({
            "path_policy": "SPARSE_OBSERVATION_FORWARD_FILL",
            "calendar_days": calendar_days, "in_window_observation_count": 0,
            "in_window_action_count": 0, "seed_source": "NONE", "seeded": False,
        })
    elif not in_window and actions_in_window:
        return (), Decimal(0), "OBSERVATION_LOSS_SUSPECTED", MappingProxyType({
            "path_policy": "SPARSE_OBSERVATION_FORWARD_FILL",
            "calendar_days": calendar_days, "in_window_observation_count": 0,
            "in_window_action_count": len(actions_in_window), "seed_source": seed_source, "seeded": seeded,
        })
    elif not prior and initial is None:
        return (), Decimal(0), "DAILY_PATH_REQUIRES_SEED", MappingProxyType({
            "path_policy": "SPARSE_OBSERVATION_FORWARD_FILL",
            "calendar_days": calendar_days, "in_window_observation_count": len(in_window),
            "in_window_action_count": len(actions_in_window), "seed_source": "NONE", "seeded": False,
        })

    if not in_window and actions_in_window:
        return (), Decimal(0), "OBSERVATION_LOSS_SUSPECTED", MappingProxyType({
            "path_policy": "SPARSE_OBSERVATION_FORWARD_FILL",
            "calendar_days": calendar_days, "in_window_observation_count": 0,
            "in_window_action_count": len(actions_in_window), "seed_source": seed_source, "seeded": seeded,
        })

    # An observation exactly at the period edge is real; a prior observation
    # is a seed and is excluded from observed-day coverage only when it is
    # outside the selected window.
    if prior is not None and prior[0] == start:
        seed_source = "EQUITY_OBSERVATION"
        seeded = False
    observed_timestamps = [timestamp for timestamp, _value in in_window]
    boundaries = [start, *observed_timestamps, end]
    maximum_gap = max((right - left for left, right in zip(boundaries, boundaries[1:])), default=timedelta(0))
    observed_ratio = (Decimal(len(real_days)) * Decimal(100) / Decimal(calendar_days)).quantize(Decimal("0.00000001"))
    maximum_gap_days = (Decimal(maximum_gap.total_seconds()) / Decimal(86400)).quantize(Decimal("0.00000001"))
    reason = "ZERO_ACTIVITY_IN_WINDOW" if not in_window and not actions_in_window else None
    evidence = MappingProxyType({
        "sparse_observation_tag": "SPARSE_OBSERVATION_FORWARD_FILL",
        "path_policy": "SPARSE_OBSERVATION_FORWARD_FILL",
        "pretest_source_mode": "CURRENT_RESULT",
        "calendar_days": calendar_days,
        "in_window_observation_count": len(in_window),
        "in_window_action_count": len(actions_in_window),
        "observed_day_count": len(real_days),
        "observed_day_ratio": observed_ratio,
        "observed_sample_day_count": len(real_days),
        "observed_sample_day_ratio": observed_ratio,
        "observed_distinct_date_ratio": observed_ratio,
        "max_observation_gap_days": maximum_gap_days,
        "maximum_observation_gap_days": maximum_gap_days,
        "max_observation_gap_including_boundaries_days": maximum_gap_days,
        "max_real_observation_gap_days": maximum_gap_days,
        "max_real_observation_gap_including_boundaries_days": maximum_gap_days,
        "max_real_observation_gap_days_including_boundaries": maximum_gap_days,
        "seed_source": seed_source,
        "start_seed_source": "PRIOR_OBSERVATION" if prior is not None else "INITIAL_BALANCE",
        "initial_balance_seed_used": prior is None,
        "seeded": seeded,
        "dd_bias": "DOWNWARD_BIASED_BETWEEN_OBSERVATIONS",
        "linear_scaling_assumption": "UNVERIFIED",
        **({"reason": reason} if reason else {}),
    })
    result: list[Mapping[str, Any]] = []
    last_value: Decimal | None = prior[1] if prior is not None else seed_value
    latest_sample: tuple[datetime, Decimal] | None = prior
    sample_index = 0
    for day in days:
        # Resolve each endpoint from observations at or before that endpoint.
        # A noon observation can therefore affect the following UTC endpoint,
        # never the midnight at which it was first observed.
        while sample_index < len(in_window) and in_window[sample_index][0] <= day:
            latest_sample = in_window[sample_index]
            sample_index += 1
        if latest_sample is not None and latest_sample[0] <= day:
            is_real = latest_sample[0] == day
            last_value = latest_sample[1]
            result.append({"timestamp_utc": day, "equity": last_value, "real": is_real, "filled": not is_real and not (day == start and seeded), "seed": day == start and seeded and not is_real})
        elif last_value is not None:
            result.append({"timestamp_utc": day, "equity": last_value, "real": False, "filled": day != start or not seeded, "seed": day == start and seeded})
        else:
            return tuple(result), observed_ratio, "DAILY_PATH_REQUIRES_SEED", evidence
    return tuple(result), observed_ratio, reason, evidence


def _evaluate_period(rows: Sequence[Mapping[str, Any]], minimum_days: int, minimum_coverage: int, max_gap_days: int) -> CommonPretestPeriodResult:
    ordered = tuple(sorted((row for row in rows if isinstance(row, Mapping)), key=_period_sort_key))
    intervals_by_key = {_period_row_key(row): _period_interval(row) for row in ordered}
    if not ordered:
        return CommonPretestPeriodResult("UNAVAILABLE", reason="COMMON_PRETEST_PERIOD_UNAVAILABLE")
    valid_intervals = [item for item in intervals_by_key.values() if item is not None]
    invalid_keys = tuple(key for key, item in intervals_by_key.items() if item is None)
    if not valid_intervals:
        violations = tuple({"key": key, "reason": "INVALID_REPORT_INTERVAL"} for key in invalid_keys)
        return CommonPretestPeriodResult(
            "UNAVAILABLE",
            exclusions=violations,
            evidence=MappingProxyType({
                "period_basis": "CURRENT_RESULT_SPARSE_OBSERVATION",
                "path_policy": "SPARSE_OBSERVATION_FORWARD_FILL",
                "violations": violations,
                "retained_identities": (),
                "excluded_identities": (),
            }),
            reason="COMMON_PRETEST_PERIOD_UNAVAILABLE",
        )
    start_edge = max(item[0] for item in valid_intervals)
    end_edge = min(item[1] for item in valid_intervals)
    start = _day_ceil(start_edge)
    end = _day_floor(end_edge)
    calendar_days = max(0, (end - start).days)
    start_binders = tuple(_period_row_key(row) for row in ordered if intervals_by_key[_period_row_key(row)] is not None and intervals_by_key[_period_row_key(row)][0] == start_edge)
    end_binders = tuple(_period_row_key(row) for row in ordered if intervals_by_key[_period_row_key(row)] is not None and intervals_by_key[_period_row_key(row)][1] == end_edge)
    paths: dict[str, tuple[Mapping[str, Any], ...]] = {}
    coverage: dict[str, Decimal] = {}
    row_evidence: dict[str, Mapping[str, Any]] = {}
    failures: list[Mapping[str, Any]] = [{"key": key, "reason": "INVALID_REPORT_INTERVAL"} for key in invalid_keys]
    for row in ordered:
        key = _period_row_key(row)
        if intervals_by_key[key] is None or end <= start:
            continue
        path, ratio, reason, evidence = _daily_path_for_row(row, start, end, max_gap_days)
        paths[key] = path
        coverage[key] = ratio
        row_evidence[key] = evidence
        if reason and reason != "ZERO_ACTIVITY_IN_WINDOW":
            failures.append({"key": key, "reason": reason})
    violations = [*failures]
    if calendar_days < minimum_days:
        violations.append({"reason": "COMMON_PERIOD_TOO_SHORT"})
    evidence = MappingProxyType({
        "period_basis": "CURRENT_RESULT_SPARSE_OBSERVATION",
        "path_policy": "SPARSE_OBSERVATION_FORWARD_FILL",
        "common_start_utc": start,
        "common_end_utc": end,
        "calendar_days": calendar_days,
        "minimum_common_days": minimum_days,
        "minimum_daily_coverage_pct": minimum_coverage,
        "maximum_forward_fill_gap_days": max_gap_days,
        "coverage_gate": "NON_BINDING_DIAGNOSTIC",
        "forward_fill_gap_gate": "NON_BINDING_DIAGNOSTIC",
        "start_binders": start_binders,
        "end_binders": end_binders,
        "violations": tuple(violations),
        "rows": MappingProxyType(row_evidence),
        "in_window_observation_counts": MappingProxyType({key: item.get("in_window_observation_count", 0) for key, item in row_evidence.items()}),
        "in_window_action_counts": MappingProxyType({key: item.get("in_window_action_count", 0) for key, item in row_evidence.items()}),
        "observed_day_ratios": MappingProxyType({key: item.get("observed_day_ratio", Decimal(0)) for key, item in row_evidence.items()}),
        "observed_distinct_date_ratios": MappingProxyType({key: item.get("observed_distinct_date_ratio", Decimal(0)) for key, item in row_evidence.items()}),
        "max_observation_gaps_days": MappingProxyType({key: item.get("max_observation_gap_days", Decimal(0)) for key, item in row_evidence.items()}),
        "max_real_observation_gaps_days": MappingProxyType({key: item.get("max_real_observation_gap_days", Decimal(0)) for key, item in row_evidence.items()}),
        "max_real_observation_gaps_including_boundaries_days": MappingProxyType({key: item.get("max_real_observation_gap_including_boundaries_days", Decimal(0)) for key, item in row_evidence.items()}),
        "seed_sources": MappingProxyType({key: item.get("seed_source", "NONE") for key, item in row_evidence.items()}),
        "seed_flags": MappingProxyType({key: bool(item.get("seeded", False)) for key, item in row_evidence.items()}),
        "dd_biases": MappingProxyType({key: item.get("dd_bias") for key, item in row_evidence.items()}),
        "retained_identities": tuple(_period_row_key(row) for row in ordered),
        "excluded_identities": (),
    })
    status = "PASS" if not violations else "UNAVAILABLE"
    return CommonPretestPeriodResult(status, start, end, MappingProxyType(paths), MappingProxyType(coverage), tuple(failures), evidence, None if status == "PASS" else "COMMON_PRETEST_PERIOD_UNAVAILABLE")


def resolve_common_pretest_period(
    rows: Sequence[Mapping[str, Any]],
    *,
    minimum_common_days: int = 14,
    minimum_daily_coverage_pct: int = 90,
    maximum_forward_fill_gap_days: int = 3,
) -> CommonPretestPeriodResult:
    """Find a deterministic sparse CURRENT_RESULT common UTC daily period."""
    if not isinstance(minimum_common_days, int) or minimum_common_days <= 0:
        raise ValueError("minimum_common_days must be positive")
    if not isinstance(minimum_daily_coverage_pct, int) or not 1 <= minimum_daily_coverage_pct <= 100:
        raise ValueError("minimum_daily_coverage_pct must be between 1 and 100")
    if not isinstance(maximum_forward_fill_gap_days, int) or maximum_forward_fill_gap_days < 0:
        raise ValueError("maximum_forward_fill_gap_days must be non-negative")
    active = tuple(dict(row) for row in rows if isinstance(row, Mapping))
    removed: list[Mapping[str, Any]] = []

    def score(result: CommonPretestPeriodResult) -> tuple[int, int, int, int, int]:
        evidence = result.evidence
        days = int(evidence.get("calendar_days", (result.end_utc - result.start_utc).days if result.start_utc and result.end_utc else 0))
        violations = len(tuple(evidence.get("violations", ())))
        return (
            1 if result.available else 0,
            -violations,
            days,
            -len(tuple(evidence.get("start_binders", ()))),
            -len(tuple(evidence.get("end_binders", ()))),
        )

    def finish(
        result: CommonPretestPeriodResult,
        excluded: Sequence[Mapping[str, Any]],
        retained: Sequence[Mapping[str, Any]] | None = None,
        ledger: Sequence[Mapping[str, Any]] | None = None,
    ) -> CommonPretestPeriodResult:
        evidence = dict(result.evidence)
        excluded_ids = tuple(item.get("key") for item in excluded if item.get("key"))
        evidence["excluded_identities"] = excluded_ids
        removal_ledger = tuple(excluded) if ledger is None else tuple(ledger)
        evidence["binding_removals"] = removal_ledger
        evidence["binding_removal_ledger"] = removal_ledger
        retained_ids = tuple(_period_row_key(row) for row in retained) if retained is not None else tuple(evidence.get("retained_identities", ()))
        evidence["retained_identities"] = retained_ids
        evidence["final_retained_size"] = len(retained_ids)
        return CommonPretestPeriodResult(result.status, result.start_utc, result.end_utc, result.daily_paths, result.coverage_pct, tuple(excluded), MappingProxyType(evidence), result.reason)

    last_result: CommonPretestPeriodResult | None = None
    while active:
        result = _evaluate_period(active, minimum_common_days, minimum_daily_coverage_pct, maximum_forward_fill_gap_days)
        last_result = result
        if result.available:
            return finish(result, removed, active, removed)
        evidence = result.evidence
        violated = {str(item.get("key")) for item in evidence.get("violations", ()) if item.get("key")}
        offenders = {_period_row_key(row): row for row in active if _period_row_key(row) in violated}
        if int(evidence.get("calendar_days", 0)) < minimum_common_days:
            binders = set(evidence.get("start_binders", ())) | set(evidence.get("end_binders", ()))
            offenders.update({_period_row_key(row): row for row in active if _period_row_key(row) in binders})
        if not offenders:
            return finish(result, (*removed, *result.exclusions), active, removed)
        current_score = score(result)
        trials: list[tuple[tuple[int, int, int, int, int], Mapping[str, Any], CommonPretestPeriodResult]] = []
        for row in offenders.values():
            candidate = tuple(item for item in active if item is not row)
            trial = _evaluate_period(candidate, minimum_common_days, minimum_daily_coverage_pct, maximum_forward_fill_gap_days)
            trials.append((score(trial), row, trial))
        improving = [item for item in trials if item[0] > current_score]
        if not improving:
            return finish(result, (*removed, *result.exclusions), active, removed)
        best_score = max(item[0] for item in improving)
        best_candidates = [item for item in improving if item[0] == best_score]
        _best_score, best_row, best_trial = sorted(best_candidates, key=lambda item: _period_sort_key(item[1]))[0]
        removed_reason = "PERIOD_BINDING_REMOVAL"
        key = _period_row_key(best_row)
        failed_reason = next((str(item.get("reason")) for item in result.exclusions if item.get("key") == key), None)
        if failed_reason:
            removed_reason = failed_reason
        else:
            intervals_by_key = {_period_row_key(row): _period_interval(row) for row in active}
            interval = intervals_by_key.get(key)
            if interval is not None:
                starts = [item[0] for item in intervals_by_key.values() if item is not None]
                ends = [item[1] for item in intervals_by_key.values() if item is not None]
                if interval[0] == max(starts):
                    removed_reason = "LATEST_START_OFFENDER"
                elif interval[1] == min(ends):
                    removed_reason = "EARLIEST_END_OFFENDER"
        removed.append({
            "key": key, "reason": removed_reason,
            "before": {"start_utc": result.start_utc, "end_utc": result.end_utc, "calendar_days": evidence.get("calendar_days"), "violations": tuple(evidence.get("violations", ()))},
            "after": {"start_utc": best_trial.start_utc, "end_utc": best_trial.end_utc, "calendar_days": best_trial.evidence.get("calendar_days"), "violations": tuple(best_trial.evidence.get("violations", ()))},
            "score_before": current_score, "score_after": best_score, "chosen": True,
        })
        active = tuple(item for item in active if item is not best_row)
    if last_result is not None:
        return finish(last_result, removed, active, removed)
    return CommonPretestPeriodResult(
        "UNAVAILABLE",
        exclusions=tuple(removed),
        evidence=MappingProxyType({
            "period_basis": "CURRENT_RESULT_SPARSE_OBSERVATION",
            "path_policy": "SPARSE_OBSERVATION_FORWARD_FILL",
            "retained_identities": (),
            "excluded_identities": tuple(item.get("key") for item in removed if item.get("key")),
            "binding_removals": tuple(removed),
            "binding_removal_ledger": tuple(removed),
            "final_retained_size": 0,
        }),
        reason="COMMON_PRETEST_PERIOD_UNAVAILABLE",
    )


build_common_pretest_period = resolve_common_pretest_period
common_pretest_period = resolve_common_pretest_period
read_snapshot = read_performance_snapshot
read_portfolio_input = read_performance_snapshot
snapshot_performance_input = read_performance_snapshot
PortfolioInputIdentity = SnapshotIdentity
PortfolioSnapshot = PerformanceInputSnapshot

"""Read-only, immutable input snapshots for the portfolio optimizer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
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
) -> tuple[
    dict[tuple[str, str, int, int], dict[str, str | int]],
    set[tuple[str, str, int, int]],
    tuple[dict[str, str], ...],
]:
    """Return only statuses from the latest imported review for each pair/side."""
    facts: dict[tuple[str, str, int, int], dict[str, str | int]] = {}
    selected: set[tuple[str, str, int, int]] = set()
    lineage: list[dict[str, str]] = []

    def latest_unique(query: str, parameters: Sequence[object], field: str) -> str:
        rows = connection.execute(query, list(parameters)).fetchall()
        if not rows or rows[0][1] is None or (len(rows) > 1 and rows[1][1] == rows[0][1]):
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
            "select strategy_id, user_status from selection_review_rows where review_import_id = ?",
            [review_id],
        ).fetchall()
        review_statuses: dict[int, object] = {}
        for strategy_id, user_status in rows:
            strategy_key = _source_integer(strategy_id, "strategy_id")
            if strategy_key in review_statuses:
                raise PortfolioInputError(
                    "duplicate current review row identity",
                    code=SOURCE_SNAPSHOT_UNAVAILABLE,
                )
            review_statuses[strategy_key] = user_status
        facts.update(
            {
                (symbol, side, strategy_id, selected_results[strategy_id]): {
                    "user_status": user_status,
                    "selection_run_id": run_id,
                    "review_import_id": review_id,
                }
                for strategy_id, user_status in review_statuses.items()
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
read_snapshot = read_performance_snapshot
read_portfolio_input = read_performance_snapshot
snapshot_performance_input = read_performance_snapshot
PortfolioInputIdentity = SnapshotIdentity
PortfolioSnapshot = PerformanceInputSnapshot

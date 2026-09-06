"""Read-only liquidity, exchange-reference, ticker, and coarse sizing contracts.

M2 deliberately contains no writers and no network default.  All values used by
the admission screens must be supplied by the caller and are retained in the
returned evidence objects.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
from pathlib import Path
from typing import Any

import duckdb

from mrs3.bybit_collector.aggregation import BANDS_BPS, LIQUIDITY_1M_COLUMNS, LIQUIDITY_1M_SCHEMA
from mrs3.bybit_collector.storage import PublishedHour, SQLiteSpool

from .canonical import CanonicalEnvelope, canonical_digest_v1


DAY_MS = 86_400_000
COARSE_ESTIMATE = "COARSE_ESTIMATE"
_SCHEMA_NAME = "bybit_liquidity_1m"
_SCHEMA_VERSION = "2"


class LiquidityError(ValueError):
    """A marked liquidity artifact cannot satisfy the reader contract."""

    def __init__(self, message: str, reason: str = "LIQUIDITY_MISSING") -> None:
        super().__init__(message)
        self.reason = reason


def _decimal(value: Decimal | int | str, name: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, float) or isinstance(value, bool):
        raise ValueError(f"{name} must be an exact decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be an exact decimal") from exc
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f"{name} has an invalid value")
    return result


def _int(value: int, name: str, *, positive: bool = False, nonnegative: bool = False) -> int:
    if type(value) is not int or (positive and value <= 0) or (nonnegative and value < 0):
        raise ValueError(f"{name} must be an integer")
    return value


def _digest(payload: Any, *, schema_id: str) -> str:
    def normalize(value: Any) -> Any:
        if is_dataclass(value):
            return {"type": type(value).__name__, "value": normalize(asdict(value))}
        if isinstance(value, Decimal):
            return {"type": "Decimal", "value": format(value, "f")}
        if isinstance(value, float):
            return {"type": "float", "value": format(value, ".17g")}
        if isinstance(value, Mapping):
            return {str(key): normalize(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        if isinstance(value, bool):
            return {"type": "bool", "value": value}
        if isinstance(value, int):
            return {"type": "int", "value": value}
        if isinstance(value, str):
            return {"type": "str", "value": value}
        if value is None:
            return {"type": "NoneType", "value": None}
        return str(value)

    encoded = json.dumps(normalize(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return canonical_digest_v1(CanonicalEnvelope(schema_id, 1, "snapshot", "json", encoded))


@dataclass(frozen=True, slots=True)
class LiquidityPolicy:
    """Every numeric quality/freshness choice is explicit; no defaults are safe."""

    band_bps: int
    lookback_days: int
    distribution_quantile: Decimal
    allowed_depth_share: Decimal
    min_coverage: Decimal
    min_completeness: Decimal
    min_eligible_fraction: Decimal
    max_age_ms: int
    expiry_ms: int

    def __post_init__(self) -> None:
        if self.band_bps not in BANDS_BPS:
            raise ValueError("band must be one of 10, 25, 50, 100 bps")
        _int(self.lookback_days, "lookback_days", positive=True)
        if self.lookback_days != 7:
            raise ValueError("liquidity lookback must be seven fully completed UTC days")
        for name, value in (("distribution_quantile", self.distribution_quantile), ("allowed_depth_share", self.allowed_depth_share), ("min_coverage", self.min_coverage), ("min_completeness", self.min_completeness), ("min_eligible_fraction", self.min_eligible_fraction)):
            if not isinstance(value, Decimal):
                raise ValueError(f"{name} must be an exact Decimal")
            value = _decimal(value, name, nonnegative=True)
            if value > 1:
                raise ValueError(f"{name} must be at most one")
        _int(self.max_age_ms, "max_age_ms", nonnegative=True)
        _int(self.expiry_ms, "expiry_ms", nonnegative=True)


@dataclass(frozen=True, slots=True)
class LiquidityHistory:
    rows: tuple[Mapping[str, Any], ...]
    symbol: str
    window_start_ms: int
    window_end_ms: int
    source: str
    captured_at_ms: int
    expires_at_ms: int
    content_digest: str


def _safe_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or any(char in name for char in "*?[]"):
        raise LiquidityError("published marker path is not an exact file path")
    root = root.resolve()
    path = (root / name).resolve()
    if path == root or root not in path.parents:
        raise LiquidityError("published marker path escapes collector root")
    return path


def _metadata(connection: duckdb.DuckDBPyConnection, path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    escaped = str(path).replace("'", "''")
    for key, value in connection.execute(f"SELECT key, value FROM parquet_kv_metadata('{escaped}')").fetchall():
        key = key.decode() if isinstance(key, bytes) else str(key)
        value = value.decode() if isinstance(value, bytes) else str(value)
        values[key] = value
    return values


def _read_marked_file(root: Path, marker: PublishedHour) -> tuple[dict[str, Any], ...]:
    path = _safe_path(root, marker.file_name)
    if not path.is_file():
        raise LiquidityError(f"marked liquidity file is missing: {marker.file_name}", "LIQUIDITY_MISSING")
    connection = duckdb.connect()
    try:
        escaped = str(path).replace("'", "''")
        description = connection.execute(f"DESCRIBE SELECT * FROM read_parquet('{escaped}', hive_partitioning=false)").fetchall()
        if tuple(str(item[0]) for item in description) != LIQUIDITY_1M_COLUMNS:
            raise LiquidityError("liquidity schema columns do not match schema v2", "LIQUIDITY_QUALITY_INSUFFICIENT")
        expected_types = {name: data_type for name, data_type, _nullable in LIQUIDITY_1M_SCHEMA}
        if any(str(item[1]).upper() != expected_types[str(item[0])] for item in description):
            raise LiquidityError("liquidity schema types do not match schema v2", "LIQUIDITY_QUALITY_INSUFFICIENT")
        metadata = _metadata(connection, path)
        required = {"schema_name": _SCHEMA_NAME, "schema_version": _SCHEMA_VERSION, "exchange": "bybit", "category": "linear"}
        if any(metadata.get(key) != value for key, value in required.items()):
            raise LiquidityError("liquidity parquet metadata is not schema v2 linear Bybit", "LIQUIDITY_QUALITY_INSUFFICIENT")
        if not metadata.get("collector_version") or not metadata.get("created_at_utc"):
            raise LiquidityError("liquidity parquet metadata lacks provenance", "LIQUIDITY_QUALITY_INSUFFICIENT")
        rows = connection.execute(f"SELECT * FROM read_parquet('{escaped}', hive_partitioning=false) ORDER BY minute_ts_ms, symbol").fetchall()
        if len(rows) != marker.row_count:
            raise LiquidityError("published marker row count does not match parquet", "LIQUIDITY_QUALITY_INSUFFICIENT")
        result = tuple(dict(zip(LIQUIDITY_1M_COLUMNS, row)) for row in rows)
        for row in result:
            minute = row["minute_ts_ms"]
            if type(minute) is not int or not marker.hour_start_ms <= minute < marker.hour_start_ms + 3_600_000:
                raise LiquidityError("liquidity row timestamp is outside marked hour", "LIQUIDITY_QUALITY_INSUFFICIENT")
        return result
    except LiquidityError:
        raise
    except Exception as exc:
        raise LiquidityError(f"cannot read marked liquidity parquet: {exc}", "LIQUIDITY_QUALITY_INSUFFICIENT") from exc
    finally:
        connection.close()


class LiquidityReader:
    """Read only marker-authoritative hourly files for seven completed UTC days."""

    def __init__(self, root: str | Path, policy: LiquidityPolicy) -> None:
        self.root = Path(root)
        self.policy = policy

    def read(self, symbol: str, *, now_ms: int) -> LiquidityHistory:
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol must be a non-empty string")
        _int(now_ms, "now_ms", nonnegative=True)
        day_end = (now_ms // DAY_MS) * DAY_MS
        window_start = day_end - self.policy.lookback_days * DAY_MS
        with SQLiteSpool.open_read_only(self.root) as spool:
            marker_rows = spool.published_hours()
        if len({marker.hour_start_ms for marker in marker_rows}) != len(marker_rows):
            raise LiquidityError("published hour index contains duplicate hour markers", "LIQUIDITY_QUALITY_INSUFFICIENT")
        markers = {marker.hour_start_ms: marker for marker in marker_rows}
        selected: list[PublishedHour] = []
        for hour in range(window_start, day_end, 3_600_000):
            marker = markers.get(hour)
            if marker is None:
                raise LiquidityError("not all seven UTC days are fully published", "LIQUIDITY_MISSING")
            selected.append(marker)
        rows: list[Mapping[str, Any]] = []
        seen_keys: set[tuple[int, str]] = set()
        for marker in selected:
            for row in _read_marked_file(self.root, marker):
                key = (row["minute_ts_ms"], row["symbol"])
                if key in seen_keys:
                    raise LiquidityError("duplicate minute/symbol rows across marked files", "LIQUIDITY_QUALITY_INSUFFICIENT")
                seen_keys.add(key)
                rows.append(row)
        rows = [row for row in rows if row["symbol"] == symbol]
        if not rows:
            raise LiquidityError("symbol has no published liquidity rows", "LIQUIDITY_MISSING")
        captured = max(marker.validated_at_ms for marker in selected)
        digest = _digest({"markers": selected, "rows": rows, "symbol": symbol}, schema_id="portfolio_liquidity_history_v2")
        return LiquidityHistory(tuple(rows), symbol, window_start, day_end, "bybit_liquidity_1m/published_hours", captured, captured + min(self.policy.expiry_ms, self.policy.max_age_ms), digest)

    read_history = read


def read_indexed_liquidity(root: str | Path, symbol: str, *, policy: LiquidityPolicy, now_ms: int) -> LiquidityHistory:
    return LiquidityReader(root, policy).read(symbol, now_ms=now_ms)


def _depth_field(side: str, band: int) -> tuple[str, str]:
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    side_name = "bid" if side == "LONG" else "ask"
    # schema v2's source observation is always minute p05.  The configured
    # distribution quantile is applied across those observations below.
    return f"{side_name}_depth_usdt_{band}bps_p05", f"{side_name}_depth_{band}bps_complete_ratio"


def _decimal_quantile(values: Sequence[Decimal], probability: Decimal) -> Decimal:
    if not values or probability < 0 or probability > 1:
        raise ValueError("quantile requires values and a probability in [0, 1]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    h = Decimal(len(ordered) - 1) * probability
    lower = int(h // 1)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (h - Decimal(lower)) * (ordered[upper] - ordered[lower])


def _opening_notional(level: Mapping[str, Any], sizing_base: Decimal) -> Decimal | None:
    if not isinstance(level, Mapping):
        raise ValueError("geometry level must be a mapping")
    if "role" not in level:
        raise ValueError("geometry level role is required")
    role = level["role"]
    if not isinstance(role, str) or role.lower() not in {"opening", "position_opening", "open", "closing", "close", "reduce_only", "tp", "sl", "exit"}:
        raise ValueError("geometry level role is invalid")
    role = role.lower()
    if "reduce_only" not in level or type(level["reduce_only"]) is not bool:
        raise ValueError("geometry level reduce_only must be an explicit boolean")
    if level["reduce_only"] or role in {"closing", "close", "reduce_only", "tp", "sl", "exit"}:
        return None
    if level.get("is_opening") is False:
        return None
    if "notional" in level:
        value = _decimal(level["notional"], "opening notional", positive=True)
    elif "lot_x" in level:
        value = sizing_base * _decimal(level["lot_x"], "lot_x", positive=True)
    else:
        raise ValueError("opening geometry requires notional or lot_x")
    return value


@dataclass(frozen=True, slots=True)
class LiquidityCeiling:
    status: str
    reason: str | None
    liquidity_scalar_pct_max: Decimal | None
    evidence_class: str
    used_band_bps: int
    depth_reference: Decimal | None
    single_order_cap: Decimal | None
    source: str
    window_start_ms: int | None
    window_end_ms: int | None
    captured_at_ms: int | None
    expires_at_ms: int | None
    content_digest: str
    geometry_digest: str
    filters_digest: str
    tier_digest: str
    exposure_digest: str
    policy_digest: str
    symbol: str
    side: str

    def is_valid(
        self,
        *,
        now_ms: int,
        geometry: Sequence[Mapping[str, Any]],
        filters: Mapping[str, Any] | None = None,
        tier: Any | None = None,
        exposure: Mapping[str, Any] | None = None,
        policy: LiquidityPolicy | None = None,
        symbol: str | None = None,
        side: str | None = None,
        window_start_ms: int | None = None,
        window_end_ms: int | None = None,
    ) -> bool:
        if self.status != "PASS" or self.expires_at_ms is None or now_ms >= self.expires_at_ms:
            return False
        if tier is None or exposure is None or policy is None or symbol is None or side is None or window_start_ms is None or window_end_ms is None:
            return False
        return (
            _digest(tuple(geometry), schema_id="portfolio_liquidity_geometry_v1") == self.geometry_digest
            and _digest(filters or {}, schema_id="portfolio_exchange_filters_v1") == self.filters_digest
            and _digest(tier, schema_id="portfolio_liquidity_tier_v1") == self.tier_digest
            and _digest(exposure, schema_id="portfolio_liquidity_exposure_v1") == self.exposure_digest
            and _digest(policy, schema_id="portfolio_liquidity_policy_v1") == self.policy_digest
            and symbol == self.symbol
            and side == self.side
            and window_start_ms == self.window_start_ms
            and window_end_ms == self.window_end_ms
        )


def directional_liquidity_ceiling(
    rows: Iterable[Mapping[str, Any]],
    geometry: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    side: str,
    policy: LiquidityPolicy,
    sizing_base: Decimal | int | str,
    captured_at_ms: int,
    now_ms: int | None = None,
    source: str | None = None,
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
    filters: Mapping[str, Any] | None = None,
    tier: Any | None = None,
    exposure: Mapping[str, Any] | None = None,
) -> LiquidityCeiling:
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("symbol must be a non-empty string")
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if not isinstance(geometry, Sequence):
        raise ValueError("geometry must be a sequence")
    aligned_window = (
        type(window_start_ms) is int
        and type(window_end_ms) is int
        and window_start_ms >= 0
        and window_end_ms > window_start_ms
        and window_start_ms % DAY_MS == 0
        and window_end_ms % DAY_MS == 0
        and window_end_ms - window_start_ms == policy.lookback_days * DAY_MS
    )
    base = _decimal(sizing_base, "sizing_base", positive=True)
    opening_levels = [value for level in geometry if (value := _opening_notional(level, base)) is not None]
    captured = _int(captured_at_ms, "captured_at_ms", nonnegative=True)
    now = None if now_ms is None else _int(now_ms, "now_ms", nonnegative=True)
    row_values = (
        tuple(
            sorted(
                (
                    row for row in rows
                    if row.get("symbol") == symbol
                    and type(row.get("minute_ts_ms")) is int
                    and window_start_ms <= row["minute_ts_ms"] < window_end_ms
                ),
                key=lambda row: (row.get("minute_ts_ms", 0), row.get("symbol", "")),
            )
        )
        if aligned_window
        else ()
    )
    geometry_digest = _digest(tuple(geometry), schema_id="portfolio_liquidity_geometry_v1")
    filters_digest = _digest(filters or {}, schema_id="portfolio_exchange_filters_v1")
    tier_digest = _digest(tier, schema_id="portfolio_liquidity_tier_v1") if tier is not None else ""
    exposure_digest = _digest(exposure, schema_id="portfolio_liquidity_exposure_v1") if exposure is not None else ""
    policy_digest = _digest(policy, schema_id="portfolio_liquidity_policy_v1")
    content_digest = _digest(row_values, schema_id="portfolio_liquidity_snapshot_v2")
    expires_at = captured + min(policy.expiry_ms, policy.max_age_ms)
    unknown = dict(status="UNKNOWN", reason=None, liquidity_scalar_pct_max=None, evidence_class="UNKNOWN", used_band_bps=policy.band_bps, depth_reference=None, single_order_cap=None, source=source, window_start_ms=window_start_ms, window_end_ms=window_end_ms, captured_at_ms=captured, expires_at_ms=expires_at, content_digest=content_digest, geometry_digest=geometry_digest, filters_digest=filters_digest, tier_digest=tier_digest, exposure_digest=exposure_digest, policy_digest=policy_digest, symbol=symbol, side=side)
    if source is None or not source or not aligned_window:
        unknown["reason"] = "OPEN_POLICY"
        return LiquidityCeiling(**unknown)
    if tier is None or exposure is None:
        unknown["reason"] = "OPEN_POLICY"
        return LiquidityCeiling(**unknown)
    if now is None:
        unknown["reason"] = "LIQUIDITY_STALE"
        return LiquidityCeiling(**unknown)
    if now - captured > policy.max_age_ms:
        unknown["reason"] = "LIQUIDITY_STALE"
        return LiquidityCeiling(**unknown)
    try:
        depth_key, complete_key = _depth_field(side, policy.band_bps)
    except ValueError:
        unknown["reason"] = "OPEN_POLICY"
        return LiquidityCeiling(**unknown)
    if not row_values:
        unknown["reason"] = "LIQUIDITY_MISSING"
        return LiquidityCeiling(**unknown)
    if len({row["minute_ts_ms"] for row in row_values}) != len(row_values):
        unknown["reason"] = "LIQUIDITY_QUALITY_INSUFFICIENT"
        return LiquidityCeiling(**unknown)
    eligible: list[Decimal] = []
    for row in row_values:
        coverage = row.get("coverage_ratio")
        completeness = row.get(complete_key)
        depth = row.get(depth_key)
        if depth is None or coverage is None or completeness is None:
            continue
        coverage_value = _decimal(str(coverage), "coverage_ratio", nonnegative=True)
        completeness_value = _decimal(str(completeness), "completeness_ratio", nonnegative=True)
        if coverage_value < policy.min_coverage or completeness_value < policy.min_completeness:
            continue
        eligible.append(_decimal(str(depth), "depth", nonnegative=True))
    expected_minutes = policy.lookback_days * 24 * 60
    eligible_fraction = Decimal(len(eligible)) / Decimal(expected_minutes)
    if not eligible or eligible_fraction < policy.min_eligible_fraction:
        unknown["reason"] = "LIQUIDITY_QUALITY_INSUFFICIENT"
        return LiquidityCeiling(**unknown)
    levels = opening_levels
    if not levels:
        unknown["reason"] = "LIQUIDITY_MISSING"
        return LiquidityCeiling(**unknown)
    reference = _decimal_quantile(eligible, policy.distribution_quantile)
    cap = reference * policy.allowed_depth_share
    scalar = min((cap / level * Decimal(100) for level in levels), default=Decimal(0))
    return LiquidityCeiling("PASS", None, scalar, "CONSERVATIVE_BOUND", policy.band_bps, reference, cap, source, window_start_ms, window_end_ms, captured, expires_at, content_digest, geometry_digest, filters_digest, tier_digest, exposure_digest, policy_digest, symbol, side)


@dataclass(frozen=True, slots=True)
class ExitLiquidityDiagnostic:
    """Diagnostic-only close-side depth; it never raises opening capacity."""

    status: str
    reason: str | None
    depth_reference: Decimal | None
    evidence_class: str
    source: str
    content_digest: str


def exit_liquidity_diagnostic(
    rows: Iterable[Mapping[str, Any]],
    geometry: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    side: str,
    policy: LiquidityPolicy,
    captured_at_ms: int,
    now_ms: int | None = None,
) -> ExitLiquidityDiagnostic:
    """Report available close-side depth separately from opening ceilings."""
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("symbol must be a non-empty string")
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    captured = _int(captured_at_ms, "captured_at_ms", nonnegative=True)
    now = None if now_ms is None else _int(now_ms, "now_ms", nonnegative=True)
    opposite = "SHORT" if side == "LONG" else "LONG"
    close_rows = tuple(
        sorted(
            (row for row in rows if row.get("symbol") == symbol),
            key=lambda row: (row.get("minute_ts_ms", 0), row.get("symbol", "")),
        )
    )
    digest = _digest(close_rows, schema_id="portfolio_exit_liquidity_v1")
    if now is None or now - captured > policy.max_age_ms:
        return ExitLiquidityDiagnostic("UNKNOWN", "LIQUIDITY_STALE", None, "UNKNOWN", "bybit_liquidity_1m", digest)
    try:
        key, complete = _depth_field(opposite, policy.band_bps)
    except ValueError:
        return ExitLiquidityDiagnostic("UNKNOWN", "OPEN_POLICY", None, "UNKNOWN", "bybit_liquidity_1m", digest)
    values = [_decimal(str(row[key]), "depth", nonnegative=True) for row in close_rows if row.get(key) is not None and row.get(complete) is not None and _decimal(str(row.get(complete)), "completeness_ratio", nonnegative=True) >= policy.min_completeness]
    if not values:
        return ExitLiquidityDiagnostic("UNKNOWN", "LIQUIDITY_MISSING", None, "UNKNOWN", "bybit_liquidity_1m", digest)
    return ExitLiquidityDiagnostic("PASS", None, _decimal_quantile(values, policy.distribution_quantile), "CONSERVATIVE_BOUND", "bybit_liquidity_1m", digest)


@dataclass(frozen=True, slots=True)
class EmpiricalCapacity:
    status: str
    reason: str
    evidence_class: str
    content_digest: str


def empirical_capacity(facts: Mapping[str, Any] | None = None) -> EmpiricalCapacity:
    """M2 has no fill-history model; empirical capacity remains explicitly open."""
    return EmpiricalCapacity("UNKNOWN", "OPEN_POLICY", "UNKNOWN", _digest(facts or {}, schema_id="portfolio_empirical_capacity_v1"))


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    status: str
    contract_type: str
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    leverage_step: Decimal
    max_leverage: Decimal
    min_notional: Decimal | None = None

    @property
    def min_order_qty(self) -> Decimal:
        return self.min_qty


@dataclass(frozen=True, slots=True)
class RiskTier:
    symbol: str
    risk_limit_value: Decimal
    max_leverage: Decimal
    risk_id: int | None = None
    maintenance_margin: Decimal | None = None
    initial_margin: Decimal | None = None
    mm_deduction: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ReferenceSnapshot:
    instruments: tuple[Instrument, ...]
    risk_tiers: tuple[RiskTier, ...]
    captured_at_ms: int
    content_digest: str

    def instrument(self, symbol: str) -> Instrument:
        matches = tuple(item for item in self.instruments if item.symbol == symbol)
        if len(matches) != 1:
            raise LiquidityError(f"instrument reference is not unique for {symbol}", "LIQUIDITY_MISSING")
        return matches[0]

    def tiers(self, symbol: str) -> tuple[RiskTier, ...]:
        result = tuple(sorted((item for item in self.risk_tiers if item.symbol == symbol), key=lambda item: item.risk_limit_value))
        if not result:
            raise LiquidityError(f"risk tiers are missing for {symbol}", "LIQUIDITY_MISSING")
        return result

    def applicable_tier(self, symbol: str, position_exposure: Decimal | int | str, active_order_exposure: Decimal | int | str) -> RiskTier:
        exposure = _decimal(position_exposure, "position_exposure", nonnegative=True) + _decimal(active_order_exposure, "active_order_exposure", nonnegative=True)
        for tier in self.tiers(symbol):
            if exposure < tier.risk_limit_value:
                return tier
        raise LiquidityError("exposure exceeds all available risk tiers", "LIQUIDITY_QUALITY_INSUFFICIENT")

    def maximum_symbol_leverage(self, symbol: str, position_exposure: Decimal | int | str, active_order_exposure: Decimal | int | str) -> Decimal:
        instrument = self.instrument(symbol)
        if instrument.status != "Trading" or instrument.contract_type != "LinearPerpetual":
            raise LiquidityError("instrument is not an active linear perpetual", "LIQUIDITY_QUALITY_INSUFFICIENT")
        tier = self.applicable_tier(symbol, position_exposure, active_order_exposure)
        maximum = min(instrument.max_leverage, tier.max_leverage)
        steps = (maximum / instrument.leverage_step).to_integral_value(rounding=ROUND_DOWN)
        return steps * instrument.leverage_step


class ReferenceReader:
    def __init__(self, snapshot: ReferenceSnapshot) -> None:
        self.snapshot = snapshot

    @classmethod
    def from_records(cls, *, instruments: Iterable[Mapping[str, Any]], risk_tiers: Iterable[Mapping[str, Any]], captured_at_ms: int) -> ReferenceSnapshot:
        captured = _int(captured_at_ms, "captured_at_ms", nonnegative=True)
        def get(item: Mapping[str, Any], *keys: str, required: bool = True) -> Any:
            for key in keys:
                current: Any = item
                found = True
                for part in key.split("."):
                    if not isinstance(current, Mapping) or part not in current:
                        found = False
                        break
                    current = current[part]
                if found:
                    return current
            if required:
                raise ValueError(f"reference field {keys[0]} is missing")
            return None
        parsed_instruments: list[Instrument] = []
        for item in instruments:
            symbol = get(item, "symbol")
            parsed_instruments.append(Instrument(symbol, str(get(item, "status", required=False) or ""), str(get(item, "contract_type", "contractType")), _decimal(get(item, "tick_size", "tickSize", "priceFilter.tickSize"), "tick_size", positive=True), _decimal(get(item, "qty_step", "qtyStep", "lotSizeFilter.qtyStep"), "qty_step", positive=True), _decimal(get(item, "min_qty", "min_order_qty", "minOrderQty", "lotSizeFilter.minOrderQty"), "min_qty", positive=True), _decimal(get(item, "max_qty", "max_order_qty", "maxOrderQty", "lotSizeFilter.maxOrderQty"), "max_qty", positive=True), _decimal(get(item, "leverage_step", "leverageStep", "leverageFilter.leverageStep"), "leverage_step", positive=True), _decimal(get(item, "max_leverage", "maxLeverage", "leverageFilter.maxLeverage"), "max_leverage", positive=True), _decimal(get(item, "min_notional", "min_notional_value", "minNotionalValue", "lotSizeFilter.minNotionalValue"), "min_notional", positive=True) if get(item, "min_notional", "min_notional_value", "minNotionalValue", "lotSizeFilter.minNotionalValue", required=False) is not None else None))
        parsed_tiers: list[RiskTier] = []
        for item in risk_tiers:
            parsed_tiers.append(RiskTier(str(get(item, "symbol")), _decimal(get(item, "risk_limit_value", "riskLimitValue"), "risk_limit_value", positive=True), _decimal(get(item, "max_leverage", "maxLeverage"), "max_leverage", positive=True), int(get(item, "risk_id", "riskId", "id", required=False)) if get(item, "risk_id", "riskId", "id", required=False) is not None else None, _decimal(get(item, "maintenance_margin", "maintenanceMargin", required=False), "maintenance_margin", nonnegative=True) if get(item, "maintenance_margin", "maintenanceMargin", required=False) is not None else None, _decimal(get(item, "initial_margin", "initialMargin", required=False), "initial_margin", nonnegative=True) if get(item, "initial_margin", "initialMargin", required=False) is not None else None, _decimal(get(item, "mm_deduction", "mmDeduction", required=False), "mm_deduction", nonnegative=True) if get(item, "mm_deduction", "mmDeduction", required=False) is not None else None))
        parsed_instruments.sort(key=lambda item: item.symbol)
        parsed_tiers.sort(key=lambda item: (item.symbol, item.risk_limit_value))
        digest = _digest({"instruments": parsed_instruments, "risk_tiers": parsed_tiers, "captured_at_ms": captured}, schema_id="portfolio_reference_v1")
        return ReferenceSnapshot(tuple(parsed_instruments), tuple(parsed_tiers), captured, digest)

    @classmethod
    def from_parquet(
        cls,
        instruments_path: str | Path,
        risk_tiers_path: str | Path,
        *,
        captured_at_ms: int,
        symbol: str | None = None,
    ) -> ReferenceSnapshot:
        """Read the collector's immutable reference parquet files without writes."""
        connection = duckdb.connect()
        try:
            def rows(path: str | Path) -> list[dict[str, Any]]:
                escaped = str(Path(path)).replace("'", "''")
                description = connection.execute(f"DESCRIBE SELECT * FROM read_parquet('{escaped}')").fetchall()
                columns = tuple(str(item[0]) for item in description)
                values = connection.execute(f"SELECT * FROM read_parquet('{escaped}')").fetchall()
                return [dict(zip(columns, value)) for value in values]
            instruments = rows(instruments_path)
            risks = rows(risk_tiers_path)
        except Exception as exc:
            raise LiquidityError(f"cannot read reference parquet: {exc}", "LIQUIDITY_MISSING") from exc
        finally:
            connection.close()
        if symbol is not None:
            instruments = [item for item in instruments if item.get("symbol") == symbol]
            risks = [item for item in risks if item.get("symbol") == symbol]
        return cls.from_records(instruments=instruments, risk_tiers=risks, captured_at_ms=captured_at_ms)

    read_parquet = from_parquet

    def instrument(self, symbol: str) -> Instrument:
        return self.snapshot.instrument(symbol)

    def tiers(self, symbol: str) -> tuple[RiskTier, ...]:
        return self.snapshot.tiers(symbol)

    def applicable_tier(self, symbol: str, position_exposure: Decimal | int | str, active_order_exposure: Decimal | int | str) -> RiskTier:
        return self.snapshot.applicable_tier(symbol, position_exposure, active_order_exposure)

    def maximum_symbol_leverage(self, symbol: str, position_exposure: Decimal | int | str, active_order_exposure: Decimal | int | str) -> Decimal:
        return self.snapshot.maximum_symbol_leverage(symbol, position_exposure, active_order_exposure)


def read_reference(
    instruments_path: str | Path,
    risk_tiers_path: str | Path,
    *,
    captured_at_ms: int,
    symbol: str | None = None,
) -> ReferenceSnapshot:
    return ReferenceReader.from_parquet(
        instruments_path, risk_tiers_path, captured_at_ms=captured_at_ms, symbol=symbol
    )


@dataclass(frozen=True, slots=True)
class TickerSnapshot:
    status: str
    symbol: str
    category: str
    turnover24h: Decimal | None
    volume24h: Decimal | None
    server_time_ms: int | None
    capture_time_ms: int
    unit: str
    provenance: str
    content_digest: str
    reason: str | None = None

    @property
    def turnover_24h(self) -> Decimal | None:
        return self.turnover24h

    @property
    def volume_24h(self) -> Decimal | None:
        return self.volume24h

    @property
    def server_timestamp_ms(self) -> int | None:
        return self.server_time_ms

    @property
    def capture_timestamp_ms(self) -> int:
        return self.capture_time_ms


class TickerAdapter:
    def __init__(self, fetcher: Callable[[dict[str, str]], Mapping[str, Any]]) -> None:
        if not callable(fetcher):
            raise TypeError("ticker fetcher must be callable")
        self.fetcher = fetcher

    def fetch(self, symbol: str, *, captured_at_ms: int) -> TickerSnapshot:
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol must be a non-empty string")
        captured = _int(captured_at_ms, "captured_at_ms", nonnegative=True)
        params = {"category": "linear", "symbol": symbol}

        def failure(reason: str, failure_class: str) -> TickerSnapshot:
            digest = _digest(
                {"capture_time_ms": captured, "failure_class": failure_class, "params": params, "reason": reason},
                schema_id="portfolio_turnover_ticker_failure_v1",
            )
            return TickerSnapshot("UNKNOWN", symbol, "linear", None, None, None, captured, "USDT", "bybit_public_market_tickers", digest, reason)

        try:
            response = self.fetcher(params)
            result = response.get("result") if isinstance(response, Mapping) else None
            items = result.get("list") if isinstance(result, Mapping) else None
            if not isinstance(response, Mapping) or response.get("retCode") != 0 or not isinstance(items, list) or len(items) != 1:
                raise ValueError("invalid ticker response")
            item = items[0]
            if not isinstance(item, Mapping) or item.get("symbol") != symbol:
                raise ValueError("ticker symbol mismatch")
            category = str(result.get("category", ""))
            if category != "linear":
                raise ValueError("ticker category mismatch")
            turnover = _decimal(item["turnover24h"], "turnover24h", nonnegative=True)
            volume = _decimal(item["volume24h"], "volume24h", nonnegative=True)
            server_raw = response.get("time", result.get("time"))
            server = _int(int(server_raw), "server_time_ms", nonnegative=True) if server_raw is not None else None
            digest = _digest({"category": category, "item": item, "server_time_ms": server}, schema_id="portfolio_turnover_ticker_v1")
            return TickerSnapshot("PASS", symbol, category, turnover, volume, server, captured, "USDT", "bybit_public_market_tickers", digest)
        except (TimeoutError, ConnectionError, OSError) as exc:
            return failure("TURNOVER_REQUEST_FAILED", type(exc).__name__)
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            return failure("TURNOVER_MISSING", type(exc).__name__)

    get = fetch


@dataclass(frozen=True, slots=True)
class CoarseScreen:
    status: str
    evidence_class: str
    reason: str | None
    total_opening_notional: Decimal
    composition_digest: str
    ratios: Mapping[str, Decimal]


def coarse_symbol_screen(
    members: Iterable[Mapping[str, Any]],
    turnovers: Mapping[str, TickerSnapshot],
    *,
    permitted_turnover_share: Decimal,
    composition_id: str,
    now_ms: int | None = None,
    max_age_ms: int | None = None,
) -> CoarseScreen:
    share = _decimal(permitted_turnover_share, "permitted_turnover_share", positive=True)
    if not isinstance(composition_id, str) or not composition_id:
        raise ValueError("composition_id must be a non-empty string")
    member_list = tuple(
        sorted(
            (dict(item) for item in members),
            key=lambda item: (
                str(item.get("symbol", "")),
                str(item.get("side", "")),
                str(item.get("member_id", item.get("account", ""))),
            ),
        )
    )
    digest = _digest({"composition_id": composition_id, "members": member_list}, schema_id="portfolio_composition_v1")
    totals: dict[str, Decimal] = {}
    for member in member_list:
        symbol = member.get("symbol")
        side = member.get("side")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("member symbol must be a non-empty string")
        if side not in {"LONG", "SHORT"}:
            raise ValueError("member side must be LONG or SHORT")
        amount = _decimal(member.get("opening_notional"), "opening_notional", nonnegative=True)
        totals[symbol] = totals.get(symbol, Decimal(0)) + amount
    ratios: dict[str, Decimal] = {}
    for symbol, total in totals.items():
        ticker = turnovers.get(symbol)
        if not isinstance(ticker, TickerSnapshot):
            return CoarseScreen("UNKNOWN", COARSE_ESTIMATE, "TURNOVER_MISSING", sum(totals.values(), Decimal(0)), digest, ratios)
        if (
            ticker.status != "PASS"
            or ticker.symbol != symbol
            or ticker.category != "linear"
            or ticker.unit != "USDT"
            or not isinstance(ticker.provenance, str)
            or not ticker.provenance
            or not isinstance(ticker.content_digest, str)
            or not ticker.content_digest
            or now_ms is None
            or max_age_ms is None
            or type(now_ms) is not int
            or type(max_age_ms) is not int
            or type(ticker.capture_time_ms) is not int
            or ticker.capture_time_ms < 0
            or max_age_ms < 0
            or now_ms < ticker.capture_time_ms
            or now_ms - ticker.capture_time_ms > max_age_ms
        ):
            return CoarseScreen("UNKNOWN", COARSE_ESTIMATE, "TURNOVER_MISSING", sum(totals.values(), Decimal(0)), digest, ratios)
        turnover = ticker.turnover24h
        if turnover is None:
            return CoarseScreen("UNKNOWN", COARSE_ESTIMATE, "TURNOVER_MISSING", sum(totals.values(), Decimal(0)), digest, ratios)
        denominator = _decimal(turnover, "turnover24h", positive=True)
        ratios[symbol] = total / denominator
    status = "FAIL" if any(ratio > share for ratio in ratios.values()) else "PASS"
    return CoarseScreen(status, COARSE_ESTIMATE, None, sum(totals.values(), Decimal(0)), digest, ratios)


@dataclass(frozen=True, slots=True)
class SizingEnvelopeResult:
    status: str
    reason: str | None
    upper_bound: Decimal | None


def sizing_envelope(envelope_max: Decimal | int | str | None, max_balance: Decimal | int | str | None) -> SizingEnvelopeResult:
    if envelope_max is None:
        return SizingEnvelopeResult("UNKNOWN", "SIZING_ENVELOPE_UNBOUNDED", None)
    upper = _decimal(envelope_max, "envelope_max", positive=True)
    if max_balance is None:
        return SizingEnvelopeResult("PASS", None, upper)
    cap = _decimal(max_balance, "max_balance", positive=True)
    return SizingEnvelopeResult("PASS", None, min(upper, cap))


compute_sizing_envelope = sizing_envelope


__all__ = [
    "BANDS_BPS", "COARSE_ESTIMATE", "DAY_MS", "LiquidityError", "LiquidityPolicy", "LiquidityHistory", "LiquidityReader", "read_indexed_liquidity", "LiquidityCeiling", "directional_liquidity_ceiling", "ExitLiquidityDiagnostic", "exit_liquidity_diagnostic", "EmpiricalCapacity", "empirical_capacity", "Instrument", "RiskTier", "ReferenceSnapshot", "ReferenceReader", "read_reference", "TickerSnapshot", "TickerAdapter", "CoarseScreen", "coarse_symbol_screen", "SizingEnvelopeResult", "sizing_envelope", "compute_sizing_envelope",
]

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import json
import tempfile

from .models import Side


DEFAULT_BASE_COLUMNS = {
    "symbol": "settings[*].basic.symbol",
    "timeframe": "settings[*].basic.time_frame",
    "report_start": "StartDate",
    "report_end": "EndDate",
    "pnl_pct": "TotalPnLPercent",
    "trades": "TotalTrades",
    "wins": "Win",
    "losses": "Los",
    "win_rate_pct": "WinRate",
    "dd_pct": "MaxDrawdownPercent",
    "profit_factor": "ProfitFactor",
    "run_id": "Run id",
}

DEFAULT_CANONICAL_SHIFTS_BP = (
    30, 40, 50, 60, 70,
    90, 110, 140, 170, 200,
    230, 270, 310, 350, 390,
    430, 470, 510, 550,
)

DEFAULT_GAP_RULES = (
    (30, 80, 80),
    (80, 200, 100),
    (200, 300, 130),
    (300, 551, 150),
)

DEFAULT_SIDE_COLUMNS = {
    Side.LONG: {
        "open_ma": "settings[*].mrs2.ma_long.len",
        "close_ma": "settings[*].mrs2.ma_close_long.len",
        "multiplier": "settings[*].mrs2.ma_long.multiplier",
    },
    Side.SHORT: {
        "open_ma": "settings[*].mrs2.ma_short.len",
        "close_ma": "settings[*].mrs2.ma_close_short.len",
        "multiplier": "settings[*].mrs2.ma_short.multiplier",
    },
}


@dataclass(frozen=True, slots=True)
class DuckDBImportSettings:
    source_duckdb_path: Path | None = None
    analysis_duckdb_path: Path | None = None
    default_html_root: Path | None = None
    audit_root: Path | None = None
    workers: int = 4
    transaction_batch_size: int = 250
    source_v6_surface_dir: Path | None = None

    def __post_init__(self) -> None:
        for name in (
            "source_duckdb_path",
            "analysis_duckdb_path",
            "source_v6_surface_dir",
            "default_html_root",
            "audit_root",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Path):
                raise ValueError(f"duckdb_import.{name} must be a path or null")
        for name in ("workers", "transaction_batch_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"duckdb_import.{name} must be a positive integer")


def _local_config_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config.local.json must be an object")
    return raw


def _optional_path_from_json(value: object, name: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"duckdb_import.{name} must be a string or null")
    return Path(value)


def load_duckdb_import_settings(path: Path) -> DuckDBImportSettings:
    raw = _local_config_object(path)
    section = raw.get("duckdb_import")
    if section is None:
        return DuckDBImportSettings()
    if not isinstance(section, dict):
        raise ValueError("duckdb_import must be an object")
    return DuckDBImportSettings(
        source_duckdb_path=_optional_path_from_json(section.get("source_duckdb_path"), "source_duckdb_path"),
        analysis_duckdb_path=_optional_path_from_json(section.get("analysis_duckdb_path"), "analysis_duckdb_path"),
        source_v6_surface_dir=_optional_path_from_json(section.get("source_v6_surface_dir"), "source_v6_surface_dir"),
        default_html_root=_optional_path_from_json(section.get("default_html_root"), "default_html_root"),
        audit_root=_optional_path_from_json(section.get("audit_root"), "audit_root"),
        workers=section.get("workers", 4),
        transaction_batch_size=section.get("transaction_batch_size", 250),
    )


def save_duckdb_import_settings(path: Path, settings: DuckDBImportSettings) -> None:
    raw = _local_config_object(path)
    existing = raw.get("duckdb_import")
    if existing is not None and not isinstance(existing, dict):
        raise ValueError("duckdb_import must be an object")
    section = dict(existing or {})
    for name in (
        "source_duckdb_path",
        "analysis_duckdb_path",
        "source_v6_surface_dir",
        "default_html_root",
        "audit_root",
    ):
        value = getattr(settings, name)
        section[name] = None if value is None else str(value)
    section["workers"] = settings.workers
    section["transaction_batch_size"] = settings.transaction_batch_size
    raw["duckdb_import"] = section
    _save_config_object(path, raw)


def _save_config_object(path: Path, raw: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        json.dump(raw, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class DirectMaterializationSettings:
    workers: int = 15
    fetch_batch_size: int = 256
    worker_chunk_size: int = 16
    max_in_flight_chunks: int = 30

    def __post_init__(self) -> None:
        for name in (
            "workers",
            "fetch_batch_size",
            "worker_chunk_size",
            "max_in_flight_chunks",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(
                    f"direct_materialization.{name} must be a positive integer"
                )
        if self.max_in_flight_chunks < self.workers:
            raise ValueError(
                "direct_materialization.max_in_flight_chunks must be at least workers"
            )


def load_direct_materialization_settings(path: Path) -> DirectMaterializationSettings:
    raw = _local_config_object(path)
    section = raw.get("direct_materialization")
    if section is None:
        return DirectMaterializationSettings()
    if not isinstance(section, dict):
        raise ValueError("direct_materialization must be an object")
    return DirectMaterializationSettings(
        workers=section.get("workers", 15),
        fetch_batch_size=section.get("fetch_batch_size", 256),
        worker_chunk_size=section.get("worker_chunk_size", 16),
        max_in_flight_chunks=section.get("max_in_flight_chunks", 30),
    )


def save_direct_materialization_settings(
    path: Path, settings: DirectMaterializationSettings
) -> None:
    raw = _local_config_object(path)
    existing = raw.get("direct_materialization")
    if existing is not None and not isinstance(existing, dict):
        raise ValueError("direct_materialization must be an object")
    section = dict(existing or {})
    section["workers"] = settings.workers
    section["fetch_batch_size"] = settings.fetch_batch_size
    section["worker_chunk_size"] = settings.worker_chunk_size
    section["max_in_flight_chunks"] = settings.max_in_flight_chunks
    raw["direct_materialization"] = section
    _save_config_object(path, raw)


def _base_rates_from_json(value: object) -> dict[str, Decimal]:
    if not isinstance(value, dict):
        error = TypeError("base_rate_tf must be an object")
        raise ValueError("base_rate_tf must be an object") from error
    rates: dict[str, Decimal] = {}
    for timeframe, rate in value.items():
        field = f"base_rate_tf.{timeframe}"
        if (
            not isinstance(timeframe, str)
            or rate is None
            or isinstance(rate, bool)
            or not isinstance(rate, (str, int, float, Decimal))
        ):
            error = TypeError(f"{field} must be a decimal")
            raise ValueError(f"{field} must be a decimal") from error
        try:
            rates[timeframe] = Decimal(str(rate))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError(f"{field} must be a decimal") from error
    return rates


@dataclass(frozen=True, slots=True)
class AlgorithmConfig:
    base_columns: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_BASE_COLUMNS))
    side_columns: dict[Side, dict[str, str]] = field(
        default_factory=lambda: {side: dict(columns) for side, columns in DEFAULT_SIDE_COLUMNS.items()}
    )
    grid_tolerance_bp: float = 0.000001
    history_min_days: Decimal = Decimal("7")
    base_rates: dict[str, Decimal] = field(
        default_factory=lambda: {
            "5m": Decimal("2.27"),
            "15m": Decimal("1.82"),
            "30m": Decimal("1.59"),
            "45m": Decimal("1.36"),
            "1h": Decimal("1.36"),
            "2h": Decimal("1.14"),
            "3h": Decimal("0.91"),
            "4h": Decimal("0.91"),
        }
    )
    canonical_shifts_bp: tuple[int, ...] = DEFAULT_CANONICAL_SHIFTS_BP
    shift_factors: tuple[tuple[int, Decimal], ...] = (
        (150, Decimal("1.00")),
        (200, Decimal("0.90")),
        (310, Decimal("0.30")),
        (550, Decimal("0.20")),
    )
    absolute_floor_boundary_bp: int = 200
    absolute_floor_at_or_below: int = 10
    absolute_floor_above: int = 5
    economic_min_pnl_pct: Decimal = Decimal("0")
    economic_min_win_rate_pct: Decimal = Decimal("70")
    economic_max_dd_pct: Decimal = Decimal("11")
    economic_min_efficiency: Decimal = Decimal("3")
    shift_domain_min_bp: int = 30
    shift_domain_max_bp: int = 550
    fine_zone_max_exclusive_bp: int = 150
    boundary_zone_max_bp: int = 170
    fine_step_bp: int = 10
    fine_radius_bp: int = 30
    boundary_down_radius_bp: int = 30
    boundary_up_radius_bp: int = 50
    coarse_radius_bp: int = 50
    ma_neighbor_radius: int = 1
    core_link_min: Decimal = Decimal("0.90")
    plateau_envelope_min: Decimal = Decimal("0.75")
    supported_link_min: Decimal = Decimal("0.75")
    isolated_peak_relative: Decimal = Decimal("0.90")
    equivalent_tolerance: Decimal = Decimal("0.05")
    close_core_min: Decimal = Decimal("0.90")
    close_supported_min: Decimal = Decimal("0.60")
    gap_mid_start_bp: int = 150
    gap_lower_lt_150_bp: int = 60
    gap_lower_150_to_400_bp: int = 80
    deep_gap_boundary_bp: int = 400
    gap_rules: tuple[tuple[int, int, int], ...] = DEFAULT_GAP_RULES
    max_orders: int = 4
    lot_rounding_decimals: int = 12
    numeric_tolerance: Decimal = Decimal("0.000000001")
    initial_lot_sum: Decimal = Decimal("1")
    target_dd_pct: Decimal = Decimal("5")
    close_multiplier_long: Decimal = Decimal("1.003")
    close_multiplier_short: Decimal = Decimal("0.997")
    min_point_events: int = 3

    def __post_init__(self) -> None:
        missing_base = sorted(set(DEFAULT_BASE_COLUMNS).difference(self.base_columns))
        if missing_base or any(not str(value).strip() for value in self.base_columns.values()):
            raise ValueError(
                f"base column mappings are incomplete or empty: {missing_base}"
            )
        for side, required in DEFAULT_SIDE_COLUMNS.items():
            configured = self.side_columns.get(side, {})
            missing = sorted(set(required).difference(configured))
            if missing or any(not str(value).strip() for value in configured.values()):
                raise ValueError(
                    f"{side.value} side column mappings are incomplete or empty: {missing}"
                )

        if Decimal(str(self.grid_tolerance_bp)) <= 0:
            raise ValueError("grid_tolerance_bp must be greater than zero")
        if self.history_min_days <= 0:
            raise ValueError("history_min_days must be greater than zero")
        if not self.base_rates or any(rate <= 0 for rate in self.base_rates.values()):
            raise ValueError("base rates must be non-empty and greater than zero")

        if self.shift_domain_min_bp < 0 or self.shift_domain_min_bp > self.shift_domain_max_bp:
            raise ValueError("shift domain must be non-negative and ordered")
        try:
            canonical_shifts_bp = tuple(self.canonical_shifts_bp)
        except TypeError as error:
            raise ValueError("canonical shifts must be an integer sequence") from error
        object.__setattr__(self, "canonical_shifts_bp", canonical_shifts_bp)
        if (
            not canonical_shifts_bp
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in canonical_shifts_bp
            )
            or any(
                left >= right
                for left, right in zip(
                    canonical_shifts_bp, canonical_shifts_bp[1:]
                )
            )
            or canonical_shifts_bp[0] != self.shift_domain_min_bp
            or canonical_shifts_bp[-1] != self.shift_domain_max_bp
        ):
            raise ValueError(
                "canonical shifts must be a non-empty strictly increasing "
                "integer tuple spanning the shift domain"
            )
        if not self.shift_factors:
            raise ValueError("shift factors cannot be empty")
        boundaries = [maximum for maximum, _ in self.shift_factors]
        factors = [factor for _, factor in self.shift_factors]
        if (
            any(boundary <= 0 for boundary in boundaries)
            or any(left >= right for left, right in zip(boundaries, boundaries[1:]))
        ):
            raise ValueError("shift factor boundaries must be positive and increasing")
        if any(factor <= 0 for factor in factors) or any(
            left < right for left, right in zip(factors, factors[1:])
        ):
            raise ValueError("shift factor values must be positive and non-increasing")

        if self.absolute_floor_boundary_bp < 0 or min(
            self.absolute_floor_at_or_below, self.absolute_floor_above
        ) <= 0:
            raise ValueError("absolute trade floors must be positive")
        if not self.economic_min_pnl_pct.is_finite():
            raise ValueError("economic_min_pnl_pct must be finite")
        if not Decimal("0") <= self.economic_min_win_rate_pct <= Decimal("100"):
            raise ValueError("economic_min_win_rate_pct must be between 0 and 100")
        if self.economic_max_dd_pct <= 0 or self.economic_min_efficiency <= 0:
            raise ValueError("economic drawdown and efficiency thresholds must be positive")

        if not (
            0 < self.fine_zone_max_exclusive_bp <= self.boundary_zone_max_bp
        ):
            raise ValueError("refine zone boundaries must be positive and ordered")
        if min(
            self.fine_step_bp,
            self.fine_radius_bp,
            self.boundary_down_radius_bp,
            self.boundary_up_radius_bp,
            self.coarse_radius_bp,
        ) <= 0 or self.ma_neighbor_radius < 0:
            raise ValueError("refine steps and radii must be positive")

        if not (
            Decimal("0") < self.plateau_envelope_min
            <= self.supported_link_min
            <= self.core_link_min
            <= Decimal("1")
        ):
            raise ValueError(
                "plateau thresholds must satisfy 0 < envelope <= supported <= core <= 1"
            )
        if not Decimal("0") <= self.isolated_peak_relative <= Decimal("1"):
            raise ValueError("isolated_peak_relative must be between 0 and 1")
        if not Decimal("0") <= self.equivalent_tolerance <= Decimal("1"):
            raise ValueError("equivalent_tolerance must be between 0 and 1")
        if not (
            Decimal("0") < self.close_supported_min
            <= self.close_core_min
            <= Decimal("1")
        ):
            raise ValueError(
                "close support thresholds must satisfy 0 < supported <= core <= 1"
            )

        if min(self.gap_lower_lt_150_bp, self.gap_lower_150_to_400_bp) <= 0:
            raise ValueError("gap thresholds must be positive")
        if not 0 < self.gap_mid_start_bp <= self.deep_gap_boundary_bp:
            raise ValueError("gap boundaries must be positive and ordered")
        try:
            gap_rules = tuple(
                (
                    rule["lower_min_bp"],
                    rule["lower_max_exclusive_bp"],
                    rule["min_gap_bp"],
                )
                if isinstance(rule, dict)
                else tuple(rule)
                for rule in self.gap_rules
            )
        except (KeyError, TypeError) as error:
            raise ValueError(
                "gap rules must define lower_min_bp, lower_max_exclusive_bp and min_gap_bp"
            ) from error
        object.__setattr__(self, "gap_rules", gap_rules)
        if not gap_rules:
            raise ValueError("gap rules must be non-empty")
        expected_min = self.shift_domain_min_bp
        for rule in gap_rules:
            if len(rule) != 3:
                raise ValueError("gap rules must contain exactly three values")
            lower_min_bp, lower_max_exclusive_bp, min_gap_bp = rule
            if any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (lower_min_bp, lower_max_exclusive_bp, min_gap_bp)
            ) or not (
                0 <= lower_min_bp < lower_max_exclusive_bp
                and min_gap_bp > 0
                and lower_min_bp == expected_min
            ):
                raise ValueError(
                    "gap rules must be ordered, non-overlapping, cover the shift "
                    "domain and have positive minimum gaps"
                )
            expected_min = lower_max_exclusive_bp
        if expected_min != self.shift_domain_max_bp + 1:
            raise ValueError(
                "gap rules must be ordered, non-overlapping, cover the shift "
                "domain and have positive minimum gaps"
            )
        if not 2 <= self.max_orders <= 4:
            raise ValueError("max_orders must be between 2 and 4")
        if self.lot_rounding_decimals < 0:
            raise ValueError("lot_rounding_decimals cannot be negative")
        if self.numeric_tolerance <= 0:
            raise ValueError("numeric_tolerance must be greater than zero")
        if self.initial_lot_sum <= 0:
            raise ValueError("initial_lot_sum must be greater than zero")
        if self.target_dd_pct <= 0:
            raise ValueError("target_dd_pct must be greater than zero")
        if self.close_multiplier_long <= 1:
            raise ValueError("close_multiplier_long must be greater than one")
        if not Decimal("0") < self.close_multiplier_short < Decimal("1"):
            raise ValueError("close_multiplier_short must be between zero and one")
        if self.min_point_events < 1:
            raise ValueError("min_point_events must be at least one")

    @classmethod
    def defaults(cls) -> "AlgorithmConfig":
        return cls()

    @classmethod
    def from_json(cls, path: Path) -> "AlgorithmConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        base_rates = _base_rates_from_json(raw.get("base_rate_tf", cls().base_rates))
        columns = raw.get("columns", {})
        base = dict(DEFAULT_BASE_COLUMNS)
        base.update(columns.get("base", {}))
        side_columns = {side: dict(values) for side, values in DEFAULT_SIDE_COLUMNS.items()}
        for side in Side:
            side_columns[side].update(columns.get(side.value.lower(), {}))
        return cls(
            base_columns=base,
            side_columns=side_columns,
            grid_tolerance_bp=float(raw.get("grid_tolerance_bp", 0.000001)),
            history_min_days=Decimal(str(raw.get("history_min_days", 7))),
            base_rates=base_rates,
            shift_factors=tuple(
                (int(item["max_bp"]), Decimal(str(item["value"])))
                for item in raw.get(
                    "shift_factors",
                    [
                        {"max_bp": 150, "value": 1.0},
                        {"max_bp": 200, "value": 0.9},
                        {"max_bp": 310, "value": 0.3},
                        {"max_bp": 550, "value": 0.2},
                    ],
                )
            ),
            absolute_floor_boundary_bp=int(raw.get("absolute_floor_boundary_bp", 200)),
            absolute_floor_at_or_below=int(raw.get("absolute_floor_at_or_below", 10)),
            absolute_floor_above=int(raw.get("absolute_floor_above", 5)),
            economic_min_pnl_pct=Decimal(
                str(raw.get("economic_min_pnl_pct", 0))
            ),
            economic_min_win_rate_pct=Decimal(
                str(raw.get("economic_min_win_rate_pct", 70))
            ),
            economic_max_dd_pct=Decimal(str(raw.get("economic_max_dd_pct", 11))),
            economic_min_efficiency=Decimal(
                str(raw.get("economic_min_efficiency", 3))
            ),
            shift_domain_min_bp=int(raw.get("shift_domain", {}).get("min_bp", 30)),
            shift_domain_max_bp=int(raw.get("shift_domain", {}).get("max_bp", 550)),
            fine_zone_max_exclusive_bp=int(
                raw.get("refine", {}).get("fine_zone_max_exclusive_bp", 150)
            ),
            boundary_zone_max_bp=int(
                raw.get("refine", {}).get("boundary_zone_max_bp", 170)
            ),
            fine_step_bp=int(raw.get("refine", {}).get("fine_step_bp", 10)),
            fine_radius_bp=int(raw.get("refine", {}).get("fine_radius_bp", 30)),
            boundary_down_radius_bp=int(
                raw.get("refine", {}).get("boundary_down_radius_bp", 30)
            ),
            boundary_up_radius_bp=int(
                raw.get("refine", {}).get("boundary_up_radius_bp", 50)
            ),
            coarse_radius_bp=int(raw.get("refine", {}).get("coarse_radius_bp", 50)),
            ma_neighbor_radius=int(raw.get("refine", {}).get("ma_neighbor_radius", 1)),
            core_link_min=Decimal(str(raw.get("plateau", {}).get("core_link_min", 0.90))),
            plateau_envelope_min=Decimal(
                str(raw.get("plateau", {}).get("envelope_min", 0.75))
            ),
            supported_link_min=Decimal(
                str(raw.get("plateau", {}).get("supported_link_min", 0.75))
            ),
            isolated_peak_relative=Decimal(
                str(raw.get("plateau", {}).get("isolated_peak_relative", 0.90))
            ),
            equivalent_tolerance=Decimal(
                str(raw.get("plateau", {}).get("equivalent_tolerance", 0.05))
            ),
            close_core_min=Decimal(
                str(raw.get("close_support", {}).get("core_min", 0.90))
            ),
            close_supported_min=Decimal(
                str(raw.get("close_support", {}).get("supported_min", 0.60))
            ),
            gap_mid_start_bp=int(
                raw.get("gap", {}).get("middle_zone_start_bp", 150)
            ),
            gap_lower_lt_150_bp=int(
                raw.get("gap", {}).get("lower_shift_lt_1_5_bp", 60)
            ),
            gap_lower_150_to_400_bp=int(
                raw.get("gap", {}).get("lower_shift_1_5_to_4_bp", 80)
            ),
            deep_gap_boundary_bp=int(
                raw.get("gap", {}).get("deep_gap_boundary_bp", 400)
            ),
            canonical_shifts_bp=tuple(
                raw.get("canonical_shifts_bp", DEFAULT_CANONICAL_SHIFTS_BP)
            ),
            gap_rules=tuple(raw.get("gap_rules", DEFAULT_GAP_RULES)),
            max_orders=int(raw.get("max_orders", 4)),
            lot_rounding_decimals=int(raw.get("lot_rounding_decimals", 12)),
            numeric_tolerance=Decimal(str(raw.get("numeric_tolerance", 0.000000001))),
            initial_lot_sum=Decimal(str(raw.get("initial_lot_sum", 1))),
            target_dd_pct=Decimal(str(raw.get("target_dd", 5))),
            close_multiplier_long=Decimal(
                str(raw.get("close_multiplier", {}).get("long", 1.003))
            ),
            close_multiplier_short=Decimal(
                str(raw.get("close_multiplier", {}).get("short", 0.997))
            ),
            min_point_events=int(raw.get("event_filter", {}).get("min_point_events", 3)),
        )

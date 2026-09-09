"""Strict, fail-closed configuration for the research-only portfolio optimizer."""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
POLICY_VERSION = "portfolio_optimizer_research_risk_v1"
ALGORITHM_VERSIONS = MappingProxyType(
    {
        "sizing": "portfolio_optimizer_sizing_v1",
        "ranking": "portfolio_optimizer_ranking_v1",
    }
)
PROFILE_NAMES = ("AGGRESSIVE", "BALANCED", "CONSERVATIVE")
SIZING_MODE = "liquidity_cap_single"
RANKING_IDS = frozenset({"portfolio_preliminary_ranking_v1"})
INDIVIDUAL_DD_DEFAULTS = MappingProxyType(
    {
        "AGGRESSIVE": Decimal("30"),
        "BALANCED": Decimal("20"),
        "CONSERVATIVE": Decimal("15"),
    }
)
RANKING_METRICS = MappingProxyType(
    {
        "AGGRESSIVE": (
            {"field": "net_pnl", "direction": "DESC"},
            {"field": "recovery_factor", "direction": "DESC"},
            {"field": "max_dd_pct", "direction": "ASC"},
        ),
        "BALANCED": (
            {"field": "recovery_factor", "direction": "DESC"},
            {"field": "net_pnl", "direction": "DESC"},
            {"field": "max_dd_pct", "direction": "ASC"},
        ),
        "CONSERVATIVE": (
            {"field": "recovery_factor", "direction": "DESC"},
            {"field": "max_dd_pct", "direction": "ASC"},
            {"field": "net_pnl", "direction": "DESC"},
        ),
    }
)
RANKING_ID = "portfolio_preliminary_ranking_v1"
LIQUIDITY_DEFAULTS = MappingProxyType(
    {
        "close_volume_participation_pct": 30,
        "round_down_usdt": 50,
        "minimum_coverage_pct": 90,
        "maximum_age_hours": 2,
        "weekend_start_utc": "SATURDAY 00:00",
        "weekend_end_utc": "MONDAY 00:00",
        "archive_publication_lag_hours": 6,
        "backfill_write_enabled": False,
    }
)
RESEARCH_RISK_POLICY = MappingProxyType(
    {
        "AGGRESSIVE": MappingProxyType(
            {
                "max_actual_equity_dd_pct": Decimal("20"),
                "min_calculated_free_margin_reserve_pct": Decimal("20"),
                "max_calculated_account_mm_load_pct": Decimal("50"),
            }
        ),
        "BALANCED": MappingProxyType(
            {
                "max_actual_equity_dd_pct": Decimal("10"),
                "min_calculated_free_margin_reserve_pct": Decimal("40"),
                "max_calculated_account_mm_load_pct": Decimal("35"),
            }
        ),
        "CONSERVATIVE": MappingProxyType(
            {
                "max_actual_equity_dd_pct": Decimal("5"),
                "min_calculated_free_margin_reserve_pct": Decimal("60"),
                "max_calculated_account_mm_load_pct": Decimal("20"),
            }
        ),
    }
)


class PortfolioConfigError(ValueError):
    """Raised when a portfolio optimizer config cannot be trusted."""


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.amount, float) or not isinstance(self.amount, Decimal):
            raise TypeError("Money.amount requires Decimal")
        if not self.amount.is_finite():
            raise ValueError("Money.amount must be finite")


@dataclass(frozen=True, slots=True)
class Scenario:
    account: str
    deposit: Money
    collateral: Money
    max_balance: Money
    sizing_upper_bound: Money


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    max_actual_equity_dd_pct: Decimal
    min_calculated_free_margin_reserve_pct: Decimal
    max_calculated_account_mm_load_pct: Decimal
    individual_max_dd_pct: Decimal
    individual_net_pnl_min_exclusive: Decimal
    pnl: Mapping[str, Any]
    liquidity: Mapping[str, Any]
    ranking_id: str
    ranking_parameters: Mapping[str, Any]
    top_n: int


@dataclass(frozen=True, slots=True)
class PortfolioConfig:
    schema_version: int
    policy_version: str
    algorithm_versions: Mapping[str, str]
    inputs: Mapping[str, Any]
    scenarios: Mapping[str, Scenario]
    search: Mapping[str, Any]
    research: Mapping[str, Any]
    liquidity: Mapping[str, Any]
    margin: Mapping[str, Any]
    profiles: Mapping[str, Profile]
    runner: Mapping[str, Any]


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _object(value: Any, path: str, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PortfolioConfigError(f"{path} must be an object")
    allowed = set(required) | set(optional)
    unknown = sorted(set(value) - allowed)
    if unknown:
        suffix = " (risk override)" if any(item in {"risk_policy", "max_actual_equity_dd_pct", "min_calculated_free_margin_reserve_pct", "max_calculated_account_mm_load_pct", "interpolation", "override"} for item in unknown) else ""
        raise PortfolioConfigError(f"{path} has unknown field(s): {', '.join(unknown)}{suffix}")
    missing = [item for item in required if item not in value]
    if missing:
        raise PortfolioConfigError(f"{path} missing required field(s): {', '.join(missing)}")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise PortfolioConfigError(f"{path} must be a non-empty string")
    return value


def _weekday_time(value: Any, path: str) -> tuple[str, int]:
    text = _string(value, path)
    match = re.fullmatch(r"(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY) ([01][0-9]|2[0-3]):([0-5][0-9])", text)
    if match is None:
        raise PortfolioConfigError(f"{path} must be WEEKDAY HH:MM in UTC")
    weekday = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY").index(match.group(1))
    return text, weekday * 1440 + int(match.group(2)) * 60 + int(match.group(3))


def _finite_number(value: Any, path: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PortfolioConfigError(f"{path} must be finite")
    if positive and value <= 0:
        raise PortfolioConfigError(f"{path} must be positive")
    return float(value)


def _finite_decimal(value: Any, path: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (int, str, Decimal)):
        raise PortfolioConfigError(f"{path} must be a finite decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise PortfolioConfigError(f"{path} must be a finite decimal") from error
    if not number.is_finite():
        raise PortfolioConfigError(f"{path} must be a finite decimal")
    if positive and number <= 0:
        raise PortfolioConfigError(f"{path} must be positive")
    return number


def _integer(value: Any, path: str, *, positive: bool = False, nonnegative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PortfolioConfigError(f"{path} must be an integer")
    if positive and value <= 0:
        raise PortfolioConfigError(f"{path} must be positive")
    if nonnegative and value < 0:
        raise PortfolioConfigError(f"{path} must be non-negative")
    return value


def _money(value: Any, path: str) -> Money:
    raw = _object(value, path, ("amount", "currency"))
    amount = _finite_decimal(raw["amount"], f"{path}.amount", positive=True)
    return Money(amount, _string(raw["currency"], f"{path}.currency"))


def _unit_number(value: Any, path: str, units: frozenset[str], *, positive: bool = False, nonnegative: bool = False) -> Mapping[str, Any]:
    raw = _object(value, path, ("value", "unit"))
    unit = _string(raw["unit"], f"{path}.unit")
    if unit not in units:
        raise PortfolioConfigError(f"{path}.unit is invalid")
    number = _finite_number(raw["value"], f"{path}.value", positive=positive)
    if nonnegative and number < 0:
        raise PortfolioConfigError(f"{path}.value must be non-negative")
    return MappingProxyType({"value": number, "unit": unit})


def _descriptor(value: Any, path: str, allowed_ids: frozenset[str] | None = None) -> Mapping[str, Any]:
    raw = _object(value, path, ("policy_id", "parameters"))
    policy_id = _string(raw["policy_id"], f"{path}.policy_id")
    if allowed_ids is not None and policy_id not in allowed_ids:
        raise PortfolioConfigError(f"{path}.policy_id is unknown")
    parameters = raw["parameters"]
    if not isinstance(parameters, dict):
        raise PortfolioConfigError(f"{path}.parameters must be an object")
    if not parameters:
        raise PortfolioConfigError(f"{path}.parameters must be explicit")
    if set(parameters) & {"override", "per_run_override", "interpolation", "blend"}:
        raise PortfolioConfigError(f"{path}.parameters contains a forbidden override")
    return _freeze({"policy_id": policy_id, "parameters": parameters})


def _parse_scenario(value: Any, name: str) -> Scenario:
    raw = _object(value, f"scenarios.{name}", ("account", "deposit", "collateral", "max_balance", "sizing"))
    account = _string(raw["account"], f"scenarios.{name}.account")
    deposit = _money(raw["deposit"], f"scenarios.{name}.deposit")
    collateral = _money(raw["collateral"], f"scenarios.{name}.collateral")
    max_balance = _money(raw["max_balance"], f"scenarios.{name}.max_balance")
    sizing = _object(raw["sizing"], f"scenarios.{name}.sizing", ("upper_bound",))
    upper_bound = _money(sizing["upper_bound"], f"scenarios.{name}.sizing.upper_bound")
    currencies = {deposit.currency, collateral.currency, max_balance.currency, upper_bound.currency}
    if len(currencies) != 1:
        raise PortfolioConfigError(f"scenarios.{name} money currencies must match")
    return Scenario(account, deposit, collateral, max_balance, upper_bound)


def _ranking_metrics(value: Any, path: str, profile: str) -> tuple[Mapping[str, str], ...]:
    if not isinstance(value, list) or value != list(RANKING_METRICS[profile]):
        raise PortfolioConfigError(f"{path}.metrics must be the concrete {profile} preliminary ranking")
    metrics: list[Mapping[str, str]] = []
    for index, item in enumerate(value):
        raw = _object(item, f"{path}.metrics[{index}]", ("field", "direction"))
        field = _string(raw["field"], f"{path}.metrics[{index}].field")
        direction = _string(raw["direction"], f"{path}.metrics[{index}].direction")
        if direction not in {"ASC", "DESC"}:
            raise PortfolioConfigError(f"{path}.metrics[{index}].direction is invalid")
        metrics.append(MappingProxyType({"field": field, "direction": direction}))
    return tuple(metrics)


def _parse_profiles(value: Any) -> Mapping[str, Profile]:
    if not isinstance(value, dict) or set(value) != set(PROFILE_NAMES):
        raise PortfolioConfigError("profiles must contain exactly AGGRESSIVE, BALANCED, CONSERVATIVE")
    profiles: dict[str, Profile] = {}
    for name in PROFILE_NAMES:
        raw = _object(
            value[name],
            f"profiles.{name}",
            ("pnl", "liquidity", "ranking", "individual_max_dd_pct", "individual_net_pnl_min_exclusive"),
        )
        pnl = _descriptor(raw["pnl"], f"profiles.{name}.pnl")
        liquidity = _descriptor(raw["liquidity"], f"profiles.{name}.liquidity")
        ranking = _object(raw["ranking"], f"profiles.{name}.ranking", ("id", "parameters", "top_n"))
        ranking_id = _string(ranking["id"], f"profiles.{name}.ranking.id")
        if ranking_id not in RANKING_IDS:
            raise PortfolioConfigError(f"profiles.{name}.ranking id is unknown")
        parameters = ranking["parameters"]
        if not isinstance(parameters, dict) or not parameters:
            raise PortfolioConfigError(f"profiles.{name}.ranking.parameters must be explicit")
        _ranking_metrics(parameters.get("metrics"), f"profiles.{name}.ranking.parameters", name)
        top_n = _integer(ranking["top_n"], f"profiles.{name}.ranking.top_n", positive=True)
        risk = RESEARCH_RISK_POLICY[name]
        dd = risk["max_actual_equity_dd_pct"]
        reserve = risk["min_calculated_free_margin_reserve_pct"]
        mm = risk["max_calculated_account_mm_load_pct"]
        individual_dd = _finite_decimal(raw["individual_max_dd_pct"], f"profiles.{name}.individual_max_dd_pct", positive=True)
        individual_pnl = _finite_decimal(raw["individual_net_pnl_min_exclusive"], f"profiles.{name}.individual_net_pnl_min_exclusive")
        profiles[name] = Profile(name, dd, reserve, mm, individual_dd, individual_pnl, pnl, liquidity, ranking_id, _freeze(parameters), top_n)
    return MappingProxyType(profiles)


def _parse_inputs(value: Any) -> Mapping[str, Any]:
    raw = _object(value, "inputs", ("performance_db", "portfolio_db", "collector_root", "approved_templates", "bybit_minute_data_root"))
    result = {key: _string(raw[key], f"inputs.{key}") for key in ("performance_db", "portfolio_db", "collector_root")}
    result["bybit_minute_data_root"] = _string(raw["bybit_minute_data_root"], "inputs.bybit_minute_data_root")
    templates = raw["approved_templates"]
    if not isinstance(templates, list) or not templates:
        raise PortfolioConfigError("inputs.approved_templates must be nonempty")
    result["approved_templates"] = tuple(_string(item, "inputs.approved_templates[]") for item in templates)
    return MappingProxyType(result)


def _parse_liquidity(value: Any) -> Mapping[str, Any]:
    raw = _object(
        value,
        "liquidity",
        (
            "policy_id", "parameters", "round_down_usdt", "minimum_coverage_pct", "maximum_age_hours",
            "weekend_start_utc", "weekend_end_utc", "archive_publication_lag_hours", "backfill_write_enabled",
        ),
    )
    descriptor = _descriptor({"policy_id": raw["policy_id"], "parameters": raw["parameters"]}, "liquidity")
    parameters = dict(descriptor["parameters"])
    participation = _integer(parameters.get("close_volume_participation_pct"), "liquidity.parameters.close_volume_participation_pct")
    if not 1 <= participation <= 200:
        raise PortfolioConfigError("liquidity.parameters.close_volume_participation_pct must be between 1 and 200")
    result: dict[str, Any] = dict(descriptor)
    result["parameters"] = MappingProxyType(parameters)
    result["round_down_usdt"] = _finite_decimal(raw["round_down_usdt"], "liquidity.round_down_usdt", positive=True)
    coverage = _integer(raw["minimum_coverage_pct"], "liquidity.minimum_coverage_pct")
    if not 1 <= coverage <= 100:
        raise PortfolioConfigError("liquidity.minimum_coverage_pct must be between 1 and 100")
    result["minimum_coverage_pct"] = coverage
    result["maximum_age_hours"] = _integer(raw["maximum_age_hours"], "liquidity.maximum_age_hours", positive=True)
    result["weekend_start_utc"], start_minutes = _weekday_time(raw["weekend_start_utc"], "liquidity.weekend_start_utc")
    result["weekend_end_utc"], end_minutes = _weekday_time(raw["weekend_end_utc"], "liquidity.weekend_end_utc")
    if (end_minutes - start_minutes) % (7 * 1440) == 0:
        raise PortfolioConfigError("liquidity weekend interval must be non-empty")
    lag = _integer(raw["archive_publication_lag_hours"], "liquidity.archive_publication_lag_hours", nonnegative=True)
    if lag > 48:
        raise PortfolioConfigError("liquidity.archive_publication_lag_hours must be between 0 and 48")
    result["archive_publication_lag_hours"] = lag
    if not isinstance(raw["backfill_write_enabled"], bool):
        raise PortfolioConfigError("liquidity.backfill_write_enabled must be a boolean")
    result["backfill_write_enabled"] = raw["backfill_write_enabled"]
    return MappingProxyType(result)


def _parse_groups(raw: dict[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    search_raw = _object(raw["search"], "search", ("universe", "composition", "sizing", "limiter", "priority", "seed", "rounds", "total_test_budget", "sizing_mode", "max_enumerated_combinations"))
    search = {key: _descriptor(search_raw[key], f"search.{key}") for key in ("universe", "composition", "sizing", "limiter", "priority")}
    search.update({key: _integer(search_raw[key], f"search.{key}", positive=True if key != "seed" else False, nonnegative=key == "seed") for key in ("seed", "rounds", "total_test_budget")})
    if search_raw["sizing_mode"] != SIZING_MODE:
        raise PortfolioConfigError("search.sizing_mode is unsupported")
    search["sizing_mode"] = SIZING_MODE
    search["max_enumerated_combinations"] = _integer(search_raw["max_enumerated_combinations"], "search.max_enumerated_combinations", positive=True)

    research_raw = _object(raw["research"], "research", ("development_window", "validation_window", "warmup", "boundary", "evidence_minimum"))
    research = {
        "development_window": _unit_number(research_raw["development_window"], "research.development_window", frozenset({"seconds", "minutes", "hours", "days"}), positive=True),
        "validation_window": _unit_number(research_raw["validation_window"], "research.validation_window", frozenset({"seconds", "minutes", "hours", "days"}), positive=True),
        "warmup": _unit_number(research_raw["warmup"], "research.warmup", frozenset({"seconds", "minutes", "hours", "days"}), nonnegative=True),
        "boundary": _string(research_raw["boundary"], "research.boundary"),
        "evidence_minimum": _unit_number(research_raw["evidence_minimum"], "research.evidence_minimum", frozenset({"count"}), positive=True),
    }

    liquidity = _parse_liquidity(raw["liquidity"])
    margin = _descriptor(raw["margin"], "margin")

    runner_raw = _object(raw["runner"], "runner", ("target", "root", "timeout", "retries"))
    target = _string(runner_raw["target"], "runner.target")
    if target not in {"local", "provided_remote"}:
        raise PortfolioConfigError("runner.target is invalid")
    runner = {
        "target": target,
        "root": _string(runner_raw["root"], "runner.root"),
        "timeout": _unit_number(runner_raw["timeout"], "runner.timeout", frozenset({"seconds", "minutes"}), positive=True),
        "retries": _integer(runner_raw["retries"], "runner.retries", nonnegative=True),
    }
    return tuple(MappingProxyType(item) for item in (search, research, liquidity, margin, runner))


def _reject_json_constant(value: str) -> None:
    raise PortfolioConfigError(f"JSON number {value} is not finite")


def migrate_portfolio_config_document(document: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return a deterministic v2 copy; v1 monetary sizing grids are discarded."""
    if not isinstance(document, Mapping):
        raise PortfolioConfigError("config must be an object")
    if document.get("schema_version") == SCHEMA_VERSION:
        return deepcopy(dict(document)), False
    if document.get("schema_version") != LEGACY_SCHEMA_VERSION:
        return deepcopy(dict(document)), False
    migrated = deepcopy(dict(document))
    migrated["schema_version"] = SCHEMA_VERSION
    runner = migrated.get("runner") if isinstance(migrated.get("runner"), dict) else {}
    inputs = migrated.get("inputs") if isinstance(migrated.get("inputs"), dict) else {}
    inputs.setdefault("bybit_minute_data_root", f"{runner.get('root', '.')}/tester/data/bybit")
    migrated["inputs"] = inputs
    search = migrated.get("search") if isinstance(migrated.get("search"), dict) else {}
    search.setdefault("sizing_mode", SIZING_MODE)
    search.setdefault("max_enumerated_combinations", 100000)
    migrated["search"] = search
    liquidity = migrated.get("liquidity") if isinstance(migrated.get("liquidity"), dict) else {}
    parameters = liquidity.get("parameters") if isinstance(liquidity.get("parameters"), dict) else {}
    parameters.setdefault("close_volume_participation_pct", LIQUIDITY_DEFAULTS["close_volume_participation_pct"])
    liquidity["parameters"] = parameters
    for key, default in LIQUIDITY_DEFAULTS.items():
        if key != "close_volume_participation_pct":
            liquidity.setdefault(key, default)
    migrated["liquidity"] = liquidity
    scenarios = migrated.get("scenarios") if isinstance(migrated.get("scenarios"), dict) else {}
    for scenario in scenarios.values():
        if isinstance(scenario, dict) and isinstance(scenario.get("sizing"), dict):
            scenario["sizing"].pop("grid", None)
    profiles = migrated.get("profiles") if isinstance(migrated.get("profiles"), dict) else {}
    for name in PROFILE_NAMES:
        profile = profiles.get(name)
        if not isinstance(profile, dict):
            continue
        profile.setdefault("individual_max_dd_pct", str(INDIVIDUAL_DD_DEFAULTS[name]))
        profile.setdefault("individual_net_pnl_min_exclusive", "0")
        ranking = profile.get("ranking") if isinstance(profile.get("ranking"), dict) else {}
        parameters = ranking.get("parameters") if isinstance(ranking.get("parameters"), dict) else {}
        parameters["metrics"] = [dict(item) for item in RANKING_METRICS[name]]
        ranking["parameters"] = parameters
        ranking["id"] = RANKING_ID
        profile["ranking"] = ranking
    migrated["profiles"] = profiles
    return migrated, True


def load_portfolio_config(path: str | Path = "portfolio_optimizer.local.json") -> PortfolioConfig:
    """Load and validate a complete portfolio optimizer config once."""
    config_path = Path(path)
    if not config_path.is_file():
        raise PortfolioConfigError(f"required config file is missing: {config_path}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
    except PortfolioConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PortfolioConfigError(f"cannot read config {config_path}: {exc}") from exc
    raw, _ = migrate_portfolio_config_document(raw)
    top = _object(raw, "config", ("schema_version", "policy_version", "algorithm_versions", "inputs", "scenarios", "search", "research", "liquidity", "margin", "profiles", "runner"))
    if top["schema_version"] != SCHEMA_VERSION or isinstance(top["schema_version"], bool):
        raise PortfolioConfigError("schema_version is unsupported")
    if top["policy_version"] != POLICY_VERSION:
        raise PortfolioConfigError("policy_version is unsupported")
    algorithm_versions = _object(top["algorithm_versions"], "algorithm_versions", tuple(ALGORITHM_VERSIONS))
    if algorithm_versions != dict(ALGORITHM_VERSIONS):
        raise PortfolioConfigError("algorithm_versions are unsupported")
    scenarios_raw = top["scenarios"]
    if not isinstance(scenarios_raw, dict) or not scenarios_raw:
        raise PortfolioConfigError("scenarios must be a nonempty object")
    scenarios = MappingProxyType({
        _string(name, "scenarios key"): _parse_scenario(value, name)
        for name, value in scenarios_raw.items()
    })
    search, research, liquidity, margin, runner = _parse_groups(top)
    return PortfolioConfig(
        SCHEMA_VERSION,
        POLICY_VERSION,
        MappingProxyType(dict(ALGORITHM_VERSIONS)),
        _parse_inputs(top["inputs"]),
        scenarios,
        search,
        research,
        liquidity,
        margin,
        _parse_profiles(top["profiles"]),
        runner,
    )


load = load_portfolio_config
load_config = load_portfolio_config

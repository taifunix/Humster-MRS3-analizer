"""Pure adapter from frozen Panel facts to sized multi-pair candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..config import AlgorithmConfig
from ..lots import LotMethod
from ..strategy_json import generate_strategy
from . import minute_capacity
from .config import RESEARCH_RISK_POLICY
from .canonical import CanonicalEnvelope, canonical_digest_v1, typed_value
from .liquidity import ReferenceSnapshot
from .margin import derive_reference_margin_coefficients
from .market_snapshot import ApiRateLimiter, MarketSnapshotError, load_market_snapshot
from .minute_capacity import MinuteCapacityError, MinuteCapacityResult, backfill_missing_days, calculate_minute_capacity
from .spread_screen import read_spread_history


CAMPAIGN_CONTRACT_VERSION = "PORTFOLIO_WEIGHTED_CAMPAIGN_V1"
CAMPAIGN_SEARCH_MODE = "WEIGHTED_V1"
CAMPAIGN_LEGACY_SEARCH_MODE = "PRETEST_PROXY"
CAMPAIGN_WEIGHTED_ALGO_VERSION = "WS1.1"
WEIGHTED_SEARCH_NOT_IMPLEMENTED = "WEIGHTED_SEARCH_NOT_IMPLEMENTED"
WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE = "WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE"
WEIGHTED_INPUT_PREPARATION_FAILED = "WEIGHTED_INPUT_PREPARATION_FAILED"
MARGIN_BOUND_UNAVAILABLE = "MARGIN_BOUND_UNAVAILABLE"
MINUTE_CAPACITY_UNAVAILABLE = "MINUTE_CAPACITY_UNAVAILABLE"
MARKET_SNAPSHOT_UNAVAILABLE = "MARKET_SNAPSHOT_UNAVAILABLE"
ADAPTER_FACTS_UNAVAILABLE = "ADAPTER_FACTS_UNAVAILABLE"
ADAPTER_BUILD_FAILED = "ADAPTER_BUILD_FAILED"
WEIGHTED_EXECUTABLE_IDENTITY_COLLISION = "WEIGHTED_EXECUTABLE_IDENTITY_COLLISION"
_WEIGHTED_SEARCH_CONFIG_INVALID = "WEIGHTED_SEARCH_CONFIG_INVALID"


class CampaignContractError(ValueError):
    """A stable fail-closed Campaign contract error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_WEIGHTED_PAYLOAD_ERROR = "WEIGHTED_PAYLOAD_INVALID"
_WEIGHTED_RELATIVE_TOLERANCE = Decimal("1e-12")
_WEIGHTED_MAX_ADJUSTED_EXPONENT = 38  # Keep persisted money values bounded before fixed-point formatting.
_RAW_SERIES_KEYS = frozenset({"actions", "action_series", "minute_actions", "equity", "equity_series"})
_STRATEGY_DROPPED_KEYS = _RAW_SERIES_KEYS | {"raw", "open_positions_limiter", "k", "size_composition_vector"}


def weighted_search(*args: Any, **kwargs: Any) -> Any:
    """Lazy adapter seam for the weighted search implementation."""
    from .weighted_search import weighted_search as implementation

    return implementation(*args, **kwargs)


def enrich_finalist_rows(*args: Any, **kwargs: Any) -> Any:
    """Lazy adapter seam for finalist enrichment."""
    from .position_sizing import enrich_finalist_rows as implementation

    return implementation(*args, **kwargs)


def _weighted_decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    try:
        number = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError, OverflowError) as error:
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR) from error
    if not number.is_finite():
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    if abs(number.adjusted()) > _WEIGHTED_MAX_ADJUSTED_EXPONENT:
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    return number


def _weighted_decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _weighted_json_number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _weighted_close(left: Decimal, right: Decimal) -> bool:
    scale = max(Decimal("1"), abs(left), abs(right))
    return abs(left - right) <= scale * _WEIGHTED_RELATIVE_TOLERANCE


def _copy_without_raw_series(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _copy_without_raw_series(item)
            for key, item in value.items()
            if key not in _STRATEGY_DROPPED_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_copy_without_raw_series(item) for item in value]
    return value


def _copy_candidate_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy_candidate_fields(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_copy_candidate_fields(item) for item in value)
    if isinstance(value, list):
        return [_copy_candidate_fields(item) for item in value]
    return value


def _weighted_geometry_readback(
    strategy: Mapping[str, Any],
    bank: Decimal,
    x: Decimal,
) -> Mapping[str, str] | None:
    mrs3 = strategy.get("mrs3")
    if not isinstance(mrs3, Mapping) or "order_geometry" not in mrs3:
        return None
    geometry = mrs3["order_geometry"]
    if not isinstance(geometry, Mapping):
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    entries = geometry.get("entry")
    explicit = "sizing_base" in geometry
    if not explicit and isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
        explicit = any(isinstance(entry, Mapping) and "qty_step" in entry for entry in entries)
    if not explicit:
        return None
    if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence) or not entries:
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    if "sizing_base" not in geometry:
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    sizing_base = _weighted_decimal(geometry.get("sizing_base"))
    basic = strategy.get("basic")
    if not isinstance(basic, Mapping) or "initial_balance" not in basic:
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    initial_balance = _weighted_decimal(basic["initial_balance"])
    if sizing_base != bank or initial_balance != bank:
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    full_position = Decimal("0")
    epsilon = Decimal("0")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
        price = _weighted_decimal(entry.get("price"))
        quantity = _weighted_decimal(entry.get("qty"))
        qty_step = _weighted_decimal(entry.get("qty_step"))
        if price <= 0 or quantity <= 0 or qty_step <= 0:
            raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
        if quantity % qty_step != 0:
            raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
        full_position += price * quantity
        epsilon += price * qty_step
    if abs(full_position - x) > epsilon:
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    return {
        "status": "PASS",
        "A0": _weighted_decimal_text(initial_balance),
        "sizing_base": _weighted_decimal_text(sizing_base),
        "expected_full_position_usdt": _weighted_decimal_text(x),
        "full_position_usdt": _weighted_decimal_text(full_position),
        "epsilon_usdt": _weighted_decimal_text(epsilon),
    }


def build_weighted_strategy_payload(
    template: Mapping[str, Any],
    member: Mapping[str, Any],
    bank_usdt: Any,
    capacity_usdt: Any,
    open_positions_limiter: Any,
) -> Mapping[str, Any]:
    """Build one JSON-safe strategy payload from a weighted member.

    This is deliberately a pure template adapter.  It does not invoke legacy
    sizing/export code and keeps the account limiter outside strategy settings.
    The weighted contract permits x_usdt > bank_usdt; it has no sum(x)=B constraint.
    """
    if not isinstance(template, Mapping) or not isinstance(member, Mapping):
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)

    strategy = _copy_without_raw_series(template)
    if not isinstance(strategy, dict):
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    exchange = strategy.get("exchange")
    basic = strategy.get("basic")
    mrs = strategy.get("mrs")
    if not isinstance(exchange, Mapping) or not isinstance(basic, Mapping) or not isinstance(mrs, Mapping):
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    if exchange.get("use_upnl") is not True or exchange.get("use_frozen_balance") is not True:
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    if basic.get("use_fix") is not False or basic.get("use_long") is not True or basic.get("use_short") is not False:
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    if _weighted_decimal(basic.get("balance_percentage_long")) != Decimal("100"):
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    if _weighted_decimal(basic.get("risk_long")) != Decimal("1"):
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    if _weighted_decimal(basic.get("max_balance")) != Decimal("0"):
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    symbol = member.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    if member.get("side") != "LONG":
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    priority = member.get("priority")
    if type(priority) is not int or not 1 <= priority <= 5:
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    if type(open_positions_limiter) is not int or open_positions_limiter < 0:
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)

    x = _weighted_decimal(member.get("x_usdt"))
    bank = _weighted_decimal(bank_usdt)
    capacity = _weighted_decimal(capacity_usdt)
    if x <= 0 or bank <= 0 or capacity <= 0:
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    try:
        q = x / bank
        max_balance = (capacity * bank) / x
        balance_percentage = Decimal("100") * q
    except (ArithmeticError, TypeError, ValueError, OverflowError) as error:
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR) from error
    if not q.is_finite() or not max_balance.is_finite() or not balance_percentage.is_finite():
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    _weighted_decimal(q)
    _weighted_decimal(max_balance)
    _weighted_decimal(balance_percentage)
    json_balance = _weighted_json_number(balance_percentage)
    json_max_balance = _weighted_json_number(max_balance)

    strategy["basic"] = dict(basic)
    strategy["basic"]["symbol"] = symbol
    strategy["basic"]["balance_percentage_long"] = json_balance
    strategy["basic"]["max_balance"] = json_max_balance
    strategy["mrs"] = dict(mrs)
    strategy["mrs"]["position_priority"] = priority
    payload = {
        "strategy": strategy,
        "account": {"open_positions_limiter": open_positions_limiter},
        "facts": {
            "B": _weighted_decimal_text(bank),
            "C": _weighted_decimal_text(capacity),
            "q": _weighted_decimal_text(q),
            "x": _weighted_decimal_text(x),
        },
    }
    try:
        typed_payload = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        read_facts = typed_payload["facts"]
        read_bank = _weighted_decimal(read_facts["B"])
        read_capacity = _weighted_decimal(read_facts["C"])
        read_x = _weighted_decimal(read_facts["x"])
        read_q = _weighted_decimal(read_facts["q"])
        read_basic = typed_payload["strategy"]["basic"]
        read_mrs = typed_payload["strategy"]["mrs"]
        recomputed_q = read_x / read_bank
        recomputed_balance = Decimal("100") * read_q
        recomputed_max_balance = (read_capacity * read_bank) / read_x
        read_balance = _weighted_decimal(read_basic["balance_percentage_long"])
        read_max_balance = _weighted_decimal(read_basic["max_balance"])
        expected_balance = _weighted_json_number(recomputed_balance)
        expected_max_balance = _weighted_json_number(recomputed_max_balance)
        if (
            read_q != recomputed_q
            or typed_payload["strategy"]["basic"]["balance_percentage_long"] != expected_balance
            or typed_payload["strategy"]["basic"]["max_balance"] != expected_max_balance
            or _weighted_decimal(read_basic["risk_long"]) != Decimal("1")
            or read_mrs["position_priority"] != priority
            or "position_priority" in read_basic
            or not _weighted_close(read_max_balance * read_q, read_capacity)
            or typed_payload["account"]["open_positions_limiter"] != open_positions_limiter
        ):
            raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
        readback = _weighted_geometry_readback(typed_payload["strategy"], read_bank, read_x)
        if readback is not None:
            typed_payload["readback"] = readback
    except CampaignContractError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError, ArithmeticError) as error:
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR) from error
    return typed_payload


def _weighted_input_identity(row: Any) -> tuple[str, str, int, int]:
    if not isinstance(row, Mapping):
        raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
    fields = ("symbol", "side", "strategy_id", "result_id")
    if any(field not in row for field in fields):
        raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
    symbol, side = row["symbol"], row["side"]
    strategy_id, result_id = row["strategy_id"], row["result_id"]
    if (
        not isinstance(symbol, str) or not symbol.strip()
        or side != "LONG"
        or type(strategy_id) is not int
        or type(result_id) is not int
    ):
        raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
    return symbol, side, strategy_id, result_id


def _weighted_geometry_int(value: Any, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
    return value


def _prepare_frozen_weighted_input(
    selected_rows: Sequence[Mapping[str, Any]],
    campaign: Mapping[str, Any],
) -> Any:
    """Prepare weighted input strictly from the Campaign's frozen rows."""
    if (
        not isinstance(selected_rows, Sequence)
        or isinstance(selected_rows, (str, bytes))
        or not isinstance(campaign, Mapping)
        or not selected_rows
    ):
        raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
    frozen_rows = campaign.get("weighted_input_rows")
    if not isinstance(frozen_rows, Sequence) or isinstance(frozen_rows, (str, bytes)):
        raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)

    by_identity: dict[tuple[str, str, int, int], Mapping[str, Any]] = {}
    for row in frozen_rows:
        identity = _weighted_input_identity(row)
        if identity in by_identity:
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
        if (
            not isinstance(row.get("actions"), Sequence)
            or isinstance(row.get("actions"), (str, bytes))
            or not isinstance(row.get("equity"), Sequence)
            or isinstance(row.get("equity"), (str, bytes))
        ):
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
        by_identity[identity] = row

    selected_identities: list[tuple[str, str, int, int]] = []
    for row in selected_rows:
        identity = _weighted_input_identity(row)
        if identity in selected_identities or identity not in by_identity:
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
        selected_identities.append(identity)

    config_document = campaign.get("config_document")
    if "history_step_minutes" in campaign or "minimum_common_days" in campaign:
        raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
    if isinstance(config_document, Mapping) and (
        "history_step_minutes" in config_document or "minimum_common_days" in config_document
    ):
        raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
    search = config_document.get("search") if isinstance(config_document, Mapping) else None
    weighted_search = search.get("weighted_search") if isinstance(search, Mapping) else None
    composition = search.get("composition") if isinstance(search, Mapping) else None
    composition_parameters = composition.get("parameters") if isinstance(composition, Mapping) else None
    if not isinstance(weighted_search, Mapping) or not isinstance(composition_parameters, Mapping):
        raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
    if (
        "minimum_common_days" in weighted_search
        or "history_step_minutes" in composition_parameters
        or "history_step_minutes" in search
        or "minimum_common_days" in search
        or "history_step_minutes" in composition
        or "minimum_common_days" in composition
    ):
        raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
    history_step_minutes = weighted_search.get("history_step_minutes")
    minimum_common_days = composition_parameters.get("minimum_common_days")
    if (
        type(history_step_minutes) is not int
        or history_step_minutes <= 0
        or type(minimum_common_days) is not int
        or minimum_common_days <= 0
    ):
        raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)

    prepared_rows = tuple(_freeze(dict(by_identity[identity])) for identity in selected_identities)
    from .input import prepare_weighted_input

    try:
        return prepare_weighted_input(
            prepared_rows,
            history_step_minutes=history_step_minutes,
            minimum_common_days=minimum_common_days,
        )
    except Exception as error:
        raise CampaignContractError(WEIGHTED_INPUT_PREPARATION_FAILED) from error


def validate_campaign_contract(campaign: Any) -> None:
    """Validate the exact weighted Campaign envelope before any I/O."""
    if not isinstance(campaign, Mapping):
        raise CampaignContractError("CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED")
    versions = campaign.get("versions")
    if "stage1_mode" in campaign or (isinstance(versions, Mapping) and "stage1_mode" in versions):
        raise CampaignContractError("CAMPAIGN_LEGACY_STAGE1_MODE_UNSUPPORTED")

    contract_version = campaign.get("campaign_contract_version")
    if not isinstance(contract_version, str) or not contract_version or contract_version != CAMPAIGN_CONTRACT_VERSION:
        raise CampaignContractError("CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED")

    search_mode = campaign.get("search_mode")
    if not isinstance(search_mode, str) or not search_mode:
        raise CampaignContractError("CAMPAIGN_SEARCH_MODE_REQUIRED")
    if search_mode == CAMPAIGN_LEGACY_SEARCH_MODE:
        raise CampaignContractError("CAMPAIGN_LEGACY_SEARCH_MODE_UNSUPPORTED")
    if search_mode != CAMPAIGN_SEARCH_MODE:
        raise CampaignContractError("CAMPAIGN_SEARCH_MODE_UNSUPPORTED")

    algo_version = campaign.get("weighted_algo_version")
    if not isinstance(algo_version, str) or not algo_version:
        raise CampaignContractError("CAMPAIGN_WEIGHTED_ALGO_VERSION_REQUIRED")
    if algo_version != CAMPAIGN_WEIGHTED_ALGO_VERSION:
        raise CampaignContractError("CAMPAIGN_WEIGHTED_ALGO_VERSION_UNSUPPORTED")

    if (
        not isinstance(versions, Mapping)
        or any(
            versions.get(key) != value
            for key, value in (
                ("campaign_contract_version", contract_version),
                ("search_mode", search_mode),
                ("weighted_algo_version", algo_version),
            )
        )
    ):
        raise CampaignContractError("CAMPAIGN_VERSIONS_MISMATCH")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class AdapterResult:
    status: str
    variants: tuple[Mapping[str, Any], ...] = ()
    excluded: tuple[Mapping[str, Any], ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "variants", tuple(_freeze(item) for item in self.variants))
        object.__setattr__(self, "excluded", tuple(_freeze(item) for item in self.excluded))
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(self.blockers)))
        object.__setattr__(self, "warnings", tuple(dict.fromkeys(self.warnings)))

def _adapter_gate(campaign: Any) -> AdapterResult:
    try:
        validate_campaign_contract(campaign)
    except CampaignContractError as error:
        return AdapterResult("FAIL", blockers=(error.code,))
    return AdapterResult("FAIL", blockers=(WEIGHTED_SEARCH_NOT_IMPLEMENTED,))


def _run_weighted_search(
    prepared: Any,
    members: Sequence[Mapping[str, Any]],
    campaign: Mapping[str, Any],
    launch_profile: Mapping[str, Any],
    margin_coefficients: Any,
    *,
    workers: int,
) -> Any:
    """Call weighted search once with one launch profile and frozen capacities."""
    try:
        if prepared is None:
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
        if margin_coefficients is None:
            raise CampaignContractError(MARGIN_BOUND_UNAVAILABLE)
        if not isinstance(campaign, Mapping) or not isinstance(launch_profile, Mapping):
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        config_document = campaign.get("config_document")
        if not isinstance(config_document, Mapping):
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        search = config_document.get("search")
        if not isinstance(search, Mapping):
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        settings = search.get("weighted_search")
        if not isinstance(settings, Mapping):
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        required_settings = (
            "max_targets", "bootstrap_scenarios_per_block", "bootstrap_diagnostic_scenarios",
            "wall_time_seconds", "solver_time_seconds",
        )
        if "seed" not in search or any(key not in settings for key in required_settings):
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        if (
            type(search["seed"]) is not int or search["seed"] < 0
            or any(type(settings[key]) is not int or settings[key] <= 0 for key in required_settings)
            or not 1 <= settings["max_targets"] <= 8
            or settings["bootstrap_diagnostic_scenarios"] > settings["bootstrap_scenarios_per_block"]
        ):
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        if (
            isinstance(members, (str, bytes)) or not isinstance(members, Sequence)
            or not members
            or any(not isinstance(member, Mapping) for member in members)
            or type(workers) is not int or workers <= 0
        ):
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        capacities = {}
        for member in members:
            strategy_id = member.get("strategy_id")
            position_size = member.get("position_size_usdt")
            if not isinstance(position_size, Decimal) or not position_size.is_finite() or position_size <= 0:
                raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
            if type(strategy_id) is not int or strategy_id <= 0:
                raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
            if strategy_id in capacities:
                raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
            capacities[strategy_id] = position_size
        if not isinstance(margin_coefficients, Mapping) or not margin_coefficients:
            raise CampaignContractError(MARGIN_BOUND_UNAVAILABLE)
        try:
            margin_covers_members = all(strategy_id in margin_coefficients for strategy_id in capacities)
            from .weighted_search import _public_margin_coefficients_are_known

            margin_known = _public_margin_coefficients_are_known(margin_coefficients)
        except (ImportError, AttributeError, KeyError, TypeError, ValueError, ArithmeticError) as error:
            raise CampaignContractError(MARGIN_BOUND_UNAVAILABLE) from error
        if not margin_covers_members or not margin_known:
            raise CampaignContractError(MARGIN_BOUND_UNAVAILABLE)
        profile_id = launch_profile.get("profile_id")
        scenario_id = launch_profile.get("scenario_id")
        equity_usdt = launch_profile.get("equity_usdt")
        if not isinstance(profile_id, str) or not profile_id or profile_id not in RESEARCH_RISK_POLICY:
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        if not isinstance(scenario_id, str) or not scenario_id:
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        if not isinstance(equity_usdt, Decimal) or not equity_usdt.is_finite() or equity_usdt <= 0:
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        if "max_candidates" in launch_profile and (
            type(launch_profile["max_candidates"]) is not int
            or not 1 <= launch_profile["max_candidates"] <= 50
        ):
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        policy = RESEARCH_RISK_POLICY[profile_id]
        max_dd = policy["max_actual_equity_dd_pct"] / Decimal("100")
        margin_kwargs = {
            "reserve": policy["min_calculated_free_margin_reserve_pct"] / Decimal("100"),
            "max_mm_load": policy["max_calculated_account_mm_load_pct"] / Decimal("100"),
        }
        kwargs = {
            "members": members,
            "bank_available": equity_usdt,
            "profile_id": profile_id,
            "scenario_id": scenario_id,
            "margin_coefficients": margin_coefficients,
            "max_dd": max_dd,
            "margin_kwargs": margin_kwargs,
            "max_targets": settings["max_targets"],
            "seed": search["seed"],
            "bootstrap_scenarios": settings["bootstrap_scenarios_per_block"],
            "screening_scenarios": settings["bootstrap_diagnostic_scenarios"],
            "workers": workers,
            "wall_time": settings["wall_time_seconds"],
            "solver_time": settings["solver_time_seconds"],
        }
        if "max_candidates" in launch_profile:
            kwargs["max_candidates"] = launch_profile["max_candidates"]
    except CampaignContractError:
        raise
    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
        raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID) from error
    return weighted_search(prepared, capacities, **kwargs)


def _normalise_scalar_exclusion(item: Any) -> Mapping[str, Any]:
    row = getattr(item, "row", None)
    if not isinstance(row, Mapping):
        row = item if isinstance(item, Mapping) else {}
    result: dict[str, Any] = {
        "strategy_id": row.get("strategy_id", getattr(item, "strategy_id", None)),
        "result_id": row.get("result_id", getattr(item, "result_id", None)),
        "symbol": row.get("symbol", getattr(item, "symbol", "")),
        "side": row.get("side", getattr(item, "side", "")),
        "reason": row.get("reason", getattr(item, "reason", "UNKNOWN")),
    }
    status = getattr(item, "status", None)
    if status is not None:
        result["status"] = status
    return result


def _safe_search_warnings(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
    warnings = tuple(value)
    if any(not isinstance(warning, str) or not warning for warning in warnings):
        raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
    return warnings


def _assert_no_raw_series(value: Any) -> None:
    if isinstance(value, Mapping):
        if any(key in _RAW_SERIES_KEYS or key == "raw" for key in value):
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        for item in value.values():
            _assert_no_raw_series(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_raw_series(item)


def _safe_weighted_variant(candidate: Any, result: Any) -> Mapping[str, Any]:
    members = getattr(candidate, "members", None)
    metrics = getattr(candidate, "metrics", None)
    if isinstance(members, (str, bytes)) or not isinstance(members, Sequence) or not isinstance(metrics, Mapping):
        raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
    for member in members:
        _assert_no_raw_series(member)
    _assert_no_raw_series(metrics)
    members = tuple(_copy_candidate_fields(member) for member in members)
    if not members:
        raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
    metrics = _copy_candidate_fields(metrics)
    if not isinstance(metrics, Mapping):
        raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
    _assert_no_raw_series(members)
    _assert_no_raw_series(metrics)
    candidate_identity = getattr(candidate, "identity", None)
    profile_id = getattr(candidate, "profile_id", None)
    scenario_id = getattr(candidate, "scenario_id", None)
    schema_version = getattr(candidate, "schema_version", None)
    if (
        not isinstance(candidate_identity, str) or not candidate_identity
        or not isinstance(profile_id, str) or not profile_id
        or not isinstance(scenario_id, str) or not scenario_id
        or not isinstance(schema_version, str) or not schema_version
    ):
        raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
    if any(not isinstance(member, Mapping) for member in members):
        raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
    symbols = []
    for member in members:
        symbol = member.get("symbol")
        if type(symbol) is not str or not symbol or symbol != symbol.strip() or member.get("side") != "LONG":
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        symbols.append(symbol)
    if len(set(symbols)) != len(symbols):
        raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
    search_mode = getattr(result, "mode", None)
    if search_mode != CAMPAIGN_SEARCH_MODE:
        raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
    variant: dict[str, Any] = {
        "candidate_id": candidate_identity,
        "identity": candidate_identity,
        "profile": profile_id,
        "profile_id": profile_id,
        "scenario_id": scenario_id,
        "schema_version": schema_version,
        "members": tuple(members),
        "member_count": len(members),
        "pair_count": len(set(symbols)),
        "metrics": dict(metrics),
        "search_mode": search_mode,
        "gate": "PASS",
    }
    if "limiter_L" in metrics:
        variant["limiter_L"] = metrics["limiter_L"]
    _assert_no_raw_series(variant)
    return variant


def _build_strategy_payloads(
    template: Mapping[str, Any],
    candidate_members: Sequence[Mapping[str, Any]],
    source_rows: Sequence[Mapping[str, Any]],
    enriched_rows: Sequence[Mapping[str, Any]],
    bank_usdt: Any,
    open_positions_limiter: Any,
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(template, Mapping):
        raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
    source_by_identity: dict[tuple[str, str, int, int], Mapping[str, Any]] = {}
    for row in source_rows:
        identity = _weighted_input_identity(row)
        if identity in source_by_identity:
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
        source_by_identity[identity] = row
    enriched_by_identity: dict[tuple[str, str, int, int], Mapping[str, Any]] = {}
    for row in enriched_rows:
        identity = _weighted_input_identity(row)
        if identity in enriched_by_identity:
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
        enriched_by_identity[identity] = row

    payloads: list[Mapping[str, Any]] = []
    positive_identities: set[tuple[str, str, int, int]] = set()
    for member in candidate_members:
        x = _weighted_decimal(member.get("x_usdt"))
        if x == 0:
            continue
        if x < 0:
            raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
        identity = _weighted_input_identity(member)
        if identity in positive_identities:
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
        positive_identities.add(identity)
        source = source_by_identity.get(identity)
        if source is None:
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
        enriched = enriched_by_identity.get(identity)
        if not isinstance(enriched, Mapping):
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
        try:
            candidate_capacity = _weighted_decimal(member.get("capacity_usdt"))
            enriched_capacity = _weighted_decimal(enriched.get("position_size_usdt"))
        except CampaignContractError as error:
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE) from error
        if (
            candidate_capacity <= 0
            or enriched_capacity <= 0
            or not _weighted_close(candidate_capacity, enriched_capacity)
        ):
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
        try:
            leverage = _weighted_decimal(enriched.get("planned_leverage"))
        except CampaignContractError as error:
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE) from error
        if leverage <= 0:
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
        source_orders = source.get("strategy_orders")
        if isinstance(source_orders, (str, bytes)) or not isinstance(source_orders, Sequence) or not source_orders:
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
        if any(not isinstance(order, Mapping) for order in source_orders):
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
        order_count = _weighted_geometry_int(source.get("order_count"), minimum=1)
        close_ma_len = _weighted_geometry_int(source.get("close_ma_len"), minimum=1)
        if len(source_orders) != order_count:
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
        try:
            orders = []
            lots = []
            for order in source_orders:
                shift_bp = _weighted_geometry_int(order.get("shift_bp"), minimum=0)
                if source.get("side") == "LONG" and shift_bp >= 10000:
                    raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
                orders.append({
                    "open_ma": _weighted_geometry_int(order.get("open_ma_len"), minimum=1),
                    "shift_bp": shift_bp,
                })
                try:
                    lot = _weighted_decimal(order.get("lot_x"))
                except CampaignContractError as error:
                    raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE) from error
                if lot <= 0:
                    raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
                lots.append(lot)
            structure = {
                "symbol": source["symbol"],
                "side": source["side"],
                "timeframe": source["timeframe"],
                "common_close_ma": close_ma_len,
                "order_count": order_count,
                "structure_id": f"PORTFOLIO_{source['strategy_id']}_{source['result_id']}",
                "orders": tuple(orders),
            }
            strategy = generate_strategy(
                template, structure, tuple(lots), LotMethod.EQUAL, AlgorithmConfig.defaults()
            )
            if not isinstance(strategy, dict) or not isinstance(strategy.get("basic"), dict):
                raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE)
        except (ArithmeticError, KeyError, TypeError, ValueError, OverflowError) as error:
            raise CampaignContractError(WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE) from error
        strategy["name"] = f"PORTFOLIO_{source['symbol']}_{source['strategy_id']}_{source['result_id']}"
        strategy["basic"]["leverage"] = _weighted_json_number(leverage)
        payloads.append(
            build_weighted_strategy_payload(
                strategy,
                member,
                bank_usdt,
                member.get("capacity_usdt"),
                open_positions_limiter,
            )
        )
    if not payloads:
        raise CampaignContractError(_WEIGHTED_PAYLOAD_ERROR)
    return tuple(payloads)


def _weighted_executable_identity(
    campaign: Mapping[str, Any],
    profile_id: str,
    scenario_id: str,
    bank_usdt: Any,
    members: Sequence[Mapping[str, Any]],
    enriched_rows: Sequence[Mapping[str, Any]],
    payloads: Sequence[Mapping[str, Any]],
    *,
    source_rows: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Digest only the frozen inputs and executable weighted package."""
    try:
        if (
            not isinstance(campaign, Mapping)
            or type(profile_id) is not str
            or not profile_id
            or type(scenario_id) is not str
            or not scenario_id
            or isinstance(members, (str, bytes))
            or not isinstance(members, Sequence)
            or isinstance(enriched_rows, (str, bytes))
            or not isinstance(enriched_rows, Sequence)
            or isinstance(payloads, (str, bytes))
            or not isinstance(payloads, Sequence)
            or isinstance(source_rows, (str, bytes))
            or not isinstance(source_rows, Sequence)
        ):
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        positive_members = []
        for member in members:
            if not isinstance(member, Mapping):
                raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
            if _weighted_decimal(member.get("x_usdt")) > 0:
                positive_members.append((_weighted_input_identity(member), member))
        if len(positive_members) != len(payloads):
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        evidence_by_identity: dict[tuple[str, str, int, int], dict[str, Any]] = {}
        evidence_fields = (
            "reference_digest", "sizing_digest", "capacity_digest",
            "report_start_utc", "report_end_utc", "effective_start_utc", "effective_end_utc",
            "optimizer_source_metadata", "source_provenance",
        )
        for rows in (source_rows, enriched_rows):
            seen: set[tuple[str, str, int, int]] = set()
            for row in rows:
                identity = _weighted_input_identity(row)
                if identity in seen:
                    raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
                seen.add(identity)
                evidence = evidence_by_identity.setdefault(identity, {})
                for field in evidence_fields:
                    if field not in row:
                        continue
                    if field in evidence and evidence[field] != row[field]:
                        raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
                    evidence[field] = row[field]

        ordered_members = []
        for (identity, _member), payload in sorted(zip(positive_members, payloads), key=lambda item: item[0][0]):
            if not isinstance(payload, Mapping):
                raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
            strategy = payload.get("strategy")
            basic = strategy.get("basic") if isinstance(strategy, Mapping) else None
            expected_name = f"PORTFOLIO_{identity[0]}_{identity[2]}_{identity[3]}"
            if (
                not isinstance(strategy, Mapping)
                or not isinstance(basic, Mapping)
                or basic.get("symbol") != identity[0]
                or strategy.get("name") != expected_name
            ):
                raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
            evidence = evidence_by_identity.get(identity)
            if evidence is None:
                raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
            for field in ("reference_digest", "sizing_digest", "capacity_digest", "report_start_utc", "report_end_utc"):
                if type(evidence.get(field)) is not str or not evidence[field]:
                    raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
            ordered_members.append({
                "identity": {
                    "symbol": identity[0],
                    "side": identity[1],
                    "strategy_id": identity[2],
                    "result_id": identity[3],
                },
                "evidence": {field: evidence[field] for field in evidence_fields if field in evidence},
                "payload": payload,
            })

        envelope_values: dict[str, Any] = {
            "campaign_contract_version": campaign.get("campaign_contract_version"),
            "search_mode": campaign.get("search_mode"),
            "weighted_algo_version": campaign.get("weighted_algo_version"),
            "profile_id": profile_id,
            "scenario_id": scenario_id,
            "B": _weighted_decimal_text(_weighted_decimal(bank_usdt)),
            "members": ordered_members,
        }
        for field in ("input_digest", "config_digest", "frozen_config_digest", "strategy_template_digest"):
            if field in campaign:
                envelope_values[field] = campaign[field]
        encoded = json.dumps(envelope_values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return canonical_digest_v1(
            CanonicalEnvelope(
                "portfolio_weighted_executable",
                1,
                "weighted_executable",
                "json",
                typed_value("json", encoded, unit="1"),
            )
        )
    except CampaignContractError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError, ArithmeticError) as error:
        raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID) from error


def _profile_margin_blockers(profiles: Sequence[Any]) -> tuple[str, ...]:
    return tuple("PROFILE:" + MARGIN_BOUND_UNAVAILABLE for _ in profiles)


def build_portfolio_candidates(
    selected_rows: Sequence[Mapping[str, Any]],
    campaign: Mapping[str, Any],
    *,
    capacities: Mapping[str, MinuteCapacityResult],
    reference: ReferenceSnapshot,
    mark_prices: Mapping[str, Any],
    spread_observations: Mapping[str, Sequence[Mapping[str, Any]]],
    spread_history_statuses: Mapping[str, str],
    now_ms: int,
    workers: int = 1,
    margin_coefficients: Any = None,
    strategy_template: Mapping[str, Any] | None = None,
) -> AdapterResult:
    """Build scalar weighted variants from one frozen Campaign snapshot."""
    try:
        validate_campaign_contract(campaign)
    except CampaignContractError as error:
        return AdapterResult("FAIL", blockers=(error.code,))

    if not isinstance(spread_history_statuses, Mapping):
        return AdapterResult("FAIL", blockers=("SPREAD_HISTORY_STATUS_UNKNOWN",))

    selected_for_adapter = selected_rows
    spread_excluded: tuple[Mapping[str, Any], ...] = ()
    if isinstance(selected_rows, Sequence) and not isinstance(selected_rows, (str, bytes)):
        eligible_rows = []
        excluded_rows = []
        for row in selected_rows:
            symbol = row.get("symbol") if isinstance(row, Mapping) else None
            status = spread_history_statuses.get(symbol) if isinstance(symbol, str) else None
            if status not in {"READY", "PRELIMINARY"}:
                excluded = dict(_normalise_scalar_exclusion(row))
                reason = (
                    f"SPREAD_HISTORY_STATUS_{status}"
                    if status in {"CLEAR", "OVERLAPS_SPREAD"}
                    else "SPREAD_HISTORY_STATUS_UNKNOWN"
                )
                excluded["reason"] = reason
                excluded_rows.append(excluded)
            else:
                eligible_rows.append(row)
        selected_for_adapter = tuple(eligible_rows)
        spread_excluded = tuple(excluded_rows)
        if spread_excluded and not selected_for_adapter:
            return AdapterResult(
                "FAIL",
                excluded=spread_excluded,
                blockers=(spread_excluded[0]["reason"],),
            )

    try:
        prepared = _prepare_frozen_weighted_input(selected_for_adapter, campaign)
    except CampaignContractError as error:
        return AdapterResult("FAIL", blockers=(error.code,))

    try:
        if not isinstance(campaign, Mapping):
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        config_document = campaign.get("config_document")
        liquidity = config_document.get("liquidity") if isinstance(config_document, Mapping) else None
        maximum_age_hours = liquidity.get("maximum_age_hours") if isinstance(liquidity, Mapping) else None
        launch = campaign.get("launch")
        profiles = launch.get("profiles") if isinstance(launch, Mapping) else None
        if (
            isinstance(profiles, (str, bytes))
            or not isinstance(profiles, Sequence)
            or not profiles
            or any(not isinstance(profile, Mapping) for profile in profiles)
        ):
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        if maximum_age_hours is None:
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
        profile_ids: set[str] = set()
        for profile in profiles:
            profile_id = profile.get("profile_id")
            if type(profile_id) is str:
                if profile_id in profile_ids:
                    return AdapterResult(
                        "FAIL", blockers=("PROFILE:" + _WEIGHTED_SEARCH_CONFIG_INVALID,)
                    )
                profile_ids.add(profile_id)
    except CampaignContractError as error:
        return AdapterResult("FAIL", blockers=(error.code,))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return AdapterResult("FAIL", blockers=(_WEIGHTED_SEARCH_CONFIG_INVALID,))

    try:
        sizing = enrich_finalist_rows(
            selected_for_adapter,
            capacities,
            reference,
            mark_prices,
            now_ms=now_ms,
            maximum_age_hours=maximum_age_hours,
        )
    except CampaignContractError as error:
        return AdapterResult("FAIL", excluded=spread_excluded, blockers=(error.code,))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return AdapterResult("FAIL", excluded=spread_excluded, blockers=(_WEIGHTED_SEARCH_CONFIG_INVALID,))
    except Exception:
        return AdapterResult(
            "FAIL",
            excluded=spread_excluded,
            blockers=("POSITION_SIZING_FAILED",),
        )

    sizing_status = getattr(sizing, "status", None)
    sizing_excluded = tuple(_normalise_scalar_exclusion(item) for item in getattr(sizing, "exclusions", ()))
    if sizing_status != "PASS":
        reason = getattr(sizing, "reason", None) or "POSITION_SIZING_FAILED"
        return AdapterResult("FAIL", excluded=spread_excluded + sizing_excluded, blockers=(reason,))
    members = getattr(sizing, "rows", None)
    if isinstance(members, (str, bytes)) or not isinstance(members, Sequence) or not members:
        return AdapterResult("FAIL", excluded=spread_excluded + sizing_excluded, blockers=("POSITION_SIZING_FAILED",))

    if margin_coefficients is None:
        try:
            margin = config_document.get("margin") if isinstance(config_document, Mapping) else None
            parameters = margin.get("parameters") if isinstance(margin, Mapping) else None
            policy_id = margin.get("policy_id") if isinstance(margin, Mapping) else None
            if (
                not isinstance(parameters, Mapping)
                or not isinstance(policy_id, str)
                or not policy_id.strip()
                or "open_fee_rate" not in parameters
                or "close_fee_rate" not in parameters
            ):
                raise ValueError("MARGIN_POLICY_INVALID")
            open_fee_rate = parameters["open_fee_rate"]
            close_fee_rate = parameters["close_fee_rate"]
            order_loss_rate = parameters.get("order_loss_rate", Decimal("0"))
        except Exception:
            return AdapterResult("FAIL", excluded=spread_excluded + sizing_excluded, blockers=_profile_margin_blockers(profiles))
        try:
            derived = derive_reference_margin_coefficients(
                reference,
                members,
                open_fee_rate=open_fee_rate,
                close_fee_rate=close_fee_rate,
                order_loss_rate=order_loss_rate,
                policy_id=policy_id,
            )
        except Exception:
            derived = None
        if derived is None or derived.status != "PASS":
            reason = getattr(derived, "reason", None)
            reason = reason if isinstance(reason, str) and reason.startswith("MARGIN_") and reason != "MARGIN_INPUT_INVALID" else MARGIN_BOUND_UNAVAILABLE
            return AdapterResult("FAIL", excluded=spread_excluded + sizing_excluded, blockers=tuple("PROFILE:" + reason for _ in profiles))
        margin_coefficients = derived.by_strategy

    if not isinstance(margin_coefficients, Mapping) or not margin_coefficients:
        return AdapterResult("FAIL", excluded=spread_excluded + sizing_excluded, blockers=_profile_margin_blockers(profiles))

    variants: list[Mapping[str, Any]] = []
    excluded: list[Mapping[str, Any]] = list(spread_excluded) + list(sizing_excluded)
    blockers: list[str] = []
    warnings: list[str] = []
    for profile in profiles:
        profile_id = profile.get("profile_id")
        if type(profile_id) is not str or not profile_id or profile_id not in RESEARCH_RISK_POLICY:
            blockers.append("PROFILE:" + _WEIGHTED_SEARCH_CONFIG_INVALID)
            continue
        profile_with_scenario = dict(profile)
        profile_with_scenario["scenario_id"] = profile_id
        try:
            search_result = _run_weighted_search(
                prepared,
                members,
                campaign,
                profile_with_scenario,
                margin_coefficients,
                workers=workers,
            )
        except CampaignContractError as error:
            blockers.append("PROFILE:" + error.code)
            continue
        except Exception:
            blockers.append("PROFILE:WEIGHTED_SEARCH_FAILED")
            continue
        status = getattr(search_result, "status", None)
        try:
            warnings.extend(_safe_search_warnings(getattr(search_result, "warnings", ())))
        except CampaignContractError as error:
            blockers.append("PROFILE:" + error.code)
            continue
        if status != "PASS":
            reason = getattr(search_result, "reason", None) or "WEIGHTED_SEARCH_FAILED"
            blockers.append("PROFILE:" + str(reason))
            search_excluded = getattr(search_result, "excluded", ())
            if isinstance(search_excluded, Sequence) and not isinstance(search_excluded, (str, bytes)):
                excluded.extend(_normalise_scalar_exclusion(item) for item in search_excluded)
            continue
        if getattr(search_result, "mode", None) != CAMPAIGN_SEARCH_MODE:
            blockers.append("PROFILE:" + _WEIGHTED_SEARCH_CONFIG_INVALID)
            continue
        candidates = getattr(search_result, "candidates", ())
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            blockers.append("PROFILE:" + _WEIGHTED_SEARCH_CONFIG_INVALID)
            continue
        if not candidates:
            blockers.append("PROFILE:WEIGHTED_SEARCH_FAILED")
            continue
        try:
            profile_variants = []
            for candidate in candidates:
                if (
                    getattr(candidate, "profile_id", None) != profile_id
                    or getattr(candidate, "scenario_id", None) != profile_id
                ):
                    raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
                variant = dict(_safe_weighted_variant(candidate, search_result))
                if strategy_template is not None:
                    limiter = variant.get("limiter_L")
                    if type(limiter) is not int or limiter < 0:
                        raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)
                    variant["strategy_payloads"] = _build_strategy_payloads(
                        strategy_template,
                        variant["members"],
                        selected_for_adapter,
                        members,
                        profile.get("equity_usdt"),
                        limiter,
                    )
                    variant["search_identity"] = variant["identity"]
                    variant["candidate_id"] = variant["identity"] = _weighted_executable_identity(
                        campaign,
                        variant["profile_id"],
                        variant["scenario_id"],
                        profile.get("equity_usdt"),
                        variant["members"],
                        members,
                        variant["strategy_payloads"],
                        source_rows=selected_for_adapter,
                    )
                profile_variants.append(variant)
        except CampaignContractError as error:
            blockers.append("PROFILE:" + error.code)
        else:
            variants.extend(profile_variants)

    if not blockers:
        identities = [variant["identity"] for variant in variants]
        if len(identities) != len(set(identities)):
            blockers.append("PROFILE:" + WEIGHTED_EXECUTABLE_IDENTITY_COLLISION)
    if blockers:
        return AdapterResult("FAIL", excluded=excluded, blockers=blockers, warnings=warnings)
    return AdapterResult("PASS", variants=variants, excluded=excluded, warnings=warnings)


def run_portfolio_adapter(
    selected_rows: Sequence[Mapping[str, Any]],
    campaign: Mapping[str, Any],
    *,
    workspace_root: str | Path,
    market_fetcher: Any = None,
    archive_fetcher: Any = None,
    workers: int = 1,
) -> AdapterResult:
    """Load official public/local facts for one frozen weighted Campaign."""
    try:
        validate_campaign_contract(campaign)
    except CampaignContractError as error:
        return AdapterResult("FAIL", blockers=(error.code,))

    strategy_template = campaign.get("strategy_template") if isinstance(campaign, Mapping) else None
    if not isinstance(strategy_template, Mapping):
        return AdapterResult("FAIL", blockers=(_WEIGHTED_SEARCH_CONFIG_INVALID,))
    if market_fetcher is not None and not callable(market_fetcher):
        return AdapterResult("FAIL", blockers=(MARKET_SNAPSHOT_UNAVAILABLE,))
    if archive_fetcher is not None and not callable(archive_fetcher):
        return AdapterResult("FAIL", blockers=(MINUTE_CAPACITY_UNAVAILABLE,))

    try:
        document = campaign.get("config_document")
        inputs = document.get("inputs") if isinstance(document, Mapping) else None
        liquidity = document.get("liquidity") if isinstance(document, Mapping) else None
        parameters = liquidity.get("parameters") if isinstance(liquidity, Mapping) else None
        if not isinstance(inputs, Mapping) or not isinstance(liquidity, Mapping) or not isinstance(parameters, Mapping):
            raise CampaignContractError(_WEIGHTED_SEARCH_CONFIG_INVALID)

        now = datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        symbols = tuple(sorted({row["symbol"] for row in selected_rows if isinstance(row, Mapping) and isinstance(row.get("symbol"), str) and row["symbol"]}))
        if not symbols:
            return AdapterResult("FAIL", blockers=("NO_SELECTED_SYMBOLS",))

        lag = liquidity["archive_publication_lag_hours"]
        end_date = now.date() - timedelta(days=1)
        if now < datetime.combine(now.date(), time(), timezone.utc) + timedelta(hours=lag):
            end_date -= timedelta(days=1)
        days = tuple(end_date - timedelta(days=offset) for offset in range(6, -1, -1))
        minute_root = Path(workspace_root) / Path(str(inputs["bybit_minute_data_root"]))
        backfill_enabled = liquidity["backfill_write_enabled"]
        archive_fetch_day = archive_fetcher if callable(archive_fetcher) else minute_capacity.fetch_bybit_trade_archive

        capacities: dict[str, MinuteCapacityResult] = {}
        for symbol in symbols:
            backfill = backfill_missing_days(
                minute_root,
                symbol,
                days,
                fetch_day=archive_fetch_day,
                enabled=backfill_enabled,
            )
            if backfill_enabled and backfill.failed:
                failed_days = ", ".join(day.isoformat() for day in sorted(backfill.failed))
                raise MinuteCapacityError(f"Bybit archive backfill failed for {symbol}: {failed_days}")
            capacities[symbol] = calculate_minute_capacity(
                minute_root,
                symbol,
                end_date=end_date,
                participation_pct=parameters["close_volume_participation_pct"],
                round_down_usdt=liquidity["round_down_usdt"],
                weekend_start_utc=liquidity["weekend_start_utc"],
                weekend_end_utc=liquidity["weekend_end_utc"],
                publication_lag_hours=lag,
                now=now,
            )

        market_kwargs = {
            "captured_at_ms": now_ms,
            "limiter": ApiRateLimiter(Path(workspace_root) / ".mrs3-market-api-cooldown.json"),
        }
        if callable(market_fetcher):
            market_kwargs["fetcher"] = market_fetcher
        market = load_market_snapshot(symbols, **market_kwargs)
        mark_prices = getattr(market, "mark_prices", None)
        if getattr(market, "reference", None) is None or not isinstance(mark_prices, Mapping) or any(symbol not in mark_prices for symbol in symbols):
            raise MarketSnapshotError("market snapshot facts are incomplete")
        spread = read_spread_history(
            Path(workspace_root) / Path(str(inputs["collector_root"])),
            symbols,
            now_ms=now_ms,
            minimum_coverage_pct=liquidity["minimum_coverage_pct"],
        )
        spread_observations = getattr(spread, "observations", None)
        spread_statuses = getattr(spread, "statuses", None)
        if (
            not isinstance(spread_observations, Mapping)
            or not isinstance(spread_statuses, Mapping)
            or any(
                symbol not in spread_observations
                or symbol not in spread_statuses
                or isinstance(spread_observations[symbol], (str, bytes))
                or not isinstance(spread_observations[symbol], Sequence)
                or not spread_observations[symbol]
                for symbol in symbols
            )
        ):
            raise ValueError("spread history facts are incomplete")

    except MinuteCapacityError:
        return AdapterResult("FAIL", blockers=(MINUTE_CAPACITY_UNAVAILABLE,))
    except MarketSnapshotError:
        return AdapterResult("FAIL", blockers=(MARKET_SNAPSHOT_UNAVAILABLE,))
    except CampaignContractError as error:
        return AdapterResult("FAIL", blockers=(error.code,))
    except Exception:
        return AdapterResult("FAIL", blockers=(ADAPTER_FACTS_UNAVAILABLE,))

    try:
        return build_portfolio_candidates(
            selected_rows,
            campaign,
            capacities=capacities,
            reference=market.reference,
            mark_prices=mark_prices,
            spread_observations=spread_observations,
            spread_history_statuses=spread_statuses,
            now_ms=now_ms,
            workers=workers,
            strategy_template=strategy_template,
        )
    except Exception:
        return AdapterResult("FAIL", blockers=(ADAPTER_BUILD_FAILED,))


__all__ = [
    "CAMPAIGN_CONTRACT_VERSION",
    "CAMPAIGN_LEGACY_SEARCH_MODE",
    "CAMPAIGN_SEARCH_MODE",
    "CAMPAIGN_WEIGHTED_ALGO_VERSION",
    "CampaignContractError",
    "WEIGHTED_INPUT_PREPARATION_FAILED",
    "WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE",
    "MARGIN_BOUND_UNAVAILABLE",
    "MINUTE_CAPACITY_UNAVAILABLE",
    "MARKET_SNAPSHOT_UNAVAILABLE",
    "WEIGHTED_EXECUTABLE_IDENTITY_COLLISION",
    "ADAPTER_FACTS_UNAVAILABLE",
    "ADAPTER_BUILD_FAILED",
    "WEIGHTED_SEARCH_NOT_IMPLEMENTED",
    "AdapterResult",
    "build_weighted_strategy_payload",
    "build_portfolio_candidates",
    "run_portfolio_adapter",
    "validate_campaign_contract",
]

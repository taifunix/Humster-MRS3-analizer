"""Protocol v2 render and launch checks for MRS3 evaluation tests (protocol v2 §4.2, §4.3, §7a)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, MutableMapping

import duckdb

from .performance_v2_store import (
    PerformanceV2Config,
    PerformanceV2IdentityError,
    PerformanceV2StoreError,
    performance_v2_database_path,
    require_database_identity,
)

DATABASE_PROTOCOL_V1 = "DATABASE_PROTOCOL_V1"
DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
PROTOCOL_V2_RENDER_REJECTED = "PROTOCOL_V2_RENDER_REJECTED"
PROTOCOL_V2_TESTER_CONFIG_REJECTED = "PROTOCOL_V2_TESTER_CONFIG_REJECTED"


class EvaluationProtocolError(ValueError):
    """An MRS3 evaluation render or launch that does not satisfy protocol v2."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class EvaluationTarget:
    """Base B and exchange that every MRS3 evaluation test of the active database uses."""

    base: Decimal
    exchange: str


def _number(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def tester_base(tester_template: Path) -> Decimal:
    """Base B: `InitialBalance` of the MRS3 tester template (protocol v2 §4.3)."""
    try:
        document = json.loads(Path(tester_template).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvaluationProtocolError(PROTOCOL_V2_TESTER_CONFIG_REJECTED, "MRS3 tester template is unreadable") from error
    base = _number(document.get("InitialBalance")) if isinstance(document, Mapping) else None
    if base is None or base <= 0:
        raise EvaluationProtocolError(PROTOCOL_V2_TESTER_CONFIG_REJECTED, "MRS3 tester template has no positive InitialBalance")
    return base


def evaluation_target(config: PerformanceV2Config, tester_template: Path) -> EvaluationTarget:
    """Target of the active PerformanceDB; refuses before any render or launch when it is not protocol v2."""
    exchange = (config.exchange or "").strip()
    if config.evaluation_protocol != "v2" or not exchange:
        raise EvaluationProtocolError(
            DATABASE_PROTOCOL_V1,
            "MRS3 evaluation tests need a protocol v2 PerformanceDB with an exchange "
            "(unified_performance_v2.evaluation_protocol = v2 and exchange)",
        )
    target = performance_v2_database_path(config)
    if target.is_file() and target.stat().st_size > 0:
        try:
            with duckdb.connect(str(target), read_only=True) as connection:
                require_database_identity(connection, config)
        except PerformanceV2IdentityError as error:
            raise EvaluationProtocolError(error.code, str(error)) from error
        except (PerformanceV2StoreError, duckdb.Error) as error:
            raise EvaluationProtocolError(DATABASE_UNAVAILABLE, "active PerformanceDB cannot be checked") from error
    return EvaluationTarget(tester_base(tester_template), exchange)


def protocol_v2_unmet(strategy: Mapping[str, Any], target: EvaluationTarget) -> tuple[str, ...]:
    """Unmet fields of the §4.2 predicate for one rendered strategy."""
    from .performance_v2_import import report_evaluation_protocol

    basic = strategy.get("basic") if isinstance(strategy.get("basic"), Mapping) else {}
    exchange = strategy.get("exchange") if isinstance(strategy.get("exchange"), Mapping) else {}
    flags = (basic.get("use_long"), basic.get("use_short"))  # type: ignore[union-attr]
    side = {(True, False): "LONG", (False, True): "SHORT"}.get(flags)  # type: ignore[arg-type]
    _protocol, unmet = report_evaluation_protocol(strategy, side)
    fields = list(unmet)
    if exchange.get("use_upnl") is not False:  # type: ignore[union-attr]
        fields.insert(0, "exchange.use_upnl")
    if _number(basic.get("my_fix_balance")) != target.base:  # type: ignore[union-attr]
        fields.append("basic.my_fix_balance")
    name = exchange.get("name")  # type: ignore[union-attr]
    if not isinstance(name, str) or name.strip().casefold() != target.exchange.casefold():
        fields.append("exchange.name")
    return tuple(dict.fromkeys(fields))


def apply_protocol_v2(strategy: MutableMapping[str, Any], target: EvaluationTarget) -> MutableMapping[str, Any]:
    """Set the base and the exchange of one rendered strategy, then require the §4.2 predicate."""
    basic = strategy.get("basic")
    exchange = strategy.get("exchange")
    if not isinstance(basic, MutableMapping) or not isinstance(exchange, MutableMapping):
        raise EvaluationProtocolError(PROTOCOL_V2_RENDER_REJECTED, "strategy has no basic or exchange settings")
    basic["my_fix_balance"] = float(target.base)
    exchange["name"] = target.exchange
    require_protocol_v2_strategy(strategy, target)
    return strategy


def require_protocol_v2_strategy(strategy: Mapping[str, Any], target: EvaluationTarget) -> None:
    unmet = protocol_v2_unmet(strategy, target)
    if unmet:
        name = strategy.get("name")
        raise EvaluationProtocolError(
            PROTOCOL_V2_RENDER_REJECTED,
            f"strategy {name!r} does not meet protocol v2: " + ", ".join(unmet),
        )


def require_protocol_v2_tester_config(config: Mapping[str, Any], target: EvaluationTarget) -> None:
    """Tester config clauses of the §4.2 predicate: no volume check and the base as initial balance."""
    unmet = []
    if config.get("LimitOrderVolumeCheck") is not False:
        unmet.append("LimitOrderVolumeCheck")
    if _number(config.get("InitialBalance")) != target.base:
        unmet.append("InitialBalance")
    if unmet:
        raise EvaluationProtocolError(
            PROTOCOL_V2_TESTER_CONFIG_REJECTED, "tester config does not meet protocol v2: " + ", ".join(unmet),
        )


__all__ = [
    "DATABASE_PROTOCOL_V1",
    "DATABASE_UNAVAILABLE",
    "PROTOCOL_V2_RENDER_REJECTED",
    "PROTOCOL_V2_TESTER_CONFIG_REJECTED",
    "EvaluationProtocolError",
    "EvaluationTarget",
    "apply_protocol_v2",
    "evaluation_target",
    "protocol_v2_unmet",
    "require_protocol_v2_strategy",
    "require_protocol_v2_tester_config",
    "tester_base",
]

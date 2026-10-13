"""Protocol v2 render and launch checks for MRS3 evaluation tests (protocol v2 §4.2, §4.3, §7a)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, MutableMapping

import duckdb

from .exchange_profiles import ExchangeProfile
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
    """Base B, exchange and exchange profile that every MRS3 evaluation test of the active database uses.

    Profile fields are empty for an exchange without a profile (Bybit, P7 spec §3);
    `profile_digest` is then None and the render is the pre-P7 one.
    """

    base: Decimal
    exchange: str
    strategy_exchange: tuple[tuple[str, str], ...] = ()
    tester_fields: tuple[tuple[str, int | float], ...] = ()
    profile_digest: str | None = None
    # Folder of the active database: a job marker field (P7 spec §4), not a render input.
    database: str = field(default="", compare=False)


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


def evaluation_target(
    config: PerformanceV2Config,
    tester_template: Path,
    profile: ExchangeProfile | None = None,
    *,
    project_root: Path | None = None,
) -> EvaluationTarget:
    """Target of the active PerformanceDB; refuses before any render or launch when it is not protocol v2.

    `profile` is the exchange profile of the active database (P7 spec §3); the
    caller loads it so that a missing or invalid profile refuses with its code.
    """
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
    if profile is not None and profile.exchange.casefold() != exchange.casefold():
        raise EvaluationProtocolError("EXCHANGE_PROFILE_INVALID", f"profile of {profile.exchange} is not for {exchange}")
    database = Path(config.database_root)
    if project_root is not None:
        try:
            database = database.relative_to(Path(project_root).resolve())
        except ValueError:
            pass
    has_profile = profile is not None and bool(profile.strategy_exchange or profile.tester or profile.listing_dates)
    return EvaluationTarget(
        tester_base(tester_template),
        exchange,
        strategy_exchange=profile.strategy_exchange if profile is not None else (),
        tester_fields=profile.tester if profile is not None else (),
        profile_digest=profile.digest() if has_profile else None,  # type: ignore[union-attr]
        database=database.as_posix(),
    )


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
    for key, value in target.strategy_exchange:
        if exchange.get(key) != value:  # type: ignore[union-attr]
            fields.append(f"exchange.{key}")
    return tuple(dict.fromkeys(fields))


def apply_protocol_v2(strategy: MutableMapping[str, Any], target: EvaluationTarget) -> MutableMapping[str, Any]:
    """Set the base and the exchange of one rendered strategy, then require the §4.2 predicate."""
    basic = strategy.get("basic")
    exchange = strategy.get("exchange")
    if not isinstance(basic, MutableMapping) or not isinstance(exchange, MutableMapping):
        raise EvaluationProtocolError(PROTOCOL_V2_RENDER_REJECTED, "strategy has no basic or exchange settings")
    basic["my_fix_balance"] = float(target.base)
    exchange["name"] = target.exchange
    exchange.update(dict(target.strategy_exchange))
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
    """Tester config clauses of the §4.2 predicate: no volume check, the base as initial balance
    and the fees of the exchange profile (P7 spec §4)."""
    unmet = []
    if config.get("LimitOrderVolumeCheck") is not False:
        unmet.append("LimitOrderVolumeCheck")
    if _number(config.get("InitialBalance")) != target.base:
        unmet.append("InitialBalance")
    for key, value in target.tester_fields:
        if _number(config.get(key)) != _number(value):
            unmet.append(key)
    if unmet:
        raise EvaluationProtocolError(
            PROTOCOL_V2_TESTER_CONFIG_REJECTED, "tester config does not meet protocol v2: " + ", ".join(unmet),
        )


_Facts = tuple[Any, Any]
StoredMaFacts = tuple[_Facts, dict[int, _Facts]]


def load_stored_ma_facts(connection: duckdb.DuckDBPyConnection, strategy_ids: Any) -> dict[int, StoredMaFacts]:
    """Stored MA type/source of the close MA and of every order by strategy; empty before schema v12."""
    from .performance_v2_selection import has_ma_fact_columns

    ids = sorted({int(value) for value in strategy_ids})
    if not ids or not has_ma_fact_columns(connection):
        return {}
    close = {
        int(strategy_id): (kind, source)
        for strategy_id, kind, source in connection.execute(
            """select strategy_id, close_ma_type, close_ma_source from strategies
                where strategy_id in (select unnest(?::bigint[]))""",
            [ids],
        ).fetchall()
    }
    orders: dict[int, dict[int, _Facts]] = {}
    for strategy_id, order_id, kind, source in connection.execute(
        """select strategy_id, order_id, open_ma_type, open_ma_source from strategy_orders
            where strategy_id in (select unnest(?::bigint[]))""",
        [ids],
    ).fetchall():
        orders.setdefault(int(strategy_id), {})[int(order_id)] = (kind, source)
    return {strategy_id: (facts, orders.get(strategy_id, {})) for strategy_id, facts in close.items()}


def apply_stored_ma_facts(
    strategy: MutableMapping[str, Any], side: object, facts: StoredMaFacts | None, order_ids: Any,
) -> None:
    """Set each order's and the close MA's type/source from the stored strategy (protocol v2 §4.5, §7a).

    A RETEST repeats the strategy as tested; empty (legacy) facts keep the template prototype.
    """
    if facts is None:
        return
    suffix = "short" if str(side).strip().upper() == "SHORT" else "long"
    try:
        entries = strategy["mrs3"][f"ma_{suffix}"]
        close = strategy["mrs3"][f"ma_close_{suffix}"]
    except (KeyError, TypeError) as error:
        raise EvaluationProtocolError(PROTOCOL_V2_RENDER_REJECTED, "strategy has no MA settings") from error
    order_ids = [int(value) for value in order_ids]
    if len(entries) != len(order_ids):
        raise EvaluationProtocolError(PROTOCOL_V2_RENDER_REJECTED, "strategy orders do not match the stored strategy")
    close_facts, order_facts = facts
    for entry, order_id in zip(entries, order_ids, strict=True):
        for key, value in zip(("type", "source"), order_facts.get(order_id, (None, None)), strict=True):
            if value is not None:
                entry[key] = value
    for key, value in zip(("type", "source"), close_facts, strict=True):
        if value is not None:
            close[key] = value


__all__ = [
    "DATABASE_PROTOCOL_V1",
    "DATABASE_UNAVAILABLE",
    "PROTOCOL_V2_RENDER_REJECTED",
    "PROTOCOL_V2_TESTER_CONFIG_REJECTED",
    "EvaluationProtocolError",
    "EvaluationTarget",
    "apply_protocol_v2",
    "apply_stored_ma_facts",
    "load_stored_ma_facts",
    "evaluation_target",
    "protocol_v2_unmet",
    "require_protocol_v2_strategy",
    "require_protocol_v2_tester_config",
    "tester_base",
]

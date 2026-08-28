"""Lock-first transactional publication for the unified Performance v2 store."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Mapping
from uuid import uuid4

import duckdb

from .performance import PerformanceParseError, report_range
from .performance_v2_html import ParsedPerformanceV2Report, parse_current_performance_v2_html
from .performance_v2_input import (
    PerformanceV2InputError,
    PreparedV2Entry,
    PreparedV2Input,
    create_v2_parser_staging,
    read_performance_v2_inbox,
    remove_v2_parser_staging,
)
from .performance_v2_store import (
    PerformanceV2Config,
    PerformanceV2StoreError,
    performance_v2_database_path,
    require_performance_v2,
)


class PerformanceV2ImportError(RuntimeError):
    """Raised when a v2 publication cannot be committed safely."""


class PerformanceV2LockedError(PerformanceV2ImportError):
    """Raised when another process owns the v2 DuckDB writer lock."""


@dataclass(frozen=True, slots=True, init=False)
class PerformanceV2ImportRequest:
    inbox: Path
    report_root: Path
    config: PerformanceV2Config
    mode: str
    replacement_strategy_ids: Mapping[str, int]
    expected_strategy_identities: Mapping[str, object] | None

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
        object.__setattr__(self, "inbox", Path(inbox))
        object.__setattr__(self, "report_root", Path(report_root))
        object.__setattr__(self, "config", config)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "replacement_strategy_ids", dict(mapping))
        object.__setattr__(self, "expected_strategy_identities", expected_strategy_identities)

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


def _parse_reports(staging: Path, prepared: PreparedV2Input, config: PerformanceV2Config) -> tuple[ParsedPerformanceV2Report, ...]:
    paths = tuple(staging / "reports" / entry.report_path.name for entry in prepared.entries)
    workers = min(config.workers, len(paths))
    if workers == 1:
        return tuple(_parse_staged_report(path, config) for path in paths)
    with ProcessPoolExecutor(max_workers=workers) as executor:
        return tuple(executor.map(_parse_staged_report, paths, (config,) * len(paths)))


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


def _validate_report(entry: PreparedV2Entry, report: ParsedPerformanceV2Report) -> None:
    basic = report.settings.get("basic")
    if not isinstance(basic, Mapping) or str(basic.get("symbol", "")).strip() != entry.identity.symbol:
        raise PerformanceV2ImportError(f"report symbol does not match strategy {entry.strategy_name!r}")
    expected_order_ids = {order.order_id for order in entry.identity.orders}
    if any(action.order_id not in expected_order_ids for action in report.actions):
        raise PerformanceV2ImportError(f"report order does not match strategy {entry.strategy_name!r}")


def _load_existing(connection: duckdb.DuckDBPyConnection, names: tuple[str, ...]) -> tuple[dict[str, tuple[object, ...]], dict[int, list[tuple[object, ...]]], dict[int, tuple[object, ...]], dict[str, set[str]]]:
    placeholders = ",".join("?" for _ in names)
    strategies = {
        str(row[0]): row
        for row in connection.execute(
            f"""select strategy_name, strategy_id, symbol, side, timeframe, close_ma_len,
                       order_count, analysis_run_id, candidate_identity, lifecycle_status,
                       current_result_id from strategies where strategy_name in ({placeholders})""",
            list(names),
        ).fetchall()
    }
    ids = tuple(int(row[1]) for row in strategies.values())
    if not ids:
        return strategies, {}, {}, {}
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
            int(row[1]): row
            for row in connection.execute(
                f"""select result_id, strategy_id, report_start_utc, report_end_utc, exchange,
                           commission_rate, initial_balance, final_balance, total_pnl,
                           total_pnl_pct, max_drawdown, max_drawdown_pct, total_fees, total_trades
                       from strategy_results where result_id in ({result_placeholders})""",
                list(result_ids),
            ).fetchall()
        }
    known_hashes: dict[str, set[str]] = {}
    for row in connection.execute(
        "select source_filename, source_html_sha256 from import_files where status in ('IMPORTED', 'SKIPPED', 'REPLACED')"
    ).fetchall():
        known_hashes.setdefault(str(row[0]), set()).add(str(row[1]))
    return strategies, orders, results, known_hashes


def _strategy_matches(entry: PreparedV2Entry, row: tuple[object, ...]) -> bool:
    return (
        str(row[2]) == entry.identity.symbol
        and str(row[3]) == entry.identity.side
        and str(row[4]) == entry.identity.timeframe
        and int(row[5]) == entry.identity.close_ma_len
        and int(row[6]) == entry.identity.order_count
        and str(row[7]) == entry.analysis_run_id
        and str(row[8]) == entry.candidate_identity
    )


def _orders_match(entry: PreparedV2Entry, rows: list[tuple[object, ...]]) -> bool:
    if len(rows) != len(entry.identity.orders):
        return False
    for expected, row in zip(entry.identity.orders, rows, strict=True):
        if (
            int(row[1]) != expected.order_id
            or int(row[2]) != expected.open_ma_len
            or Decimal(str(row[3])) != expected.open_multiplier
            or int(row[4]) != expected.shift_bp
            or Decimal(str(row[5])) != expected.lot_x
            or str(row[6]) != entry.analysis_run_id
            or str(row[7]) != expected.plateau_id
            or int(row[8]) != expected.base_point_trades
        ):
            return False
    return True


def _result_matches(report: ParsedPerformanceV2Report, row: tuple[object, ...], entry: PreparedV2Entry, contract: Mapping[str, str]) -> bool:
    try:
        start, end = report_range(report.metrics)
    except PerformanceParseError:
        return False
    expected = (
        start,
        end,
        str(report.settings.get("exchange", {}).get("name", entry.exchange_name)) if isinstance(report.settings.get("exchange"), Mapping) else entry.exchange_name,
        Decimal(contract["TakerFee"]),
        _decimal_metric(report.metrics, "Initial balance", default=Decimal("0")),
        _decimal_metric(report.metrics, "Final balance", default=Decimal("0")),
        _decimal_metric(report.metrics, "Total PnL"),
        _decimal_metric(report.metrics, "Total PnL, %", "Total PnL %"),
        _decimal_metric(report.metrics, "Max Drawdown", "Max drawdown"),
        _decimal_metric(report.metrics, "Max Drawdown, %", "Max Drawdown %", "Max drawdown, %"),
        _decimal_metric(report.metrics, "Total fees", "Total Fees"),
        _int_metric(report.metrics, "Total Trades"),
    )
    return all(a == b for a, b in zip(expected, row[2:], strict=True))


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


def _after_delete_before_insert(connection: duckdb.DuckDBPyConnection, strategy_id: int) -> None:
    """Test seam for proving replacement rollback after old-row deletion."""


_REPLACE_CHILD_TABLES = (
    "strategy_actions",
    "strategy_equity",
    "window_metrics",
)

_REPLACE_CHILD_DDL = {
    "strategy_actions": """
        create table strategy_actions (
            result_id bigint not null references strategy_results(result_id),
            action_index integer not null check (action_index >= 0),
            timestamp_utc timestamptz not null,
            symbol varchar not null,
            order_id integer,
            action varchar not null,
            size decimal(38,12) not null,
            post_size decimal(38,12) not null,
            post_side varchar not null,
            pnl decimal(38,12) not null,
            fee decimal(38,12) not null,
            balance decimal(38,12) not null,
            raw_action_json varchar,
            primary key (result_id, action_index)
        )
    """,
    "strategy_equity": """
        create table strategy_equity (
            result_id bigint not null references strategy_results(result_id),
            sample_index integer not null check (sample_index >= 0),
            timestamp_utc timestamptz not null,
            wallet decimal(38,12) not null,
            equity decimal(38,12) not null,
            primary key (result_id, sample_index)
        )
    """,
    "window_metrics": """
        create table window_metrics (
            result_id bigint not null references strategy_results(result_id),
            requested_start_utc timestamptz not null,
            requested_end_utc timestamptz not null,
            metrics_version varchar not null,
            effective_start_utc timestamptz,
            effective_end_utc timestamptz,
            availability_status varchar not null,
            unavailable_reason varchar,
            growth_factor decimal(38,12),
            return_pct decimal(38,12),
            daily_log_return decimal(38,12),
            daily_growth_pct decimal(38,12),
            max_drawdown_pct decimal(38,12),
            return_dd_ratio decimal(38,12),
            fees_pct decimal(38,12),
            profit_factor decimal(38,12),
            trade_count integer,
            win_rate_pct decimal(38,12),
            calculated_at_utc timestamptz not null,
            primary key (result_id, requested_start_utc, requested_end_utc, metrics_version),
            check (requested_end_utc >= requested_start_utc)
        )
    """,
}


def _prepare_replace_children(connection: duckdb.DuckDBPyConnection, old_result_ids: tuple[int, ...]) -> None:
    """Rebuild FK children so DuckDB's transaction-local FK indexes can forget old rows.

    DuckDB 1.5 eagerly checks the old child index entries after a DELETE in the
    same transaction. Rebuilding the three dependent tables inside that same
    transaction preserves their constraints and lets the parent result be
    replaced without committing a partial state.
    """
    placeholders = ",".join("?" for _ in old_result_ids)
    for table in _REPLACE_CHILD_TABLES:
        backup = f"v2_replace_backup_{table}"
        connection.execute(f"create temp table {backup} as select * from {table}")
    for table in _REPLACE_CHILD_TABLES:
        connection.execute(f"drop table {table}")
    for table in _REPLACE_CHILD_TABLES:
        connection.execute(_REPLACE_CHILD_DDL[table])
        backup = f"v2_replace_backup_{table}"
        connection.execute(
            f"insert into {table} select * from {backup} where result_id not in ({placeholders})",
            list(old_result_ids),
        )
    connection.execute("create index strategy_actions_result_timestamp_idx on strategy_actions(result_id, timestamp_utc)")
    connection.execute("create index strategy_equity_result_timestamp_idx on strategy_equity(result_id, timestamp_utc)")
    for table in _REPLACE_CHILD_TABLES:
        connection.execute(f"drop table v2_replace_backup_{table}")


def _publish(
    connection: duckdb.DuckDBPyConnection,
    request: PerformanceV2ImportRequest,
    prepared: PreparedV2Input,
    parsed: tuple[ParsedPerformanceV2Report, ...],
    import_id: str,
) -> tuple[int, int, int]:
    names = tuple(entry.strategy_name for entry in prepared.entries)
    existing, existing_orders, existing_results, known_hashes = _load_existing(connection, names)
    existing_plateaus = _plateau_facts(connection, prepared)
    for fact in prepared.plateaus:
        old = existing_plateaus.get((fact.analysis_run_id, fact.plateau_id))
        if old is not None and (int(old[2]), int(old[3])) != (fact.plateau_point_count, fact.plateau_total_trades):
            raise PerformanceV2ImportError(f"typed plateau mismatch for {fact.plateau_id!r}")
    if request.mode == "REPLACE":
        if set(request.replacement_strategy_ids) != set(names):
            raise PerformanceV2ImportError("REPLACE requires an explicit strategy mapping for every strategy")
        for name, strategy_id in request.replacement_strategy_ids.items():
            row = existing.get(name)
            if row is None or int(row[1]) != int(strategy_id):
                raise PerformanceV2ImportError(f"replacement mapping does not match existing strategy {name!r}")

    decisions: list[tuple[str, PreparedV2Entry, ParsedPerformanceV2Report, tuple[object, ...] | None]] = []
    skipped = 0
    for entry, report in zip(prepared.entries, parsed, strict=True):
        _validate_report(entry, report)
        row = existing.get(entry.strategy_name)
        if row is None:
            if request.mode == "REPLACE":
                raise PerformanceV2ImportError(f"REPLACE target {entry.strategy_name!r} does not exist")
            decisions.append(("ADD", entry, report, None))
            continue
        if not _strategy_matches(entry, row):
            raise PerformanceV2ImportError(f"typed strategy mismatch for existing {entry.strategy_name!r}")
        if not _orders_match(entry, existing_orders.get(int(row[1]), [])):
            raise PerformanceV2ImportError(f"typed order mismatch for existing {entry.strategy_name!r}")
        if request.mode == "ADD":
            if str(row[9]) != "ACTIVE" or row[10] is None:
                raise PerformanceV2ImportError(f"existing strategy {entry.strategy_name!r} has no current result")
            current = existing_results.get(int(row[10]))
            if current is None:
                raise PerformanceV2ImportError(f"existing strategy {entry.strategy_name!r} has no current result")
            if _result_matches(report, current, entry, prepared.commission_contract) and report_hash(entry) in known_hashes.get(entry.report_path.name, set()):
                decisions.append(("SKIPPED", entry, report, current))
                skipped += 1
                continue
            raise PerformanceV2ImportError(f"changed content for existing strategy {entry.strategy_name!r} requires REPLACE")
        if request.expected_strategy_identities and entry.strategy_name in request.expected_strategy_identities:
            expected = request.expected_strategy_identities[entry.strategy_name]
            if isinstance(expected, Mapping) and expected.get("close_ma_len") not in (None, entry.identity.close_ma_len):
                raise PerformanceV2ImportError("typed strategy mismatch in replacement mapping")
        decisions.append(("REPLACE", entry, report, row))

    now = _utc_now()
    connection.execute("begin")
    try:
        old_result_ids = tuple(
            int(old[10])
            for decision, _entry, _report, old in decisions
            if decision == "REPLACE" and old is not None and old[10] is not None
        )
        if old_result_ids:
            _prepare_replace_children(connection, old_result_ids)
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
        result_files: list[tuple[str, str, int, int, int, str]] = []
        result_ids: dict[str, int] = {}
        for fact in prepared.plateaus:
            connection.execute(
                """insert into analysis_plateaus (analysis_run_id, plateau_id, plateau_point_count, plateau_total_trades)
                   values (?, ?, ?, ?) on conflict (analysis_run_id, plateau_id) do nothing""",
                [fact.analysis_run_id, fact.plateau_id, fact.plateau_point_count, fact.plateau_total_trades],
            )
        for decision, entry, report, old in decisions:
            if decision == "SKIPPED":
                result_files.append((entry.report_path.name, report_hash(entry), entry.report_path.stat().st_size, len(report.actions), len(report.equity_series), "SKIPPED"))
                continue
            strategy_id: int
            if decision == "ADD":
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
                         order.shift_bp, order.lot_x, entry.analysis_run_id, order.plateau_id, order.base_point_trades],
                    )
            else:
                strategy_id = int(old[1])  # type: ignore[index]
                connection.execute("update strategies set current_result_id = null, updated_at_utc = ? where strategy_id = ?", [now, strategy_id])
                old_result_id = int(old[10])  # type: ignore[index]
                # _prepare_replace_children removed the old child rows while
                # rebuilding the constrained tables in this transaction.
                connection.execute("delete from strategy_results where result_id = ?", [old_result_id])
                _after_delete_before_insert(connection, strategy_id)
            values = _result_values(entry, report, prepared.commission_contract, now)
            result_id = int(connection.execute(
                """insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange,
                   commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
                   max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc)
                   values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) returning result_id""",
                [strategy_id, *values],
            ).fetchone()[0])
            result_ids[entry.strategy_name] = result_id
            connection.execute("update strategies set current_result_id = ?, updated_at_utc = ? where strategy_id = ?", [result_id, now, strategy_id])
            for action in report.actions:
                action_rows.append((result_id, action.action_index, action.timestamp_utc, action.symbol,
                                    action.order_id, action.action, action.size, action.post_size, action.post_side,
                                    action.pnl, action.fee, action.balance, None))
            for sample_index, (timestamp, wallet) in enumerate(report.wallet_series):
                equity = report.equity_series[sample_index][1]
                equity_rows.append((result_id, sample_index, timestamp, wallet, equity))
            result_files.append((entry.report_path.name, report_hash(entry), entry.report_path.stat().st_size, len(report.actions), len(report.equity_series), "REPLACED" if decision == "REPLACE" else "IMPORTED"))
            imported += 1

        if action_rows:
            connection.execute("create temp table v2_import_actions (result_id bigint, action_index integer, timestamp_utc timestamptz, symbol varchar, order_id integer, action varchar, size decimal(38,12), post_size decimal(38,12), post_side varchar, pnl decimal(38,12), fee decimal(38,12), balance decimal(38,12), raw_action_json varchar)")
            connection.executemany("insert into v2_import_actions values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", action_rows)
            connection.execute("insert into strategy_actions select * from v2_import_actions")
            connection.execute("drop table v2_import_actions")
        if equity_rows:
            connection.execute("create temp table v2_import_equity (result_id bigint, sample_index integer, timestamp_utc timestamptz, wallet decimal(38,12), equity decimal(38,12))")
            connection.executemany("insert into v2_import_equity values (?, ?, ?, ?, ?)", equity_rows)
            connection.execute("insert into strategy_equity select * from v2_import_equity")
            connection.execute("drop table v2_import_equity")

        connection.executemany(
            """insert into import_files (import_run_id, source_filename, source_html_sha256, source_size_bytes,
               action_count, equity_sample_count, status) values (?, ?, ?, ?, ?, ?, ?)
               on conflict (import_run_id, source_html_sha256) do update set source_filename = excluded.source_filename,
               source_size_bytes = excluded.source_size_bytes, action_count = excluded.action_count,
               equity_sample_count = excluded.equity_sample_count, status = excluded.status""",
            [[run_id, name, digest, size, actions, equity, status] for name, digest, size, actions, equity, status in result_files],
        )
        expected_action_counts = {
            result_ids[entry.strategy_name]: len(report.actions)
            for decision, entry, report, _old in decisions
            if decision != "SKIPPED"
        }
        actual_action_counts = {
            int(row[0]): int(row[1])
            for row in connection.execute(
                "select result_id, count(*) from strategy_actions where result_id in (select current_result_id from strategies where strategy_name in (" + ",".join("?" for _ in expected_action_counts) + ")) group by result_id",
                list(names),
            ).fetchall()
        } if expected_action_counts else {}
        if any(actual_action_counts.get(result_id, 0) != count for result_id, count in expected_action_counts.items()):
            raise PerformanceV2ImportError("action readback count mismatch")
        expected_equity_counts = {
            result_ids[entry.strategy_name]: len(report.equity_series)
            for decision, entry, report, _old in decisions
            if decision != "SKIPPED"
        }
        actual_equity_counts = {
            int(row[0]): int(row[1])
            for row in connection.execute(
                "select result_id, count(*) from strategy_equity where result_id in (select current_result_id from strategies where strategy_name in (" + ",".join("?" for _ in expected_equity_counts) + ")) group by result_id",
                list(names),
            ).fetchall()
        } if expected_equity_counts else {}
        if any(actual_equity_counts.get(result_id, 0) != count for result_id, count in expected_equity_counts.items()):
            raise PerformanceV2ImportError("equity readback count mismatch")
        connection.execute(
            """update import_runs set imported_count = ?, skipped_count = ?, rejected_count = 0,
               status = 'COMMITTED', finished_at_utc = ? where source_inbox_sha256 = ?""",
            [imported, skipped, _utc_now(), prepared.inbox_snapshot_sha256],
        )
        connection.execute("commit")
        return imported, skipped, 0
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
    return [
        start,
        end,
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
    ]


def report_hash(entry: PreparedV2Entry) -> str:
    return entry.report_sha256


def _write_audit(config: PerformanceV2Config, request: PerformanceV2ImportRequest, result: PerformanceV2ImportResult | None, error: Exception | None, source_hash: str | None) -> Path:
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
        "error": str(error) if error is not None else None,
        "database_path": str(performance_v2_database_path(config)),
    }
    path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    return path


def import_performance_v2(request: PerformanceV2ImportRequest) -> PerformanceV2ImportResult:
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
    prepared: PreparedV2Input | None = None
    result: PerformanceV2ImportResult | None = None
    failure: Exception | None = None
    try:
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
        lock_acquired = True
        prepared = read_performance_v2_inbox(request.inbox, request.report_root, config=config)
        staging = create_v2_parser_staging(config.database_root, prepared)
        parsed = _parse_reports(staging, prepared, config)
        import_id = uuid4().hex
        imported, skipped, rejected = _publish(connection, request, prepared, parsed, import_id)
        result = PerformanceV2ImportResult(import_id, "COMMITTED", imported, skipped, rejected, target)
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
                audit_path = _write_audit(config, request, result, failure, prepared.inbox_snapshot_sha256 if prepared else None)
                if result is not None:
                    result = PerformanceV2ImportResult(result.import_id, result.status, result.imported_count, result.skipped_count, result.rejected_count, result.database_path, audit_path, result.phases)
            except Exception as error:
                if failure is None:
                    failure = PerformanceV2ImportError("v2 audit write failed")
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

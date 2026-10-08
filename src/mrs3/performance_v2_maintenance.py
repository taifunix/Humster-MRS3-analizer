"""Read-only planning and serialized deletes for schema-v9 PerformanceDB."""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from typing import Callable, Mapping, Sequence

import duckdb

from .performance_v2_selection_review import effective_selection_decisions
from .performance_v2_store import require_performance_v2


_PAIR_TABLES = (
    "strategies", "strategy_orders", "strategy_results", "strategy_actions",
    "strategy_equity", "window_metrics", "optimizer_prepared_inputs",
    "equity_quality_metrics", "strategy_tags", "strategy_rejection_sources",
    "selection_runs", "selection_results", "selection_review_imports",
    "selection_review_rows", "analysis_plateaus",
)
# Rejected retirement keeps only strategies, typed orders, and a compact
# current strategy_results tombstone. The tombstone retains only temporal
# identity/provenance fields used by interval deduplication; metric payloads
# are stripped in the same transaction.
# Every per-strategy fact/cache/review row is removed to take it out of active
# cache, filter, and export work rather than merely hiding it at read time.
_REJECTED_RETAINED_TABLES = frozenset({"strategies", "strategy_orders", "strategy_results"})
_REJECTED_TABLES = (
    "selection_review_rows", "selection_results",
    "strategy_actions", "strategy_equity", "window_metrics",
    "optimizer_prepared_inputs", "equity_quality_metrics",
    "strategy_tags", "strategy_rejection_sources",
)
_REJECTED_RESIDUAL_EXISTS = {
    "selection_review_rows": "exists (select 1 from selection_review_rows where strategy_id = strategies.strategy_id)",
    "selection_results": "exists (select 1 from selection_results where strategy_id = strategies.strategy_id)",
    "strategy_actions": "exists (select 1 from strategy_actions facts where facts.result_id in (select result_id from strategy_results where strategy_id = strategies.strategy_id union all select strategies.current_result_id))",
    "strategy_equity": "exists (select 1 from strategy_equity facts where facts.result_id in (select result_id from strategy_results where strategy_id = strategies.strategy_id union all select strategies.current_result_id))",
    "window_metrics": "exists (select 1 from window_metrics facts where facts.result_id in (select result_id from strategy_results where strategy_id = strategies.strategy_id union all select strategies.current_result_id))",
    "optimizer_prepared_inputs": "exists (select 1 from optimizer_prepared_inputs facts where facts.result_id in (select result_id from strategy_results where strategy_id = strategies.strategy_id union all select strategies.current_result_id))",
    "equity_quality_metrics": "exists (select 1 from equity_quality_metrics facts where facts.result_id in (select result_id from strategy_results where strategy_id = strategies.strategy_id union all select strategies.current_result_id))",
    "strategy_tags": "exists (select 1 from strategy_tags where strategy_id = strategies.strategy_id)",
    "strategy_rejection_sources": "exists (select 1 from strategy_rejection_sources where strategy_id = strategies.strategy_id)",
}
if set(_REJECTED_RESIDUAL_EXISTS) != set(_REJECTED_TABLES):
    raise RuntimeError("rejected retirement residual map must cover every deleted table")
_GLOBAL_JOURNAL_TABLES = frozenset({"import_files", "import_runs"})
_PRESERVED_METADATA_TABLES = frozenset({"schema_info"})
_LEGACY_V7_RESULTS_TABLE = "__performance_v2_v7_strategy_results"
_SCHEMA_TABLE_CLASSES = {
    "pair-scoped": _PAIR_TABLES,
    "global-journal": ("import_files", "import_runs"),
    "preserved-metadata": ("schema_info",),
}
_OPERATION_TABLES = {
    "rejected": _REJECTED_TABLES,
    "full": _PAIR_TABLES,
}


class PerformanceV2MaintenanceError(ValueError):
    """A catalog, preview, or apply request cannot be safely completed."""


class PerformanceV2MaintenanceSchemaError(PerformanceV2MaintenanceError):
    """The database does not match the classified schema-v9 contract."""


def set_query_workers(connection: duckdb.DuckDBPyConnection, workers: int) -> int:
    """Use the existing worker setting, capped at the project limit for reads."""
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    capped = min(workers, 16)
    connection.execute(f"set threads to {capped}")
    return capped


def _classify_schema_v9_tables(connection: duckdb.DuckDBPyConnection) -> None:
    classified: dict[str, str] = {}
    for category, tables in _SCHEMA_TABLE_CLASSES.items():
        for table in tables:
            if table in classified:
                raise PerformanceV2MaintenanceSchemaError(f"schema v9 table is classified more than once: {table}")
            classified[table] = category
    actual = {
        str(row[0])
        for row in connection.execute(
            """select table_name from information_schema.tables
                 where table_catalog = current_database()
                   and table_schema = 'main'
                   and table_type = 'BASE TABLE'"""
        ).fetchall()
    }
    unclassified = sorted(actual - set(classified))
    if unclassified:
        raise PerformanceV2MaintenanceSchemaError(
            "unclassified schema v9 table(s): " + ", ".join(unclassified)
        )
    missing = sorted(set(classified) - actual)
    if missing:
        raise PerformanceV2MaintenanceSchemaError(
            "missing classified schema v9 table(s): " + ", ".join(missing)
        )


def _require_schema_v9(connection: duckdb.DuckDBPyConnection) -> None:
    try:
        row = connection.execute("select value from schema_info where key = 'schema_version'").fetchone()
    except Exception as error:
        raise PerformanceV2MaintenanceSchemaError(str(error)) from error
    version = None if row is None else row[0]
    if str(version) != "9":
        raise PerformanceV2MaintenanceSchemaError(f"maintenance requires PerformanceDB schema v9; found {version}")
    _classify_schema_v9_tables(connection)
    try:
        require_performance_v2(connection)
    except Exception as error:
        raise PerformanceV2MaintenanceSchemaError(str(error)) from error


def _audit_reachability(connection: duckdb.DuckDBPyConnection) -> None:
    orphan_queries = (
        ("strategy_orders", "select count(*) from strategy_orders rows left join strategies using (strategy_id) where strategies.strategy_id is null"),
        ("strategy_results", "select count(*) from strategy_results rows left join strategies using (strategy_id) where strategies.strategy_id is null"),
        ("strategy_tags", "select count(*) from strategy_tags rows left join strategies using (strategy_id) where strategies.strategy_id is null"),
        ("strategy_rejection_sources", "select count(*) from strategy_rejection_sources rows left join strategies using (strategy_id) where strategies.strategy_id is null"),
        ("strategy_actions", "select count(*) from strategy_actions rows left join strategy_results using (result_id) where strategy_results.result_id is null"),
        ("strategy_equity", "select count(*) from strategy_equity rows left join strategy_results using (result_id) where strategy_results.result_id is null"),
        ("window_metrics", "select count(*) from window_metrics rows left join strategy_results using (result_id) where strategy_results.result_id is null"),
        ("optimizer_prepared_inputs", "select count(*) from optimizer_prepared_inputs rows left join strategy_results using (result_id) where strategy_results.result_id is null"),
        ("equity_quality_metrics", "select count(*) from equity_quality_metrics rows left join strategy_results using (result_id) where strategy_results.result_id is null"),
        ("selection_results", "select count(*) from selection_results rows left join selection_runs using (selection_run_id) left join strategies using (strategy_id) where selection_runs.selection_run_id is null or strategies.strategy_id is null"),
        ("selection_review_imports", "select count(*) from selection_review_imports rows left join selection_runs using (selection_run_id) where selection_runs.selection_run_id is null"),
        ("selection_review_rows", "select count(*) from selection_review_rows rows left join selection_review_imports using (review_import_id) left join strategies using (strategy_id) where selection_review_imports.review_import_id is null or strategies.strategy_id is null"),
        ("strategy_rejection_sources upstream", "select count(*) from strategy_rejection_sources rows left join strategy_results results on results.result_id = rows.first_result_id left join selection_runs runs on runs.selection_run_id = rows.first_selection_run_id where (rows.first_result_id is not null and results.result_id is null) or (rows.first_selection_run_id is not null and runs.selection_run_id is null)"),
        ("strategy_orders plateau", "select count(*) from strategy_orders rows left join analysis_plateaus plateaus on plateaus.analysis_run_id = rows.analysis_run_id and plateaus.plateau_id = rows.plateau_id where plateaus.analysis_run_id is null"),
    )
    for name, sql in orphan_queries:
        count = int(connection.execute(sql).fetchone()[0])
        if count:
            raise PerformanceV2MaintenanceError(f"{name} contains {count} orphan reference(s)")


def _audit_full_delete_result(
    connection: duckdb.DuckDBPyConnection,
    symbols: Sequence[str],
    strategy_ids: Sequence[int],
    plateau_keys: Sequence[tuple[str, str]],
) -> None:
    _audit_reachability(connection)
    for table in ("strategies", "selection_runs"):
        count = _count_where(connection, table, "symbol in (select unnest(?::varchar[]))", [list(symbols)])
        if count:
            raise PerformanceV2MaintenanceError(f"full delete left {count} selected {table} row(s)")
    if strategy_ids:
        strategy_id_array = list(strategy_ids)
        for table in ("strategy_orders", "strategy_results", "strategy_tags", "strategy_rejection_sources", "selection_results", "selection_review_rows"):
            count = _count_where(connection, table, "strategy_id in (select unnest(?::bigint[]))", [strategy_id_array])
            if count:
                raise PerformanceV2MaintenanceError(f"full delete left {count} selected strategy reference(s) in {table}")
    if plateau_keys:
        count = _count_where(
            connection,
            "analysis_plateaus",
            "exists (select 1 from (select unnest(?::varchar[]) as analysis_run_id, "
            "unnest(?::varchar[]) as plateau_id) target "
            "where target.analysis_run_id = analysis_plateaus.analysis_run_id "
            "and target.plateau_id = analysis_plateaus.plateau_id)",
            [[run_id for run_id, _ in plateau_keys], [plateau_id for _, plateau_id in plateau_keys]],
        )
        if count:
            raise PerformanceV2MaintenanceError(
                f"full delete left {count} selected target row(s) in analysis_plateaus"
            )


def _audit_pair_ownership(connection: duckdb.DuckDBPyConnection) -> None:
    row = connection.execute(
        """select actions.result_id, actions.symbol, strategies.symbol
             from strategy_actions actions
             join strategy_results results using (result_id)
             join strategies using (strategy_id)
            where actions.symbol is distinct from strategies.symbol
            limit 1"""
    ).fetchone()
    if row:
        raise PerformanceV2MaintenanceError(
            "strategy_actions.symbol does not match its owning strategy "
            f"(result_id={row[0]}, action_symbol={row[1]!r}, strategy_symbol={row[2]!r})"
        )


def catalog(connection: duckdb.DuckDBPyConnection) -> list[str]:
    _require_schema_v9(connection)
    _audit_pair_ownership(connection)
    _check_selection_ownership(connection)
    _audit_reachability(connection)
    rows = connection.execute(
        "select symbol from strategies union select symbol from selection_runs order by symbol"
    ).fetchall()
    if any(row[0] is None for row in rows):
        raise PerformanceV2MaintenanceError("catalog contains a NULL symbol")
    return [str(row[0]) for row in rows]


def _ids_by_symbol(
    connection: duckdb.DuckDBPyConnection,
    symbols: Sequence[str],
    *,
    active_only: bool = False,
) -> dict[str, list[int]]:
    grouped: dict[str, list[int]] = {symbol: [] for symbol in symbols}
    for symbol, identifier in connection.execute(
        "select symbol, strategy_id from strategies "
        "where symbol in (select unnest(?::varchar[])) "
        + ("and lifecycle_status = 'ACTIVE' " if active_only else "")
        + "order by symbol, strategy_id",
        [list(symbols)],
    ).fetchall():
        grouped[str(symbol)].append(int(identifier))
    return grouped


def _check_selection_ownership(connection: duckdb.DuckDBPyConnection) -> None:
    mismatch = connection.execute(
        """select runs.selection_run_id, rows.strategy_id, runs.symbol, strategies.symbol
             from selection_results rows
             join selection_runs runs using (selection_run_id)
             join strategies using (strategy_id)
            where runs.symbol is distinct from strategies.symbol
            limit 1"""
    ).fetchone()
    if mismatch:
        raise PerformanceV2MaintenanceError(
            "selection_results strategy does not match its owning pair "
            f"(selection_run_id={mismatch[0]!r}, strategy_id={mismatch[1]}, "
            f"run_symbol={mismatch[2]!r}, strategy_symbol={mismatch[3]!r})"
        )
    mismatch = connection.execute(
        """select imports.review_import_id, rows.strategy_id, runs.symbol, strategies.symbol
             from selection_review_rows rows
             join selection_review_imports imports using (review_import_id)
             join selection_runs runs using (selection_run_id)
             join strategies using (strategy_id)
            where runs.symbol is distinct from strategies.symbol
            limit 1"""
    ).fetchone()
    if mismatch:
        raise PerformanceV2MaintenanceError(
            "selection_review_rows strategy does not match its owning pair "
            f"(review_import_id={mismatch[0]!r}, strategy_id={mismatch[1]}, "
            f"run_symbol={mismatch[2]!r}, strategy_symbol={mismatch[3]!r})"
        )


def _normalize_symbols(
    symbols: object,
    current: Sequence[str],
    *,
    recovery_symbols: Sequence[str] = (),
) -> tuple[str, ...]:
    if not isinstance(symbols, (list, tuple)) or not symbols:
        raise PerformanceV2MaintenanceError("symbols must be a non-empty list")
    if any(not isinstance(symbol, str) or not symbol or symbol != symbol.strip() for symbol in symbols):
        raise PerformanceV2MaintenanceError("symbols must contain exact non-empty catalog symbols")
    if len(set(symbols)) != len(symbols):
        raise PerformanceV2MaintenanceError("symbols must not contain duplicates")
    unknown = sorted(set(symbols) - set(current) - set(recovery_symbols))
    if unknown:
        raise PerformanceV2MaintenanceError(f"unknown or stale symbols: {', '.join(unknown)}")
    return tuple(sorted(symbols))


def _scan_table(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    params: Sequence[object],
    *,
    collect_keys: bool = False,
) -> tuple[int, dict[str, int], str, dict[str, list[tuple[object, ...]]]]:
    """Hash and count ordered row identities in bounded batches."""
    cursor = connection.execute(f"{sql.rstrip()} order by all", list(params))
    digest = hashlib.sha256()
    counts: Counter[str] = Counter()
    collected: dict[str, list[tuple[object, ...]]] = defaultdict(list)
    count = 0
    while rows := cursor.fetchmany(4096):
        for row in rows:
            symbol = str(row[0])
            key = tuple(row[1:])
            digest.update(json.dumps([symbol, *key], default=str, separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")
            counts[symbol] += 1
            count += 1
            if collect_keys:
                collected[symbol].append(key)
    return count, dict(counts), digest.hexdigest(), collected


def _scan_global_table(connection: duckdb.DuckDBPyConnection, table: str, key_column: str) -> tuple[int, str]:
    cursor = connection.execute(f"select {key_column} from {table} order by {key_column}")
    digest = hashlib.sha256()
    count = 0
    while rows := cursor.fetchmany(4096):
        for row in rows:
            digest.update(json.dumps(row, default=str, separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")
            count += 1
    return count, digest.hexdigest()


def create_preview(
    connection: duckdb.DuckDBPyConnection,
    symbols: object,
    operation: str,
    *,
    recovery_preview: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build exact target counts and identity fingerprint without database writes."""
    if operation not in _OPERATION_TABLES:
        raise PerformanceV2MaintenanceError("operation must be 'rejected' or 'full'")
    available = catalog(connection)
    recovery_symbols: tuple[str, ...] = ()
    recovery_targets: Mapping[str, object] | None = None
    if recovery_preview is not None:
        raw_recovery_symbols = recovery_preview.get("symbols")
        raw_recovery_targets = recovery_preview.get("_targets")
        if (
            operation != "full"
            or recovery_preview.get("operation") != "full"
            or not isinstance(symbols, (list, tuple))
            or not symbols
            or any(not isinstance(symbol, str) or not symbol or symbol != symbol.strip() for symbol in symbols)
            or len(set(symbols)) != len(symbols)
            or not isinstance(raw_recovery_symbols, (list, tuple))
            or not raw_recovery_symbols
            or any(not isinstance(symbol, str) or not symbol or symbol != symbol.strip() for symbol in raw_recovery_symbols)
            or len(set(raw_recovery_symbols)) != len(raw_recovery_symbols)
            or not (set(symbols) & set(raw_recovery_symbols))
            or not isinstance(raw_recovery_targets, Mapping)
        ):
            raise PerformanceV2MaintenanceError("plateau recovery scope does not match the selected pairs")
        recovery_symbols = tuple(raw_recovery_symbols)
        recovery_targets = raw_recovery_targets
    selected = _normalize_symbols(symbols, available, recovery_symbols=recovery_symbols)
    # Full deletion sees both lifecycles. Rejected cleanup also scans both:
    # an interrupted earlier run may have archived a row before its detail
    # delete completed, and the next cleanup must remove those residual facts.
    strategy_ids_by_symbol = _ids_by_symbol(connection, selected)
    strategy_ids = [identifier for identifiers in strategy_ids_by_symbol.values() for identifier in identifiers]
    rejected_ids: set[int] = set()
    discarded_ids: set[int] = set()
    rejected_strategy_counts: Counter[str] = Counter()
    if operation == "rejected":
        decisions = effective_selection_decisions(connection)
        rejected_ids = {
            identifier for identifier in strategy_ids
            if decisions.get(identifier, (None, None, None))[0] == "REJECTED"
        }
        # A prior run removes the rejection source itself. If that run was
        # interrupted after archiving, a DISCARDED tombstone with residual
        # operational rows is still valid cleanup work even though it no
        # longer resolves through the active User Status union.
        residual_sql = " or ".join(_REJECTED_RESIDUAL_EXISTS[table] for table in _REJECTED_TABLES)
        residual_discarded_ids = {
            int(row[0]) for row in connection.execute(
                f"""select distinct strategies.strategy_id
                     from strategies
                    where strategies.strategy_id in (select unnest(?::bigint[]))
                      and strategies.lifecycle_status = 'DISCARDED'
                      and ({residual_sql})""",
                [strategy_ids],
            ).fetchall()
        }
        rejected_ids.update(residual_discarded_ids)
        # Retirement applies to every matching rejected strategy, including a
        # row whose large detail facts were already removed by an earlier run.
        # The detail-table scan below remains limited to physical rows that
        # still exist, while this complete set drives the lifecycle update.
        discarded_ids = set(rejected_ids)
        for symbol, count in connection.execute(
            f"""select strategies.symbol, count(distinct strategies.strategy_id)
                 from strategies
                 where strategies.strategy_id in (select unnest(?::bigint[]))
                   and (
                       strategies.lifecycle_status = 'ACTIVE'
                        or ({residual_sql})
                   )
                group by strategies.symbol""",
            [sorted(rejected_ids)],
        ).fetchall():
            rejected_strategy_counts[str(symbol)] = int(count)
    targets = {
        "operation": operation,
        "symbols": list(selected),
        "rejected_strategy_ids": sorted(rejected_ids),
        "discarded_strategy_ids": sorted(discarded_ids),
        "plateau_keys": [],
        "plateau_owners": {},
    }
    table_counts: Counter[str] = Counter()
    pair_counts: dict[str, Counter[str]] = {symbol: Counter() for symbol in selected}
    table_fingerprints: dict[str, tuple[int, str]] = {}
    global_counts: dict[str, int] = {}
    global_fingerprints: dict[str, tuple[int, str]] = {}
    shared_plateau_rows = 0

    if operation == "rejected":
        for table, sql in (
            ("selection_review_rows", """select strategies.symbol, rows.review_import_id, rows.strategy_id
                 from selection_review_rows rows join strategies using (strategy_id)
                where rows.strategy_id in (select unnest(?::bigint[]))"""),
            ("selection_results", """select strategies.symbol, rows.selection_run_id, rows.strategy_id
                 from selection_results rows join strategies using (strategy_id)
                where rows.strategy_id in (select unnest(?::bigint[]))"""),
            ("strategy_actions", """select strategies.symbol, actions.result_id, actions.action_index
                 from strategy_actions actions join strategy_results results using (result_id)
                 join strategies using (strategy_id)
                where results.strategy_id in (select unnest(?::bigint[]))"""),
            ("strategy_equity", """select strategies.symbol, facts.result_id, facts.sample_index
                 from strategy_equity facts join strategy_results results using (result_id)
                 join strategies using (strategy_id)
                where results.strategy_id in (select unnest(?::bigint[]))"""),
            ("optimizer_prepared_inputs", """select strategies.symbol, facts.result_id
                 from optimizer_prepared_inputs facts join strategy_results results using (result_id)
                 join strategies using (strategy_id)
                where results.strategy_id in (select unnest(?::bigint[]))"""),
            ("window_metrics", """select strategies.symbol, facts.result_id, facts.requested_start_utc,
                         facts.requested_end_utc, facts.metrics_version
                  from window_metrics facts join strategy_results results using (result_id)
                  join strategies using (strategy_id)
                 where results.strategy_id in (select unnest(?::bigint[]))"""),
            ("equity_quality_metrics", """select strategies.symbol, facts.result_id, facts.algo_version
                  from equity_quality_metrics facts join strategy_results results using (result_id)
                  join strategies using (strategy_id)
                 where results.strategy_id in (select unnest(?::bigint[]))"""),
            ("strategy_tags", """select strategies.symbol, tags.strategy_id, tags.tag
                  from strategy_tags tags join strategies using (strategy_id)
                 where tags.strategy_id in (select unnest(?::bigint[]))"""),
            ("strategy_rejection_sources", """select strategies.symbol, sources.strategy_id,
                         sources.source_kind, sources.reason_code
                  from strategy_rejection_sources sources join strategies using (strategy_id)
                 where sources.strategy_id in (select unnest(?::bigint[]))"""),
        ):
            count, grouped, digest, _ = _scan_table(connection, sql, [targets["rejected_strategy_ids"]])
            table_counts[table] = count
            table_fingerprints[table] = (count, digest)
            for symbol, value in grouped.items():
                pair_counts[symbol][table] = value
    else:
        selected_sql = "symbol in (select unnest(?::varchar[]))"
        queries = (
            ("strategies", f"select symbol, strategy_id from strategies where {selected_sql}", True),
            ("strategy_orders", f"select strategies.symbol, orders.strategy_id, orders.order_id from strategy_orders orders join strategies using (strategy_id) where strategies.{selected_sql}", False),
            ("strategy_results", f"select strategies.symbol, results.result_id, results.strategy_id from strategy_results results join strategies using (strategy_id) where strategies.{selected_sql}", False),
            ("strategy_actions", f"select strategies.symbol, actions.result_id, actions.action_index from strategy_actions actions join strategy_results results using (result_id) join strategies using (strategy_id) where strategies.{selected_sql}", False),
            ("strategy_equity", f"select strategies.symbol, facts.result_id, facts.sample_index from strategy_equity facts join strategy_results results using (result_id) join strategies using (strategy_id) where strategies.{selected_sql}", False),
            ("window_metrics", f"select strategies.symbol, facts.result_id, facts.requested_start_utc, facts.requested_end_utc, facts.metrics_version from window_metrics facts join strategy_results results using (result_id) join strategies using (strategy_id) where strategies.{selected_sql}", False),
            ("optimizer_prepared_inputs", f"select strategies.symbol, facts.result_id from optimizer_prepared_inputs facts join strategy_results results using (result_id) join strategies using (strategy_id) where strategies.{selected_sql}", False),
            ("equity_quality_metrics", f"select strategies.symbol, facts.result_id, facts.algo_version from equity_quality_metrics facts join strategy_results results using (result_id) join strategies using (strategy_id) where strategies.{selected_sql}", False),
            ("strategy_tags", f"select strategies.symbol, tags.strategy_id, tags.tag from strategy_tags tags join strategies using (strategy_id) where strategies.{selected_sql}", False),
            ("strategy_rejection_sources", f"select strategies.symbol, sources.strategy_id, sources.source_kind, sources.reason_code from strategy_rejection_sources sources join strategies using (strategy_id) where strategies.{selected_sql}", False),
            ("selection_runs", "select symbol, selection_run_id from selection_runs where " + selected_sql, False),
            ("selection_results", f"select runs.symbol, rows.selection_run_id, rows.strategy_id from selection_results rows join selection_runs runs using (selection_run_id) where runs.{selected_sql}", False),
            ("selection_review_imports", f"select runs.symbol, imports.review_import_id from selection_review_imports imports join selection_runs runs using (selection_run_id) where runs.{selected_sql}", False),
            ("selection_review_rows", f"select runs.symbol, rows.review_import_id, rows.strategy_id from selection_review_rows rows join selection_review_imports imports using (review_import_id) join selection_runs runs using (selection_run_id) where runs.{selected_sql}", False),
        )
        for table, sql, collect in queries:
            count, grouped, digest, keys = _scan_table(connection, sql, [list(selected)], collect_keys=collect)
            table_counts[table] = count
            table_fingerprints[table] = (count, digest)
            for symbol, value in grouped.items():
                pair_counts[symbol][table] = value
            if table == "strategies":
                targets["strategy_ids_by_symbol"] = {symbol: [int(key[0]) for key in values] for symbol, values in keys.items()}
                targets["strategy_ids"] = [identifier for values in targets["strategy_ids_by_symbol"].values() for identifier in values]

        plateau_cursor = connection.execute(
            """select case when count(distinct strategies.symbol) = 1 then min(strategies.symbol) else null end,
                      plateaus.analysis_run_id, plateaus.plateau_id,
                      list(distinct strategies.symbol order by strategies.symbol)
                 from analysis_plateaus plateaus
                 join strategy_orders orders on orders.analysis_run_id = plateaus.analysis_run_id and orders.plateau_id = plateaus.plateau_id
                 join strategies using (strategy_id)
                where strategies.symbol in (select unnest(?::varchar[]))
                  and not exists (
                      select 1 from strategy_orders other_orders join strategies other_strategies using (strategy_id)
                       where other_orders.analysis_run_id = plateaus.analysis_run_id
                         and other_orders.plateau_id = plateaus.plateau_id
                         and other_strategies.symbol not in (select unnest(?::varchar[]))
                  )
                group by plateaus.analysis_run_id, plateaus.plateau_id""",
            [list(selected), list(selected)],
        )
        plateau_digest = hashlib.sha256()
        while rows := plateau_cursor.fetchmany(4096):
            for owner, run_id, plateau_id, pair_owners in rows:
                owner_list = list(pair_owners or [])
                key = (str(run_id), str(plateau_id))
                targets["plateau_keys"].append(key)
                targets["plateau_owners"][key] = owner_list
                if len(owner_list) > 1:
                    shared_plateau_rows += 1
                elif owner is not None:
                    pair_counts[str(owner)]["analysis_plateaus"] += 1

        # If a prior confirmed apply committed the child-order DELETE and then
        # failed before deleting its plateau parents, the pair anchors and the
        # failed job's exact target map are still available in memory. Reuse
        # only those original keys which are now completely unreferenced.
        # A run ID alone does not establish pair ownership and is never used to
        # broaden the target set.
        if recovery_preview is not None:
            assert recovery_targets is not None
            recovered_keys = recovery_targets.get("plateau_keys", ())
            recovered_owners = recovery_targets.get("plateau_owners", {})
            if not isinstance(recovered_keys, (list, tuple)) or not isinstance(recovered_owners, Mapping):
                raise PerformanceV2MaintenanceError("plateau recovery map is invalid")
            normalized_recovery: list[tuple[str, str]] = []
            for raw_key in recovered_keys:
                if not isinstance(raw_key, (list, tuple)) or len(raw_key) != 2:
                    raise PerformanceV2MaintenanceError("plateau recovery key is invalid")
                key = (str(raw_key[0]), str(raw_key[1]))
                owners = recovered_owners.get(raw_key, recovered_owners.get(key))
                if (
                    not isinstance(owners, (list, tuple))
                    or not owners
                    or any(not isinstance(owner, str) or owner not in recovery_symbols for owner in owners)
                ):
                    raise PerformanceV2MaintenanceError("plateau recovery ownership is invalid")
                if set(owners).issubset(selected):
                    normalized_recovery.append(key)
            if normalized_recovery:
                recovered_rows = connection.execute(
                    """select plateaus.analysis_run_id, plateaus.plateau_id
                         from analysis_plateaus plateaus
                        where exists (
                            select 1 from (select unnest(?::varchar[]) as analysis_run_id,
                                                  unnest(?::varchar[]) as plateau_id) target
                             where target.analysis_run_id = plateaus.analysis_run_id
                               and target.plateau_id = plateaus.plateau_id
                        ) and not exists (
                            select 1 from strategy_orders orders
                             where orders.analysis_run_id = plateaus.analysis_run_id
                               and orders.plateau_id = plateaus.plateau_id
                        )""",
                    [[key[0] for key in normalized_recovery], [key[1] for key in normalized_recovery]],
                ).fetchall()
                for run_id, plateau_id in recovered_rows:
                    key = (str(run_id), str(plateau_id))
                    if key in targets["plateau_owners"]:
                        continue
                    source_owners = recovered_owners.get(key, recovered_owners.get((run_id, plateau_id)))
                    owner_list = [str(owner) for owner in source_owners]
                    targets["plateau_keys"].append(key)
                    targets["plateau_owners"][key] = owner_list
                    if len(owner_list) > 1:
                        shared_plateau_rows += 1
                    else:
                        pair_counts[owner_list[0]]["analysis_plateaus"] += 1

        targets["plateau_keys"] = sorted(set(targets["plateau_keys"]))
        for run_id, plateau_id in targets["plateau_keys"]:
            owner_list = targets["plateau_owners"][(run_id, plateau_id)]
            plateau_digest.update(json.dumps([run_id, plateau_id, owner_list], default=str, separators=(",", ":")).encode("utf-8"))
            plateau_digest.update(b"\n")
        plateau_count = len(targets["plateau_keys"])
        table_counts["analysis_plateaus"] = plateau_count
        table_fingerprints["analysis_plateaus"] = (plateau_count, plateau_digest.hexdigest())

        for table, key_column in (("import_files", "import_file_id"), ("import_runs", "import_run_id")):
            count, digest = _scan_global_table(connection, table, key_column)
            global_counts[table] = count
            global_fingerprints[table] = (count, digest)

    all_tables = _OPERATION_TABLES[operation]
    pair_documents = []
    for symbol in selected:
        counts = {table: int(pair_counts[symbol].get(table, 0)) for table in all_tables}
        strategy_count = counts.get("strategies", 0) if operation == "full" else rejected_strategy_counts.get(symbol, 0)
        pair_documents.append({
            "symbol": symbol,
            "strategy_count": int(strategy_count),
            "table_counts": counts,
            "rows": sum(counts.values()),
        })
    pair_scoped_total = sum(int(pair["rows"]) for pair in pair_documents) + shared_plateau_rows
    all_table_counts = {table: int(table_counts.get(table, 0)) for table in all_tables}
    fingerprint_doc = {
        "operation": operation, "symbols": list(selected),
        "rejected_strategy_ids": sorted(rejected_ids),
        "targets": dict(sorted(table_fingerprints.items())),
        "global": dict(sorted(global_fingerprints.items())),
    }
    fingerprint = hashlib.sha256(json.dumps(fingerprint_doc, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "operation": operation,
        "symbols": list(selected),
        "pairs": pair_documents,
        "table_counts": all_table_counts,
        "shared_plateau_rows": shared_plateau_rows,
        "global_counts": global_counts,
        "pair_scoped_total": pair_scoped_total,
        "_fingerprint": fingerprint,
        "_targets": targets,
    }


def public_preview(preview: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in preview.items() if not key.startswith("_")}


def _target_predicate(table: str, targets: Mapping[str, object]) -> tuple[str, list[object]]:
    symbols = list(targets["symbols"])
    rejected_ids = list(targets["rejected_strategy_ids"])
    strategy_source = (
        "select unnest(?::bigint[])" if targets.get("operation") == "rejected"
        else "select strategy_id from strategies where symbol in (select unnest(?::varchar[]))"
    )
    strategy_params: list[object] = [rejected_ids] if targets.get("operation") == "rejected" else [symbols]
    if table == "strategies":
        return "symbol in (select unnest(?::varchar[]))", [symbols]
    if table in {"strategy_orders", "strategy_tags", "strategy_rejection_sources"}:
        return f"strategy_id in ({strategy_source})", strategy_params
    if table == "strategy_results":
        return f"strategy_id in ({strategy_source})", strategy_params
    if table in {"strategy_actions", "strategy_equity", "window_metrics", "optimizer_prepared_inputs", "equity_quality_metrics"}:
        return (
            f"result_id in (select result_id from strategy_results where strategy_id in ({strategy_source}) "
            f"union all select current_result_id from strategies where strategy_id in ({strategy_source}))",
            strategy_params + strategy_params,
        )
    if table in {"selection_results", "selection_review_rows"} and targets.get("operation") == "rejected":
        return f"strategy_id in ({strategy_source})", strategy_params
    if table == "selection_review_rows":
        return (
            "review_import_id in (select review_import_id from selection_review_imports "
            "where selection_run_id in (select selection_run_id from selection_runs where symbol in (select unnest(?::varchar[]))))",
            [symbols],
        )
    if table == "selection_review_imports":
        return "selection_run_id in (select selection_run_id from selection_runs where symbol in (select unnest(?::varchar[])))", [symbols]
    if table == "selection_results":
        return "selection_run_id in (select selection_run_id from selection_runs where symbol in (select unnest(?::varchar[])))", [symbols]
    if table == "selection_runs":
        return "symbol in (select unnest(?::varchar[]))", [symbols]
    if table == "analysis_plateaus":
        keys = list(targets["plateau_keys"])
        return (
            "exists (select 1 from (select unnest(?::varchar[]) as analysis_run_id, "
            "unnest(?::varchar[]) as plateau_id) target "
            "where target.analysis_run_id = analysis_plateaus.analysis_run_id and target.plateau_id = analysis_plateaus.plateau_id)",
            [[str(row[0]) for row in keys], [str(row[1]) for row in keys]],
        )
    if table in {"import_files", "import_runs"}:
        return "true", []
    raise AssertionError(f"unsupported maintenance table {table}")


def _count_where(connection: duckdb.DuckDBPyConnection, table: str, where: str, params: Sequence[object]) -> int:
    return int(connection.execute(f"select count(*) from {table} where {where}", list(params)).fetchone()[0])


def _delete_strategies_after_legacy_v7_migration(
    connection: duckdb.DuckDBPyConnection,
    where: str,
    params: Sequence[object],
) -> None:
    """Supply the renamed FK table name left behind by the old v6->v7 migration."""
    exists = int(connection.execute(
        """select count(*) from information_schema.tables
             where table_schema = 'main' and table_name = ?""",
        [_LEGACY_V7_RESULTS_TABLE],
    ).fetchone()[0])
    if exists:
        raise PerformanceV2MaintenanceError(
            f"legacy strategy_results compatibility name already exists: {_LEGACY_V7_RESULTS_TABLE}"
        )
    row = connection.execute(
        "select sql from duckdb_tables() where schema_name = 'main' and table_name = 'strategy_results'"
    ).fetchone()
    table_sql = None if row is None else row[0]
    prefix = "CREATE TABLE strategy_results("
    if not isinstance(table_sql, str) or not table_sql.startswith(prefix):
        raise PerformanceV2MaintenanceError(
            "cannot recover legacy strategy_results foreign-key binding from its catalog definition"
        )

    compatibility_sql = table_sql.replace(
        prefix, f"CREATE TABLE {_LEGACY_V7_RESULTS_TABLE}(", 1
    )
    connection.execute("begin transaction")
    try:
        connection.execute(compatibility_sql)
        connection.execute(
            f"insert into {_LEGACY_V7_RESULTS_TABLE} select * from strategy_results"
        )
        connection.execute(f"delete from strategies where {where}", list(params))
        connection.execute(f"drop table {_LEGACY_V7_RESULTS_TABLE}")
        connection.execute("commit")
    except BaseException:
        try:
            connection.execute("rollback")
        except Exception:
            pass
        raise


def _count_by_symbol(connection: duckdb.DuckDBPyConnection, table: str, targets: Mapping[str, object]) -> tuple[dict[str, int], int]:
    where, params = _target_predicate(table, targets)
    if table in {"import_files", "import_runs"}:
        return {}, 0
    if table == "analysis_plateaus":
        keys = list(targets["plateau_keys"])
        pairs = [(str(row[0]), str(row[1])) for row in keys]
        remaining = connection.execute(
            """select analysis_run_id, plateau_id from analysis_plateaus
                where exists (select 1 from (
                                  select unnest(?::varchar[]) as analysis_run_id,
                                         unnest(?::varchar[]) as plateau_id
                              ) target
                              where target.analysis_run_id = analysis_plateaus.analysis_run_id
                                and target.plateau_id = analysis_plateaus.plateau_id)""",
            [[run for run, _ in pairs], [plateau for _, plateau in pairs]],
        ).fetchall()
        owners = targets["plateau_owners"]
        counts: Counter[str] = Counter()
        shared = 0
        # A shared physical row has no pair owner in progress; its own subtotal is reported separately.
        for run_id, plateau_id in remaining:
            row_owners = owners.get((run_id, plateau_id), [])
            if len(row_owners) == 1:
                counts[str(row_owners[0])] += 1
            elif len(row_owners) > 1:
                shared += 1
        return dict(counts), shared

    if table == "strategies":
        source, alias, symbol = "strategies", "strategies", "symbol"
    elif table in {"strategy_orders", "strategy_tags", "strategy_rejection_sources"}:
        source, alias, symbol = f"{table} child join strategies using (strategy_id)", "strategies", "symbol"
    elif table == "strategy_results":
        source, alias, symbol = "strategy_results child join strategies using (strategy_id)", "strategies", "symbol"
    elif table in {"strategy_actions", "strategy_equity", "window_metrics", "optimizer_prepared_inputs", "equity_quality_metrics"}:
        source = f"{table} child join strategy_results using (result_id) join strategies using (strategy_id)"
        alias, symbol = "strategies", "symbol"
    elif table == "selection_runs":
        source, alias, symbol = "selection_runs", "selection_runs", "symbol"
    elif table == "selection_results":
        if targets.get("operation") == "rejected":
            source, alias, symbol = "selection_results child join strategies using (strategy_id)", "strategies", "symbol"
        else:
            source, alias, symbol = "selection_results child join selection_runs using (selection_run_id)", "selection_runs", "symbol"
    elif table == "selection_review_imports":
        source, alias, symbol = "selection_review_imports child join selection_runs using (selection_run_id)", "selection_runs", "symbol"
    elif table == "selection_review_rows":
        if targets.get("operation") == "rejected":
            source, alias, symbol = "selection_review_rows child join strategies using (strategy_id)", "strategies", "symbol"
        else:
            source = "selection_review_rows child join selection_review_imports using (review_import_id) join selection_runs using (selection_run_id)"
            alias, symbol = "selection_runs", "symbol"
    else:
        raise AssertionError(f"unsupported maintenance table {table}")
    source = source.replace(" child", "")
    query = f"select {alias}.{symbol}, count(*) from {source} where {where} group by {alias}.{symbol}"
    return {str(row[0]): int(row[1]) for row in connection.execute(query, params).fetchall()}, 0


def _delete_specs(preview: Mapping[str, object]) -> tuple[str, ...]:
    if preview["operation"] == "rejected":
        return _REJECTED_TABLES
    return (
        # These rows point into both results and selection runs. Delete them
        # before either dependency so a failed later batch stays retryable.
        "strategy_rejection_sources",
        "selection_review_rows", "selection_review_imports", "selection_results",
        "strategy_actions", "strategy_equity", "optimizer_prepared_inputs", "window_metrics",
        "equity_quality_metrics", "strategy_results", "strategy_tags",
        "strategy_orders", "analysis_plateaus", "import_files", "import_runs",
        "selection_runs", "strategies",
    )


def apply_preview(
    connection: duckdb.DuckDBPyConnection,
    preview: Mapping[str, object],
    *,
    on_phase: Callable[[str], object] | None = None,
    on_commit: Callable[[dict[str, object]], object] | None = None,
) -> dict[str, object]:
    """Revalidate exact targets, then execute and commit one DELETE at a time."""
    if not isinstance(preview.get("_fingerprint"), str) or not isinstance(preview.get("_targets"), Mapping):
        raise PerformanceV2MaintenanceError("maintenance preview is invalid")
    if on_phase:
        on_phase("revalidation")
    current = create_preview(
        connection, preview.get("symbols"), str(preview.get("operation")),
        recovery_preview=preview if preview.get("operation") == "full" else None,
    )
    if current["_fingerprint"] != preview["_fingerprint"]:
        raise PerformanceV2MaintenanceError("preview is stale; request a new preview")

    targets = current["_targets"]
    rows_deleted = 0
    global_deleted = Counter()
    committed_by_table: dict[str, int] = {}
    shared_plateau_deleted = 0
    expected_table_counts = current["table_counts"]
    by_symbol = {str(pair["symbol"]): pair["table_counts"] for pair in current["pairs"]}

    def delete_batch(tables: Sequence[str]) -> None:
        nonlocal rows_deleted, shared_plateau_deleted
        states: list[dict[str, object]] = []
        for table in tables:
            is_global = table in _GLOBAL_JOURNAL_TABLES
            expected = int(
                current["global_counts"].get(table, 0) if is_global
                else expected_table_counts.get(table, 0)
            )
            where, params = _target_predicate(table, targets)
            actual_before = _count_where(connection, table, where, params)
            if actual_before != expected:
                raise PerformanceV2MaintenanceError(
                    f"target count changed before delete of {table}: expected {expected}, found {actual_before}"
                )
            expected_by_symbol = {
                symbol: int(counts.get(table, 0))
                for symbol, counts in by_symbol.items()
                if int(counts.get(table, 0))
            }
            before_by_symbol, before_shared = _count_by_symbol(connection, table, targets)
            expected_shared = int(current["shared_plateau_rows"]) if table == "analysis_plateaus" else 0
            if not is_global and (before_by_symbol != expected_by_symbol or before_shared != expected_shared):
                raise PerformanceV2MaintenanceError(f"target ownership changed before delete of {table}")
            if table == "analysis_plateaus":
                keys = list(targets["plateau_keys"])
                referenced = int(connection.execute(
                    """-- maintenance: verify plateau references
                       select count(*) from strategy_orders orders
                        where exists (
                            select 1 from (select unnest(?::varchar[]) as analysis_run_id,
                                                  unnest(?::varchar[]) as plateau_id) target
                             where target.analysis_run_id = orders.analysis_run_id
                               and target.plateau_id = orders.plateau_id
                        )""",
                    [[str(row[0]) for row in keys], [str(row[1]) for row in keys]],
                ).fetchone()[0])
                if referenced:
                    raise PerformanceV2MaintenanceError(
                        f"analysis_plateaus target became referenced by {referenced} strategy order(s)"
                    )
            states.append({
                "table": table, "global": is_global, "expected": expected,
                "where": where, "params": params, "actual_before": actual_before,
                "expected_by_symbol": expected_by_symbol, "before_by_symbol": before_by_symbol,
                "before_shared": before_shared,
            })

        if not any(int(state["expected"]) for state in states) and current["operation"] != "rejected":
            return
        if on_phase:
            for state in states:
                if int(state["expected"]):
                    on_phase(str(state["table"]))
        connection.execute("begin transaction")
        try:
            for state in states:
                table = str(state["table"])
                connection.execute(f"delete from {table} where {state['where']}", state["params"])
            if current["operation"] == "rejected":
                archive_ids = list(targets.get("discarded_strategy_ids", ()))
                if archive_ids:
                    # Keep only the temporal identity needed to compare an
                    # equal/narrower/wider future report.  The result row is
                    # otherwise reduced to a tiny dedup tombstone.  Exchange
                    # remains the original provenance value; lifecycle_status
                    # is the tombstone marker and avoids a fake domain value.
                    connection.execute(
                        """update strategy_results
                              set commission_rate = null,
                                  initial_balance = 0,
                                  final_balance = 0,
                                  total_pnl = null,
                                  total_pnl_pct = null,
                                  max_drawdown = null,
                                  max_drawdown_pct = null,
                                  total_fees = null,
                                  total_trades = null,
                                  excluded_trade_count = null,
                                  exclusion_reason = null,
                                  optimizer_source_metadata_json = null,
                                  sizing_use_upnl = null,
                                  sizing_use_frozen_balance = null,
                                  sizing_use_fix = null,
                                  sizing_balance_percentage_long = null,
                                  sizing_risk_long = null,
                                  sizing_max_balance = null
                            where strategy_id in (select unnest(?::BIGINT[]))""",
                        [archive_ids],
                    )
                    connection.execute(
                        "update strategies set lifecycle_status = 'DISCARDED' "
                        "where strategy_id in (select unnest(?::BIGINT[]))",
                        [archive_ids],
                    )
            connection.execute("commit")
        except BaseException as error:
            try:
                connection.execute("rollback")
            except Exception:
                pass
            error_text = str(error).casefold().replace('"', "").replace("'", "").replace("`", "")
            legacy_v7_name_missing = (
                len(states) == 1
                and states[0]["table"] == "strategies"
                and isinstance(error, duckdb.CatalogException)
                and "table with name" in error_text
                and _LEGACY_V7_RESULTS_TABLE.casefold() in error_text
                and "does not exist" in error_text
            )
            if not legacy_v7_name_missing:
                raise
            _delete_strategies_after_legacy_v7_migration(
                connection, str(states[0]["where"]), states[0]["params"],
            )

        for state in states:
            table = str(state["table"])
            is_global = bool(state["global"])
            where = str(state["where"])
            params = state["params"]
            expected = int(state["expected"])
            expected_by_symbol = state["expected_by_symbol"]
            remaining = _count_where(connection, table, where, params)
            committed = int(state["actual_before"]) - remaining
            remaining_by_symbol, remaining_shared = _count_by_symbol(connection, table, targets)
            actual_by_symbol = {
                symbol: int(expected_by_symbol.get(symbol, 0)) - remaining_by_symbol.get(symbol, 0)
                for symbol in set(expected_by_symbol) | set(remaining_by_symbol)
            }
            actual_shared = int(state["before_shared"]) - remaining_shared if table == "analysis_plateaus" else 0
            if is_global:
                global_deleted[table] += committed
                event = {"table": table, "rows": committed, "rows_by_symbol": {}, "global": True}
            else:
                rows_deleted += committed
                committed_by_table[table] = committed_by_table.get(table, 0) + committed
                if table == "analysis_plateaus":
                    shared_plateau_deleted = actual_shared
                event = {
                    "table": table, "rows": committed, "rows_by_symbol": actual_by_symbol,
                    "shared_plateau_rows": actual_shared, "global": False,
                }
            if on_commit and committed:
                on_commit(event)
            expected_shared = int(current["shared_plateau_rows"]) if table == "analysis_plateaus" else 0
            if (
                remaining
                or committed != expected
                or any(actual_by_symbol.get(symbol, 0) != count for symbol, count in expected_by_symbol.items())
                or actual_shared != expected_shared
            ):
                raise PerformanceV2MaintenanceError(
                    f"DELETE count mismatch for {table}: expected {expected}, committed {committed}, remaining {remaining}"
                )

    delete_specs = _delete_specs(current)
    if current["operation"] == "rejected":
        delete_batch(delete_specs)
    else:
        for table in delete_specs:
            delete_batch((table,))

    if current["operation"] == "full":
        _audit_full_delete_result(
            connection,
            targets["symbols"],
            targets.get("strategy_ids", ()),
            targets.get("plateau_keys", ()),
        )

    return {
        "pair_scoped_deleted": rows_deleted,
        "global_deleted": dict(global_deleted),
        "table_counts": committed_by_table,
        "shared_plateau_deleted": shared_plateau_deleted,
    }

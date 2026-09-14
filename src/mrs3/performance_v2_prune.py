from __future__ import annotations

from datetime import UTC, date, datetime
import shutil
from pathlib import Path
from typing import Any

import duckdb

from .performance_v2_store import (
    PerformanceV2Config,
    PerformanceV2StoreError,
    PerformanceV2WriterLock,
    performance_v2_database_path,
    require_performance_v2,
)


DEFAULT_CUTOFF = "2026-09-06"
_CHILD_TABLES = (
    "window_metrics",
    "strategy_actions",
    "strategy_equity",
    "strategy_results",
    "strategy_tags",
    "strategy_orders",
)


class PerformanceV2PruneError(PerformanceV2StoreError):
    """Raised when Performance v2 cannot be safely inspected or pruned."""


def _cutoff_utc(value: str | date | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if not isinstance(value, str):
        raise PerformanceV2PruneError("cutoff must be a YYYY-MM-DD date")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise PerformanceV2PruneError("cutoff must be a YYYY-MM-DD date") from error
    return parsed.replace(tzinfo=UTC)


def _target_path(target: Path | PerformanceV2Config) -> Path:
    try:
        path = performance_v2_database_path(target) if isinstance(target, PerformanceV2Config) else Path(target)
    except (TypeError, ValueError) as error:
        raise PerformanceV2PruneError("invalid Performance v2 database target") from error
    return path.resolve()


def _open_read_only(target: Path) -> duckdb.DuckDBPyConnection:
    try:
        if not target.is_file() or target.stat().st_size == 0:
            raise PerformanceV2PruneError("Performance v2 database does not exist")
    except OSError as error:
        raise PerformanceV2PruneError("Performance v2 database could not be read") from error
    try:
        connection = duckdb.connect(str(target), read_only=True)
    except duckdb.Error as error:
        raise PerformanceV2PruneError("Performance v2 database could not be opened") from error
    try:
        require_performance_v2(connection)
    except PerformanceV2StoreError as error:
        connection.close()
        raise PerformanceV2PruneError("Performance v2 database has invalid schema") from error
    except duckdb.Error as error:
        connection.close()
        raise PerformanceV2PruneError("Performance v2 database has invalid schema") from error
    return connection


def _plan(connection: duckdb.DuckDBPyConnection, cutoff: datetime) -> tuple[list[int], int, dict[str, int]]:
    rows = connection.execute(
        """
        with latest_reviewed as (
            select runs.symbol, runs.side, imports.review_import_id,
                   row_number() over (
                       partition by runs.symbol, runs.side
                       order by runs.created_at_utc desc, runs.selection_run_id desc,
                                imports.imported_at_utc desc, imports.review_import_id desc
                   ) as review_order
              from selection_runs runs
              join selection_review_imports imports using (selection_run_id)
        ), user_finalists as (
            select distinct rows.strategy_id
              from latest_reviewed reviewed
              join selection_review_rows rows using (review_import_id)
             where reviewed.review_order = 1 and rows.user_status = 'FINALIST'
        )
        select s.strategy_id
        from strategies s
        where not (
            exists (
                select 1 from strategy_results r
                where r.strategy_id = s.strategy_id and r.report_end_utc >= ?
            )
            or not exists (
                select 1 from strategy_results r where r.strategy_id = s.strategy_id
            )
            or s.strategy_id in (select strategy_id from user_finalists)
        )
        order by s.strategy_id
        """,
        [cutoff],
    ).fetchall()
    stale_ids = [int(row[0]) for row in rows]
    total = int(connection.execute("select count(*) from strategies").fetchone()[0])
    protected = total - len(stale_ids)
    counts = {table: 0 for table in _CHILD_TABLES}
    counts["strategies"] = len(stale_ids)
    if not stale_ids:
        return stale_ids, protected, counts
    for table in ("strategy_results", "strategy_orders", "strategy_tags"):
        counts[table] = int(
            connection.execute(
                f"select count(*) from {table} where strategy_id in (select unnest(?::BIGINT[]))",
                [stale_ids],
            ).fetchone()[0]
        )
    for table in ("window_metrics", "strategy_actions", "strategy_equity"):
        counts[table] = int(
            connection.execute(
                f"""
                select count(*) from {table} child
                where child.result_id in (
                    select result_id from strategy_results
                    where strategy_id in (select unnest(?::BIGINT[]))
                )
                """,
                [stale_ids],
            ).fetchone()[0]
        )
    return stale_ids, protected, counts


def _delete(connection: duckdb.DuckDBPyConnection, stale_ids: list[int]) -> None:
    if not stale_ids:
        return
    result_ids = [
        int(row[0])
        for row in connection.execute(
            "select result_id from strategy_results where strategy_id in (select unnest(?::BIGINT[]))",
            [stale_ids],
        ).fetchall()
    ]
    for table in ("window_metrics", "strategy_actions", "strategy_equity"):
        if result_ids:
            connection.execute(
                f"delete from {table} where result_id in (select unnest(?::BIGINT[]))",
                [result_ids],
            )
    connection.execute(
        "delete from strategy_results where strategy_id in (select unnest(?::BIGINT[]))",
        [stale_ids],
    )
    for table in ("strategy_tags", "strategy_orders"):
        connection.execute(
            f"delete from {table} where strategy_id in (select unnest(?::BIGINT[]))",
            [stale_ids],
        )
    connection.execute(
        "delete from strategies where strategy_id in (select unnest(?::BIGINT[]))",
        [stale_ids],
    )


def _backup(target: Path, cutoff: datetime) -> Path:
    if Path(f"{target}.wal").exists():
        raise PerformanceV2PruneError("Performance v2 database must be checkpointed before backup")
    backup_root = target.parent / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = backup_root / f"{target.stem}.prune-{cutoff:%Y%m%d}-{stamp}.duckdb"
    try:
        candidate.touch(exist_ok=False)
        shutil.copy2(target, candidate)
        if candidate.stat().st_size != target.stat().st_size:
            raise OSError("backup size does not match source")
        validation = _open_read_only(candidate)
        validation.close()
    except Exception as error:
        candidate.unlink(missing_ok=True)
        raise PerformanceV2PruneError("could not create Performance v2 backup") from error
    return candidate


def _restore(backup: Path, target: Path) -> None:
    try:
        Path(f"{target}.wal").unlink(missing_ok=True)
        shutil.copy2(backup, target)
    except Exception as error:
        raise PerformanceV2PruneError(
            f"Performance v2 prune failed and automatic restore failed; backup: {backup}"
        ) from error


def _result(
    *,
    target: Path,
    cutoff: datetime,
    mode: str,
    protected: int,
    counts: dict[str, int],
    backup_path: Path | None = None,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "database_path": str(target),
        "cutoff_utc": cutoff.isoformat().replace("+00:00", "Z"),
        "protected_strategies": protected,
        "counts": counts,
        "backup_path": None if backup_path is None else str(backup_path),
    }


def prune_performance_v2(
    target: Path | PerformanceV2Config,
    cutoff: str | date | datetime = DEFAULT_CUTOFF,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or safely remove stale unprotected Performance v2 rows."""
    database = _target_path(target)
    cutoff_utc = _cutoff_utc(cutoff)
    if not apply:
        connection = _open_read_only(database)
        try:
            stale_ids, protected, counts = _plan(connection, cutoff_utc)
        except duckdb.Error as error:
            raise PerformanceV2PruneError("Performance v2 database has invalid schema") from error
        finally:
            connection.close()
        return _result(
            target=database,
            cutoff=cutoff_utc,
            mode="preview",
            protected=protected,
            counts=counts,
        )

    try:
        with PerformanceV2WriterLock(database.parent):
            validation = _open_read_only(database)
            validation.close()
            try:
                checkpoint = duckdb.connect(str(database))
                try:
                    require_performance_v2(checkpoint)
                    checkpoint.execute("checkpoint")
                finally:
                    checkpoint.close()
            except PerformanceV2StoreError as error:
                raise PerformanceV2PruneError("Performance v2 database has invalid schema") from error
            except duckdb.Error as error:
                raise PerformanceV2PruneError("Performance v2 database could not be checkpointed") from error
            backup_path = _backup(database, cutoff_utc)
            writable: duckdb.DuckDBPyConnection | None = None
            try:
                writable = duckdb.connect(str(database))
                require_performance_v2(writable)
                stale_ids, protected, counts = _plan(writable, cutoff_utc)
                # DuckDB rejects a referenced parent delete after deleting its children
                # in the same transaction. The verified file backup is the atomic boundary.
                _delete(writable, stale_ids)
            except BaseException as error:
                if writable is not None:
                    try:
                        writable.close()
                    except Exception:
                        pass
                    writable = None
                _restore(backup_path, database)
                if not isinstance(error, Exception):
                    raise
                if isinstance(error, PerformanceV2StoreError):
                    raise PerformanceV2PruneError("Performance v2 database has invalid schema") from error
                raise PerformanceV2PruneError(
                    f"Performance v2 prune failed; restored from {backup_path}"
                ) from error
            finally:
                if writable is not None:
                    try:
                        writable.close()
                    except Exception:
                        pass
    except PerformanceV2PruneError:
        raise
    except PerformanceV2StoreError as error:
        raise PerformanceV2PruneError(str(error)) from error
    except duckdb.Error as error:
        raise PerformanceV2PruneError("Performance v2 prune failed") from error
    return _result(
        target=database,
        cutoff=cutoff_utc,
        mode="apply",
        protected=protected,
        counts=counts,
        backup_path=backup_path,
    )


__all__ = [
    "DEFAULT_CUTOFF",
    "PerformanceV2PruneError",
    "prune_performance_v2",
]

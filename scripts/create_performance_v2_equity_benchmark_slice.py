"""Create a bounded v6 slice from a read-only v5/v6 source; output FS must support hard links."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Sequence
import uuid

import duckdb

from mrs3.performance_v2_store import initialize_performance_v2, require_performance_v2_readable


ROOT = Path(__file__).resolve().parents[1]
_OMITTED_TABLES = (
    "import_runs", "import_files", "selection_runs", "selection_results",
    "selection_review_imports", "selection_review_rows", "optimizer_prepared_inputs",
)


def _stat(path: Path) -> dict[str, int]:
    info = path.stat()
    return {"size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns}


def _source_path(path: Path) -> Path:
    source = path.expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in {".duckdb", ".db"}:
        raise ValueError("source must be an existing DuckDB file")
    if any(source.with_name(source.name + suffix).exists() for suffix in (".wal", "-wal")):
        raise ValueError("source has a WAL sidecar; checkpoint and freeze the copy first")
    return source


def _output_path(path: Path, source: Path) -> Path:
    requested = path.expanduser()
    if requested.is_symlink():
        raise FileExistsError(requested)
    output = requested.resolve(strict=False)
    if output.is_relative_to(ROOT.resolve()):
        raise ValueError("output must be outside the repository")
    if output == source or (output.exists() and os.path.samefile(output, source)):
        raise ValueError("output aliases the source database")
    if output.exists():
        raise FileExistsError(output)
    if output.suffix.lower() not in {".duckdb", ".db"}:
        raise ValueError("output must have a .duckdb or .db suffix")
    if not output.parent.is_dir():
        raise ValueError("output parent directory must already exist")
    return output


def _marks(values: Sequence[int]) -> str:
    return ", ".join("?" for _ in values)


def _copy_query(target, table: str, query: str, parameters: Sequence[object]) -> int:
    columns = [row[0] for row in target.execute(f"describe frozen_source.main.{table}").fetchall()]
    insert_select = f"insert into {table} ({', '.join(columns)}) {query}"
    result = target.execute(insert_select, list(parameters)).fetchone()
    # DuckDB reports the number of rows inserted as the INSERT result.
    return int(result[0]) if result else 0


def _count(connection, table: str) -> int:
    return int(connection.execute(f"select count(*) from {table}").fetchone()[0])


def _source_scoped_count(connection, table: str, column: str, ids: Sequence[int]) -> int:
    return int(connection.execute(
        f"select count(*) from {table} where {column} in ({_marks(ids)})", list(ids),
    ).fetchone()[0])


def _source_plateau_count(connection, strategy_ids: Sequence[int]) -> int:
    return int(connection.execute(
        """select count(*) from (
               select distinct p.analysis_run_id, p.plateau_id
                 from analysis_plateaus p join strategy_orders o
                   on o.analysis_run_id = p.analysis_run_id and o.plateau_id = p.plateau_id
                where o.strategy_id in (""" + _marks(strategy_ids) + "))",
        list(strategy_ids),
    ).fetchone()[0])


def _cleanup_spill_directory(path: Path, output: Path) -> None:
    parent = output.parent.resolve()
    if path.parent.resolve() != parent or not path.name.startswith(f".{output.name}.") or not path.name.endswith(".spill"):
        raise RuntimeError("refusing to clean an unexpected DuckDB spill path")
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        resolved = path.resolve(strict=True)
        if resolved.parent != parent:
            raise RuntimeError("refusing to clean a DuckDB spill path outside the output directory")
        shutil.rmtree(path)


def create_benchmark_slice(
    source_path: Path | str,
    output_path: Path | str,
    *,
    symbol: str,
    side: str,
    limit: int = 512,
) -> dict[str, object]:
    """Copy a deterministic sample of ACTIVE strategies and their result facts."""
    if not symbol.strip():
        raise ValueError("symbol must not be empty")
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if not 1 <= limit <= 512:
        raise ValueError("limit must be between 1 and 512")
    source_path = _source_path(Path(source_path))
    output_path = _output_path(Path(output_path), source_path)
    source_before = _stat(source_path)
    source_identity = source_path.stat()
    source_identity = (source_identity.st_dev, source_identity.st_ino)
    token = uuid.uuid4().hex
    temp_path = output_path.with_name(f".{output_path.name}.{token}.tmp")
    spill_directory = output_path.with_name(f".{output_path.name}.{token}.spill").resolve(strict=False)
    if spill_directory.parent != output_path.parent.resolve() or spill_directory.exists():
        raise FileExistsError(spill_directory)
    spill_directory.mkdir()

    try:
        with duckdb.connect(str(source_path), read_only=True) as source:
            source_version = require_performance_v2_readable(source)
            candidates = source.execute(
                """select s.strategy_id, s.current_result_id
                     from strategies s join strategy_results r
                       on r.result_id = s.current_result_id and r.strategy_id = s.strategy_id
                    where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?
                    order by s.strategy_id""",
                [symbol, side],
            ).fetchall()
            if not candidates:
                raise ValueError(f"source has no ACTIVE current-result strategies for {symbol}/{side}")
            candidates.sort(key=lambda row: (hashlib.sha256(str(int(row[0])).encode("ascii")).digest(), int(row[0])))
            selected_pairs = [(int(strategy_id), int(result_id)) for strategy_id, result_id in candidates[:limit]]
            strategy_ids = sorted(strategy_id for strategy_id, _ in selected_pairs)
            result_ids = sorted(result_id for _, result_id in selected_pairs)
            source_scoped_counts = {
                table: _source_scoped_count(source, table, "result_id", result_ids)
                for table in ("strategy_actions", "strategy_equity", "window_metrics")
            }
            source_scoped_counts.update({
                "strategy_orders": _source_scoped_count(source, "strategy_orders", "strategy_id", strategy_ids),
                "strategy_tags": _source_scoped_count(source, "strategy_tags", "strategy_id", strategy_ids),
                "analysis_plateaus": _source_plateau_count(source, strategy_ids),
                "equity_quality_metrics": (
                    _source_scoped_count(source, "equity_quality_metrics", "result_id", result_ids)
                    if source_version == 6 else 0
                ),
            })

        target = duckdb.connect(str(temp_path))
        try:
            initialize_performance_v2(target)
            spill_sql_path = spill_directory.as_posix().replace("'", "''")
            target.execute(f"set temp_directory = '{spill_sql_path}'")
            source_sql_path = source_path.as_posix().replace("'", "''")
            target.execute(f"attach '{source_sql_path}' as frozen_source (read_only)")
            copied_tables = (
                "strategies", "strategy_results", "analysis_plateaus", "strategy_orders",
                "strategy_actions", "strategy_equity", "window_metrics", "strategy_tags",
            ) + (("equity_quality_metrics",) if source_version == 6 else ())
            for table in copied_tables:
                source_columns = {
                    str(name): str(column_type)
                    for name, column_type, *_ in target.execute(f"describe frozen_source.main.{table}").fetchall()
                }
                target_columns = {
                    str(name): str(column_type)
                    for name, column_type, *_ in target.execute(f"describe main.{table}").fetchall()
                }
                if source_columns != target_columns:
                    raise RuntimeError(f"source/target column schema mismatch for {table}")
            target.execute("begin transaction")
            counts: dict[str, int] = {}
            strategy_marks = _marks(strategy_ids)
            result_marks = _marks(result_ids)
            counts["strategies"] = _copy_query(
                target, "strategies",
                f"select * from frozen_source.main.strategies where strategy_id in ({strategy_marks}) order by strategy_id",
                strategy_ids,
            )
            counts["strategy_results"] = _copy_query(
                target, "strategy_results",
                f"select * from frozen_source.main.strategy_results where result_id in ({result_marks}) order by result_id",
                result_ids,
            )
            counts["analysis_plateaus"] = _copy_query(
                target, "analysis_plateaus",
                f"select distinct p.* from frozen_source.main.analysis_plateaus p join frozen_source.main.strategy_orders o "
                f"on o.analysis_run_id = p.analysis_run_id and o.plateau_id = p.plateau_id "
                f"where o.strategy_id in ({strategy_marks}) order by p.analysis_run_id, p.plateau_id",
                strategy_ids,
            )
            counts["strategy_orders"] = _copy_query(
                target, "strategy_orders",
                f"select * from frozen_source.main.strategy_orders where strategy_id in ({strategy_marks}) order by strategy_id, order_id",
                strategy_ids,
            )
            for table in ("strategy_actions", "strategy_equity", "window_metrics"):
                counts[table] = _copy_query(
                    target, table,
                    f"select * from frozen_source.main.{table} where result_id in ({result_marks})", result_ids,
                )
            if source_version == 6:
                counts["equity_quality_metrics"] = _copy_query(
                    target, "equity_quality_metrics",
                    f"select * from frozen_source.main.equity_quality_metrics where result_id in ({result_marks})",
                    result_ids,
                )
            else:
                counts["equity_quality_metrics"] = 0
            counts["strategy_tags"] = _copy_query(
                target, "strategy_tags",
                f"select * from frozen_source.main.strategy_tags where strategy_id in ({strategy_marks}) order by strategy_id, tag",
                strategy_ids,
            )
            target.execute("commit")
            target.execute("checkpoint")
        except BaseException:
            try:
                target.execute("rollback")
            except duckdb.Error:
                pass
            raise
        finally:
            target.close()
        _cleanup_spill_directory(spill_directory, output_path)

        with duckdb.connect(str(temp_path), read_only=True) as check:
            if require_performance_v2_readable(check) != 6:
                raise RuntimeError("benchmark slice is not a readable v6 database")
            actual_tables = {
                row[0] for row in check.execute(
                    "select table_name from information_schema.tables where table_schema = 'main'"
                ).fetchall()
            }
            declared_tables = set(counts) | set(_OMITTED_TABLES) | {"schema_info"}
            if declared_tables != actual_tables:
                raise RuntimeError("benchmark slice table catalog has unclassified tables")
            actual_ids = [int(row[0]) for row in check.execute("select strategy_id from strategies order by strategy_id").fetchall()]
            if actual_ids != strategy_ids:
                raise RuntimeError("benchmark slice strategy IDs do not match the selected cohort")
            actual_pairs = [
                (int(strategy_id), int(result_id))
                for strategy_id, result_id in check.execute(
                    "select strategy_id, current_result_id from strategies order by strategy_id"
                ).fetchall()
            ]
            if actual_pairs != sorted(selected_pairs):
                raise RuntimeError("benchmark slice current-result pairs do not match the selected cohort")
            for table, expected in counts.items():
                if _count(check, table) != expected:
                    raise RuntimeError(f"benchmark slice row-count validation failed for {table}")
            for table, expected in source_scoped_counts.items():
                if _count(check, table) != expected:
                    raise RuntimeError(f"benchmark slice source-scoped count mismatch for {table}")
            target_version = 6

        _source_path(source_path)
        after_info = source_path.stat()
        if _stat(source_path) != source_before or (after_info.st_dev, after_info.st_ino) != source_identity:
            raise RuntimeError("source database stat changed during extraction")

        if any(Path(str(temp_path) + suffix).exists() for suffix in (".wal", "-wal")):
            raise RuntimeError("temporary benchmark database has a WAL sidecar")

        # Hard-link creation is atomic and fails instead of replacing a racing output.
        os.link(temp_path, output_path)
        try:
            temp_path.unlink()
        except OSError:
            # The published file is complete; do not turn temp cleanup into a false failure.
            pass
        return {
            "source": {"path": str(source_path), **source_before},
            "source_stat_unchanged": True,
            "source_schema_version": source_version,
            "output": {"path": str(output_path), **_stat(output_path)},
            "output_schema_version": target_version,
            "symbol": symbol,
            "side": side,
            "limit": limit,
            "selected_count": len(strategy_ids),
            "selected_strategy_ids": strategy_ids,
            "selected_ids_sha256": hashlib.sha256(("\n".join(map(str, strategy_ids)) + "\n").encode("ascii")).hexdigest(),
            "table_counts": counts,
            "source_scoped_counts": source_scoped_counts,
            "v6_only_columns": {},
            "analysis_plateaus_scope": "order_referenced",
            "omitted_tables": list(_OMITTED_TABLES),
        }
    finally:
        for candidate in (temp_path, Path(str(temp_path) + ".wal"), Path(str(temp_path) + "-wal")):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # Best-effort cleanup must not hide the extraction/publish failure.
                pass
        try:
            _cleanup_spill_directory(spill_directory, output_path)
        except OSError:
            # Preserve the original failure if temporary spill cleanup is unavailable.
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--side", required=True, choices=("LONG", "SHORT"))
    parser.add_argument("--limit", type=int, default=512)
    args = parser.parse_args(argv)
    print(json.dumps(create_benchmark_slice(
        args.source, args.output, symbol=args.symbol, side=args.side, limit=args.limit,
    ), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

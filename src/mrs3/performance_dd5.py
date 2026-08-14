from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import uuid

import duckdb
import pandas as pd

from .config import AlgorithmConfig
from .posttest import compare_posttest, write_posttest_outputs


@dataclass(frozen=True, slots=True)
class PerformanceDd5Artifacts:
    workbook: Path
    csv_directory: Path
    manifest: Path
    manifest_json: dict[str, object]
    dd5_run_id: str


def _lots(value: object) -> list[Decimal]:
    found: list[Decimal] = []
    if isinstance(value, dict):
        if "lot_x" in value:
            found.append(Decimal(str(value["lot_x"])))
        for item in value.values():
            found.extend(_lots(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_lots(item))
    return found


def _read_rows(database: Path, import_id: str) -> list[dict[str, object]]:
    with duckdb.connect(str(database), read_only=True) as connection:
        run = connection.execute(
            "select status, quarantined_count from import_runs where import_id = ?",
            [import_id],
        ).fetchone()
        if run is None or run[0] != "COMMITTED" or run[1] != 0:
            raise ValueError("DD5 requires a committed import with zero quarantine")
        rows = connection.execute(
            """
            select r.test_run_id, s.strategy_name, s.settings_json,
                   m.total_pnl_pct, m.max_drawdown_pct, m.win_rate_pct,
                   m.profit_factor, m.total_trades, m.days_in_test
            from backtest_runs r
            join backtest_metrics m on m.test_run_id = r.test_run_id
            join strategy_versions s on s.strategy_version_id = r.strategy_version_id
            join import_files f on f.test_run_id = r.test_run_id
            where f.import_id = ? and f.status in ('IMPORTED', 'SKIPPED')
            order by r.test_run_id
            """,
            [import_id],
        ).fetchall()
    if not rows:
        raise ValueError("committed import has no backtest results")
    result: list[dict[str, object]] = []
    for test_run_id, name, settings_json, pnl, dd, win_rate, profit_factor, trades, days in rows:
        settings = json.loads(settings_json)
        lots = _lots(settings)
        if not lots:
            raise ValueError(f"no lot_x values for strategy: {name}")
        result.append(
            {
                "test_run_id": test_run_id,
                "strategy_name": name,
                "pnl_pct": Decimal(str(pnl)),
                "dd_pct": Decimal(str(dd)),
                "win_rate_pct": Decimal(str(win_rate)),
                "profit_factor": Decimal(str(profit_factor)),
                "trades": int(trades),
                "effective_days": Decimal(str(days)),
                "lots": lots,
            }
        )
    return result


def _verify_dd5_readback(database: Path, dd5_run_id: str, import_id: str, expected_count: int) -> None:
    with duckdb.connect(str(database), read_only=True) as connection:
        run = connection.execute(
            "select import_id, status, input_test_count from dd5_runs where dd5_run_id = ?",
            [dd5_run_id],
        ).fetchone()
        result_count = connection.execute(
            "select count(*) from dd5_results where dd5_run_id = ?",
            [dd5_run_id],
        ).fetchone()[0]
    if run != (import_id, "CALCULATION_ONLY", expected_count) or result_count != expected_count:
        raise ValueError("DD5 readback verification failed")


def _read_persisted_results(database: Path, dd5_run_id: str, import_id: str, config: AlgorithmConfig):
    with duckdb.connect(str(database), read_only=True) as connection:
        run = connection.execute(
            "select import_id, status, input_test_count from dd5_runs where dd5_run_id = ?", [dd5_run_id]
        ).fetchone()
        rows = connection.execute(
            "select test_run_id, raw_json, projected_pnl_dd5, projected_dd_pct, projected_pnl30_dd5, pareto_rank, pareto from dd5_results where dd5_run_id = ? order by test_run_id",
            [dd5_run_id],
        ).fetchall()
    if run is None or run[0] != import_id or run[1] != "CALCULATION_ONLY" or run[2] != len(rows) or not rows:
        raise ValueError("DD5 persisted run readback failed")
    raw_rows: list[dict[str, object]] = []
    persisted: dict[str, tuple[object, ...]] = {}
    for test_run_id, raw_json, projected_pnl, projected_dd, projected_pnl30, pareto_rank, pareto in rows:
        raw = json.loads(raw_json)
        raw["test_run_id"] = test_run_id
        for field in ("pnl_pct", "dd_pct", "win_rate_pct", "profit_factor", "effective_days"):
            raw[field] = Decimal(str(raw[field]))
        raw["lots"] = [Decimal(str(value)) for value in raw["lots"]]
        raw_rows.append(raw)
        persisted[test_run_id] = (projected_pnl, projected_dd, projected_pnl30, pareto_rank, pareto)
    raw_frame = pd.DataFrame(raw_rows).drop(columns=["lots"])
    variants = pd.DataFrame([{"strategy_name": row["strategy_name"], "lots": row["lots"]} for row in raw_rows])
    tables = compare_posttest(raw_frame, variants, config)
    for row in tables.comparison.to_dict(orient="records"):
        stored = persisted[row["test_run_id"]]
        if stored[4] != bool(row["pareto"]) or stored[3] != int(row["near_tie_rank"]):
            raise ValueError("DD5 persisted result readback failed")
        for actual, expected in zip(stored[:3], (row["projected_pnl_dd5"], row["projected_dd_pct"], row["pnl30_dd5"]), strict=True):
            if Decimal(str(actual)) != Decimal(str(expected)):
                raise ValueError("DD5 persisted result readback failed")
    return tables


def run_performance_dd5(
    database: Path,
    import_id: str,
    output_dir: Path,
    config: AlgorithmConfig,
) -> PerformanceDd5Artifacts:
    database = Path(database).resolve()
    rows = _read_rows(database, import_id)
    raw = pd.DataFrame(rows).drop(columns=["lots"])
    variants = pd.DataFrame(
        [{"strategy_name": row["strategy_name"], "lots": row["lots"]} for row in rows]
    )
    tables = compare_posttest(raw, variants, config)
    output_dir = Path(output_dir).resolve()

    dd5_run_id = uuid.uuid4().hex
    manifest_json: dict[str, object] = {
        "database": database.name,
        "import_id": import_id,
        "dd5_run_id": dd5_run_id,
        "raw_result_count": len(tables.raw),
        "pareto_count": int(tables.comparison["pareto"].sum()),
        "target_dd_pct": str(config.target_dd_pct),
        "dd5_mode": "CALCULATION_ONLY",
        "scaled_strategy_count": 0,
        "scaled_strategies_require_retest": False,
    }
    with duckdb.connect(str(database)) as connection:
        connection.execute("begin transaction")
        try:
            connection.execute(
                "insert into dd5_runs values (?, ?, ?, ?, ?, ?, ?)",
                [
                    dd5_run_id,
                    import_id,
                    datetime.now(timezone.utc),
                    config.target_dd_pct,
                    json.dumps({"target_dd_pct": str(config.target_dd_pct)}),
                    len(tables.normalized),
                    "CALCULATION_ONLY",
                ],
            )
            for row in tables.comparison.to_dict(orient="records"):
                raw_json = {
                    key: ([str(value) for value in row[key]] if key == "lots" else str(row[key]) if isinstance(row[key], Decimal) else row[key])
                    for key in ("test_run_id", "strategy_name", "pnl_pct", "dd_pct", "win_rate_pct", "profit_factor", "trades", "effective_days", "lots")
                }
                connection.execute(
                    "insert into dd5_results (dd5_run_id, test_run_id, projected_pnl_dd5, projected_dd_pct, projected_pnl30_dd5, scaled_lots_json, capital_requirement_proxy, holding_filter, pareto_rank, raw_json, pareto) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        dd5_run_id,
                        row["test_run_id"],
                        row["projected_pnl_dd5"],
                        row["projected_dd_pct"],
                        row["pnl30_dd5"],
                        json.dumps([str(item) for item in row["scaled_lots"]]),
                        row["capital_requirement_proxy"],
                        None,
                        int(row["near_tie_rank"]),
                        json.dumps(raw_json, sort_keys=True),
                        bool(row["pareto"]),
                    ],
                )
            connection.execute("commit")
        except Exception:
            connection.execute("rollback")
            raise

    _verify_dd5_readback(database, dd5_run_id, import_id, len(tables.normalized))
    tables = _read_persisted_results(database, dd5_run_id, import_id, config)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_posttest_outputs(tables, output_dir)
    manifest = output_dir / "posttest_manifest.json"
    manifest.write_text(json.dumps(manifest_json, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return PerformanceDd5Artifacts(
        workbook=output_dir / "posttest.xlsx",
        csv_directory=output_dir / "posttest_csv",
        manifest=manifest,
        manifest_json=manifest_json,
        dd5_run_id=dd5_run_id,
    )

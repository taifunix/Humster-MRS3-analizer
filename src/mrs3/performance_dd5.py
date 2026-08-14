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
            where r.import_id = ?
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
    output_dir.mkdir(parents=True, exist_ok=True)
    write_posttest_outputs(tables, output_dir)

    dd5_run_id = uuid.uuid4().hex
    manifest_json: dict[str, object] = {
        "database": database.name,
        "import_id": import_id,
        "raw_result_count": len(tables.raw),
        "pareto_count": int(tables.comparison["pareto"].sum()),
        "target_dd_pct": str(config.target_dd_pct),
        "dd5_mode": "CALCULATION_ONLY",
        "scaled_strategy_count": 0,
        "scaled_strategies_require_retest": False,
    }
    manifest = output_dir / "posttest_manifest.json"
    manifest.write_text(json.dumps(manifest_json, indent=2, sort_keys=True) + "\n", encoding="utf-8")

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
                connection.execute(
                    "insert into dd5_results values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    ],
                )
            connection.execute("commit")
        except Exception:
            connection.execute("rollback")
            raise

    return PerformanceDd5Artifacts(
        workbook=output_dir / "posttest.xlsx",
        csv_directory=output_dir / "posttest_csv",
        manifest=manifest,
        manifest_json=manifest_json,
        dd5_run_id=dd5_run_id,
    )

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .config import AlgorithmConfig
from .duckdb_events import build_duckdb_package
from .models import Side
from .panel import serve_panel
from .pipeline import SelectionInputs, run_selection
from .portfolio.layer_a_pipeline import LayerAInputs, run_layer_a
from .portfolio.models import RunConfig
from .portfolio.pipeline import PortfolioInputs, run_portfolio
from .posttest import run_posttest
from .runner.config import RunnerConfig
from .runner.workflow import plan_batch, run_batch
from .source_packs import build_csv_package


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mrs3")
    subparsers = parser.add_subparsers(dest="command", required=True)
    select = subparsers.add_parser("select", help="build deterministic MRS3 candidates")
    source = select.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-csv", type=Path)
    source.add_argument("--source-package", type=Path)
    select.add_argument("--dates", type=Path, required=True)
    select.add_argument("--template", type=Path, required=True)
    select.add_argument("--side", choices=[side.value for side in Side], required=True)
    select.add_argument("--config", type=Path, required=True)
    select.add_argument("--output-dir", type=Path, required=True)
    source_csv = subparsers.add_parser("source-csv", help="build an auditable legacy CSV source package")
    source_csv.add_argument("--input-csv", type=Path, action="append", required=True)
    source_csv.add_argument("--start", required=True)
    source_csv.add_argument("--end", required=True)
    source_csv.add_argument("--output-dir", type=Path, required=True)
    source_csv.add_argument("--config", type=Path, required=True)
    source_duckdb = subparsers.add_parser("source-duckdb", help="build an auditable DuckDB event source package")
    source_duckdb.add_argument("--database", type=Path, required=True)
    source_duckdb.add_argument("--start", required=True)
    source_duckdb.add_argument("--end", required=True)
    source_duckdb.add_argument("--output-dir", type=Path, required=True)
    source_duckdb.add_argument("--verify-html-root", type=Path)
    source_duckdb.add_argument(
        "--verification-sample-count", type=int, choices=range(3, 6), default=3
    )
    source_duckdb.add_argument("--config", type=Path, required=True)
    tester_plan = subparsers.add_parser(
        "tester-plan", help="validate and print a read-only Hamster Bot batch plan"
    )
    tester_plan.add_argument("--config", type=Path, required=True)
    tester_plan.add_argument("--strategies", type=Path, required=True)
    tester_run = subparsers.add_parser(
        "tester-run", help="run every strategy through the Hamster Bot tester"
    )
    tester_run.add_argument("--config", type=Path, required=True)
    tester_run.add_argument("--strategies", type=Path, required=True)
    tester_run.add_argument("--output-csv", type=Path, required=True)
    posttest = subparsers.add_parser(
        "posttest", help="normalize tester results to DD5 and build retest JSON files"
    )
    posttest.add_argument("--results-csv", type=Path, required=True)
    posttest.add_argument("--audit-xlsx", type=Path, required=True)
    posttest.add_argument("--strategies-dir", type=Path, required=True)
    posttest.add_argument("--config", type=Path, required=True)
    posttest.add_argument("--output-dir", type=Path, required=True)
    portfolio = subparsers.add_parser(
        "portfolio-layer-a",
        help="screen strategy combinations before simulation (Portfolio Analyzer, Layer A)",
    )
    portfolio.add_argument("--candidates-csv", type=Path, required=True)
    portfolio.add_argument("--output-dir", type=Path, required=True)
    portfolio.add_argument("--trades-db", type=Path, default=None)
    portfolio.add_argument("--trades-table", default="trades")
    portfolio.add_argument("--limiters", default="2,3,4")
    portfolio.add_argument("--max-size-factor", type=int, default=3)
    run = subparsers.add_parser(
        "portfolio-run",
        help="full portfolio analysis: simulation, lot fitting, margin, pareto, OOS",
    )
    run.add_argument("--strategies-csv", type=Path, required=True)
    run.add_argument("--trades-db", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--trades-table", default="trades")
    run.add_argument("--deposit", type=float, default=1000.0)
    run.add_argument("--dd-target", type=float, default=5.0)
    run.add_argument("--margin-limit", type=float, default=0.40)
    run.add_argument("--limiters", default="2,3,4")
    run.add_argument("--max-size-factor", type=int, default=3)
    run.add_argument("--weight-levels", default="1")
    run.add_argument("--keep-opposite", action="store_true")
    run.add_argument("--long-short-same-slot", action="store_true")
    panel = subparsers.add_parser(
        "panel", help="run the local MRS3 control panel"
    )
    panel.add_argument(
        "--host",
        default="127.0.0.1",
        choices=("127.0.0.1", "localhost"),
    )
    panel.add_argument("--port", type=int, default=8765)
    panel.add_argument("--config", type=Path, default=Path("config.example.json"))
    panel.add_argument("--no-browser", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "select":
        config = AlgorithmConfig.from_json(args.config)
        result = run_selection(
            SelectionInputs(
                csv_path=args.input_csv,
                dates_path=args.dates,
                template_path=args.template,
                side=Side(args.side),
                output_dir=args.output_dir,
                source_package_dir=args.source_package,
            ),
            config,
        )
        print(json.dumps(result.manifest, ensure_ascii=False, indent=2))
        return 0
    if args.command == "source-csv":
        _validate_source_output(args.output_dir, args.config)
        package = build_csv_package(args.input_csv, args.start, args.end, args.output_dir)
        print(json.dumps(package.manifest, ensure_ascii=False, indent=2))
        return 0
    if args.command == "source-duckdb":
        _validate_source_output(args.output_dir, args.config)
        package = build_duckdb_package(
            args.database,
            args.start,
            args.end,
            args.output_dir,
            verification_html_root=args.verify_html_root,
            verification_sample_count=args.verification_sample_count,
        )
        print(json.dumps(package.manifest, ensure_ascii=False, indent=2))
        return 0
    if args.command == "portfolio-layer-a":
        limiters = tuple(
            int(part) for part in str(args.limiters).split(",") if part.strip()
        )
        manifest = run_layer_a(
            LayerAInputs(
                candidates_csv=args.candidates_csv,
                output_dir=args.output_dir,
                trades_db=args.trades_db,
                trades_table=args.trades_table,
                limiters=limiters or (2, 3, 4),
                max_size_factor=args.max_size_factor,
            )
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    if args.command == "portfolio-run":
        manifest = run_portfolio(
            PortfolioInputs(
                strategies_csv=args.strategies_csv,
                trades_db=args.trades_db,
                output_dir=args.output_dir,
                trades_table=args.trades_table,
                config=RunConfig(
                    deposit=args.deposit,
                    dd_target_pct=args.dd_target,
                    margin_limit=args.margin_limit,
                    limiters=tuple(
                        int(p) for p in str(args.limiters).split(",") if p.strip()
                    )
                    or (2, 3, 4),
                    max_size_factor=args.max_size_factor,
                    cancel_opposite=not args.keep_opposite,
                    long_short_same_slot=args.long_short_same_slot,
                    weight_levels=tuple(
                        int(p) for p in str(args.weight_levels).split(",") if p.strip()
                    )
                    or (1,),
                ),
            )
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    if args.command == "tester-plan":
        config = RunnerConfig.from_json(args.config)
        plan = plan_batch(config, args.strategies)
        print(
            json.dumps(
                {
                    "strategy_source": str(plan.strategy_source),
                    "expected_count": len(plan.expected_names),
                    "expected_names": plan.expected_names,
                    "filenames": plan.filenames,
                    "file_hashes": dict(plan.file_hashes),
                    "actions": plan.actions,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "tester-run":
        config = RunnerConfig.from_json(args.config)
        result = run_batch(config, args.strategies, args.output_csv)
        print(
            json.dumps(
                {
                    "output_csv": str(result.output_csv),
                    "state_file": str(result.state_file),
                    "progress_file": str(result.progress_file),
                    "result_rows": result.result_rows,
                    "events": result.events,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "posttest":
        config = AlgorithmConfig.from_json(args.config)
        result = run_posttest(
            args.results_csv,
            args.audit_xlsx,
            args.strategies_dir,
            args.output_dir,
            config,
        )
        print(
            json.dumps(
                {
                    "workbook": str(result.workbook),
                    "csv_directory": str(result.csv_directory),
                    "scaled_strategies_dir": str(result.scaled_strategies_dir),
                    "manifest": str(result.manifest),
                    "scaled_count": result.scaled_count,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "panel":
        serve_panel(
            args.host,
            args.port,
            args.config,
            open_browser=not args.no_browser,
        )
        return 0
    return 2


def _validate_source_output(output_dir: Path, config_path: Path) -> None:
    bot_root = RunnerConfig.from_json(config_path).bot_root.resolve()
    try:
        output_dir.resolve().relative_to(bot_root)
    except ValueError:
        return
    raise ValueError("source package output must be outside bot_root")


if __name__ == "__main__":
    raise SystemExit(main())

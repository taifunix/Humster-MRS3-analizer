from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .config import AlgorithmConfig, load_duckdb_import_settings
from .duckdb_events import build_duckdb_package
from .models import Side
from .panel import serve_panel
from .pipeline import SelectionInputs, run_selection
from .posttest import run_posttest
from .performance_dd5 import run_performance_dd5
from .performance_import import (
    PerformanceImportProgress,
    PerformanceImportRequest,
    import_performance_batch,
    resume_performance_cleanup,
)
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
    tester_plan.add_argument("--output-csv", type=Path)
    tester_run = subparsers.add_parser(
        "tester-run", help="run every strategy through the Hamster Bot tester"
    )
    tester_run.add_argument("--config", type=Path, required=True)
    tester_run.add_argument("--strategies", type=Path, required=True)
    tester_run.add_argument("--output-csv", type=Path, required=True)
    posttest = subparsers.add_parser(
        "posttest", help="normalize tester results to calculated DD5 comparison metrics"
    )
    posttest.add_argument("--results-csv", type=Path, required=True)
    posttest.add_argument("--audit-xlsx", type=Path, required=True)
    posttest.add_argument("--strategies-dir", type=Path, required=True)
    posttest.add_argument("--config", type=Path, required=True)
    posttest.add_argument("--output-dir", type=Path, required=True)
    performance_dd5 = subparsers.add_parser(
        "performance-dd5", help="import committed tester evidence and calculate DD5"
    )
    performance_dd5.add_argument("--database", type=Path, required=True)
    performance_dd5.add_argument("--inbox", type=Path, required=True)
    performance_dd5.add_argument("--config", type=Path, required=True)
    performance_dd5.add_argument("--output-dir", type=Path, required=True)
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
    if args.command == "tester-plan":
        config = RunnerConfig.from_json(args.config)
        plan = plan_batch(config, args.strategies, args.output_csv)
        print(
            json.dumps(
                {
                    "strategy_source": str(plan.strategy_source),
                    "expected_count": len(plan.expected_names),
                    "expected_names": plan.expected_names,
                    "filenames": plan.filenames,
                    "file_hashes": dict(plan.file_hashes),
                    "resume_completed_count": len(plan.resume_completed_names),
                    "resume_remaining_names": plan.resume_remaining_names,
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
                    "manifest": str(result.manifest),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "performance-dd5":
        config = AlgorithmConfig.from_json(args.config)
        settings = load_duckdb_import_settings(args.config)
        request = PerformanceImportRequest(
            args.inbox,
            args.database,
            workers=settings.workers,
            transaction_batch_size=settings.transaction_batch_size,
        )
        last_progress = {"stage": "VALIDATE", "completed": 0, "total": 0, "quarantined": 0}

        def emit(progress: PerformanceImportProgress) -> None:
            payload = {
                "stage": progress.stage,
                "completed": progress.completed,
                "total": progress.total,
                "quarantined": progress.quarantined,
                "scheduled": progress.scheduled,
                "prepared": progress.prepared,
                "imported": progress.imported,
                "skipped": progress.skipped,
                "phase_seconds": progress.phase_seconds,
            }
            last_progress.update(payload)
            print(json.dumps({"performance_progress": payload}, ensure_ascii=False), flush=True)

        def emit_stage(stage: str, terminal_error: str | None = None) -> None:
            last_progress["stage"] = stage
            payload = dict(last_progress)
            if terminal_error is not None: payload["terminal_error"] = terminal_error
            print(json.dumps({"performance_progress": payload}, ensure_ascii=False), flush=True)

        try:
            imported = import_performance_batch(request, emit)
            if imported.quarantined_count:
                raise ValueError("DD5 refused an import with quarantined reports")
            emit_stage("CALCULATE_EXPORT")
            result = run_performance_dd5(args.database, imported.import_id, args.output_dir, config)
            emit_stage("CLEANUP")
            resume_performance_cleanup(request)
            emit_stage("COMPLETED")
        except Exception as error:
            emit_stage(str(last_progress["stage"]), terminal_error=type(error).__name__)
            raise
        print(json.dumps({"import_id": imported.import_id, "workbook": str(result.workbook), "manifest": str(result.manifest)}, ensure_ascii=False))
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

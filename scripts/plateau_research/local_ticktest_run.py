"""Run one tick-test group through the project's SINGLE_MODE tester service, without Panel.

Usage (from the repository root, Panel must not be running a tester job):
    .venv\\Scripts\\python.exe scripts\\plateau_research\\local_ticktest_run.py <group dir> [--config config.local.json]

<group dir> is one `T_YYYY-MM-DD` folder made by local_ticktest_build.py: `strategies/*.json`
and `config_tester.json`. The script writes a generation manifest for the folder, runs the
strategies with the same service Panel uses (bot start, submission, report verification,
tester-settings restore) and copies the verified HTML reports to `<group dir>/reports`.
"""
import argparse
import json
import shutil
import sys
import time
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from mrs3.panel_fast_strategy_test import LocalSingleModeStrategyTestService  # noqa: E402
from mrs3.panel_strategy_batch import _canonical, _strategy_digest, validate_strategy_manifest  # noqa: E402
from mrs3.runner.config import RunnerConfig  # noqa: E402

TERMINAL = {"COMMITTED", "CANCELLED", "FAILED"}


def write_manifest(group: Path, analysis_run_id: str) -> Path:
    strategies = sorted((group / "strategies").glob("*.json"))
    if not strategies:
        raise SystemExit(f"no strategies in {group / 'strategies'}")
    hashes, diagnostics, mapping = {}, {}, {}
    for path in strategies:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("name") != path.stem:
            raise SystemExit(f"strategy name does not match file: {path.name}")
        hashes[path.name] = _strategy_digest(document)
        orders = document["mrs3"]["ma_long"]
        candidate = f"{path.stem}"
        diagnostics[candidate] = {
            "order_count": len(orders),
            "orders": [{"order_id": int(order["id"]), "plateau_id": "PLATEAU_RESEARCH",
                        "plateau_point_count": 0, "base_point_trades": 0, "plateau_total_trades": 0}
                       for order in orders],
        }
        mapping[candidate] = [path.stem]
    manifest = {
        "format_version": 1,
        "analysis_run_id": analysis_run_id,
        "event_mode": "real_independent_events",
        "purpose": "plateau v4 tick test (research, not a READY publication)",
        "strategy_count": len(hashes),
        "strategy_json_sha256": hashes,
        "candidate_diagnostics": diagnostics,
        "candidate_identity_to_strategy_names": mapping,
    }
    manifest["generation_manifest_sha256"] = sha256(_canonical(manifest)).hexdigest()
    path = group / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    validate_strategy_manifest(path)
    return path


def tester_template(group: Path) -> tuple[Path, str, str]:
    wanted = json.loads((group / "config_tester.json").read_text(encoding="utf-8"))
    base = json.loads((ROOT / "templates/exchanges/Bybit/mrs3-tester.json").read_text(encoding="utf-8"))
    base["WarmupDays"] = wanted["WarmupDays"]
    path = group / "tester_template.json"
    path.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, str(wanted["StartDate"])[:10], str(wanted["EndDate"])[:10]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("group", type=Path)
    parser.add_argument("--config", type=Path, default=ROOT / "config.local.json")
    parser.add_argument("--poll", type=float, default=15.0)
    args = parser.parse_args()
    group = args.group.resolve()
    analysis_run_id = f"plateau-v4-ticktest-{group.name}"
    manifest = write_manifest(group, analysis_run_id)
    template, start, end = tester_template(group)
    config = RunnerConfig.from_json(args.config.resolve())
    service = LocalSingleModeStrategyTestService(config, tester_config_template=template)
    job_id = f"plateau-v4-{group.name}-{int(time.time())}"
    print(f"start {job_id}: {start} .. {end}, manifest {manifest.name}", flush=True)
    service.start(manifest, analysis_run_id=analysis_run_id, start_date=start, end_date=end,
                  job_id=job_id, tester_config_template=template)
    last = None
    while True:
        snapshot = service.status(job_id)
        line = (snapshot["state"], snapshot["phase"], json.dumps(snapshot["progress"], sort_keys=True))
        if line != last:
            print(time.strftime("%H:%M:%S"), *line, flush=True)
            last = line
        if snapshot["state"] in TERMINAL:
            break
        time.sleep(args.poll)
    reports = group / "reports"
    reports.mkdir(exist_ok=True)
    copied = 0
    for name, filename in snapshot["evidence"]["verified_reports"].items():
        source = Path(config.report_dir) / filename
        if source.is_file():
            shutil.copy2(source, reports / filename)
            copied += 1
    (group / "run_result.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"done: state={snapshot['state']} reports copied={copied} failed={snapshot['evidence']['failed_names']}")
    if snapshot.get("error"):
        print("error:", snapshot["error"])


if __name__ == "__main__":
    main()

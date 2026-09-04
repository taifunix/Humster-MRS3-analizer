from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

import mrs3.runner.inbox as inbox_module
from mrs3.runner.config import RunnerConfig
from mrs3.runner.inbox import InboxCaptureError, capture_run_snapshot_inbox, capture_verified_inbox
from mrs3.runner.results import WizardResult
from mrs3.runner.workflow import BatchPlan
from mrs3.panel import PanelController


def _config(tmp_path: Path, *, complete: bool = True) -> RunnerConfig:
    bot = tmp_path / "bot"
    tester = bot / "tester"
    tester.mkdir(parents=True)
    tester_config = tester / "tester_config.json"
    commission = {
        "MakerFee": "0.0002",
        "TakerFee": "0.0004",
        "SlippagePercent": "0.01",
        "FundingRate": "0.0001",
        "FundingIntervalHours": "8",
    }
    if not complete:
        commission.pop("MakerFee")
    tester_config.write_text(json.dumps({"tester_config": commission}), encoding="utf-8")
    return RunnerConfig(
        bot_root=bot,
        executable_path=bot / "hb_c.exe",
        base_url="http://127.0.0.1:8087",
        port=8087,
        strategy_dir=bot / "settings_strategy",
        report_dir=tester / "report" / "my_test",
        wizard_result=tester / "wizard_result.json",
        wizard_progress=tester / "wizard_progress.json",
        tester_config=tester_config,
        inbox_root=tmp_path / "data" / "tester_inbox",
    )


def _inputs(tmp_path: Path, config: RunnerConfig) -> tuple[Path, BatchPlan, WizardResult, Path]:
    source = tmp_path / "strategies"
    source.mkdir()
    (source / "A.json").write_text(
        json.dumps({"name": "A", "exchange": {"name": "Bybit"}, "settings": []}),
        encoding="utf-8",
    )
    report = tmp_path / "A.html"
    report.write_bytes(
        b'<pre>{"name":"A","basic":{"symbol":"ONUSDT"},"exchange":{"name":"Bybit"}}</pre>\n'
    )
    plan = BatchPlan(source, ("A",), ("A.json",), (("A.json", "hash"),), (), ())
    wizard = WizardResult("run-1", "now", ("A",), {}, "/tester-report/my_test/A.html", "A.html", "period", "0")
    return tmp_path / "results.csv", plan, wizard, report


def test_capture_stages_strategy_and_panel_validates_direct_inbox(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)

    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report})

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert manifest["commission_contract"]["MakerFee"] == "0.0002"
    entry = manifest["entries"][0]
    assert Path(entry["report_path"]).resolve() == report.resolve()
    assert entry["strategy_name"] == "A"
    assert entry["exchange_name"] == "Bybit"
    assert Path(entry["strategy_path"]).resolve() == (inbox / "strategies" / "A.json").resolve()
    assert (inbox / "strategies" / "A.json").read_bytes() == (plan.strategy_source / "A.json").read_bytes()
    PanelController._validate_performance_inbox(inbox)


def test_capture_uses_installed_strategy_when_generated_file_is_gone(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    config.strategy_dir.mkdir(parents=True)
    installed = config.strategy_dir / "A.json"
    installed.write_bytes((plan.strategy_source / "A.json").read_bytes())
    (plan.strategy_source / "A.json").unlink()

    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report})

    entry = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))["entries"][0]
    assert Path(entry["strategy_path"]).resolve() == (inbox / "strategies" / "A.json").resolve()
    assert (inbox / "strategies" / "A.json").read_bytes() == installed.read_bytes()
    PanelController._validate_performance_inbox(inbox)


def test_captured_inbox_survives_source_strategy_removal(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)

    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report})
    (plan.strategy_source / "A.json").unlink()

    PanelController._validate_performance_inbox(inbox)


def test_capture_accepts_html_escaped_strategy_settings_pre(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    report.write_text(
        '<html><body><pre>{&quot;name&quot;:&quot;A&quot;,&quot;basic&quot;:{&quot;symbol&quot;:&quot;ONUSDT&quot;}}</pre></body></html>',
        encoding="utf-8",
    )

    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report})

    entry = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))["entries"][0]
    assert Path(entry["report_path"]).read_text(encoding="utf-8") == report.read_text(
        encoding="utf-8"
    )


def test_capture_accepts_flat_tester_config_contract(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.tester_config.write_text(
        json.dumps(
            {
                "MakerFee": "0.0002",
                "TakerFee": "0.0004",
                "SlippagePercent": "0.01",
                "FundingRate": "0.0001",
                "FundingIntervalHours": "8",
            }
        ),
        encoding="utf-8",
    )
    output, plan, wizard, report = _inputs(tmp_path, config)

    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report})

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert manifest["commission_contract"]["TakerFee"] == "0.0004"


def test_capture_rejects_missing_maker_fee(tmp_path: Path) -> None:
    config = _config(tmp_path, complete=False)
    output, plan, wizard, report = _inputs(tmp_path, config)

    with pytest.raises(InboxCaptureError, match="MakerFee"):
        capture_verified_inbox(config, output, plan, (wizard,), {"A": report})


def test_capture_rejects_duplicate_verified_results(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)

    with pytest.raises(InboxCaptureError, match="duplicate"):
        capture_verified_inbox(config, output, plan, (wizard, wizard), {"A": report})


def test_capture_manifest_hashes_source_bytes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report})

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    entry = manifest["entries"][0]
    assert entry["source_strategy_sha256"] == sha256(
        Path(entry["strategy_path"]).read_bytes()
    ).hexdigest()
    assert entry["source_report_sha256"] == sha256(
        Path(entry["report_path"]).read_bytes()
    ).hexdigest()


def test_capture_rejects_blank_exchange_name(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    (plan.strategy_source / "A.json").write_text(
        json.dumps({"name": "A", "exchange": {"name": "  "}, "settings": []}),
        encoding="utf-8",
    )

    with pytest.raises(InboxCaptureError, match="exchange.name"):
        capture_verified_inbox(config, output, plan, (wizard,), {"A": report})


def test_capture_uses_supplied_immutable_tester_config_snapshot(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    snapshot = config.tester_config.read_bytes()
    config.tester_config.write_text(json.dumps({"tester_config": {}}), encoding="utf-8")

    inbox = capture_verified_inbox(
        config, output, plan, (wizard,), {"A": report}, tester_config_bytes=snapshot
    )

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert manifest["tester_config_sha256"] == sha256(snapshot).hexdigest()


def test_capture_persists_v6_provenance(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    provenance = {
        "analysis_run_id": "run-v6",
        "generation_manifest_sha256": "a" * 64,
        "strategy_json_sha256": {"A.json": "b" * 64},
    }
    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report}, provenance=provenance)
    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert manifest["v6_provenance"] == provenance


def test_capture_run_snapshots_keeps_original_report_for_guarded_cleanup(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report = tmp_path / "my_test_runs" / "run.html"
    report.parent.mkdir()
    report.write_bytes((Path(__file__).parents[1] / "fixtures" / "performance" / "report_import.html").read_bytes())
    strategy = {"name": "MRS3 Demo", "exchange": {"name": "Bybit"}, "basic": {"symbol": "ONUSDT", "time_frame": "1h"}}
    digest = sha256(json.dumps(strategy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    provenance = {"analysis_run_id": "a" * 64, "generation_manifest_sha256": "b" * 64, "strategy_json_sha256": {"MRS3 Demo.json": digest}}

    inbox = capture_run_snapshot_inbox(
        config, "runs-job", {"MRS3 Demo": strategy}, {"MRS3 Demo": report},
        tester_config_bytes=config.tester_config.read_bytes(), provenance=provenance,
        test_start="2026-08-01", test_end="2026-08-18",
    )

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_mode"] == "RUNS" and manifest["test_start"] == "2026-08-01"
    entry = manifest["entries"][0]
    assert Path(entry["report_path"]).resolve() == report.resolve()
    assert not (inbox / "reports").exists()
    PanelController._validate_performance_inbox(inbox)


def test_capture_run_snapshot_inbox_supports_fast_mode(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report = tmp_path / "my_test_runs" / "run.html"
    report.parent.mkdir()
    report.write_bytes((Path(__file__).parents[1] / "fixtures" / "performance" / "report_import.html").read_bytes())
    strategy = {"name": "MRS3 Demo", "exchange": {"name": "Bybit"}, "basic": {"symbol": "ONUSDT", "time_frame": "1h"}}
    digest = sha256(json.dumps(strategy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    provenance = {"analysis_run_id": "a" * 64, "generation_manifest_sha256": "b" * 64, "strategy_json_sha256": {"MRS3 Demo.json": digest}}

    inbox = capture_run_snapshot_inbox(
        config, "fast-job", {"MRS3 Demo": strategy}, {"MRS3 Demo": report},
        tester_config_bytes=config.tester_config.read_bytes(), provenance=provenance,
        test_start="2026-08-01", test_end="2026-08-18", run_mode="FAST", workers=2,
    )

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_mode"] == "FAST"
    entry = manifest["entries"][0]
    assert Path(entry["report_path"]).resolve() == report.resolve()
    assert not (inbox / "reports").exists()


def test_single_mode_inbox_keeps_report_as_configured_filename_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report = tmp_path / "my_test_runs" / "run.html"
    report.parent.mkdir(parents=True)
    report.write_bytes((Path(__file__).parents[1] / "fixtures" / "performance" / "report_import.html").read_bytes())
    strategy = {"name": "MRS3 Demo", "exchange": {"name": "Bybit"}, "basic": {"symbol": "ONUSDT", "time_frame": "1h"}}
    digest = sha256(json.dumps(strategy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    strategy_path = tmp_path / "strategy.json"
    strategy_path.write_bytes(json.dumps(strategy, sort_keys=True, separators=(",", ":")).encode())
    inbox = capture_run_snapshot_inbox(
        config,
        "single-metadata",
        {"MRS3 Demo": strategy},
        {"MRS3 Demo": report},
        tester_config_bytes=config.tester_config.read_bytes(),
        provenance={"analysis_run_id": "a" * 64, "generation_manifest_sha256": "b" * 64, "strategy_json_sha256": {"MRS3 Demo.json": digest}},
        test_start="2026-08-01",
        test_end="2026-08-18",
        run_mode="SINGLE_MODE",
        strategy_paths={"MRS3 Demo": strategy_path},
    )

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert manifest["entries"][0]["report_path"] == report.name
    assert not (inbox / "reports").exists()


def test_single_mode_replace_rejects_reparse_inbox_before_recursive_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _output, plan, _wizard, report = _inputs(tmp_path, config)
    strategy = json.loads((plan.strategy_source / "A.json").read_text(encoding="utf-8"))
    inbox = config.inbox_root / "single-job"
    inbox.mkdir(parents=True)
    stale = inbox / "stale.txt"
    stale.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(inbox_module, "_is_reparse", lambda path: path == inbox)

    with pytest.raises(InboxCaptureError, match="reparse"):
        capture_run_snapshot_inbox(
            config,
            "single-job",
            {"A": strategy},
            {"A": report},
            tester_config_bytes=config.tester_config.read_bytes(),
            provenance={
                "analysis_run_id": "a" * 64,
                "generation_manifest_sha256": "b" * 64,
                "strategy_json_sha256": {"A.json": "c" * 64},
            },
            test_start="2026-08-01",
            test_end="2026-08-18",
            run_mode="SINGLE_MODE",
            strategy_paths={"A": plan.strategy_source / "A.json"},
            replace_existing=True,
        )

    assert stale.read_text(encoding="utf-8") == "keep"


def test_capture_failure_preserves_reparse_inbox_and_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _output, plan, _wizard, report = _inputs(tmp_path, config)
    strategy = json.loads((plan.strategy_source / "A.json").read_text(encoding="utf-8"))
    inbox = config.inbox_root / "single-job"
    reparse = False

    def is_reparse(path: Path) -> bool:
        return reparse and path == inbox

    def fail_atomic(target: Path, _data: bytes) -> bytes:
        nonlocal reparse
        target.parent.mkdir(parents=True, exist_ok=True)
        (target.parent / "preserved.txt").write_text("keep", encoding="utf-8")
        reparse = True
        raise RuntimeError("capture failed")

    monkeypatch.setattr(inbox_module, "_is_reparse", is_reparse)
    monkeypatch.setattr(inbox_module, "_atomic_bytes", fail_atomic)

    with pytest.raises(RuntimeError, match="capture failed"):
        capture_run_snapshot_inbox(
            config,
            "single-job",
            {"A": strategy},
            {"A": report},
            tester_config_bytes=config.tester_config.read_bytes(),
            provenance={
                "analysis_run_id": "a" * 64,
                "generation_manifest_sha256": "b" * 64,
                "strategy_json_sha256": {"A.json": "c" * 64},
            },
            test_start="2026-08-01",
            test_end="2026-08-18",
            run_mode="SINGLE_MODE",
            strategy_paths={"A": plan.strategy_source / "A.json"},
        )

    assert (inbox / "preserved.txt").read_text(encoding="utf-8") == "keep"


def test_capture_run_snapshot_caps_worker_pool_and_keeps_entry_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    names = tuple(f"A{index:02d}" for index in range(17))
    reports: dict[str, Path] = {}
    snapshots: dict[str, dict[str, object]] = {}
    hashes: dict[str, str] = {}
    for name in names:
        strategy = {"name": name, "exchange": {"name": "Bybit"}, "basic": {"symbol": "ONUSDT"}}
        snapshots[name] = strategy
        payload = json.dumps(strategy, sort_keys=True, separators=(",", ":")).encode()
        hashes[f"{name}.json"] = sha256(payload).hexdigest()
        report = tmp_path / f"{name}.html"
        report.write_text(f"<pre>{payload.decode()}</pre>", encoding="utf-8")
        reports[name] = report
    observed: list[int] = []

    class Executor:
        def __init__(self, max_workers: int) -> None:
            observed.append(max_workers)

        def __enter__(self) -> "Executor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def map(self, function: object, values: tuple[str, ...]) -> list[dict[str, object]]:
            return [function(value) for value in values]  # type: ignore[operator]

    monkeypatch.setattr(inbox_module, "ThreadPoolExecutor", Executor)
    inbox = capture_run_snapshot_inbox(
        config,
        "worker-cap",
        snapshots,
        reports,
        tester_config_bytes=config.tester_config.read_bytes(),
        provenance={"analysis_run_id": "a" * 64, "generation_manifest_sha256": "b" * 64, "strategy_json_sha256": hashes},
        test_start="2026-08-01",
        test_end="2026-08-18",
        workers=100,
    )

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert observed == [16]
    assert [entry["strategy_name"] for entry in manifest["entries"]] == list(names)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"workers": 0}, "positive integer"),
        ({"workers": True}, "positive integer"),
        ({"run_mode": "OTHER"}, "unsupported .*run mode"),
    ],
)
def test_capture_run_snapshot_rejects_invalid_worker_or_mode(tmp_path: Path, kwargs: dict[str, object], message: str) -> None:
    config = _config(tmp_path)
    _output, _plan, _wizard, report = _inputs(tmp_path, config)
    strategy = {"name": "A", "exchange": {"name": "Bybit"}, "basic": {"symbol": "ONUSDT"}}
    digest = sha256(json.dumps(strategy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(InboxCaptureError, match=message):
        capture_run_snapshot_inbox(
            config,
            "invalid-capture",
            {"A": strategy},
            {"A": report},
            tester_config_bytes=config.tester_config.read_bytes(),
            provenance={"analysis_run_id": "a" * 64, "generation_manifest_sha256": "b" * 64, "strategy_json_sha256": {"A.json": digest}},
            test_start="2026-08-01",
            test_end="2026-08-18",
            **kwargs,
        )


def test_capture_run_snapshot_parallel_writes_are_independent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    names = tuple(f"P{index}" for index in range(4))
    snapshots: dict[str, dict[str, object]] = {}
    reports: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    for name in names:
        strategy = {"name": name, "exchange": {"name": "Bybit"}, "basic": {"symbol": "ONUSDT"}}
        snapshots[name] = strategy
        payload = json.dumps(strategy, sort_keys=True, separators=(",", ":")).encode()
        hashes[f"{name}.json"] = sha256(payload).hexdigest()
        report = tmp_path / f"{name}.html"
        report.write_bytes(b"<pre>" + payload + b"</pre>")
        reports[name] = report

    inbox = capture_run_snapshot_inbox(
        config,
        "parallel-capture",
        snapshots,
        reports,
        tester_config_bytes=config.tester_config.read_bytes(),
        provenance={"analysis_run_id": "a" * 64, "generation_manifest_sha256": "b" * 64, "strategy_json_sha256": hashes},
        test_start="2026-08-01",
        test_end="2026-08-18",
        workers=4,
    )

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    entries = manifest["entries"]
    assert [entry["strategy_name"] for entry in entries] == list(names)
    for entry in entries:
        source = reports[entry["strategy_name"]]
        assert entry["source_report_sha256"] == sha256(source.read_bytes()).hexdigest()
        assert (inbox / "strategies" / f"{entry['strategy_name']}.json").is_file()


def test_capture_rejects_incomplete_v6_provenance(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    with pytest.raises(InboxCaptureError, match="provenance is incomplete"):
        capture_verified_inbox(
            config, output, plan, (wizard,), {"A": report},
            provenance={"analysis_run_id": "run-v6"},
        )


def test_capture_rejects_v6_provenance_hashes_not_covering_batch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    with pytest.raises(InboxCaptureError, match="strategy_json_sha256"):
        capture_verified_inbox(
            config, output, plan, (wizard,), {"A": report},
            provenance={
                "analysis_run_id": "run-v6",
                "generation_manifest_sha256": "a" * 64,
                "strategy_json_sha256": "not-a-map",
            },
        )


def test_capture_rejects_malformed_v6_generation_hash(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    with pytest.raises(InboxCaptureError, match="generation_manifest_sha256"):
        capture_verified_inbox(
            config, output, plan, (wizard,), {"A": report},
            provenance={
                "analysis_run_id": "run-v6",
                "generation_manifest_sha256": "short",
                "strategy_json_sha256": {"A.json": "b" * 64},
            },
        )

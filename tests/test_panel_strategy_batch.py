from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace

import pytest

from mrs3.panel import PanelController
import mrs3.panel_strategy_batch as strategy_batch
from mrs3.panel_strategy_batch import (
    LocalStrategyBatchService,
    StrategyBatchValidationError,
    validate_strategy_manifest,
)
from mrs3.runner.config import RunnerConfig


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _strategy(name: str, analysis_id: str, generation_hash: str) -> dict[str, object]:
    return {
        "name": name,
        "exchange": {"name": "Bybit"},
        "basic": {"symbol": "BTCUSDT", "use_long": True, "use_short": False},
    }


def _strategy_digest(strategy: dict[str, object]) -> str:
    return sha256(_canonical(strategy)).hexdigest()


def _manifest(tmp_path: Path, *, strategy_count: int = 1) -> Path:
    analysis_id = "a" * 64
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    names = [f"S{index}" for index in range(strategy_count)]
    generation_unsigned: dict[str, object] = {
        "format_version": 1,
        "fingerprint": "analysis-v6-fresh-compact-v1",
        "event_mode": "real_independent_events",
        "analysis_run_id": analysis_id,
        "strategy_count": strategy_count,
        "strategy_json_sha256": {},
    }
    hashes: dict[str, str] = {}
    for name in names:
        document = _strategy(name, analysis_id, "")
        hashes[f"{name}.json"] = _strategy_digest(document)
    generation_unsigned["strategy_json_sha256"] = hashes
    generation_hash = sha256(_canonical(generation_unsigned)).hexdigest()
    for name in names:
        document = _strategy(name, analysis_id, generation_hash)
        (strategies / f"{name}.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    generation_unsigned["generation_manifest_sha256"] = generation_hash
    manifest = tmp_path / "strategy_manifest.json"
    manifest.write_text(json.dumps(generation_unsigned, indent=2) + "\n", encoding="utf-8")
    return manifest


def test_validate_strategy_manifest_recomputes_generation_and_exact_strategy_hashes(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)

    validated = validate_strategy_manifest(manifest)

    assert validated.analysis_run_id == "a" * 64
    assert validated.strategy_source == (tmp_path / "strategies").resolve()
    assert len(str(validated.provenance["generation_manifest_sha256"])) == 64
    assert set(validated.provenance["strategy_json_sha256"]) == {"S0.json"}


def test_validate_strategy_manifest_rejects_changed_json_bytes(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    path = tmp_path / "strategies" / "S0.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["basic"]["symbol"] = "ETHUSDT"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(StrategyBatchValidationError, match="strategy JSON hash"):
        validate_strategy_manifest(manifest)


def test_start_installs_root_json_writes_dates_and_stops_bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path)
    source = tmp_path / "generated"
    source.mkdir()
    (tmp_path / "strategies" / "S0.json").replace(source / "S0.json")
    metadata = source / ".mrs3"
    metadata.mkdir()
    manifest.replace(metadata / "strategy_manifest.json")

    bot_root = tmp_path / "bot"
    report_dir = bot_root / "tester" / "report" / "my_test"
    strategy_dir = bot_root / "settings_strategy"
    report_dir.mkdir(parents=True)
    strategy_dir.mkdir(parents=True)
    (report_dir / "old.html").write_text("old", encoding="utf-8")
    (strategy_dir / "old.json").write_text("{}", encoding="utf-8")
    legacy_config = bot_root / "tester" / "tester_config.json"
    legacy_config.parent.mkdir(parents=True, exist_ok=True)
    legacy_config.write_text(json.dumps({"MakerFee": "0.1", "StartDate": "old"}), encoding="utf-8")
    config = RunnerConfig(
        bot_root=bot_root,
        executable_path=bot_root / "hb_c.exe",
        base_url="http://127.0.0.1:8087",
        port=8087,
        strategy_dir=strategy_dir,
        report_dir=report_dir,
        wizard_result=bot_root / "tester" / "wizard_result.json",
        wizard_progress=bot_root / "tester" / "wizard_progress.json",
        tester_config=legacy_config,
        inbox_root=tmp_path / "inbox-root",
    )
    inbox = tmp_path / "inbox" / "batch"
    (inbox / "reports").mkdir(parents=True)
    (inbox / "reports" / "S0.html").write_text("report", encoding="utf-8")
    (inbox / "inbox_manifest.json").write_text(json.dumps({
        "entries": [{"strategy_name": "S0", "report_path": "reports/S0.html"}],
    }), encoding="utf-8")
    calls: list[tuple[Path, Path, Path]] = []
    stops: list[Path] = []

    def fake_run(received: RunnerConfig, received_source: Path, output: Path, *, provenance):
        calls.append((received.tester_config, received_source, received.strategy_dir))
        assert sorted(path.name for path in received.strategy_dir.glob("*.json")) == ["S0.json"]
        return _FakeResult(inbox, tmp_path / "progress.json")

    monkeypatch.setattr(strategy_batch, "validate_runtime_preflight", lambda _config: None)
    service = LocalStrategyBatchService(
        config, run_batch=fake_run, stop_bot=lambda received: stops.append(received.tester_config)
    )
    job = service.start(manifest_path=metadata / "strategy_manifest.json", start_date="2026-08-01", end_date="2026-08-31")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and service.status(str(job["job_id"]))["state"] == "RUNNING":
        time.sleep(0.01)

    assert service.status(str(job["job_id"]))["state"] == "COMMITTED"
    exact_config = bot_root / "config_tester.json"
    document = json.loads(exact_config.read_text(encoding="utf-8"))
    assert document["StartDate"] == "2026-08-01"
    assert document["EndDate"] == "2026-08-31"
    assert document["MakerFee"] == "0.1"
    assert calls == [(exact_config.resolve(), source.resolve(), strategy_dir.resolve())]
    assert stops == [exact_config.resolve()]
    assert not (report_dir / "old.html").exists()
    assert not (strategy_dir / "old.json").exists()
    assert (report_dir / "S0.html").read_text(encoding="utf-8") == "report"


def test_start_rejects_invalid_dates_before_mutating_tester_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path)
    bot_root = tmp_path / "bot"
    report_dir = bot_root / "tester" / "report" / "my_test"
    strategy_dir = bot_root / "settings_strategy"
    report_dir.mkdir(parents=True)
    strategy_dir.mkdir(parents=True)
    (report_dir / "old.html").write_text("old", encoding="utf-8")
    (strategy_dir / "old.json").write_text("{}", encoding="utf-8")
    config_path = bot_root / "config_tester.json"
    config_path.write_text(json.dumps({"StartDate": "old"}), encoding="utf-8")
    config = RunnerConfig(
        bot_root=bot_root,
        executable_path=bot_root / "hb_c.exe",
        base_url="http://127.0.0.1:8087",
        port=8087,
        strategy_dir=strategy_dir,
        report_dir=report_dir,
        wizard_result=bot_root / "tester" / "wizard_result.json",
        wizard_progress=bot_root / "tester" / "wizard_progress.json",
        tester_config=config_path,
        inbox_root=tmp_path / "inbox-root",
    )
    monkeypatch.setattr(strategy_batch, "validate_runtime_preflight", lambda _config: None)
    service = LocalStrategyBatchService(config, run_batch=lambda *_args, **_kwargs: pytest.fail("runner must not start"))

    with pytest.raises(StrategyBatchValidationError, match="ISO date"):
        service.start(manifest, start_date="2026-8-01", end_date="2026-08-31")

    assert (report_dir / "old.html").read_text(encoding="utf-8") == "old"
    assert (strategy_dir / "old.json").read_text(encoding="utf-8") == "{}"
    assert json.loads(config_path.read_text(encoding="utf-8")) == {"StartDate": "old"}


def test_start_rejects_reversed_dates_before_mutating_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path)
    bot_root = tmp_path / "bot"
    report_dir = bot_root / "tester" / "report" / "my_test"
    strategy_dir = bot_root / "settings_strategy"
    report_dir.mkdir(parents=True)
    strategy_dir.mkdir(parents=True)
    (report_dir / "old.html").write_text("old", encoding="utf-8")
    config_path = bot_root / "config_tester.json"
    config_path.write_text(json.dumps({"StartDate": "old"}), encoding="utf-8")
    config = RunnerConfig(
        bot_root=bot_root,
        executable_path=bot_root / "hb_c.exe",
        base_url="http://127.0.0.1:8087",
        port=8087,
        strategy_dir=strategy_dir,
        report_dir=report_dir,
        wizard_result=bot_root / "tester" / "wizard_result.json",
        wizard_progress=bot_root / "tester" / "wizard_progress.json",
        tester_config=config_path,
        inbox_root=tmp_path / "inbox-root",
    )
    monkeypatch.setattr(strategy_batch, "validate_runtime_preflight", lambda _config: None)
    service = LocalStrategyBatchService(config, run_batch=lambda *_args, **_kwargs: pytest.fail("runner must not start"))

    with pytest.raises(StrategyBatchValidationError, match="on or before"):
        service.start(manifest, start_date="2026-09-01", end_date="2026-08-31")

    assert (report_dir / "old.html").read_text(encoding="utf-8") == "old"
    assert json.loads(config_path.read_text(encoding="utf-8")) == {"StartDate": "old"}


def test_start_rejects_manifest_for_another_analysis_before_mutating_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path)
    config = SimpleNamespace(inbox_root=tmp_path / "inbox-root")
    monkeypatch.setattr(strategy_batch, "validate_runtime_preflight", lambda _config: pytest.fail("preflight must not run"))
    service = LocalStrategyBatchService(config, run_batch=lambda *_args, **_kwargs: pytest.fail("runner must not start"))

    with pytest.raises(StrategyBatchValidationError, match="does not match analysis run"):
        service.start(manifest, analysis_run_id="b" * 64, start_date="2026-08-01", end_date="2026-08-31")


def test_failed_run_stops_bot_before_failure_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path)
    stops: list[object] = []

    def fail_run(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("tester failed")

    monkeypatch.setattr(strategy_batch, "validate_runtime_preflight", lambda _config: None)
    service = LocalStrategyBatchService(
        SimpleNamespace(inbox_root=tmp_path / "inbox-root"),
        run_batch=fail_run,
        stop_bot=lambda config: stops.append(config),
    )
    job = service.start(manifest)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and service.status(str(job["job_id"]))["state"] == "RUNNING":
        time.sleep(0.01)

    assert service.status(str(job["job_id"]))["state"] == "FAILED"
    assert len(stops) == 1


def test_committed_run_without_inbox_still_reports_terminal_counters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path)
    monkeypatch.setattr(strategy_batch, "validate_runtime_preflight", lambda _config: None)
    service = LocalStrategyBatchService(
        SimpleNamespace(inbox_root=tmp_path / "inbox-root"),
        run_batch=lambda *_args, **_kwargs: _FakeResult(None, tmp_path / "progress.json"),
    )
    job = service.start(manifest)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and service.status(str(job["job_id"]))["state"] == "RUNNING":
        time.sleep(0.01)

    status = service.status(str(job["job_id"]))
    assert status["state"] == "COMMITTED"
    assert status["progress"] == {"sent": 1, "running": 0, "result": 1, "checked": 1, "retries": 0, "total": 1}


def test_panel_rejects_unknown_tester_start_fields() -> None:
    controller = object.__new__(PanelController)
    with pytest.raises(ValueError, match="unsupported fields"):
        controller.strategies_tester_start({
            "analysis_run_id": "run",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "output_path": "unsafe",
        })


@dataclass(frozen=True)
class _FakeResult:
    inbox_path: Path
    progress_file: Path


def test_start_passes_v6_provenance_and_preserves_inbox_reports_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path)
    bot_root = tmp_path / "bot"
    report_dir = bot_root / "tester" / "report" / "my_test"
    report_dir.mkdir(parents=True)
    config = RunnerConfig(
        bot_root=bot_root,
        executable_path=bot_root / "hb_c.exe",
        base_url="http://127.0.0.1:8087",
        port=8087,
        strategy_dir=bot_root / "settings_strategy",
        report_dir=report_dir,
        wizard_result=bot_root / "tester" / "wizard_result.json",
        wizard_progress=bot_root / "tester" / "wizard_progress.json",
        tester_config=bot_root / "tester" / "tester_config.json",
        inbox_root=tmp_path / "inbox-root",
    )
    calls: list[tuple[object, Path, Path, dict[str, object]]] = []
    inbox = tmp_path / "inbox" / "batch"
    (inbox / "reports").mkdir(parents=True)
    report = '<pre>{"name":"S0"}</pre>'
    (inbox / "reports" / "entry.html").write_text(report, encoding="utf-8")
    (inbox / "inbox_manifest.json").write_text(json.dumps({
        "entries": [{"strategy_name": "S0", "report_path": "reports/entry.html"}],
    }), encoding="utf-8")

    def fake_run(received_config: object, source: Path, output: Path, *, provenance):
        calls.append((received_config, source, output, dict(provenance)))
        return _FakeResult(inbox, tmp_path / "progress.json")

    monkeypatch.setattr(strategy_batch, "validate_runtime_preflight", lambda _config: None)
    service = LocalStrategyBatchService(config, run_batch=fake_run)
    started = service.start(manifest)
    job_id = str(started["job_id"])
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and service.status(job_id)["state"] == "RUNNING":
        time.sleep(0.01)
    status = service.status(job_id)

    assert status["state"] == "COMMITTED"
    assert calls[0][1] == (tmp_path / "strategies").resolve()
    assert calls[0][3]["analysis_run_id"] == "a" * 64
    assert (report_dir / "S0.html").read_text(encoding="utf-8") == report


def test_start_republishes_report_from_its_verified_snapshot_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path)
    bot_root = tmp_path / "bot"
    report_dir = bot_root / "tester" / "report" / "my_test"
    report_dir.mkdir(parents=True)
    config = RunnerConfig(
        bot_root=bot_root,
        executable_path=bot_root / "hb_c.exe",
        base_url="http://127.0.0.1:8087",
        port=8087,
        strategy_dir=bot_root / "settings_strategy",
        report_dir=report_dir,
        wizard_result=bot_root / "tester" / "wizard_result.json",
        wizard_progress=bot_root / "tester" / "wizard_progress.json",
        tester_config=bot_root / "tester" / "tester_config.json",
        inbox_root=tmp_path / "inbox-root",
    )
    inbox = tmp_path / "inbox" / "batch"
    snapshot = inbox.parent / ".batch.report_snapshots"
    snapshot.mkdir(parents=True)
    report = '<pre>{"name":"S0"}</pre>'
    source = snapshot / "S0.html"
    source.write_text(report, encoding="utf-8")
    inbox.mkdir()
    (inbox / "inbox_manifest.json").write_text(json.dumps({
        "entries": [{"strategy_name": "S0", "report_path": str(source.resolve())}],
    }), encoding="utf-8")

    monkeypatch.setattr(strategy_batch, "validate_runtime_preflight", lambda _config: None)
    service = LocalStrategyBatchService(config, run_batch=lambda *_args, **_kwargs: _FakeResult(inbox, tmp_path / "progress.json"))
    job_id = str(service.start(manifest)["job_id"])
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and service.status(job_id)["state"] == "RUNNING":
        time.sleep(0.01)

    assert service.status(job_id)["state"] == "COMMITTED"
    assert (report_dir / "S0.html").read_text(encoding="utf-8") == report


def test_cancel_is_cooperative_and_does_not_expose_exception_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path)
    started = Event()
    release = Event()

    def fake_run(*_args: object, **_kwargs: object):
        started.set()
        release.wait(2)
        raise RuntimeError("secret D:/private/report.html")

    monkeypatch.setattr(strategy_batch, "validate_runtime_preflight", lambda _config: None)
    stops: list[object] = []
    service = LocalStrategyBatchService(
        SimpleNamespace(inbox_root=tmp_path / "inbox-root"),
        run_batch=fake_run,
        stop_bot=lambda config: stops.append(config),
    )
    job = service.start(manifest)
    assert started.wait(1)
    assert service.cancel(str(job["job_id"]))["state"] == "CANCELLING"
    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and service.status(str(job["job_id"]))["state"] == "CANCELLING":
        time.sleep(0.01)
    result = service.status(str(job["job_id"]))
    assert result["state"] == "CANCELLED"
    assert "secret" not in json.dumps(result)
    assert len(stops) == 1


def test_status_reads_runner_progress_and_keeps_inbox_internal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path)
    started = Event()
    release = Event()

    def fake_run(_config: object, _source: Path, output: Path, *, provenance: object):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.with_name(f"{output.stem}.progress.json").write_text(json.dumps({
            "submitted_count": 2, "running_count": 1, "result_count": 1,
            "completed_count": 1, "retry_count": 3,
        }), encoding="utf-8")
        started.set()
        release.wait(2)
        return _FakeResult(tmp_path / "inbox", tmp_path / "progress.json")

    monkeypatch.setattr(strategy_batch, "validate_runtime_preflight", lambda _config: None)
    service = LocalStrategyBatchService(SimpleNamespace(inbox_root=tmp_path / "inbox-root"), run_batch=fake_run)
    job = service.start(manifest)
    assert started.wait(1)
    progress = service.status(str(job["job_id"]))["progress"]
    assert progress == {"sent": 2, "running": 1, "result": 1, "checked": 1, "retries": 3, "total": 1}
    release.set()

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace

import pytest

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
        "provenance": {
            "analysis_run_id": analysis_id,
            "generation_manifest_sha256": generation_hash,
            "event_mode": "real_independent_events",
        },
    }


def _strategy_digest(strategy: dict[str, object]) -> str:
    value = json.loads(json.dumps(strategy))
    provenance = value["provenance"]
    assert isinstance(provenance, dict)
    provenance.pop("strategy_json_sha256", None)
    provenance.pop("generation_manifest_sha256", None)
    return sha256(_canonical(value)).hexdigest()


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
        document["provenance"]["strategy_json_sha256"] = _strategy_digest(document)
        hashes[f"{name}.json"] = str(document["provenance"]["strategy_json_sha256"])
    generation_unsigned["strategy_json_sha256"] = hashes
    generation_hash = sha256(_canonical(generation_unsigned)).hexdigest()
    for name in names:
        document = _strategy(name, analysis_id, generation_hash)
        document["provenance"]["strategy_json_sha256"] = hashes[f"{name}.json"]
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


def test_cancel_is_cooperative_and_does_not_expose_exception_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path)
    started = Event()
    release = Event()

    def fake_run(*_args: object, **_kwargs: object):
        started.set()
        release.wait(2)
        raise RuntimeError("secret D:/private/report.html")

    monkeypatch.setattr(strategy_batch, "validate_runtime_preflight", lambda _config: None)
    service = LocalStrategyBatchService(SimpleNamespace(inbox_root=tmp_path / "inbox-root"), run_batch=fake_run)
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

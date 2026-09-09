from __future__ import annotations

import hashlib
import json
from http.client import HTTPConnection
from pathlib import Path
import threading
import time
from decimal import Decimal
from types import SimpleNamespace
import zipfile
from openpyxl import load_workbook
from openpyxl import Workbook

import pytest
import duckdb

from mrs3.panel import PanelController, create_panel_server
from mrs3.panel_portfolio import PortfolioPanelError, PortfolioPanelService, STAGES, _redact_text, _safe_cell
from mrs3.panel_jobs import PanelJobError, PanelJobRegistry
from mrs3.portfolio.config import PortfolioConfigError, migrate_portfolio_config_document


def _config() -> dict:
    money = lambda amount: {"amount": str(amount), "currency": "USDT"}
    profile = lambda: {
        "pnl": {"policy_id": "operator_supplied_pnl_v1", "parameters": {"operator_supplied": True}},
        "liquidity": {"policy_id": "operator_supplied_liquidity_v1", "parameters": {"operator_supplied": True}},
        "ranking": {"id": "operator_supplied_ranking_v1", "parameters": {"operator_supplied": True}, "top_n": 1},
    }
    return migrate_portfolio_config_document({
        "schema_version": 1,
        "policy_version": "portfolio_optimizer_research_risk_v1",
        "algorithm_versions": {"sizing": "portfolio_optimizer_sizing_v1", "ranking": "portfolio_optimizer_ranking_v1"},
        "inputs": {"performance_db": "performance.duckdb", "portfolio_db": "portfolio.duckdb", "collector_root": "collector", "approved_templates": ["template.json"]},
        "scenarios": {name: {"account": name, "deposit": money(10000), "collateral": money(10000), "max_balance": money(10000), "sizing": {"upper_bound": money(10000), "grid": [money(1000)]}} for name in ("AGGRESSIVE", "BALANCED", "CONSERVATIVE")},
        "search": {"universe": {"policy_id": "u", "parameters": {"x": 1}}, "composition": {"policy_id": "c", "parameters": {"x": 1}}, "sizing": {"policy_id": "s", "parameters": {"x": 1}}, "limiter": {"policy_id": "l", "parameters": {"x": 1}}, "priority": {"policy_id": "p", "parameters": {"x": 1}}, "seed": 1, "rounds": 1, "total_test_budget": 1},
        "research": {"development_window": {"value": 1, "unit": "days"}, "validation_window": {"value": 1, "unit": "days"}, "warmup": {"value": 0, "unit": "days"}, "boundary": "fixture", "evidence_minimum": {"value": 1, "unit": "count"}},
        "liquidity": {"policy_id": "liquidity", "parameters": {"x": 1}}, "margin": {"policy_id": "margin", "parameters": {"x": 1}},
        "profiles": {name: profile() for name in ("AGGRESSIVE", "BALANCED", "CONSERVATIVE")},
        "runner": {"target": "local", "root": "tester", "timeout": {"value": 1, "unit": "seconds"}, "retries": 0},
    })[0]


def _write_config(path: Path) -> str:
    raw = json.dumps(_config(), ensure_ascii=False, indent=2).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _profiled_variants(selected, *_):
    return tuple({**dict(item), "profile": "BALANCED"} for item in selected)


def test_settings_get_put_uses_exact_byte_digest_and_cas(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    service = PortfolioPanelService(tmp_path, path)
    assert service.settings_get()["digest"] == digest
    changed = _config()
    changed["search"]["seed"] = 2
    saved = service.settings_put({"expected_digest": digest, "document": changed})
    assert saved["state"] == "READY"
    assert path.read_bytes() != json.dumps(changed).encode()
    with pytest.raises(Exception) as error:
        service.settings_put({"expected_digest": digest, "document": changed})
    assert getattr(error.value, "code", None) == "CONFIG_CHANGED"


def test_settings_save_migrates_legacy_v1_document_to_v2(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "portfolio_optimizer.local.json.example"
    path = tmp_path / "portfolio_optimizer.local.json"
    path.write_bytes(source.read_bytes())
    service = PortfolioPanelService(tmp_path, path)

    loaded = service.settings_get()
    assert loaded["state"] == "READY"
    assert loaded["schema_version"] == 2
    saved = service.settings_put({"expected_digest": loaded["digest"], "document": loaded["document"]})

    document = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2
    assert document["schema_version"] == 2
    assert all("grid" not in scenario["sizing"] for scenario in document["scenarios"].values())


def test_settings_put_restores_previous_bytes_when_readback_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    previous = path.read_bytes()
    changed = _config()
    changed["search"]["seed"] = 2
    original_read_bytes = Path.read_bytes
    reads = 0

    def mismatched_readback(candidate: Path) -> bytes:
        nonlocal reads
        data = original_read_bytes(candidate)
        if candidate == path:
            reads += 1
            if reads == 2:
                return data + b" "
        return data

    monkeypatch.setattr(Path, "read_bytes", mismatched_readback)
    service = PortfolioPanelService(tmp_path, path)

    with pytest.raises(PortfolioPanelError) as error:
        service.settings_put({"expected_digest": digest, "document": changed})

    assert error.value.code == "CONFIG_WRITE_FAILED"
    assert original_read_bytes(path) == previous


def test_settings_put_does_not_relock_plain_lock(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    controller = PanelController(tmp_path, tmp_path / "panel-config.json")
    service = controller._portfolio_service
    service._lock = threading.Lock()
    changed = _config()
    changed["search"]["seed"] = 2

    saved = controller.portfolio_settings_put({"expected_digest": digest, "document": changed})

    assert saved["state"] == "READY"
    assert saved["digest"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_unsupported_settings_are_read_only(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    document = _config()
    document["schema_version"] = 3
    path.write_text(json.dumps(document), encoding="utf-8")
    service = PortfolioPanelService(tmp_path, path)
    assert service.settings_get()["state"] == "UNSUPPORTED_SCHEMA"
    with pytest.raises(Exception) as error:
        service.settings_put({"expected_digest": service.settings_get()["digest"], "document": document})
    assert getattr(error.value, "code", None) == "CONFIG_UNSUPPORTED_SCHEMA"


def test_readiness_reports_exact_config_digest(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    service = PortfolioPanelService(tmp_path, path)

    readiness = service.readiness()

    assert readiness["config_digest"] == digest
    assert readiness["schema_version"] == 2
    assert readiness["policy_version"] == "portfolio_optimizer_research_risk_v1"
    assert "search" not in readiness


def test_readiness_resets_pairs_and_counts_when_finalist_read_fails(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    _write_config(path)
    with duckdb.connect(str(tmp_path / "performance.duckdb")) as connection:
        connection.execute("create table selection_runs(symbol varchar, side varchar)")
        connection.execute("insert into selection_runs values ('BTCUSDT', 'LONG')")

    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: (_ for _ in ()).throw(OSError("finalists unavailable")))

    readiness = service.readiness()

    assert readiness["available_pairs"] == []
    assert readiness["current_finalists"] == {}
    assert readiness["stage1"]["blockers"] == ["FINALISTS_UNAVAILABLE", "NO_FINALISTS"]


def test_active_campaign_conflict_is_reported_before_source_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    calls = []

    def reader(*_args):
        calls.append(True)
        raise RuntimeError("source must not be read")

    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    service = PortfolioPanelService(tmp_path, path, registry=registry, finalists_reader=reader)
    payload = {
        "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
        "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
        "expected_config_digest": digest,
    }
    config, _raw, _document = service._config()
    launch = service._normalise_campaign(payload, config, digest)
    input_digest = hashlib.sha256(json.dumps(launch, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    registry.submit("portfolio.stage1", {"campaign_id": "active", "input_digest": input_digest, "config_digest": digest}, "active", ("portfolio_optimizer",), job_id="active")
    registry.reserve_runtime("active", "campaign", {"campaign_id": "active", "input_digest": input_digest, "config_digest": digest})
    service._threads["active"] = object()

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign(payload)

    assert error.value.code == "PORTFOLIO_JOB_ACTIVE_DUPLICATE"
    assert calls == []


def test_other_active_campaign_is_busy_before_source_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    calls = []

    def reader(*_args):
        calls.append(True)
        raise RuntimeError("source must not be read")

    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    service = PortfolioPanelService(tmp_path, path, registry=registry, finalists_reader=reader)
    payload = {
        "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
        "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
        "expected_config_digest": digest,
    }
    registry.submit("portfolio.stage1", {"campaign_id": "other", "input_digest": "other", "config_digest": digest}, "other", ("portfolio_optimizer",), job_id="other")
    registry.reserve_runtime("other", "campaign", {"campaign_id": "other", "input_digest": "other", "config_digest": digest})
    service._threads["other"] = object()

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign(payload)

    assert error.value.code == "PORTFOLIO_JOB_BUSY"
    assert calls == []


def test_campaign_profile_rejects_unknown_fields_exactly(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: [])
    payload = {
        "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
        "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1, "unexpected": True}],
        "expected_config_digest": digest,
    }

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign(payload)

    assert error.value.code == "PORTFOLIO_CAMPAIGN_INVALID"
    assert service.registry.list() == []


def test_campaign_profile_budgets_are_independent_of_configured_total(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    service = PortfolioPanelService(tmp_path, path)
    payload = {
        "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
        "profiles": [
            {"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 2},
            {"profile_id": "AGGRESSIVE", "equity_usdt": "10000", "max_candidates": 3},
        ],
        "expected_config_digest": digest,
    }

    config, _raw, _document = service._config()
    launch = service._normalise_campaign(payload, config, digest)

    assert [profile["max_candidates"] for profile in launch["profiles"]] == [2, 3]


def test_multi_profile_budget_and_summary_stay_consistent(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    document = _config()
    document["search"]["total_test_budget"] = 2
    raw = json.dumps(document, ensure_ascii=False, indent=2).encode()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    service = PortfolioPanelService(tmp_path, path)
    payload = {
        "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
        "profiles": [
            {"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1},
            {"profile_id": "AGGRESSIVE", "equity_usdt": "10000", "max_candidates": 1},
        ],
        "expected_config_digest": digest,
    }
    config, _, _ = service._config()
    launch = service._normalise_campaign(payload, config, digest)
    summary = service._summary(
        {"campaign_id": "campaign-fixed", "launch": launch},
        (),
        (),
        ({"profile": "BALANCED"}, {"profile": "AGGRESSIVE"}),
        (),
    )

    assert summary["prepared_by_profile"] == {"AGGRESSIVE": 1, "BALANCED": 1}
    assert summary["prepared"] == 2
    assert summary["variants_created"] == 2


@pytest.mark.parametrize("value", ["999999999999999999999999999", "1.0000000000001"])
def test_campaign_money_must_fit_decimal_38_12(tmp_path: Path, value: str) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: [])

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": value, "max_candidates": 1}], "expected_config_digest": digest})

    assert error.value.code == "PORTFOLIO_CAMPAIGN_INVALID"
    assert service.registry.list() == []


def test_failed_campaign_snapshot_creates_no_job(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)

    def broken_reader(*_args):
        raise RuntimeError("source unavailable")

    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    service = PortfolioPanelService(tmp_path, path, registry=registry, finalists_reader=broken_reader)
    payload = {
        "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
        "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
        "expected_config_digest": digest,
    }

    with pytest.raises(Exception):
        service.submit_campaign(payload)

    assert registry.list() == []


def test_runtime_reservation_failure_discards_queued_job_and_retry_succeeds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: finalists, variant_generator=_profiled_variants)
    original = service.registry.reserve_runtime
    calls = 0

    def fail_once(job_id: str, key: str, value: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("runtime journal is unavailable")
        original(job_id, key, value)

    monkeypatch.setattr(service.registry, "reserve_runtime", fail_once)
    payload = {
        "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
        "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
        "expected_config_digest": digest,
    }

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign(payload)
    assert error.value.code == "PORTFOLIO_JOB_START_FAILED"
    assert service.registry.list() == []

    retry = service.submit_campaign(payload)
    assert retry["status"] == "QUEUED"


def test_worker_start_failure_removes_queued_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)

    class BrokenThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

    monkeypatch.setattr("mrs3.panel_portfolio.threading.Thread", BrokenThread)
    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    service = PortfolioPanelService(tmp_path, path, registry=registry, finalists_reader=lambda *_: [])

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest})

    assert error.value.code == "PORTFOLIO_JOB_START_FAILED"
    assert registry.list() == []


def test_campaign_uses_frozen_finalist_snapshot_after_source_changes(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    calls = []
    source_rows = [[{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]]

    def reader(*_args):
        calls.append(True)
        return source_rows[0]

    service = PortfolioPanelService(tmp_path, path, finalists_reader=reader, variant_generator=_profiled_variants)
    payload = {
        "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
        "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
        "expected_config_digest": digest,
    }
    result = service.submit_campaign(payload)
    source_rows[0] = [{"strategy_id": 99, "result_id": 100, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and service.job(result["job_id"])["status"] in {"QUEUED", "RUNNING"}:
        time.sleep(0.01)

    assert len(calls) == 1
    assert service.job(result["job_id"])["status"] == "SUCCEEDED"
    assert service.registry.runtime(result["job_id"])["campaign"]["finalists"][0]["strategy_id"] == 7


def test_invalid_published_workbook_fails_without_download(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]

    def broken_builder(workbook_path, *_args):
        workbook_path.write_bytes(b"not an xlsx")
        return workbook_path

    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: finalists, variant_generator=_profiled_variants, workbook_builder=broken_builder)
    result = service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and service.job(result["job_id"])["status"] in {"QUEUED", "RUNNING"}:
        time.sleep(0.01)

    assert service.job(result["job_id"])["status"] == "FAILED"
    assert not (tmp_path / ".portfolio-results" / result["campaign_id"] / "stage1.xlsx").exists()
    with pytest.raises(Exception):
        service.workbook(result["campaign_id"])


def test_commit_failure_rolls_back_published_workbook(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]
    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    original_sync = registry.sync

    def fail_commit(job_id, status, *, runtime=None):
        if status.get("state") == "COMMITTED":
            raise PanelJobError("COMMIT_FAILED")
        return original_sync(job_id, status, runtime=runtime)

    registry.sync = fail_commit
    service = PortfolioPanelService(tmp_path, path, registry=registry, finalists_reader=lambda *_: finalists, variant_generator=_profiled_variants)
    result = service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and service.job(result["job_id"])["status"] in {"QUEUED", "RUNNING"}:
        time.sleep(0.01)

    assert service.job(result["job_id"])["status"] == "FAILED"
    assert not (tmp_path / ".portfolio-results" / result["campaign_id"] / "stage1.xlsx").exists()
    with pytest.raises(PortfolioPanelError) as error:
        service.workbook(result["campaign_id"])
    assert error.value.code == "PORTFOLIO_JOB_WORKBOOK_UNAVAILABLE"


def test_journal_and_indeterminate_progress_are_public(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: finalists, variant_generator=_profiled_variants)
    result = service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and service.job(result["job_id"])["status"] in {"QUEUED", "RUNNING"}:
        time.sleep(0.01)

    job = service.job(result["job_id"])
    assert job["status"] == "SUCCEEDED"
    assert job["journal"]
    assert all(entry["code"].startswith("PORTFOLIO_JOB_") for entry in job["journal"])
    assert all(entry["stage"] in STAGES for entry in job["journal"])
    assert any(entry["code"] == "PORTFOLIO_JOB_COMPLETED" for entry in job["journal"])
    assert job["stage"]["completed"] == 1
    assert job["stage"]["total"] == 1
    assert job["stage"]["percent"] == 100


def test_zero_variants_fail_without_results_or_download(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: finalists, variant_generator=lambda *_: [])
    result = service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and service.job(result["job_id"])["status"] in {"QUEUED", "RUNNING"}:
        time.sleep(0.01)

    job = service.job(result["job_id"])
    assert job["status"] == "FAILED"
    assert job["diagnostics"][0]["code"] == "PORTFOLIO_JOB_VARIANTS_NOT_READY"
    with pytest.raises(PortfolioPanelError) as error:
        service.workbook(result["campaign_id"])
    assert error.value.code == "PORTFOLIO_JOB_WORKBOOK_UNAVAILABLE"
    assert job["journal"]
    assert all(entry["code"].startswith("PORTFOLIO_JOB_") for entry in job["journal"])
    assert all(entry["stage"] in STAGES for entry in job["journal"])
    assert any(entry["code"] == "PORTFOLIO_JOB_VARIANTS_NOT_READY" for entry in job["journal"])


def test_zero_adapter_variants_report_actionable_gate_details(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]
    generated = {
        "variants": (),
        "blockers": ("BALANCED:INSUFFICIENT_DIRECTIONAL_UNIVERSE",),
        "excluded": ({"symbol": "BTCUSDT", "selection_reason": "SIZE_BELOW_MINIMUM_QTY"},),
    }
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: finalists, variant_generator=lambda *_: generated)
    result = service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and service.job(result["job_id"])["status"] in {"QUEUED", "RUNNING"}:
        time.sleep(0.01)

    assert "BTCUSDT:SIZE_BELOW_MINIMUM_QTY" in service.job(result["job_id"])["diagnostics"][0]["message"]


def test_job_messages_redact_paths_and_secret_terms(tmp_path: Path) -> None:
    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    saved = registry.submit("portfolio.stage1", {}, "campaign", ("portfolio_optimizer",))
    registry.reserve_runtime(saved["job_id"], "campaign", {"campaign_id": "campaign"})
    registry.reserve_runtime(saved["job_id"], "journal", [{"text": r"C:\private\report token", "stage": "VALIDATE_SNAPSHOT"}])
    service = PortfolioPanelService(tmp_path, registry=registry)

    text = service.job(saved["job_id"])["journal"][0]["text"]

    assert "C:\\private" not in text
    assert "token" not in text
    assert "<redacted-" in text


@pytest.mark.parametrize("value", ["strategy/v1", "https://example.test/a/b", "foo/bar"])
def test_redaction_preserves_non_path_slashes(value: str) -> None:
    assert _redact_text(value) == value
    assert _safe_cell(value) == value


@pytest.mark.parametrize("value", [r"C:\private\report", r"\\server\share\report", "/var/lib/mrs3", "/a", "../private/report", "foo/../private"])
def test_redaction_hides_absolute_and_traversal_paths(value: str) -> None:
    assert _redact_text(value) == "<redacted-path>"
    assert _safe_cell(value) == "<redacted-path>"


def test_known_stage_progress_reports_local_denominator(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]
    entered = threading.Event()
    release = threading.Event()

    def generator(*_args):
        entered.set()
        assert release.wait(timeout=3)
        return []

    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: finalists, variant_generator=generator)
    result = service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest})
    assert entered.wait(timeout=3)
    try:
        job = service.job(result["job_id"])
        assert job["stage"] == {"index": 3, "name": "GENERATE_VARIANTS", "status": "RUNNING", "completed": 0, "total": 1, "percent": 0}
        assert job["overall_percent"] == 42
    finally:
        release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and service.job(result["job_id"])["status"] in {"QUEUED", "RUNNING"}:
        time.sleep(0.01)


def test_worker_start_preserves_queued_cancellation(tmp_path: Path) -> None:
    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    saved = registry.submit("portfolio.stage1", {}, "campaign", ("portfolio_optimizer",))
    registry.cancel(saved["job_id"])
    service = PortfolioPanelService(tmp_path, registry=registry)

    service._run(saved["job_id"])

    assert registry.get(saved["job_id"])["state"] == "CANCELLED"


def test_job_reports_deleted_settings_as_changed_since_freeze(tmp_path: Path) -> None:
    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    saved = registry.submit("portfolio.stage1", {}, "campaign", ("portfolio_optimizer",))
    registry.reserve_runtime(
        saved["job_id"],
        "campaign",
        {"campaign_id": "campaign", "config_digest": "frozen"},
    )
    service = PortfolioPanelService(tmp_path, registry=registry)

    job = service.job(saved["job_id"])
    assert job["stage"]["status"] == "INTERRUPTED"
    assert job["settings_changed_since_freeze"] is True


def test_summary_groups_exclusions_and_profile_counts() -> None:
    campaign = {
        "campaign_id": "campaign-fixed",
        "launch": {"profiles": [{"profile_id": "BALANCED", "max_candidates": 1}]},
    }

    summary = PortfolioPanelService._summary(
        campaign,
        ({"strategy_id": 1}, {"strategy_id": 2}),
        ({"strategy_id": 1},),
        ({"profile": "BALANCED"}, {"profile": "BALANCED"}),
        ({"selection_reason": "USER_RANK_CUTOFF"},),
        optimizer_excluded=({"selection_reason": "OPEN_POLICY"},),
        blockers=("BALANCED:OPEN_POLICY:RANKING_POLICY_REQUIRED",),
    )

    assert summary["variants_by_profile"] == {"BALANCED": 2}
    assert summary["prepared_by_profile"] == {"BALANCED": 2}
    assert summary["prepared"] == 2
    assert summary["finalist_exclusions_by_reason"] == {"USER_RANK_CUTOFF": 1}
    assert summary["optimizer_exclusions_by_reason"] == {"OPEN_POLICY": 1}


def test_workbook_bytes_are_deterministic_after_metadata_hide(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    _write_config(path)
    service = PortfolioPanelService(tmp_path, path)
    campaign = {"campaign_id": "campaign-fixed", "input_digest": "i", "config_digest": "c", "versions": {"policy_version": "p"}, "created_at_utc": "2000-01-01T00:00:00Z"}
    first = tmp_path / "one.xlsx"
    second = tmp_path / "two.xlsx"
    service._write_workbook(first, campaign, (), (), (), ())
    service._write_workbook(second, campaign, (), (), (), ())

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        core = archive.read("docProps/core.xml")
    assert b"2000-01-01T00:00:00Z" in core


def test_workbook_preserves_decimal_cells_as_exact_text(tmp_path: Path) -> None:
    service = PortfolioPanelService(tmp_path)
    campaign = {"campaign_id": "campaign-fixed", "input_digest": "i", "config_digest": "c", "versions": {"policy_version": "p"}, "created_at_utc": "2000-01-01T00:00:00Z"}
    workbook_path = tmp_path / "decimal.xlsx"
    service._write_workbook(
        workbook_path,
        campaign,
        (),
        (),
        ({"candidate_id": "candidate", "profile": "BALANCED", "scheduling_score": Decimal("12345678901234567890.123456789012"), "directions": {}},),
        (),
    )
    workbook = load_workbook(workbook_path, data_only=False)
    try:
        cell = workbook["Portfolios"][2][5]
        assert cell.value == "12345678901234567890.123456789012"
        assert isinstance(cell.value, str)
    finally:
        workbook.close()


def test_workbook_exposes_adapter_capacity_spread_and_warning_diagnostics(tmp_path: Path) -> None:
    service = PortfolioPanelService(tmp_path)
    campaign = {"campaign_id": "campaign-fixed", "input_digest": "i", "config_digest": "c", "versions": {"policy_version": "p"}, "created_at_utc": "2000-01-01T00:00:00Z"}
    member = {
        "strategy_id": 1, "result_id": 2, "symbol": "BTCUSDT", "side": "LONG",
        "position_size_usdt": Decimal("600"), "maximum_closing_quantity": Decimal("6"),
        "planned_leverage": Decimal("50"), "max_drawdown_pct": Decimal("10"),
        "capacity_status": "PRELIMINARY",
        "calendar_7d": SimpleNamespace(available_days=3, mean_minute_turnover=Decimal("2000"), rounded_cap_usdt=Decimal("600")),
        "weekday_5d": SimpleNamespace(available_days=2, mean_minute_turnover=Decimal("1800"), rounded_cap_usdt=Decimal("500")),
        "spread_status": "CLEAR", "spread_mean_bps": Decimal("8.5"),
        "sizing_digest": "sizing", "capacity_digest": "capacity", "reference_digest": "reference",
    }
    workbook_path = tmp_path / "adapter.xlsx"
    service._write_workbook(
        workbook_path, campaign, (), (),
        ({"candidate_id": "candidate", "profile": "BALANCED", "members": (member,)},), (),
        warnings=("LIQUIDITY_CAPACITY_PRELIMINARY",),
    )

    workbook = load_workbook(workbook_path, data_only=False)
    try:
        headers = {cell.value: index for index, cell in enumerate(workbook["Members"][1])}
        row = workbook["Members"][2]
        assert row[headers["7d Position Cap USDT"]].value == "600"
        assert row[headers["5d Analytic Cap USDT"]].value == "500"
        assert row[headers["Spread Status"]].value == "CLEAR"
        summary = {row[0].value: row[1].value for row in workbook["Summary"].iter_rows(min_row=2)}
        assert "LIQUIDITY_CAPACITY_PRELIMINARY" in summary["optimizer_warnings"]
    finally:
        workbook.close()


def test_workbook_does_not_invent_gate_or_counts(tmp_path: Path) -> None:
    service = PortfolioPanelService(tmp_path)
    campaign = {"campaign_id": "campaign-fixed", "input_digest": "i", "config_digest": "c", "versions": {"policy_version": "p"}, "created_at_utc": "2000-01-01T00:00:00Z"}
    workbook_path = tmp_path / "missing-evidence.xlsx"
    service._write_workbook(workbook_path, campaign, (), (), ({"candidate_id": "candidate", "profile": "BALANCED"},), ())
    workbook = load_workbook(workbook_path, data_only=False)
    try:
        row = workbook["Portfolios"][2]
        assert row[6].value is None
        assert row[7].value is None
        assert row[12].value == "UNKNOWN"
    finally:
        workbook.close()


def test_variant_validation_keeps_full_pretest_universe_without_max_candidates(tmp_path: Path) -> None:
    campaign = {
        "campaign_id": "campaign-fixed",
        "input_digest": "i",
        "config_digest": "c",
        "versions": {"policy_version": "p"},
        "created_at_utc": "2000-01-01T00:00:00Z",
        "launch": {"profiles": [{"profile_id": "BALANCED", "max_candidates": 1}]},
    }
    variants = ({"candidate_id": "first", "profile": "BALANCED"}, {"candidate_id": "second", "profile": "BALANCED"})
    kept, excluded, blockers = PortfolioPanelService._cap_variants(variants, campaign["launch"]["profiles"])
    assert tuple(item["candidate_id"] for item in kept) == ("first", "second")
    assert excluded == ()
    assert blockers == ()
    summary = PortfolioPanelService._summary(campaign, (), (), kept, (), optimizer_excluded=excluded, blockers=blockers)
    assert summary["variants_created"] == 2
    assert summary["optimizer_excluded"] == 0
    assert summary["prepared_by_profile"] == {"BALANCED": 2}
    assert summary["prepared"] == 2
    workbook_path = tmp_path / "capped.xlsx"
    PortfolioPanelService(tmp_path)._write_workbook(workbook_path, campaign, (), (), kept, (), optimizer_excluded=excluded, blockers=blockers)
    workbook = load_workbook(workbook_path, data_only=False)
    try:
        assert workbook["Portfolios"].max_row == 3
        assert workbook["Excluded"].max_row == 1
    finally:
        workbook.close()


def test_variant_without_known_profile_is_excluded_and_reported(tmp_path: Path) -> None:
    campaign = {
        "campaign_id": "campaign-fixed",
        "input_digest": "i",
        "config_digest": "c",
        "versions": {"policy_version": "p"},
        "created_at_utc": "2000-01-01T00:00:00Z",
        "launch": {"profiles": [{"profile_id": "BALANCED", "max_candidates": 1}]},
    }
    variants = ({"candidate_id": "unknown"}, {"candidate_id": "known", "profile": "BALANCED"})

    kept, excluded, blockers = PortfolioPanelService._cap_variants(variants, campaign["launch"]["profiles"])

    assert tuple(item["candidate_id"] for item in kept) == ("known",)
    assert excluded[0]["selection_reason"] == "UNKNOWN_PROFILE"
    assert blockers == ("UNKNOWN_PROFILE",)
    summary = PortfolioPanelService._summary(campaign, (), (), kept, (), optimizer_excluded=excluded, blockers=blockers)
    assert summary["variants_created"] == 1
    assert summary["prepared_by_profile"] == {"BALANCED": 1}
    workbook_path = tmp_path / "unknown-profile.xlsx"
    PortfolioPanelService(tmp_path)._write_workbook(workbook_path, campaign, (), (), kept, (), optimizer_excluded=excluded, blockers=blockers)
    workbook = load_workbook(workbook_path, data_only=False)
    try:
        assert workbook["Portfolios"][2][2].value == "BALANCED"
        assert workbook["Excluded"][2][8].value == "UNKNOWN_PROFILE"
    finally:
        workbook.close()


def test_verify_workbook_rejects_hyperlinks_without_publishing(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]

    def linked_builder(workbook_path, *_args):
        workbook = Workbook()
        workbook.active.title = "Summary"
        for name in ("Finalists", "Portfolios", "Members", "Excluded", "Metadata"):
            workbook.create_sheet(name)
        workbook["Summary"]["A1"].hyperlink = "https://example.test/"
        workbook.save(workbook_path)
        workbook.close()
        return workbook_path

    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: finalists, variant_generator=_profiled_variants, workbook_builder=linked_builder)
    result = service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and service.job(result["job_id"])["status"] in {"QUEUED", "RUNNING"}:
        time.sleep(0.01)

    assert service.job(result["job_id"])["status"] == "FAILED"
    assert not (tmp_path / ".portfolio-results" / result["campaign_id"] / "stage1.xlsx").exists()
    with pytest.raises(PortfolioPanelError):
        service.workbook(result["campaign_id"])


def test_package_adapter_status_is_a_structured_blocker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("mrs3.portfolio.adapter.run_portfolio_adapter", lambda *_args, **_kwargs: SimpleNamespace(variants=(), blockers=("BALANCED:POSITION_SIZING_FAILED",), excluded=(), warnings=()))

    result = PortfolioPanelService(tmp_path)._package_variant_generator(({"strategy_id": 1},), {}, ({"profile_id": "BALANCED"},))

    assert result["blockers"] == ["BALANCED:POSITION_SIZING_FAILED"]


def test_package_adapter_real_contract_rejects_invalid_campaign(tmp_path: Path) -> None:
    result = PortfolioPanelService(tmp_path)._package_variant_generator(
        ({"strategy_id": 1, "result_id": 2, "symbol": "BTCUSDT", "side": "LONG"},),
        {},
        ({"profile_id": "BALANCED", "max_candidates": 1},),
    )
    assert result["variants"] == ()
    assert result["blockers"] == ["CAMPAIGN_CONFIG_INVALID"]


def test_package_adapter_passes_variants_to_profile_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    passing = ({"candidate_id": "first", "profile": "BALANCED"}, {"candidate_id": "second", "profile": "BALANCED"})
    monkeypatch.setattr(
        "mrs3.portfolio.adapter.run_portfolio_adapter",
        lambda *_args, **_kwargs: SimpleNamespace(variants=passing, blockers=(), excluded=(), warnings=()),
    )
    profile = {"profile_id": "BALANCED", "max_candidates": 1}
    result = PortfolioPanelService(tmp_path)._package_variant_generator(({"strategy_id": 1},), {}, (profile,))
    assert result["variants"][0]["profile"] == "BALANCED"
    kept, excluded, blockers = PortfolioPanelService._cap_variants(result["variants"], (profile,))
    summary = PortfolioPanelService._summary(
        {"campaign_id": "campaign", "launch": {"profiles": [profile]}},
        (),
        (),
        kept,
        (),
        optimizer_excluded=excluded,
        blockers=blockers,
    )
    assert len(kept) == 2
    assert summary["variants_created"] == 2
    assert summary["prepared_by_profile"] == {"BALANCED": 2}


def test_package_search_exception_is_typed_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args, **_kwargs):
        raise RuntimeError("search crashed")

    monkeypatch.setattr("mrs3.portfolio.adapter.run_portfolio_adapter", broken)

    with pytest.raises(PortfolioPanelError) as error:
        PortfolioPanelService(Path.cwd())._package_variant_generator(({"strategy_id": 1},), {}, ({"profile_id": "BALANCED"},))

    assert error.value.code == "PORTFOLIO_JOB_FAILED"


def test_cancel_during_publication_rolls_back_every_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]
    entered = threading.Event()
    release = threading.Event()

    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: finalists, variant_generator=_profiled_variants)

    def blocked_verify(workbook_path: Path) -> None:
        entered.set()
        assert release.wait(timeout=3)

    monkeypatch.setattr(service, "_verify_workbook", blocked_verify)
    result = service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest})
    assert entered.wait(timeout=3)
    service.cancel(result["job_id"])
    release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and service.job(result["job_id"])["status"] in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}:
        time.sleep(0.01)

    assert service.job(result["job_id"])["status"] == "CANCELLED"
    assert not (tmp_path / ".portfolio-staging" / result["campaign_id"] / "stage1.xlsx").exists()
    assert not (tmp_path / ".portfolio-results" / result["campaign_id"] / "stage1.xlsx").exists()
    with pytest.raises(PortfolioPanelError):
        service.workbook(result["campaign_id"])


def test_workbook_sanitizes_formula_text_and_rejects_formula_cells(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    _write_config(path)
    service = PortfolioPanelService(tmp_path, path)
    campaign = {"campaign_id": "campaign-fixed", "input_digest": "i", "config_digest": "c", "versions": {"policy_version": "p"}, "created_at_utc": "2000-01-01T00:00:00Z"}
    workbook_path = tmp_path / "literal.xlsx"
    service._write_workbook(workbook_path, campaign, (), (), (), (), optimizer_excluded=({"profile": "BALANCED", "selection_reason": "OPEN_POLICY", "message": '=HYPERLINK("http://evil")'},))
    workbook = load_workbook(workbook_path, data_only=False)
    try:
        assert workbook["Excluded"][2][9].data_type == "s"
        assert workbook["Excluded"][2][9].value.startswith("'=")
    finally:
        workbook.close()

    formula_path = tmp_path / "formula.xlsx"
    workbook = load_workbook(workbook_path)
    workbook["Summary"]["B2"] = "=1+1"
    workbook.save(formula_path)
    workbook.close()
    with pytest.raises(ValueError, match="formulas"):
        service._verify_workbook(formula_path)


def test_staging_file_root_is_rejected_before_builder_runs(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    staging = tmp_path / ".portfolio-staging"
    staging.write_text("not a directory", encoding="utf-8")
    finalists = [{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]
    called = []

    def builder(*_args):
        called.append(True)

    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: finalists, variant_generator=_profiled_variants, workbook_builder=builder)
    result = service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and service.job(result["job_id"])["status"] in {"QUEUED", "RUNNING"}:
        time.sleep(0.01)

    assert service.job(result["job_id"])["status"] == "FAILED"
    assert called == []


def test_workbook_rejects_path_outside_results_root(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: finalists, variant_generator=_profiled_variants)
    result = service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and service.job(result["job_id"])["status"] in {"QUEUED", "RUNNING"}:
        time.sleep(0.01)
    runtime = service.registry.runtime(result["job_id"])
    runtime["workbook_path"] = str(tmp_path / "outside.xlsx")
    service.registry.sync(result["job_id"], {"state": "COMMITTED"}, runtime=runtime)

    with pytest.raises(Exception):
        service.workbook(result["campaign_id"])


def test_job_projection_progress_cancel_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    registry.submit("portfolio.stage1", {}, "x", ("portfolio_optimizer",), job_id="j")
    registry.transition("j", "RUNNING")
    service = PortfolioPanelService(tmp_path, path, registry=registry)
    service._threads["j"] = object()
    job = service.job("j")
    assert job["status"] == "RUNNING"
    assert job["overall_percent"] == 0
    assert service.cancel("j")["status"] == "CANCEL_REQUESTED"
    restarted = PortfolioPanelService(tmp_path, path, registry=PanelJobRegistry(tmp_path / ".panel-jobs.json"))
    assert restarted.job("j")["status"] == "INTERRUPTED"


def test_threadless_orphan_with_runtime_failure_is_projected_and_releases_submit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    registry.submit("portfolio.stage1", {}, "orphan", ("portfolio_optimizer",), job_id="orphan")
    service = PortfolioPanelService(tmp_path, path, registry=registry, finalists_reader=lambda *_: [{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}], variant_generator=_profiled_variants)
    original_runtime = registry.runtime
    monkeypatch.setattr(registry, "runtime", lambda *_: (_ for _ in ()).throw(OSError("runtime unavailable")))

    assert service.active_job() is None
    assert registry.get("orphan")["state"] == "FAILED"
    assert registry.get("orphan")["error"]["code"] == "INTERRUPTED"

    monkeypatch.setattr(registry, "runtime", original_runtime)
    retry = service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest})
    assert retry["status"] == "QUEUED"


def test_threadless_orphan_without_campaign_is_projected(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    _write_config(path)
    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    registry.submit("portfolio.stage1", {}, "orphan-no-runtime", ("portfolio_optimizer",), job_id="orphan-no-runtime")
    service = PortfolioPanelService(tmp_path, path, registry=registry)

    assert service.active_job() is None
    assert registry.get("orphan-no-runtime")["state"] == "FAILED"
    assert registry.get("orphan-no-runtime")["error"]["code"] == "INTERRUPTED"


def test_stage2_route_is_permanently_blocked(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    _write_config(path)
    service = PortfolioPanelService(tmp_path, path)
    with pytest.raises(Exception) as error:
        service.submit_tester_submission("campaign", {"confirmed": True, "campaign_id": "campaign"})
    assert getattr(error.value, "code", None) == "PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED"


@pytest.mark.parametrize("payload", ({}, {"confirmed": True, "campaign_id": "other"}))
def test_stage2_route_blocks_before_confirmation_validation(tmp_path: Path, payload: dict) -> None:
    service = PortfolioPanelService(tmp_path)
    with pytest.raises(PortfolioPanelError) as error:
        service.submit_tester_submission("campaign", payload)
    assert error.value.code == "PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED"


@pytest.mark.parametrize("failure", (PanelJobError("RESOURCE_BUSY"), OSError("registry unavailable"), TypeError("registry unavailable"), ValueError("registry unavailable")))
def test_cancel_maps_non_not_found_failures_to_runtime_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: Exception) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    _write_config(path)
    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    registry.submit("portfolio.stage1", {}, "cancel-runtime", ("portfolio_optimizer",), job_id="cancel-runtime")
    service = PortfolioPanelService(tmp_path, path, registry=registry)
    monkeypatch.setattr(registry, "cancel", lambda *_: (_ for _ in ()).throw(failure))

    with pytest.raises(PortfolioPanelError) as error:
        service.cancel("cancel-runtime")

    assert error.value.code == "PORTFOLIO_JOB_RUNTIME_UNAVAILABLE"
    assert error.value.status == 500


def test_failed_finalization_does_not_wedge_future_submission(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]
    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    original_transition = registry.transition
    original_sync = registry.sync

    def fail_terminal_transition(job_id, state, *, phase=None):
        if state == "FAILED":
            raise PanelJobError("FINALIZATION_FAILED")
        return original_transition(job_id, state, phase=phase)

    def fail_terminal_sync(job_id, status, *, runtime=None):
        if status.get("state") == "FAILED":
            raise PanelJobError("FINALIZATION_FAILED")
        return original_sync(job_id, status, runtime=runtime)

    registry.transition = fail_terminal_transition
    registry.sync = fail_terminal_sync
    service = PortfolioPanelService(
        tmp_path,
        path,
        registry=registry,
        finalists_reader=lambda *_: finalists,
        variant_generator=lambda *_: (_ for _ in ()).throw(RuntimeError("generation failed")),
    )
    payload = {"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest}
    first = service.submit_campaign({**payload})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and first["job_id"] in service._threads:
        time.sleep(0.01)
    assert service.job(first["job_id"])["status"] == "INTERRUPTED"

    service.variant_generator = lambda selected, *_: selected
    second = service.submit_campaign({**payload})
    assert second["job_id"] != first["job_id"]


def test_running_transition_failure_releases_queued_orphan(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]
    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    original_transition = registry.transition
    original_sync = registry.sync

    def fail_terminal_transition(job_id, state, *, phase=None):
        if state in {"RUNNING", "FAILED"}:
            raise PanelJobError("FINALIZATION_FAILED")
        return original_transition(job_id, state, phase=phase)

    def fail_terminal_sync(job_id, status, *, runtime=None):
        if status.get("state") == "FAILED":
            raise PanelJobError("FINALIZATION_FAILED")
        return original_sync(job_id, status, runtime=runtime)

    registry.transition = fail_terminal_transition
    registry.sync = fail_terminal_sync
    service = PortfolioPanelService(tmp_path, path, registry=registry, finalists_reader=lambda *_: finalists)
    payload = {"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest}
    first = service.submit_campaign({**payload})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and first["job_id"] in service._threads:
        time.sleep(0.01)
    assert first["job_id"] not in service._threads
    assert service.job(first["job_id"])["status"] == "INTERRUPTED"
    second = service.submit_campaign({**payload})
    assert second["job_id"] != first["job_id"]


def test_stage1_success_publishes_exact_workbook_after_commit(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [{"strategy_id": 7, "result_id": 11, "strategy_name": "fixture", "symbol": "BTCUSDT", "side": "LONG", "user_status": "FINALIST", "user_rank": 1}]
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: finalists, variant_generator=_profiled_variants)
    result = service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and service.job(result["job_id"])["status"] in {"QUEUED", "RUNNING"}:
        time.sleep(0.01)
    job = service.job(result["job_id"])
    assert job["status"] == "SUCCEEDED", (job, service.registry.runtime(result["job_id"]))
    assert "workbook_path" in service.registry.runtime(result["job_id"]), service.registry.runtime(result["job_id"]).keys()
    assert service.registry.runtime(result["job_id"])["summary"]["prepared"] == 1
    workbook_bytes = service.workbook(result["campaign_id"])
    workbook_path = tmp_path / "stage1.xlsx"
    workbook_path.write_bytes(workbook_bytes)
    workbook = load_workbook(workbook_path, data_only=False)
    assert workbook.sheetnames == ["Summary", "Finalists", "Portfolios", "Members", "Excluded", "Metadata"]
    assert workbook["Metadata"].sheet_state == "hidden"
    assert [cell.value for cell in workbook["Finalists"][2]] == [result["campaign_id"], 7, 11, "BTCUSDT", "LONG", "FINALIST", 1, 1, "SELECTED", "WITHIN_MAXIMUM"]
    assert workbook["Portfolios"].max_row == 2
    assert workbook["Portfolios"][2][6].value is None
    assert workbook["Portfolios"][2][7].value == 1
    assert workbook["Portfolios"][2][12].value == "UNKNOWN"


def test_http_routes_use_typed_error_envelope(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        body = json.dumps({"confirmed": True, "campaign_id": "missing"}).encode()
        connection.request("POST", "/api/v2/portfolio/campaigns/missing/tester-submissions", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 409
        assert payload["error"]["code"] == "PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED"
        assert payload["error"]["field_errors"] == []
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("headers", "body"),
    (({}, b"{}"), ({"Content-Type": "application/json"}, b""), ({"Content-Type": "application/json"}, b"{")),
)
def test_http_stage2_rejects_before_request_parsing(tmp_path: Path, headers: dict[str, str], body: bytes) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("POST", "/api/v2/portfolio/campaigns/missing/tester-submissions", body=body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 409
        assert payload["error"]["code"] == "PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "endpoint",
    (
        "/api/v2/portfolio/readiness",
        "/api/v2/portfolio/settings",
        "/api/v2/portfolio/jobs/unknown",
        "/api/v2/portfolio/jobs/active",
        "/api/v2/portfolio/campaigns/unknown/results",
        "/api/v2/portfolio/campaigns/unknown/stage1.xlsx",
    ),
)
def test_http_portfolio_get_requires_local_host(tmp_path: Path, endpoint: str) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", endpoint, headers={"Host": "attacker.example"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 403
        assert payload["error"] == "local Host header required"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_cancel_requires_json_request_and_returns_202(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config)
    controller._panel_jobs.submit(
        "portfolio.stage1",
        {"campaign_id": "campaign", "input_digest": "input", "config_digest": "config"},
        "portfolio-cancel",
        ("portfolio_optimizer",),
        job_id="portfolio-job",
    )
    controller._panel_jobs.reserve_runtime(
        "portfolio-job",
        "campaign",
        {"campaign_id": "campaign", "input_digest": "input", "config_digest": "config"},
    )
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("POST", "/api/v2/portfolio/jobs/portfolio-job/cancel", body=b"{}", headers={})
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 415
        assert payload["error"]["code"] == "PORTFOLIO_JOB_CANCEL_INVALID"
        connection.request("POST", "/api/v2/portfolio/jobs/portfolio-job/cancel", body=b"{}", headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 202
        assert payload["job_id"] == "portfolio-job"
        assert payload["status"] in {"CANCEL_REQUESTED", "CANCELLED"}
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("failure", (PanelJobError("RESOURCE_BUSY"), OSError("registry unavailable"), TypeError("registry unavailable"), ValueError("registry unavailable")))
def test_http_cancel_runtime_failures_return_500(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: Exception) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config)
    controller._panel_jobs.submit("portfolio.stage1", {}, "cancel-http-runtime", ("portfolio_optimizer",), job_id="cancel-http-runtime")
    monkeypatch.setattr(controller._portfolio_service.registry, "cancel", lambda *_: (_ for _ in ()).throw(failure))
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("POST", "/api/v2/portfolio/jobs/cancel-http-runtime/cancel", body=b"{}", headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 500
        assert payload["error"]["code"] == "PORTFOLIO_JOB_RUNTIME_UNAVAILABLE"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_active_runtime_lookup_failure_is_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config)
    controller._panel_jobs.submit("portfolio.stage1", {}, "active-runtime", ("portfolio_optimizer",), job_id="active-runtime")
    controller._portfolio_service._threads["active-runtime"] = object()
    monkeypatch.setattr(controller._portfolio_service.registry, "runtime", lambda *_args: (_ for _ in ()).throw(KeyError("runtime")))
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/api/v2/portfolio/jobs/active")
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 500
        assert payload["error"]["code"] == "PORTFOLIO_JOB_RUNTIME_UNAVAILABLE"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("runtime_mode", ("raises_key_error", "returns_non_mapping"))
def test_http_job_runtime_failures_are_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime_mode: str) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config)
    controller._panel_jobs.submit("portfolio.stage1", {}, "job-runtime", ("portfolio_optimizer",), job_id="job-runtime")

    if runtime_mode == "raises_key_error":
        def broken_runtime(*_args, **_kwargs):
            raise KeyError("runtime")

        monkeypatch.setattr(controller._portfolio_service.registry, "runtime", broken_runtime)
    else:
        monkeypatch.setattr(controller._portfolio_service.registry, "runtime", lambda *_args, **_kwargs: ["runtime"])

    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/api/v2/portfolio/jobs/job-runtime")
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 500
        assert payload == {
            "error": {
                "code": "PORTFOLIO_JOB_RUNTIME_UNAVAILABLE",
                "message": "portfolio job runtime is unavailable",
                "field_errors": [],
            }
        }
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("endpoint", ("/api/v2/portfolio/readiness", "/api/v2/portfolio/settings"))
@pytest.mark.parametrize(
    "failure",
    (
        OSError("read failed"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
        PortfolioConfigError("invalid config"),
    ),
)
def test_http_settings_read_failures_are_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    failure: Exception,
) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config)

    def broken_settings_raw(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(controller._portfolio_service, "_settings_raw", broken_settings_raw)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", endpoint)
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 500
        assert payload["error"]["code"] == "PORTFOLIO_SETTINGS_UNAVAILABLE"
        assert payload["error"]["field_errors"] == []
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_results_partial_runtime_is_typed(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config)
    controller._panel_jobs.submit("portfolio.stage1", {}, "partial-runtime", ("portfolio_optimizer",), job_id="partial-runtime")
    controller._panel_jobs.reserve_runtime("partial-runtime", "campaign", {"campaign_id": "campaign"})
    controller._panel_jobs.transition("partial-runtime", "RUNNING")
    controller._panel_jobs.transition("partial-runtime", "COMMITTED")
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/api/v2/portfolio/campaigns/campaign/results")
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 500
        assert payload["error"]["code"] == "PORTFOLIO_JOB_RUNTIME_UNAVAILABLE"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

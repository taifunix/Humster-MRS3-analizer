from __future__ import annotations

import asyncio
import hashlib
import json
from http.client import HTTPConnection
from pathlib import Path
import threading
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
import zipfile
from openpyxl import load_workbook
from openpyxl import Workbook

import pytest
import duckdb

from mrs3.panel import PanelController, create_panel_server
from mrs3.panel_portfolio import PortfolioPanelError, PortfolioPanelService, STAGES, _redact_text, _safe_cell, _weighted_payload_pairs
from mrs3.panel_jobs import PanelJobError, PanelJobRegistry
from mrs3.config import DuckDBImportSettings
from mrs3.performance_v2_store import PerformanceV2StoreError
from mrs3.portfolio.config import PortfolioConfigError, migrate_portfolio_config_document
from mrs3.portfolio.adapter import (
    CAMPAIGN_CONTRACT_VERSION,
    CAMPAIGN_SEARCH_MODE,
    CAMPAIGN_WEIGHTED_ALGO_VERSION,
    CampaignContractError,
    MINUTE_CAPACITY_UNAVAILABLE,
)


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


def _finalist(**updates) -> dict:
    return {
        "strategy_id": 7,
        "result_id": 11,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "user_status": "FINALIST",
        "user_rank": 1,
        "actions": [],
        "equity": [],
        **updates,
    }


def test_panel_freeze_emits_weighted_contract_without_legacy_stage1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [_finalist()]

    class IdleThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("mrs3.panel_portfolio.threading.Thread", IdleThread)
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: finalists)
    result = service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest})
    campaign = service.registry.runtime(result["job_id"])["campaign"]
    assert campaign["campaign_contract_version"] == CAMPAIGN_CONTRACT_VERSION
    assert campaign["search_mode"] == CAMPAIGN_SEARCH_MODE
    assert campaign["weighted_algo_version"] == CAMPAIGN_WEIGHTED_ALGO_VERSION
    assert "stage1_mode" not in campaign
    assert all(campaign["versions"][key] == campaign[key] for key in ("campaign_contract_version", "search_mode", "weighted_algo_version"))


def test_panel_freeze_carries_dedicated_weighted_template_and_digest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    template_path = Path(__file__).parents[1] / "templates" / "strategies" / "portfolio-weighted-mrs" / "base.json"
    expected_template = json.loads(template_path.read_text(encoding="utf-8"))
    expected_digest = hashlib.sha256(template_path.read_bytes()).hexdigest()

    class IdleThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("mrs3.panel_portfolio.threading.Thread", IdleThread)
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: [_finalist()])
    result = service.submit_campaign({
        "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
        "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
        "expected_config_digest": digest,
    })

    campaign = service.registry.runtime(result["job_id"])["campaign"]
    assert campaign["strategy_template"] == expected_template
    assert campaign["strategy_template_digest"] == expected_digest
    assert campaign["strategy_template"]["mrs"]["position_priority"] == 3


def test_panel_freeze_keeps_template_snapshot_after_file_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    source_path = Path(__file__).parents[1] / "templates" / "strategies" / "portfolio-weighted-mrs" / "base.json"
    template_path = tmp_path / "base.json"
    template_path.write_bytes(source_path.read_bytes())
    monkeypatch.setattr("mrs3.panel_portfolio._WEIGHTED_TEMPLATE_PATH", template_path)

    class IdleThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("mrs3.panel_portfolio.threading.Thread", IdleThread)
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: [_finalist()])
    result = service.submit_campaign({
        "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
        "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
        "expected_config_digest": digest,
    })
    campaign = service.registry.runtime(result["job_id"])["campaign"]
    frozen_template = json.loads(json.dumps(campaign["strategy_template"]))
    frozen_digest = campaign["strategy_template_digest"]

    template_path.write_text('{"mrs":{"position_priority":1}}', encoding="utf-8")

    assert campaign["strategy_template"] == frozen_template
    assert campaign["strategy_template_digest"] == frozen_digest


@pytest.mark.parametrize(
    "template_bytes",
    [
        b"not-json",
        b"[]",
        b"null",
        b"true",
        b"NaN",
        b"{\"value\": Infinity}",
        b"{\"nested\": {\"value\": NaN}}",
        b"{\"nested\": {\"value\": -Infinity}}",
        b"\xff\xfe{}",
    ],
)
def test_panel_freeze_rejects_malformed_or_non_object_weighted_template_before_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, template_bytes: bytes,
) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    template_path = tmp_path / "base.json"
    template_path.write_bytes(template_bytes)
    monkeypatch.setattr("mrs3.panel_portfolio._WEIGHTED_TEMPLATE_PATH", template_path)
    calls: list[tuple[object, ...]] = []

    def reader(*args):
        calls.append(args)
        return [_finalist()]

    service = PortfolioPanelService(tmp_path, path, finalists_reader=reader)
    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign({
            "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
            "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
            "expected_config_digest": digest,
        })

    assert error.value.code == "PORTFOLIO_TEMPLATE_INVALID"
    assert calls == []
    assert service.registry.list() == []


def test_panel_freeze_rejects_missing_weighted_template_before_reader(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    monkeypatch.setattr("mrs3.panel_portfolio._WEIGHTED_TEMPLATE_PATH", tmp_path / "missing.json")
    calls: list[tuple[object, ...]] = []

    def reader(*args):
        calls.append(args)
        return [_finalist()]

    service = PortfolioPanelService(tmp_path, path, finalists_reader=reader)
    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign({
            "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
            "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
            "expected_config_digest": digest,
        })

    assert error.value.code == "PORTFOLIO_TEMPLATE_INVALID"
    assert calls == []
    assert service.registry.list() == []


def test_campaign_launch_accepts_directional_shared_symbol_limits(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    service = PortfolioPanelService(tmp_path, path)
    config, _raw, _document = service._config()

    launch = service._normalise_campaign(
        {
            "pairs": [{"pair": " btCusdt ", "max_finalist_long": 1, "max_finalist_short": 1}],
            "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
            "expected_config_digest": digest,
        },
        config,
        digest,
    )

    assert launch["selected_pairs"] == [["BTCUSDT", "LONG"], ["BTCUSDT", "SHORT"]]
    assert launch["maximums"] == {"BTCUSDT|LONG": 1, "BTCUSDT|SHORT": 1}


def test_campaign_launch_accepts_one_enabled_direction(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    service = PortfolioPanelService(tmp_path, path)
    config, _raw, _document = service._config()

    launch = service._normalise_campaign(
        {
            "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 0, "max_finalist_short": 1}],
            "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
            "expected_config_digest": digest,
        },
        config,
        digest,
    )

    assert launch["selected_pairs"] == [["BTCUSDT", "SHORT"]]
    assert launch["maximums"] == {"BTCUSDT|SHORT": 1}


def test_campaign_launch_keeps_distinct_pairs_in_stable_long_only_order(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    service = PortfolioPanelService(tmp_path, path)
    config, _raw, _document = service._config()

    launch = service._normalise_campaign(
        {
            "pairs": [
                {"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0},
                {"pair": "ETHUSDT", "max_finalist_long": 1, "max_finalist_short": 0},
            ],
            "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
            "expected_config_digest": digest,
        },
        config,
        digest,
    )

    assert launch["selected_pairs"] == [["BTCUSDT", "LONG"], ["ETHUSDT", "LONG"]]
    assert launch["maximums"] == {"BTCUSDT|LONG": 1, "ETHUSDT|LONG": 1}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_finalist_long", -1),
        ("max_finalist_short", -1),
        ("max_finalist_long", True),
        ("max_finalist_short", False),
        ("max_finalist_long", 1.5),
        ("max_finalist_short", "1"),
    ],
)
def test_campaign_rejects_invalid_finalist_limits_before_reader(tmp_path: Path, field: str, value: int) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    calls: list[tuple[object, ...]] = []

    def reader(*args):
        calls.append(args)
        return ()

    service = PortfolioPanelService(tmp_path, path, finalists_reader=reader)
    pair = {"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 1}
    pair[field] = value

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign(
            {
                "pairs": [pair],
                "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
                "expected_config_digest": digest,
            }
        )

    assert error.value.code == "PORTFOLIO_CAMPAIGN_INVALID"
    assert error.value.field_errors[0]["field"] == f"pairs[0].{field}"
    assert calls == []


def test_campaign_rejects_missing_finalist_limit_with_field_error_before_reader(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    calls: list[tuple[object, ...]] = []
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *args: calls.append(args) or ())
    pair = {"pair": "BTCUSDT", "max_finalist_short": 1}

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign(
            {
                "pairs": [pair],
                "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
                "expected_config_digest": digest,
            }
        )

    assert error.value.status == 422
    assert error.value.field_errors[0]["field"] == "pairs[0]"
    assert error.value.field_errors[0]["code"] == "INVALID_PAIR"
    assert calls == []


def test_campaign_rejects_both_directions_disabled_before_reader(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    calls: list[tuple[object, ...]] = []
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *args: calls.append(args) or ())

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign(
            {
                "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 0, "max_finalist_short": 0}],
                "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
                "expected_config_digest": digest,
            }
        )

    assert error.value.code == "PORTFOLIO_CAMPAIGN_INVALID"
    assert error.value.field_errors[0]["field"] == "pairs[0].max_finalist_long"
    assert calls == []


@pytest.mark.parametrize(
    "pairs",
    [
        [
            {"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0},
            {"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0},
        ],
        [{"pair": "BTCUSDT", "max_finalist_long": 1}],
        [{"pair": "BTCUSDT", "max_finalist_long": None, "max_finalist_short": 0}],
        [{"pair": "BTCUSDT", "max_finalist_long": "1", "max_finalist_short": 0}],
    ],
)
def test_campaign_rejects_duplicate_or_malformed_limits_before_reader(tmp_path: Path, pairs: list[dict[str, object]]) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    calls: list[tuple[object, ...]] = []

    def reader(*args):
        calls.append(args)
        return ()

    service = PortfolioPanelService(tmp_path, path, finalists_reader=reader)

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign(
            {
                "pairs": pairs,
                "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
                "expected_config_digest": digest,
            }
        )

    assert error.value.code == "PORTFOLIO_CAMPAIGN_INVALID"
    assert calls == []


def test_invalid_resume_contract_maps_before_decode_or_runtime_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    saved = registry.submit("portfolio.stage1", {}, "resume-contract", ("portfolio_optimizer",), job_id="resume-contract")
    registry.reserve_runtime(saved["job_id"], "campaign", {"search_mode": "PRETEST_PROXY"})
    service = PortfolioPanelService(tmp_path, registry=registry)
    monkeypatch.setattr("mrs3.panel_portfolio.base64.b64decode", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("decode called")))
    calls: list[str] = []
    service.cutoff_selector = lambda *_args, **_kwargs: calls.append("cutoff")
    service.variant_generator = lambda *_args, **_kwargs: calls.append("variants")

    service._run(saved["job_id"])

    persisted = registry.get(saved["job_id"])
    runtime = registry.runtime(saved["job_id"])
    assert persisted["state"] == "FAILED"
    assert persisted["error"]["code"] == "CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED"
    assert runtime["diagnostics"] == [{
        "severity": "ERROR",
        "code": "CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED",
        "message": "CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED",
    }]
    assert runtime["journal"][-1]["code"] == "CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED"
    assert calls == []


def test_invalid_resume_nonmapping_runtime_persists_typed_contract_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    saved = registry.submit("portfolio.stage1", {}, "resume-runtime", ("portfolio_optimizer",), job_id="resume-runtime")
    service = PortfolioPanelService(tmp_path, registry=registry)
    monkeypatch.setattr(registry, "runtime", lambda *_args: [])

    service._run(saved["job_id"])

    persisted = registry.get(saved["job_id"])
    stored_runtime = registry.jobs[saved["job_id"]]["runtime"]
    assert persisted["state"] == "FAILED"
    assert persisted["error"]["code"] == "CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED"
    assert stored_runtime["diagnostics"][0]["code"] == "CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED"


def test_invalid_resume_nonmapping_campaign_persists_typed_contract_failure(tmp_path: Path) -> None:
    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    saved = registry.submit("portfolio.stage1", {}, "resume-campaign", ("portfolio_optimizer",), job_id="resume-campaign")
    registry.reserve_runtime(saved["job_id"], "campaign", "not-a-campaign")
    service = PortfolioPanelService(tmp_path, registry=registry)

    service._run(saved["job_id"])

    persisted = registry.get(saved["job_id"])
    runtime = registry.runtime(saved["job_id"])
    assert persisted["state"] == "FAILED"
    assert persisted["error"]["code"] == "CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED"
    assert runtime["diagnostics"][0]["code"] == "CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED"


@pytest.mark.parametrize("signal_type", (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError))
def test_worker_reraises_process_control_signals_before_failure_persistence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, signal_type: type[BaseException]) -> None:
    registry = PanelJobRegistry(tmp_path / ".panel-jobs.json")
    saved = registry.submit("portfolio.stage1", {}, f"signal-{signal_type.__name__}", ("portfolio_optimizer",), job_id=f"signal-{signal_type.__name__}")
    campaign = {
        "campaign_contract_version": CAMPAIGN_CONTRACT_VERSION,
        "search_mode": CAMPAIGN_SEARCH_MODE,
        "weighted_algo_version": CAMPAIGN_WEIGHTED_ALGO_VERSION,
        "versions": {
            "campaign_contract_version": CAMPAIGN_CONTRACT_VERSION,
            "search_mode": CAMPAIGN_SEARCH_MODE,
            "weighted_algo_version": CAMPAIGN_WEIGHTED_ALGO_VERSION,
        },
    }
    registry.reserve_runtime(saved["job_id"], "campaign", campaign)
    service = PortfolioPanelService(tmp_path, registry=registry)
    original_transition = registry.transition
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise signal_type()
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(registry, "transition", fail_once)
    with pytest.raises(signal_type):
        service._run(saved["job_id"])

    assert calls == 1
    assert registry.get(saved["job_id"])["state"] == "QUEUED"


def test_invalid_panel_create_contract_maps_before_registry_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: [])
    monkeypatch.setattr(
        "mrs3.panel_portfolio.validate_campaign_contract",
        lambda _campaign: (_ for _ in ()).throw(CampaignContractError("CAMPAIGN_SEARCH_MODE_REQUIRED")),
    )
    payload = {
        "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
        "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
        "expected_config_digest": digest,
    }
    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign(payload)
    assert (error.value.code, error.value.status, str(error.value)) == (
        "CAMPAIGN_SEARCH_MODE_REQUIRED", 422, "CAMPAIGN_SEARCH_MODE_REQUIRED"
    )
    assert service.registry.list() == []


def test_valid_frozen_campaign_reaches_adapter_fact_blocker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [_finalist()]

    class IdleThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: finalists)
    monkeypatch.setattr("mrs3.panel_portfolio.threading.Thread", IdleThread)
    result = service.submit_campaign({"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}], "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}], "expected_config_digest": digest})
    campaign = service.registry.runtime(result["job_id"])["campaign"]
    generated = service._package_variant_generator(finalists, campaign, campaign["launch"]["profiles"])
    assert generated["variants"] == ()
    assert generated["blockers"] == [MINUTE_CAPACITY_UNAVAILABLE]


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


def test_v2_legacy_algorithm_versions_are_resolved_for_panel_and_save(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    document = _config()
    document["algorithm_versions"] = {
        "sizing": "portfolio_optimizer_sizing_v1",
        "ranking": "portfolio_optimizer_ranking_v1",
    }
    source = json.dumps(document, ensure_ascii=False, indent=2).encode()
    path.write_bytes(source)
    digest = hashlib.sha256(source).hexdigest()
    service = PortfolioPanelService(tmp_path, path)

    settings = service.settings_get()
    assert settings["document"]["algorithm_versions"] == {
        "sizing": "portfolio_optimizer_sizing_v2",
        "ranking": "portfolio_optimizer_ranking_v2",
    }
    assert settings["digest"] == digest
    _, config_raw, campaign_document = service._config()
    assert config_raw == source
    assert campaign_document["algorithm_versions"] == settings["document"]["algorithm_versions"]

    saved = service.settings_put({"expected_digest": digest, "document": settings["document"]})
    assert saved["document"]["algorithm_versions"] == settings["document"]["algorithm_versions"]
    assert json.loads(path.read_text(encoding="utf-8"))["algorithm_versions"] == settings["document"]["algorithm_versions"]


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


def test_readiness_requests_metadata_only_finalists(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    _write_config(path)
    with duckdb.connect(str(tmp_path / "performance.duckdb")) as connection:
        connection.execute("create table selection_runs(symbol varchar, side varchar)")
        connection.execute("insert into selection_runs values ('BTCUSDT', 'LONG')")
    calls: list[tuple[object, ...]] = []

    def reader(*args):
        calls.append(args)
        return ({"symbol": "BTCUSDT", "side": "LONG"},)

    service = PortfolioPanelService(tmp_path, path, finalists_reader=reader)
    readiness = service.readiness()

    assert readiness["available_pairs"] == ["BTCUSDT|LONG"]
    assert len(calls) == 1
    assert calls[0][-1] is False


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


@pytest.mark.parametrize("max_candidates", [0, 51])
def test_campaign_rejects_max_candidates_outside_adapter_contract(tmp_path: Path, max_candidates: int) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    service = PortfolioPanelService(tmp_path, path)
    config, _raw, _document = service._config()

    with pytest.raises(PortfolioPanelError) as error:
        service._normalise_campaign(
            {
                "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
                "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": max_candidates}],
                "expected_config_digest": digest,
            },
            config,
            digest,
        )

    assert error.value.code == "PORTFOLIO_CAMPAIGN_INVALID"
    assert error.value.field_errors == [{
        "field": "profiles[0].max_candidates",
        "code": "MAX_CANDIDATES_RANGE",
        "message": "max_candidates must be between 1 and 50",
    }]


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
    finalists = [_finalist()]
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
    source_rows = [[_finalist()]]

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
    source_rows[0] = [_finalist(strategy_id=99, result_id=100)]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and service.job(result["job_id"])["status"] in {"QUEUED", "RUNNING"}:
        time.sleep(0.01)

    assert len(calls) == 1
    assert service.job(result["job_id"])["status"] == "SUCCEEDED"
    assert service.registry.runtime(result["job_id"])["campaign"]["finalists"][0]["strategy_id"] == 7


def test_campaign_freezes_private_weighted_rows_without_public_series_aliases(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    source_rows = [{
        "strategy_id": 7,
        "result_id": 11,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "user_status": "FINALIST",
        "user_rank": 1,
        "actions": [{"timestamp_utc": "2026-01-01T00:00:00Z", "size": "1"}],
        "action_series": [{"timestamp_utc": "2026-01-01T00:00:00Z", "size": "alias"}],
        "minute_actions": [{"timestamp_utc": "2026-01-01T00:00:00Z", "size": "minute"}],
        "equity": [{"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"}],
        "equity_series": [{"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "alias"}],
    }]

    class IdleThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("mrs3.panel_portfolio.threading.Thread", IdleThread)
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: source_rows)
    result = service.submit_campaign({
        "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
        "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
        "expected_config_digest": digest,
    })

    source_rows[0]["strategy_id"] = 99
    source_rows[0]["actions"][0]["size"] = "mutated"
    source_rows[0]["equity"][0]["equity"] = "mutated"

    campaign = service.registry.runtime(result["job_id"])["campaign"]
    assert "weighted_input_rows" in campaign
    weighted = campaign["weighted_input_rows"]
    assert weighted == [{
        "strategy_id": 7,
        "result_id": 11,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "actions": [{"timestamp_utc": "2026-01-01T00:00:00Z", "size": "1"}],
        "equity": [{"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"}],
    }]
    aliases = {"actions", "action_series", "minute_actions", "equity", "equity_series"}
    assert not aliases.intersection(campaign["finalists"][0])
    public_job = service.job(result["job_id"])
    assert not aliases.intersection(public_job)
    private_prepared_fields = {
        "prepared_json", "source_digest", "preparation_version", "_prepared_cycles",
        "raw_action_series", "raw_equity_series",
    }
    assert not private_prepared_fields.intersection(campaign["finalists"][0])
    assert not private_prepared_fields.intersection(public_job)
    assert "weighted_input_rows" not in public_job


def test_campaign_prepares_exact_production_finalist_result_ids_before_strict_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import mrs3.panel_portfolio as panel_portfolio_module

    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    (tmp_path / "config.local.json").write_text(json.dumps({"duckdb_import": {"workers": 3}}), encoding="utf-8")
    metadata_rows = [
        {"result_id": 22},
        {"result_id": 11},
        {"result_id": 22},
    ]
    full_rows = [
        _finalist(strategy_id=8, result_id=22),
        _finalist(strategy_id=7, result_id=11),
    ]
    reader_calls: list[tuple[Path, tuple[tuple[str, str], ...], bool]] = []
    preparation_calls: list[tuple[Path, tuple[int, ...], int]] = []

    def production_reader(database, pairs, include_series=True):
        reader_calls.append((database, tuple(pairs), include_series))
        return metadata_rows if not include_series else full_rows

    def preparer(database, result_ids, *, workers):
        preparation_calls.append((database, tuple(result_ids), workers))

    monkeypatch.setattr(panel_portfolio_module, "read_current_finalists", production_reader)

    class IdleThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(panel_portfolio_module.threading, "Thread", IdleThread)
    service = PortfolioPanelService(
        tmp_path,
        path,
        optimizer_input_preparer=preparer,
    )
    result = service.submit_campaign({
        "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
        "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
        "expected_config_digest": digest,
    })

    assert result["status"] == "QUEUED"
    assert [call[2] for call in reader_calls] == [False, True]
    assert preparation_calls == [(tmp_path / "performance.duckdb", (22, 11), 3)]


def test_campaign_rejects_invalid_production_metadata_result_id_before_preparation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import mrs3.panel_portfolio as panel_portfolio_module

    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    preparation_calls: list[tuple[object, ...]] = []

    def production_reader(_database, _pairs, include_series=True):
        return [{"result_id": True}] if not include_series else []

    def preparer(*args, **kwargs):
        preparation_calls.append((*args, *kwargs.values()))

    monkeypatch.setattr(panel_portfolio_module, "read_current_finalists", production_reader)
    service = PortfolioPanelService(
        tmp_path,
        path,
        optimizer_input_preparer=preparer,
    )

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign({
            "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
            "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
            "expected_config_digest": digest,
        })

    assert error.value.code == "PORTFOLIO_FINALISTS_UNAVAILABLE"
    assert preparation_calls == []


def test_production_preparation_lock_failure_is_a_typed_snapshot_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import mrs3.panel_portfolio as panel_portfolio_module

    path = tmp_path / "portfolio_optimizer.local.json"
    _write_config(path)
    monkeypatch.setattr(
        panel_portfolio_module,
        "read_current_finalists",
        lambda *_args, **_kwargs: [{"result_id": 11}],
    )

    def locked(*_args, **_kwargs):
        raise PerformanceV2StoreError("writer lock busy")

    service = PortfolioPanelService(tmp_path, path, optimizer_input_preparer=locked)
    with pytest.raises(PortfolioPanelError) as error:
        service._snapshot_finalists(
            _config(),
            {"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}]},
        )

    assert error.value.code == "PORTFOLIO_FINALISTS_UNAVAILABLE"
    assert error.value.status == 422


def test_finalist_reader_wiring_controls_production_preparation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    _write_config(path)

    default_service = PortfolioPanelService(tmp_path, path)
    custom_service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: ())

    assert default_service._uses_production_finalists_reader is True
    assert custom_service._uses_production_finalists_reader is False


def test_default_snapshot_prepares_only_current_finalist_artifacts(tmp_path: Path) -> None:
    from datetime import datetime, timezone
    from tests.test_performance_v2_selection import _candidate_db
    from tests.test_portfolio_input import _add_review

    path = tmp_path / "portfolio_optimizer.local.json"
    _write_config(path)
    connection = _candidate_db(tmp_path)
    database = tmp_path / "performance.duckdb"
    try:
        strategy_id, result_id = connection.execute(
            "select strategy_id, current_result_id from strategies"
        ).fetchone()
        connection.execute(
            """update strategy_results set reported_start_utc = report_start_utc,
               reported_end_utc = report_end_utc, effective_start_utc = report_start_utc,
               effective_end_utc = report_end_utc, sizing_use_upnl = true,
               sizing_use_frozen_balance = true, sizing_use_fix = false,
               sizing_balance_percentage_long = 100, sizing_risk_long = 1,
               sizing_max_balance = 0 where result_id = ?""",
            [result_id],
        )
        connection.execute("update strategy_actions set price = 10, cost = 10")
        _add_review(
            connection,
            run_id="run-default",
            review_id="review-default",
            symbol="BTCUSDT",
            side="LONG",
            strategy_id=strategy_id,
            result_id=result_id,
            user_status="FINALIST",
            selection_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    finally:
        connection.close()
    (tmp_path / "strategy_performance.duckdb").replace(database)

    service = PortfolioPanelService(tmp_path, path)
    finalists, weighted_rows = service._snapshot_finalists(
        _config(),
        {"pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}]},
    )

    assert [row["result_id"] for row in finalists] == [result_id]
    assert len(weighted_rows) == 1
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute(
            "select result_id from optimizer_prepared_inputs order by result_id"
        ).fetchall() == [(result_id,)]


def test_campaign_resolves_ordered_series_aliases_into_private_weighted_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    source_rows = [{
        "strategy_id": 7,
        "result_id": 11,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "user_status": "FINALIST",
        "user_rank": 1,
        "action_series": [{"timestamp_utc": "2026-01-01T00:00:00Z", "size": "1"}],
        "equity_series": [{"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"}],
    }]

    class IdleThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("mrs3.panel_portfolio.threading.Thread", IdleThread)
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: source_rows)
    result = service.submit_campaign({
        "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
        "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
        "expected_config_digest": digest,
    })

    campaign = service.registry.runtime(result["job_id"])["campaign"]
    assert campaign["weighted_input_rows"][0]["actions"] == source_rows[0]["action_series"]
    assert campaign["weighted_input_rows"][0]["equity"] == source_rows[0]["equity_series"]


def test_campaign_rejects_missing_weighted_series_at_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    source_rows = [{"strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG"}]

    class IdleThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("mrs3.panel_portfolio.threading.Thread", IdleThread)
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: source_rows)

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign({
            "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
            "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
            "expected_config_digest": digest,
        })

    assert error.value.code == "PORTFOLIO_FINALISTS_UNAVAILABLE"


def test_campaign_rejects_non_json_weighted_series_at_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    source_rows = [_finalist(equity=[{"timestamp_utc": "2026-01-01T00:00:00Z", "equity": float("nan")}])]

    class IdleThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("mrs3.panel_portfolio.threading.Thread", IdleThread)
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: source_rows)

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign({
            "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
            "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
            "expected_config_digest": digest,
        })

    assert error.value.code == "PORTFOLIO_FINALISTS_UNAVAILABLE"
    assert error.value.status == 422


def test_campaign_rejects_non_mapping_finalist_at_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)

    class IdleThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("mrs3.panel_portfolio.threading.Thread", IdleThread)
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: [None])

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign({
            "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
            "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
            "expected_config_digest": digest,
        })

    assert error.value.code == "PORTFOLIO_FINALISTS_UNAVAILABLE"
    assert error.value.status == 422


@pytest.mark.parametrize("missing", ("symbol", "side", "strategy_id", "result_id"))
def test_campaign_rejects_missing_weighted_identity_at_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    source_row = {
        "strategy_id": 7,
        "result_id": 11,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "actions": [],
        "equity": [],
    }
    source_row.pop(missing)

    class IdleThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("mrs3.panel_portfolio.threading.Thread", IdleThread)
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: [source_row])

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign({
            "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
            "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
            "expected_config_digest": digest,
        })

    assert error.value.code == "PORTFOLIO_FINALISTS_UNAVAILABLE"
    assert error.value.field_errors == [{
        "field": f"finalists[0].{missing}",
        "code": "MISSING_FINALIST_IDENTITY",
        "message": f"finalist {missing} identity is unavailable",
    }]


@pytest.mark.parametrize("missing", ("actions", "equity"))
def test_campaign_rejects_missing_weighted_series_with_field_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    source_row = {
        "strategy_id": 7,
        "result_id": 11,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "actions": [],
        "equity": [],
    }
    source_row.pop(missing)

    class IdleThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("mrs3.panel_portfolio.threading.Thread", IdleThread)
    service = PortfolioPanelService(tmp_path, path, finalists_reader=lambda *_: [source_row])

    with pytest.raises(PortfolioPanelError) as error:
        service.submit_campaign({
            "pairs": [{"pair": "BTCUSDT", "max_finalist_long": 1, "max_finalist_short": 0}],
            "profiles": [{"profile_id": "BALANCED", "equity_usdt": "10000", "max_candidates": 1}],
            "expected_config_digest": digest,
        })

    assert error.value.code == "PORTFOLIO_FINALISTS_UNAVAILABLE"
    assert error.value.field_errors == [{
        "field": f"finalists[0].{missing}",
        "code": "MISSING_FINALIST_SERIES",
        "message": f"finalist {missing} series is unavailable",
    }]


def test_invalid_published_workbook_fails_without_download(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_optimizer.local.json"
    digest = _write_config(path)
    finalists = [_finalist()]

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
    finalists = [_finalist()]
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
    finalists = [_finalist()]
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
    finalists = [_finalist()]
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
    finalists = [_finalist()]
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
    finalists = [_finalist()]
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


def test_workbook_keeps_raw_series_out_of_public_cells(tmp_path: Path) -> None:
    service = PortfolioPanelService(tmp_path)
    campaign = {
        "campaign_id": "campaign-fixed",
        "input_digest": "i",
        "config_digest": "c",
        "versions": {"policy_version": "p", "algorithm_versions": {}},
        "created_at_utc": "2000-01-01T00:00:00Z",
    }
    selected = ({
        "strategy_id": 1,
        "result_id": 2,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "actions": "PRIVATE_ACTIONS",
        "equity": "PRIVATE_EQUITY",
        "raw": "PRIVATE_RAW",
        "prepared_json": "PRIVATE_PREPARED_JSON",
        "source_digest": "PRIVATE_SOURCE_DIGEST",
        "preparation_version": "PRIVATE_PREPARATION_VERSION",
        "_prepared_cycles": "PRIVATE_PREPARED_CYCLES",
        "raw_action_series": "PRIVATE_RAW_ACTION_SERIES",
        "raw_equity_series": "PRIVATE_RAW_EQUITY_SERIES",
    },)
    variants = ({
        "candidate_id": "candidate",
        "profile": "BALANCED",
        "members": ({"symbol": "BTCUSDT", "raw": "PRIVATE_MEMBER_RAW"},),
        "metrics": {"raw": "PRIVATE_METRICS_RAW"},
    },)
    workbook_path = tmp_path / "raw-boundary.xlsx"

    service._write_workbook(workbook_path, campaign, (), selected, variants, ())

    workbook = load_workbook(workbook_path, data_only=False)
    try:
        values = [cell.value for sheet in workbook.worksheets for row in sheet.iter_rows() for cell in row]
        headers = {
            cell.value
            for worksheet in workbook.worksheets
            for cell in worksheet[1]
        }
    finally:
        workbook.close()
    assert not {
        "PRIVATE_ACTIONS", "PRIVATE_EQUITY", "PRIVATE_RAW", "PRIVATE_MEMBER_RAW", "PRIVATE_METRICS_RAW",
        "PRIVATE_PREPARED_JSON", "PRIVATE_SOURCE_DIGEST", "PRIVATE_PREPARATION_VERSION",
        "PRIVATE_PREPARED_CYCLES", "PRIVATE_RAW_ACTION_SERIES", "PRIVATE_RAW_EQUITY_SERIES",
    }.intersection(values)
    assert not {
        "prepared_json", "source_digest", "preparation_version", "_prepared_cycles",
        "raw_action_series", "raw_equity_series",
    }.intersection(headers)


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


def test_weighted_workbook_projects_model_summary_and_member_facts(tmp_path: Path) -> None:
    service = PortfolioPanelService(tmp_path)
    campaign = {
        "campaign_id": "campaign-weighted",
        "input_digest": "i",
        "config_digest": "c",
        "versions": {"policy_version": "p"},
        "created_at_utc": "2000-01-01T00:00:00Z",
        "launch": {"profiles": [{"profile_id": "BALANCED", "equity_usdt": "2000", "max_candidates": 1}]},
    }
    member = {
        "strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG",
        "user_rank": 1, "x_usdt": Decimal("100"), "capacity_usdt": Decimal("500"),
        "priority": 3, "source_scale": Decimal("0.25"), "hold90": Decimal("12"),
        "warnings": ("HOLD_UNKNOWN",), "quantity": Decimal("99"),
    }
    variant = {
        "candidate_id": "candidate-weighted", "profile": "BALANCED", "search_mode": "WEIGHTED_V1",
        "members": (member,),
        "strategy_payloads": ({
            "facts": {"B": "2000", "C": "500", "q": "0.05", "x": "100"},
            "strategy": {"basic": {"symbol": "BTCUSDT", "max_balance": 10000}},
        },),
        "metrics": {
            "required_bank_usdt": Decimal("1200"), "B_margin_usdt": Decimal("1100"),
            "p30_common_usdt_30d": Decimal("90"), "p30_limiter_model_usdt_30d": Decimal("80"),
            "max_drawdown_pct": Decimal("10"), "cdar_peak80_usdt": Decimal("30"),
            "cdar_peak90_usdt": Decimal("40"), "I_all_usdt": Decimal("100"),
            "I_held_usdt": Decimal("100"), "M_all_usdt": Decimal("200"),
            "limiter_L": 2, "limiter_p30_status": "MODEL", "limiter_release_status": "UNKNOWN",
            "reserve_before_limiter_pct": Decimal("20"), "reserve_after_limiter_pct": Decimal("10"),
            "budget_limited": True, "bottleneck": "MM",
        },
    }
    path = tmp_path / "weighted.xlsx"
    service._write_workbook(path, campaign, (), (), (variant,), ())

    workbook = load_workbook(path, data_only=False)
    try:
        summary = {row[0].value: row[1].value for row in workbook["Summary"].iter_rows(min_row=2)}
        assert summary["Weighted candidate ID"] == "candidate-weighted"
        assert summary["B required USDT"] == "1200"
        assert summary["B available USDT"] == "2000"
        assert summary["B saturation USDT"] == 10000
        assert summary["P30 common USDT/30d"] == "90"
        assert summary["MaxDD %"] == "10"
        assert summary["CDaR peak80 USDT"] == "30"
        assert summary["MM all USDT"] == "200"
        assert summary["P30 limiter USDT/30d"] == "80"
        assert summary["Limiter P30 status"] == "MODEL"
        assert summary["Joint status"] == "NOT_TESTED"
        assert summary["Reserve before limiter"] == "20"
        assert summary["Reserve after limiter"] == "10"
        assert summary["Reserve UNKNOWN reason"] == "UNKNOWN"
        assert summary["Bottleneck"] == "MM"
        assert summary["Budget status"] == "True"

        headers = {cell.value: index for index, cell in enumerate(workbook["Members"][1])}
        row = workbook["Members"][2]
        assert row[headers["Finalist"]].value == "7/11"
        assert row[headers["Side"]].value == "LONG"
        assert row[headers["x USDT"]].value == "100"
        assert row[headers["C USDT"]].value == "500"
        assert row[headers["q"]].value == "0.05"
        assert row[headers["Priority"]].value == 3
        assert row[headers["max_balance"]].value == 10000
        assert row[headers["Source Scale"]].value == "0.25"
        assert row[headers["Hold90"]].value == "12"
        assert row[headers["Warnings"]].value == '["HOLD_UNKNOWN"]'
        assert row[headers["Quantity"]].value == "UNKNOWN"
    finally:
        workbook.close()


def test_weighted_workbook_joins_payloads_by_symbol_without_shifting_rows(tmp_path: Path) -> None:
    service = PortfolioPanelService(tmp_path)
    campaign = {"campaign_id": "campaign-weighted", "input_digest": "i", "config_digest": "c", "versions": {"policy_version": "p"}, "created_at_utc": "2000-01-01T00:00:00Z"}
    members = (
        {"strategy_id": 1, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "x_usdt": Decimal("100"), "capacity_usdt": Decimal("500")},
        {"strategy_id": 2, "result_id": 22, "symbol": "ZEROUSDT", "side": "LONG", "x_usdt": Decimal("0"), "capacity_usdt": Decimal("300")},
        {"strategy_id": 3, "result_id": 33, "symbol": "ETHUSDT", "side": "LONG", "x_usdt": Decimal("200"), "capacity_usdt": Decimal("600")},
        {"strategy_id": 4, "result_id": 44, "symbol": "SOLUSDT", "side": "SHORT", "x_usdt": Decimal("300"), "capacity_usdt": Decimal("700")},
    )
    payloads = (
        {"facts": {"q": "0.2"}, "strategy": {"basic": {"symbol": "ETHUSDT", "max_balance": 2000}}},
        {"facts": {"q": "0.1"}, "strategy": {"basic": {"symbol": "BTCUSDT", "max_balance": 1000}}},
        {"facts": {"q": "0.9"}, "strategy": {"basic": {"symbol": "SOLUSDT", "max_balance": 9000}}},
        None,
        {"facts": {"q": "0.8"}, "strategy": {"basic": {"symbol": "EXTRAUSDT", "max_balance": 8000}}},
    )
    path = tmp_path / "weighted-join.xlsx"
    service._write_workbook(path, campaign, (), (), ({"candidate_id": "candidate", "profile": "BALANCED", "search_mode": "WEIGHTED_V1", "members": members, "strategy_payloads": payloads, "metrics": {}},), ())

    workbook = load_workbook(path, data_only=False)
    try:
        headers = {cell.value: index for index, cell in enumerate(workbook["Members"][1])}
        rows = {workbook["Members"].cell(row=index, column=headers["Pair"] + 1).value: workbook["Members"][index] for index in range(2, 6)}
        assert rows["BTCUSDT"][headers["q"]].value == "0.1"
        assert rows["BTCUSDT"][headers["max_balance"]].value == 1000
        assert rows["ETHUSDT"][headers["q"]].value == "0.2"
        assert rows["ETHUSDT"][headers["max_balance"]].value == 2000
        for symbol in ("ZEROUSDT", "SOLUSDT"):
            assert rows[symbol][headers["q"]].value == "UNKNOWN"
            assert rows[symbol][headers["max_balance"]].value == "UNKNOWN"
            assert rows[symbol][headers["Quantity"]].value == "UNKNOWN"
            assert rows[symbol][headers["Notional USDT"]].value == "UNKNOWN"
    finally:
        workbook.close()


def test_weighted_payload_pairing_keeps_same_symbol_long_and_short_distinct() -> None:
    members = (
        {"strategy_id": 1, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "x_usdt": Decimal("100")},
        {"strategy_id": 2, "result_id": 22, "symbol": "BTCUSDT", "side": "SHORT", "x_usdt": Decimal("100")},
    )
    payloads = (
        {"side": "LONG", "strategy": {"basic": {"symbol": "BTCUSDT"}}},
        {"side": "SHORT", "strategy": {"basic": {"symbol": "BTCUSDT"}}},
    )
    pairs = _weighted_payload_pairs({"members": members, "strategy_payloads": payloads})
    assert [payload["side"] for _member, payload in pairs] == ["LONG", "SHORT"]


def test_weighted_payload_pairing_ignores_missing_payload_side_without_crashing() -> None:
    members = ({"strategy_id": 1, "result_id": 11, "symbol": "BTCUSDT", "side": "SHORT", "x_usdt": Decimal("100")},)
    payloads = ({"strategy": {"basic": {"symbol": "BTCUSDT"}}},)

    assert _weighted_payload_pairs({"members": members, "strategy_payloads": payloads}) == ((members[0], None),)


def test_weighted_workbook_marks_invalid_projected_numbers_unknown(tmp_path: Path) -> None:
    service = PortfolioPanelService(tmp_path)
    campaign = {"campaign_id": "campaign-weighted", "input_digest": "i", "config_digest": "c", "versions": {"policy_version": "p"}, "created_at_utc": "2000-01-01T00:00:00Z"}
    members = (
        {"strategy_id": 1, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "x_usdt": Decimal("100")},
        {"strategy_id": 2, "result_id": 22, "symbol": "ETHUSDT", "side": "LONG", "x_usdt": Decimal("200")},
    )
    payloads = (
        {"facts": {}, "strategy": {"basic": {"symbol": "BTCUSDT", "max_balance": "not-a-number"}}},
        {"facts": {"q": "0.2"}, "strategy": {"basic": {"symbol": "ETHUSDT", "max_balance": 2000}}},
    )
    path = tmp_path / "weighted-invalid-numbers.xlsx"
    service._write_workbook(path, campaign, (), (), ({"candidate_id": "candidate", "profile": "BALANCED", "search_mode": "WEIGHTED_V1", "members": members, "strategy_payloads": payloads, "metrics": {}},), ())
    workbook = load_workbook(path, data_only=False)
    try:
        headers = {cell.value: index for index, cell in enumerate(workbook["Members"][1])}
        rows = {workbook["Members"].cell(row=index, column=headers["Pair"] + 1).value: workbook["Members"][index] for index in range(2, 4)}
        assert rows["BTCUSDT"][headers["q"]].value == "UNKNOWN"
        assert rows["BTCUSDT"][headers["max_balance"]].value == "UNKNOWN"
        assert rows["ETHUSDT"][headers["q"]].value == "0.2"
        assert rows["ETHUSDT"][headers["max_balance"]].value == 2000
        assert rows["ETHUSDT"][headers["Quantity"]].value == "UNKNOWN"
        assert rows["ETHUSDT"][headers["Notional USDT"]].value == "UNKNOWN"
    finally:
        workbook.close()


def test_weighted_workbook_coerces_missing_or_scalar_projection_inputs(tmp_path: Path) -> None:
    service = PortfolioPanelService(tmp_path)
    campaign = {"campaign_id": "campaign-weighted", "input_digest": "i", "config_digest": "c", "versions": {"policy_version": "p"}, "created_at_utc": "2000-01-01T00:00:00Z"}
    member = {"strategy_id": 1, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "x_usdt": Decimal("100")}
    missing_payloads = tmp_path / "weighted-none-payloads.xlsx"
    service._write_workbook(missing_payloads, campaign, (), (), ({"candidate_id": "none", "profile": "BALANCED", "search_mode": "WEIGHTED_V1", "members": (member,), "strategy_payloads": None, "metrics": {}},), ())
    scalar_members = tmp_path / "weighted-scalar-members.xlsx"
    service._write_workbook(scalar_members, campaign, (), (), ({"candidate_id": "scalar", "profile": "BALANCED", "search_mode": "WEIGHTED_V1", "members": "scalar", "strategy_payloads": 1, "metrics": {}},), ())
    workbook = load_workbook(missing_payloads, data_only=False)
    try:
        headers = {cell.value: index for index, cell in enumerate(workbook["Members"][1])}
        row = workbook["Members"][2]
        assert row[headers["q"]].value == "UNKNOWN"
        assert row[headers["max_balance"]].value == "UNKNOWN"
    finally:
        workbook.close()
    workbook = load_workbook(scalar_members, data_only=False)
    try:
        assert workbook["Members"].max_row == 1
    finally:
        workbook.close()


@pytest.mark.parametrize(
    "payloads,unknown_symbol",
    [
        (({"facts": {"q": "0.1"}, "strategy": {"basic": {"symbol": "BTCUSDT", "max_balance": 1000}}}, {"facts": {"q": "0.2"}, "strategy": {"basic": {"symbol": "BTCUSDT", "max_balance": 2000}}}, {"facts": {"q": "0.3"}, "strategy": {"basic": {"symbol": "ETHUSDT", "max_balance": 3000}}},), "BTCUSDT"),
        (({"facts": {"q": "0.1"}, "strategy": {"basic": {"symbol": "BTCUSDT", "max_balance": 1000}}}, None,), "ETHUSDT"),
        (({"facts": {"q": "0.1"}, "strategy": {"basic": {"symbol": "BTCUSDT", "max_balance": 1000}}}, {} ,), "ETHUSDT"),
        (({"facts": {"q": "0.1"}, "strategy": {"basic": {"symbol": "BTCUSDT", "max_balance": 1000}}}, {"facts": {"q": "0.2"}, "strategy": {"basic": {"symbol": "", "max_balance": 2000}}},), "ETHUSDT"),
    ],
)
def test_weighted_workbook_marks_ambiguous_payload_join_unknown(tmp_path: Path, payloads: tuple[Any, ...], unknown_symbol: str) -> None:
    service = PortfolioPanelService(tmp_path)
    campaign = {"campaign_id": "campaign-weighted", "input_digest": "i", "config_digest": "c", "versions": {"policy_version": "p"}, "created_at_utc": "2000-01-01T00:00:00Z"}
    members = (
        {"strategy_id": 1, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "x_usdt": Decimal("100"), "capacity_usdt": Decimal("500")},
        {"strategy_id": 2, "result_id": 22, "symbol": "ETHUSDT", "side": "LONG", "x_usdt": Decimal("200"), "capacity_usdt": Decimal("600")},
    )
    path = tmp_path / "weighted-ambiguous.xlsx"
    service._write_workbook(path, campaign, (), (), ({"candidate_id": "candidate", "profile": "BALANCED", "search_mode": "WEIGHTED_V1", "members": members, "strategy_payloads": payloads, "metrics": {}},), ())
    workbook = load_workbook(path, data_only=False)
    try:
        headers = {cell.value: index for index, cell in enumerate(workbook["Members"][1])}
        rows = {workbook["Members"].cell(row=index, column=headers["Pair"] + 1).value: workbook["Members"][index] for index in range(2, 4)}
        assert rows[unknown_symbol][headers["q"]].value == "UNKNOWN"
        assert rows[unknown_symbol][headers["max_balance"]].value == "UNKNOWN"
        known_symbol = "ETHUSDT" if unknown_symbol == "BTCUSDT" else "BTCUSDT"
        assert rows[known_symbol][headers["q"]].value == ("0.3" if known_symbol == "ETHUSDT" else "0.1")
    finally:
        workbook.close()


def test_weighted_summary_selects_first_variant_and_keeps_candidate_label_before_metrics(tmp_path: Path) -> None:
    service = PortfolioPanelService(tmp_path)
    campaign = {"campaign_id": "campaign-weighted", "input_digest": "i", "config_digest": "c", "versions": {"policy_version": "p"}, "created_at_utc": "2000-01-01T00:00:00Z"}
    variants = (
        {"candidate_id": "candidate-a", "profile": "BALANCED", "search_mode": "WEIGHTED_V1", "metrics": {
            "required_bank_usdt": Decimal("101"), "B_available_usdt": Decimal("1000"), "B_sat_settings_usdt": Decimal("2000"),
            "p30_common_usdt_30d": Decimal("30"), "p30_limiter_model_usdt_30d": Decimal("25"), "max_drawdown_pct": Decimal("4"),
            "cdar_peak80_usdt": Decimal("8"), "cdar_peak90_usdt": Decimal("9"), "reserve_before_limiter_pct": Decimal("20"),
            "reserve_after_limiter_pct": Decimal("10"), "M_all_usdt": Decimal("12"), "bottleneck": "MM", "limiter_L": 1,
            "limiter_p30_status": "MODEL", "limiter_release_status": "UNKNOWN", "budget_limited": True,
        }},
        {"candidate_id": "candidate-b", "profile": "BALANCED", "search_mode": "WEIGHTED_V1", "metrics": {"required_bank_usdt": Decimal("202")}},
    )
    path = tmp_path / "weighted-summary.xlsx"
    service._write_workbook(path, campaign, (), (), variants, ())
    workbook = load_workbook(path, data_only=False)
    try:
        keys = [row[0].value for row in workbook["Summary"].iter_rows(min_row=2)]
        summary = {row[0].value: row[1].value for row in workbook["Summary"].iter_rows(min_row=2)}
        candidate_index = keys.index("Weighted candidate ID")
        weighted_keys = (
            "B required USDT", "B available USDT", "B saturation USDT", "P30 common USDT/30d", "P30 limiter USDT/30d",
            "MaxDD %", "CDaR peak80 USDT", "CDaR peak90 USDT", "Reserve before limiter", "Reserve after limiter",
            "MM all USDT", "Bottleneck", "Limiter L", "Limiter P30 status", "Limiter release status", "Joint status",
            "Search status", "Budget status", "Reserve UNKNOWN reason",
        )
        assert all(keys.index(key) > candidate_index for key in weighted_keys)
        assert summary["Weighted candidate ID"] == "candidate-a"
        assert summary["B required USDT"] == "101"
        assert summary["B available USDT"] == "1000"
        assert summary["B saturation USDT"] == "2000"
        assert summary["P30 common USDT/30d"] == "30"
        assert summary["P30 limiter USDT/30d"] == "25"
        assert summary["MaxDD %"] == "4"
        assert summary["CDaR peak80 USDT"] == "8"
        assert summary["CDaR peak90 USDT"] == "9"
        assert summary["Reserve before limiter"] == "20"
        assert summary["Reserve after limiter"] == "10"
        assert summary["Reserve UNKNOWN reason"] == "UNKNOWN"
        assert summary["Bottleneck"] == "MM"
        assert summary["Limiter P30 status"] == "MODEL"
        assert summary["Limiter release status"] == "UNKNOWN"
        assert summary["Budget status"] == "True"
    finally:
        workbook.close()


def test_weighted_workbook_marks_duplicate_member_symbol_unknown(tmp_path: Path) -> None:
    service = PortfolioPanelService(tmp_path)
    campaign = {"campaign_id": "campaign-weighted", "input_digest": "i", "config_digest": "c", "versions": {"policy_version": "p"}, "created_at_utc": "2000-01-01T00:00:00Z"}
    members = (
        {"strategy_id": 1, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG", "x_usdt": Decimal("100")},
        {"strategy_id": 2, "result_id": 22, "symbol": "BTCUSDT", "side": "LONG", "x_usdt": Decimal("200")},
        {"strategy_id": 3, "result_id": 33, "symbol": "ETHUSDT", "side": "LONG", "x_usdt": Decimal("300")},
    )
    payloads = (
        {"facts": {"q": "0.1"}, "strategy": {"basic": {"symbol": "BTCUSDT", "max_balance": 1000}}},
        {"facts": {"q": "0.3"}, "strategy": {"basic": {"symbol": "ETHUSDT", "max_balance": 3000}}},
    )
    path = tmp_path / "weighted-duplicate-member.xlsx"
    service._write_workbook(path, campaign, (), (), ({"candidate_id": "candidate", "profile": "BALANCED", "search_mode": "WEIGHTED_V1", "members": members, "strategy_payloads": payloads, "metrics": {}},), ())
    workbook = load_workbook(path, data_only=False)
    try:
        headers = {cell.value: index for index, cell in enumerate(workbook["Members"][1])}
        rows = {workbook["Members"].cell(row=index, column=headers["Pair"] + 1).value: [workbook["Members"][index], index] for index in range(2, 5)}
        assert rows["ETHUSDT"][0][headers["q"]].value == "0.3"
        assert rows["BTCUSDT"][0][headers["q"]].value == "UNKNOWN"
        assert rows["BTCUSDT"][0][headers["max_balance"]].value == "UNKNOWN"
    finally:
        workbook.close()


def test_weighted_summary_uses_explicit_reserve_unknown_reason_only(tmp_path: Path) -> None:
    service = PortfolioPanelService(tmp_path)
    campaign = {"campaign_id": "campaign-weighted", "input_digest": "i", "config_digest": "c", "versions": {"policy_version": "p"}, "created_at_utc": "2000-01-01T00:00:00Z"}
    variant = {
        "candidate_id": "candidate", "profile": "BALANCED", "search_mode": "WEIGHTED_V1",
        "metrics": {"reserve_before_limiter_pct": Decimal("10"), "reserve_after_limiter_pct": Decimal("5"), "reserve_unknown_reason": "EXPLICIT_REASON", "limiter_release_status": "UNKNOWN"},
    }
    path = tmp_path / "weighted-reserve-reason.xlsx"
    service._write_workbook(path, campaign, (), (), (variant,), ())
    workbook = load_workbook(path, data_only=False)
    try:
        summary = {row[0].value: row[1].value for row in workbook["Summary"].iter_rows(min_row=2)}
        assert summary["Reserve before limiter"] == "10"
        assert summary["Reserve after limiter"] == "5"
        assert summary["Reserve UNKNOWN reason"] == "EXPLICIT_REASON"
    finally:
        workbook.close()


def test_weighted_workbook_does_not_fallback_to_legacy_member_size_without_payload(tmp_path: Path) -> None:
    service = PortfolioPanelService(tmp_path)
    campaign = {
        "campaign_id": "campaign-weighted",
        "input_digest": "i", "config_digest": "c", "versions": {"policy_version": "p"},
        "created_at_utc": "2000-01-01T00:00:00Z",
    }
    member = {
        "strategy_id": 7, "result_id": 11, "symbol": "BTCUSDT", "side": "LONG",
        "x_usdt": Decimal("100"), "capacity_usdt": Decimal("500"), "priority": 3,
        "quantity": Decimal("99"), "maximum_closing_quantity": Decimal("88"),
        "actual_size_usdt": Decimal("777"), "position_size_usdt": Decimal("666"),
    }
    path = tmp_path / "weighted-missing-payload.xlsx"
    service._write_workbook(
        path, campaign, (), (), ({"candidate_id": "candidate", "profile": "BALANCED", "search_mode": "WEIGHTED_V1", "members": (member,), "metrics": {}},), (),
    )

    workbook = load_workbook(path, data_only=False)
    try:
        headers = {cell.value: index for index, cell in enumerate(workbook["Members"][1])}
        row = workbook["Members"][2]
        assert row[headers["x USDT"]].value == "100"
        assert row[headers["C USDT"]].value == "500"
        assert row[headers["q"]].value == "UNKNOWN"
        assert row[headers["max_balance"]].value == "UNKNOWN"
        assert row[headers["Quantity"]].value == "UNKNOWN"
        assert row[headers["Notional USDT"]].value == "UNKNOWN"
    finally:
        workbook.close()


def test_workbook_uses_final_pretest_metrics_rank_and_composition_member_size(tmp_path: Path) -> None:
    service = PortfolioPanelService(tmp_path)
    campaign = {
        "campaign_id": "campaign-fixed", "input_digest": "i", "config_digest": "c",
        "versions": {"policy_version": "p"}, "created_at_utc": "2000-01-01T00:00:00Z",
        "launch": {"profiles": [{"profile_id": "BALANCED", "max_candidates": 2}]},
        "config_document": {"search": {"max_enumerated_combinations": 100}},
    }
    member = {
        "strategy_id": 1, "result_id": 2, "symbol": "BTCUSDT", "side": "LONG",
        "quantity": Decimal("4"), "maximum_closing_quantity": Decimal("99"),
        "actual_size_usdt": Decimal("123"), "position_size_usdt": Decimal("999"),
    }
    variant = {
        "candidate_id": "candidate", "profile": "BALANCED", "search_mode": "PRETEST_PROXY",
        "daily_pretest_rank": 1, "final_pretest_rank": 2, "refinement": "MINUTE",
        "members": (member,), "evaluations": 3,
        "pretest_period": {"start_utc": "2026-01-01", "end_utc": "2026-01-15", "coverage_pct": {}},
        "metrics": {
            "metric_basis": "PRETEST_PROXY", "proxy_pnl_usdt": Decimal("77"),
            "proxy_max_drawdown_pct": Decimal("2"), "proxy_recovery_factor": Decimal("3"),
            "proxy_reserve_usdt": Decimal("100"), "k": Decimal("0.5"), "k1": Decimal("0.6"),
            "tested_size_usdt": Decimal("100"), "actual_size_usdt": Decimal("123"),
            "tested_size_basis": "SOURCE_INITIAL_BALANCE_X_OPENING_LOT", "joint_metrics": "NOT_TESTED",
        },
    }
    path = tmp_path / "final-pretest.xlsx"
    service._write_workbook(path, campaign, (), (), (variant,), ())

    workbook = load_workbook(path, data_only=False)
    try:
        portfolio_headers = {cell.value: index for index, cell in enumerate(workbook["Portfolios"][1])}
        portfolio = workbook["Portfolios"][2]
        assert portfolio[portfolio_headers["Pretest PnL USDT"]].value == "77"
        assert portfolio[portfolio_headers["Actual Size USDT"]].value == "123"
        assert portfolio[portfolio_headers["Refinement"]].value == "MINUTE"
        assert portfolio[portfolio_headers["Final PRETEST Rank"]].value == 2
        member_headers = {cell.value: index for index, cell in enumerate(workbook["Members"][1])}
        members = workbook["Members"][2]
        assert members[member_headers["Quantity"]].value == "4"
        assert members[member_headers["Notional USDT"]].value == "123"
        for header in ("Finalist", "Side", "x USDT", "C USDT", "q", "Priority", "max_balance", "Source Scale", "Hold90", "Warnings"):
            assert members[member_headers[header]].value is None
        status_headers = {cell.value: index for index, cell in enumerate(workbook["Profile Status"][1])}
        status = workbook["Profile Status"][2]
        assert status[status_headers["Metric Basis"]].value == "PRETEST_PROXY"
        assert status[status_headers["Joint Metrics"]].value == "NOT_TESTED"
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
    finalists = [_finalist()]

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


def test_package_adapter_uses_duckdb_import_workers_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "config.local.json").write_text(json.dumps({
        "duckdb_import": {"workers": 3},
        "direct_materialization": {"workers": 99},
    }), encoding="utf-8")
    observed = {}

    def fake_adapter(*_args, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(variants=(), blockers=(), excluded=(), warnings=())

    monkeypatch.setattr("mrs3.portfolio.adapter.run_portfolio_adapter", fake_adapter)
    campaign = {"config_document": {"search": {"weighted_search": {
        "api_concurrency": 99,
        "csv_download_concurrency": 16,
    }}}}
    PortfolioPanelService(tmp_path)._package_variant_generator((), campaign, ())

    assert observed["workers"] == 3
    assert set(observed) == {"workspace_root", "workers"}
    (tmp_path / "config.local.json").write_text(json.dumps({"direct_materialization": {"workers": 99}}), encoding="utf-8")
    observed.clear()
    PortfolioPanelService(tmp_path)._package_variant_generator((), campaign, ())
    assert observed["workers"] == DuckDBImportSettings().workers


def test_package_adapter_real_contract_rejects_invalid_campaign(tmp_path: Path) -> None:
    result = PortfolioPanelService(tmp_path)._package_variant_generator(
        ({"strategy_id": 1, "result_id": 2, "symbol": "BTCUSDT", "side": "LONG"},),
        {},
        ({"profile_id": "BALANCED", "max_candidates": 1},),
    )
    assert result["variants"] == ()
    assert result["blockers"] == ["CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED"]


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
    finalists = [_finalist()]
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
    finalists = [_finalist()]
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
    finalists = [_finalist()]
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
    service = PortfolioPanelService(tmp_path, path, registry=registry, finalists_reader=lambda *_: [_finalist()], variant_generator=_profiled_variants)
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
    finalists = [_finalist()]
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
    finalists = [_finalist()]
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
    finalists = [_finalist(strategy_name="fixture")]
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

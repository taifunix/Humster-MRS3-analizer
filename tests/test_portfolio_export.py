from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from mrs3.portfolio.export import (
    ExportError,
    _invoke,
    _store_row_exists,
    canonical_export_json,
    composition_digest,
    export_portfolio,
    replay_decision,
)
from mrs3.portfolio.reports import normalize_report
from mrs3.portfolio.store import PortfolioStore
from tests.test_portfolio_integration import campaign, candidate, run
from tests.test_portfolio_reports import fixture_report


def _committed_result(tmp_path: Path):
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    result = run(campaign(budget=2, grid=("10",)), [candidate()], store=store)
    report = fixture_report(run_id=result.run_id, attempt_id="attempt", member="A")
    store.publish_portfolio_run(result.run_id, (normalize_report(report),), executable_identity={"binary": "fixture"})
    return store, result


def _evaluation_count(store: PortfolioStore) -> int:
    with duckdb.connect(str(store.path), read_only=True) as db:
        return int(db.execute("SELECT count(*) FROM evaluations").fetchone()[0])


def _portfolio_set_count(store: PortfolioStore) -> int:
    with duckdb.connect(str(store.path), read_only=True) as db:
        return int(db.execute("SELECT count(*) FROM portfolio_sets").fetchone()[0])


def test_package_exports_are_unique_and_keep_public_dispositions() -> None:
    import mrs3.portfolio as portfolio
    import mrs3.portfolio.export as export_module
    import mrs3.portfolio.integration as integration_module

    assert len(portfolio.__all__) == len(set(portfolio.__all__))
    for name in ("READY", "NEEDS_RETEST", "NEEDS_RESCREEN", "RESEARCH_ONLY"):
        assert portfolio.__all__.count(name) == 1
        assert getattr(portfolio, name) == getattr(export_module, name)
    assert integration_module.RESEARCH_ONLY == export_module.RESEARCH_ONLY


def test_export_writes_deterministic_strategy_manifests_and_report(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    reference = {"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS", "liquidity": "100"}}
    first = export_portfolio(result, tmp_path / "out", store=store, reference=reference, now_ms=2)
    second = export_portfolio(result, tmp_path / "out-2", store=store, reference=reference, now_ms=2)

    assert first.status == "NEEDS_RETEST"
    assert first.manifest["gates"]["settings"]["status"] == first.status
    assert first.manifest_digest == second.manifest_digest
    assert (tmp_path / "out" / "portfolio.json").is_file()
    assert (tmp_path / "out" / "portfolio_set.json").is_file()
    assert (tmp_path / "out" / "report.txt").is_file()
    assert "strategies/BTCUSDT.json" in first.artifacts
    assert first.artifacts["portfolio.json"].path == "portfolio.json"
    assert first.output_path == tmp_path / "out"
    assert first.manifest_path == tmp_path / "out" / "manifest.json"
    strategy = json.loads((tmp_path / "out" / "strategies" / "BTCUSDT.json").read_text())
    assert strategy["symbol"] == "BTCUSDT"
    assert "token" not in json.dumps(strategy).casefold()
    assert "initial/final equity" in (tmp_path / "out" / "report.txt").read_text().casefold()
    manifest = json.loads((tmp_path / "out" / "portfolio.json").read_text())
    assert manifest["scenario"]["deposit"] == "100"
    assert manifest["metrics"]["A"]["initial_equity"] == "100"
    assert manifest["validation"]["source"] == "Portfolio DB"
    assert manifest["manifest_digest_scope"] == "manifest_body_without_manifest_digest"


def test_account_cap_is_published_in_manifest(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    result = replace(result, accounts={"account-a": {"deposit": "100", "max_balance": "75"}})
    export_portfolio(result, tmp_path / "out", store=store, reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}}, now_ms=2)
    manifest = json.loads((tmp_path / "out" / "portfolio.json").read_text())
    assert manifest["scenario"]["cap"] == "75"


def test_conflicting_account_caps_are_order_independent_and_unknown(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    first_result = replace(result, accounts={"a": {"cap": "75"}, "b": {"cap": "90"}})
    second_result = replace(result, accounts={"b": {"cap": "90"}, "a": {"cap": "75"}})
    first = export_portfolio(
        first_result,
        tmp_path / "first",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
    )
    second = export_portfolio(
        second_result,
        tmp_path / "second",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
    )
    assert first.manifest["scenario"]["cap"] is None
    assert second.manifest["scenario"]["cap"] is None
    assert first.manifest_digest == second.manifest_digest
    assert (tmp_path / "first" / "portfolio.json").read_bytes() == (tmp_path / "second" / "portfolio.json").read_bytes()
    explicit_campaign = replace(first_result, campaign=replace(result.campaign, search_facts={"cap": "100"}))
    explicit = export_portfolio(
        explicit_campaign,
        tmp_path / "explicit",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
    )
    assert explicit.manifest["scenario"]["cap"] == "100"


def test_equal_numeric_account_caps_have_one_canonical_scenario_cap(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    result = replace(result, accounts={"a": {"cap": Decimal("75.0")}, "b": {"cap": "75"}})
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
    )
    assert exported.manifest["scenario"]["cap"] == "75"


def test_shared_liquidity_staleness_is_a_visible_gate_reason(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        shared_liquidity={"status": "PASS", "stale": True},
        tested_settings={"BTCUSDT": {"leverage": "3"}},
        exported_settings={"BTCUSDT": {"leverage": "3"}},
    )
    assert "LIQUIDITY_STALE" in exported.reasons
    assert exported.status == "RESEARCH_ONLY"
    gate = exported.manifest["gates"]["shared_liquidity"]
    assert gate["status"] == "UNKNOWN"
    assert gate["reason"] == "LIQUIDITY_STALE"
    assert gate["reasons"] == ["LIQUIDITY_STALE"]
    assert gate["evidence"]["status"] == "PASS"


@pytest.mark.parametrize(
    ("liquidity", "reason"),
    [
        ({"status": "PASS", "quality": {"status": "FAIL"}, "nested": {"api_key": "SECRET"}}, "LIQUIDITY_QUALITY_INSUFFICIENT"),
        ({"quality": "PASS"}, "GLOBAL_LIQUIDITY_UNKNOWN"),
    ],
)
def test_liquidity_gate_derives_unknown_for_untrusted_mapping(tmp_path: Path, liquidity: dict[str, object], reason: str):
    store, result = _committed_result(tmp_path)
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        shared_liquidity=liquidity,
        tested_settings={"BTCUSDT": {"leverage": "3"}},
        exported_settings={"BTCUSDT": {"leverage": "3"}},
    )
    gate = exported.manifest["gates"]["shared_liquidity"]
    assert gate["status"] == "UNKNOWN"
    assert gate["reason"] == reason
    assert gate["reasons"] == [reason]
    assert gate["evidence"].get("nested", {}).get("api_key") is None
    assert reason in exported.reasons


def test_boolean_liquidity_input_has_derived_gate_record(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        shared_liquidity=True,
        tested_settings={"BTCUSDT": {"leverage": "3"}},
        exported_settings={"BTCUSDT": {"leverage": "3"}},
    )
    gate = exported.manifest["gates"]["shared_liquidity"]
    assert gate["status"] == "PASS"
    assert gate["reason"] is None
    assert gate["reasons"] == []
    assert gate["evidence"] is True


def test_stale_reference_and_changed_settings_never_become_ready(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    stale = {"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 1, "quality": "PASS"}}
    blocked = export_portfolio(
        result,
        tmp_path / "stale",
        store=store,
        reference=stale,
        now_ms=2,
        tested_settings={"BTCUSDT": {"leverage": "3"}},
        exported_settings={"BTCUSDT": {"leverage": "3"}},
    )
    assert blocked.status == "RESEARCH_ONLY"
    assert "REFERENCE_STALE" in blocked.reasons
    assert blocked.evaluation_id == result.evaluation_id

    changed = export_portfolio(
        result,
        tmp_path / "changed",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        tested_settings={"BTCUSDT": {"leverage": "3", "quantity": "1"}},
        exported_settings={"BTCUSDT": {"leverage": "5", "quantity": "1"}},
    )
    assert changed.status == "NEEDS_RETEST"
    assert changed.manifest["gates"]["settings"]["status"] == changed.status
    assert "EXECUTABLE_PAYLOAD_CHANGED" in changed.reasons
    assert changed.evaluation_id == result.evaluation_id


def test_composition_digest_and_replay_use_only_portfolio_store(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    reference = {"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}}
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference=reference,
        now_ms=2,
        portfolio_set={"members": [{"portfolio_id": "one", "load": "1"}]},
    )
    assert exported.composition_digest == composition_digest({"members": [{"portfolio_id": "one", "load": "1"}]})
    replayed = replay_decision(store, result.evaluation_id)
    assert replayed["execution_campaign_id"] == result.execution_campaign_id
    assert replayed["decision_campaign_id"] == result.decision_campaign_id

    changed = export_portfolio(
        result,
        tmp_path / "changed-set",
        store=store,
        reference=reference,
        now_ms=2,
        portfolio_set={"members": [{"portfolio_id": "one", "load": "2"}]},
        previous_composition={"members": [{"portfolio_id": "one", "load": "1"}]},
    )
    assert changed.status == "NEEDS_RETEST"
    assert "PORTFOLIO_SET_CHANGED" in changed.reasons


def test_replay_requires_persisted_evaluation(tmp_path: Path):
    with pytest.raises(ExportError, match="missing"):
        replay_decision(PortfolioStore(tmp_path / "missing.duckdb"), "missing")


def test_export_embeds_exact_tested_settings_and_stable_candidate_ids(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    settings = {"BTCUSDT": {"leverage": "3", "rounded_quantity": "1", "geometry": "ma"}}
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        shared_liquidity=True,
        tested_settings=settings,
        exported_settings=settings,
    )
    strategy = json.loads((tmp_path / "out" / "strategies" / "BTCUSDT.json").read_text())
    assert strategy["settings"] == settings["BTCUSDT"]
    assert strategy["candidate_ids"]["LONG"] == "strategy-long"
    assert exported.evaluation_id != result.evaluation_id
    replayed = replay_decision(store, exported.evaluation_id)
    assert replayed["execution_campaign_id"] == result.execution_campaign_id


def test_refresh_callback_is_injected_and_reference_failure_is_visible(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    seen: list[tuple[str, ...]] = []

    def refresh(symbols):
        seen.append(tuple(symbols))
        return {"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}}

    exported = export_portfolio(result, tmp_path / "out", store=store, refresh_reference=refresh, now_ms=2)
    assert seen == [("BTCUSDT",)]
    assert "REFERENCE_MISSING" not in exported.reasons

    failed = export_portfolio(result, tmp_path / "failed", store=store, refresh_reference=lambda symbols: (_ for _ in ()).throw(RuntimeError("offline")), now_ms=2)
    assert "REFERENCE_REQUEST_FAILED" in failed.reasons
    assert failed.manifest["gates"]["exchange_reference"]["reason"] == "REFERENCE_REQUEST_FAILED"
    assert "REFERENCE_REQUEST_FAILED" in failed.manifest["gates"]["exchange_reference"]["reasons"]


def test_one_sided_settings_payload_is_unverified_and_cannot_be_ready(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        exported_settings={"BTCUSDT": {"leverage": "3"}},
    )
    assert exported.status == "NEEDS_RETEST"
    assert "EXECUTABLE_PAYLOAD_UNVERIFIED" in exported.reasons
    assert exported.manifest["gates"]["settings"]["status"] == exported.status


@pytest.mark.parametrize("blocked", ["leverage", "liquidity", "missing"])
def test_mandatory_blockers_do_not_persist_a_new_evaluation(tmp_path: Path, blocked: str):
    if blocked == "missing":
        store = PortfolioStore(tmp_path / "portfolio.duckdb")
        result = run(campaign(budget=2, grid=("10",)), [candidate()], store=store)
    else:
        store, result = _committed_result(tmp_path)
    before = _evaluation_count(store)
    kwargs = {
        "store": store,
        "reference": {"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        "now_ms": 2,
        "tested_settings": {"BTCUSDT": {"leverage": "3"}},
        "exported_settings": {"BTCUSDT": {"leverage": "3"}},
        "shared_liquidity": True,
    }
    if blocked == "leverage":
        kwargs["execution_facts"] = {"status": "COMMITTED", "metrics": {"A": {"leverage": {"status": "NEEDS_RETEST", "reason": "LEVERAGE_MISMATCH"}}}}
    elif blocked == "liquidity":
        kwargs["shared_liquidity"] = {"status": "FAIL", "reason": "LIQUIDITY_MISSING"}
    exported = export_portfolio(result, tmp_path / blocked, **kwargs)
    assert _evaluation_count(store) == before
    assert exported.evaluation_id == result.evaluation_id


@pytest.mark.parametrize(
    ("metric_key", "reason"),
    [("leverage", "CUSTOM_RETEST_REASON"), ("risk", "RISK_METRIC_RETEST")],
)
def test_any_execution_metric_retest_blocks_refresh(tmp_path: Path, metric_key: str, reason: str):
    store, result = _committed_result(tmp_path)
    before = _evaluation_count(store)
    exported = export_portfolio(
        result,
        tmp_path / metric_key,
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        shared_liquidity=True,
        tested_settings={"BTCUSDT": {"leverage": "3"}},
        exported_settings={"BTCUSDT": {"leverage": "3"}},
        execution_facts={"status": "COMMITTED", "metrics": {"A": {metric_key: {"status": "NEEDS_RETEST", "reason": reason}}}},
    )
    assert exported.status == "NEEDS_RETEST"
    assert reason in exported.reasons
    assert exported.evaluation_id == result.evaluation_id
    assert _evaluation_count(store) == before


def test_new_evaluation_persists_computed_status_and_reasons(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        shared_liquidity=True,
        tested_settings={"BTCUSDT": {"leverage": "3"}},
        exported_settings={"BTCUSDT": {"leverage": "3"}},
    )
    row = store.get_evaluation(exported.evaluation_id)
    payload = json.loads(row["payload"])
    assert payload["status"] == exported.status
    assert tuple(payload["reasons"]) == exported.reasons


def test_repeated_symbol_accounts_merge_deterministically(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    accounts = {
        "account-b": {"symbol": "BTCUSDT", "deposit": "200", "margin": {"available": "150"}},
        "account-a": {"symbol": "BTCUSDT", "deposit": "100", "margin": {"available": "75"}},
    }
    result = replace(result, accounts=accounts)
    common = {
        "store": store,
        "reference": {"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        "now_ms": 2,
        "shared_liquidity": True,
        "tested_settings": {"BTCUSDT": {"leverage": "3"}},
        "exported_settings": {"BTCUSDT": {"leverage": "3"}},
    }
    first = export_portfolio(result, tmp_path / "first", **common)
    reversed_result = replace(result, accounts=dict(reversed(tuple(accounts.items()))))
    second = export_portfolio(reversed_result, tmp_path / "second", **common)
    strategy = json.loads((tmp_path / "first" / "strategies" / "BTCUSDT.json").read_text())
    assert [item["account_id"] for item in strategy["accounts"]] == ["account-a", "account-b"]
    assert first.manifest_digest == second.manifest_digest


def test_reference_freshness_requires_injected_current_clock(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 9, "expires_at_ms": 11, "quality": "PASS"}},
        shared_liquidity=True,
        tested_settings={"BTCUSDT": {"leverage": "3"}},
        exported_settings={"BTCUSDT": {"leverage": "3"}},
    )
    assert "REFERENCE_CLOCK_UNAVAILABLE" in exported.reasons
    assert exported.evaluation_id == result.evaluation_id


def test_both_settings_absent_are_unverified(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        shared_liquidity=True,
    )
    assert exported.status == "NEEDS_RETEST"
    assert "EXECUTABLE_PAYLOAD_UNVERIFIED" in exported.reasons
    assert exported.evaluation_id == result.evaluation_id
    assert exported.manifest["gates"]["settings"]["status"] == exported.status


def test_store_readback_status_is_mapped_and_unpublished_is_missing(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    incomplete_store = PortfolioStore(tmp_path / "incomplete.duckdb")
    incomplete_result = run(campaign(budget=2, grid=("10",)), [candidate()], store=incomplete_store)
    incomplete_store.publish_portfolio_run(
        incomplete_result.run_id,
        (normalize_report(fixture_report(run_id=incomplete_result.run_id, attempt_id="attempt", member="A", equity=False)),),
        executable_identity={"binary": "fixture"},
    )
    kwargs = {
        "reference": {"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        "now_ms": 2,
        "shared_liquidity": True,
        "tested_settings": {"BTCUSDT": {"leverage": "3"}},
        "exported_settings": {"BTCUSDT": {"leverage": "3"}},
    }
    incomplete = export_portfolio(incomplete_result, tmp_path / "incomplete-out", store=incomplete_store, **kwargs)
    assert "EXECUTION_EVIDENCE_INCOMPLETE" in incomplete.reasons
    unpublished = export_portfolio(result, tmp_path / "unpublished", store=PortfolioStore(tmp_path / "unpublished.duckdb"), **kwargs)
    assert "EXECUTION_EVIDENCE_MISSING" in unpublished.reasons
    assert unpublished.manifest["gates"]["execution"]["status"] == "UNKNOWN"


def test_export_pins_metrics_to_explicit_attempt_id(tmp_path: Path):
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    result = run(campaign(budget=2, grid=("10",)), [candidate()], store=store)
    first_report = normalize_report(fixture_report(run_id=result.run_id, attempt_id="first", member="A"))
    second_report = normalize_report(fixture_report(run_id=result.run_id, attempt_id="second", member="A", actions=[
        {"timestamp": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT", "action": "OPEN", "side": "LONG", "size": "2"},
        {"timestamp": "2026-01-01T00:00:01Z", "symbol": "BTCUSDT", "action": "CLOSE", "side": "LONG", "size": "2", "pnl": "8"},
    ]))
    store.publish_portfolio_run(result.run_id, (first_report,), attempt_id="first", executable_identity={"binary": "fixture"})
    store.publish_portfolio_run(result.run_id, (second_report,), attempt_id="second", executable_identity={"binary": "fixture"})
    kwargs = {
        "store": store,
        "reference": {"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        "now_ms": 2,
        "shared_liquidity": True,
        "tested_settings": {"BTCUSDT": {"leverage": "3"}},
        "exported_settings": {"BTCUSDT": {"leverage": "3"}},
    }
    first = export_portfolio(result, tmp_path / "first", attempt_id="first", **kwargs)
    second = export_portfolio(result, tmp_path / "second", attempt_id="second", **kwargs)
    assert first.manifest["attempt_id"] == "first"
    assert second.manifest["attempt_id"] == "second"
    assert first.manifest["metrics"]["A"]["realized_pnl"] == "4"
    assert second.manifest["metrics"]["A"]["realized_pnl"] == "8"
    assert first.manifest_digest != second.manifest_digest
    first_again = export_portfolio(result, tmp_path / "first-again", attempt_id="first", **kwargs)
    assert first_again.manifest["metrics"]["A"]["realized_pnl"] == "4"


def test_zero_budget_index_is_preserved_in_manifest_fallback(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    attempts = list(result.attempts)
    validation = next(index for index, attempt in enumerate(attempts) if attempt.phase == "validation")
    attempts[validation] = replace(attempts[validation], budget_index=0)
    result = replace(result, attempts=tuple(attempts))
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
    )
    assert exported.manifest["attempt_id"] == "attempt"


def test_nested_secrets_and_live_commands_are_absent_from_all_artifacts(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    result = replace(result, accounts={"account-a": {"apiToken": "SECRET", "live_mode": {"execute": "BUY"}, "deposit": "100", "cap": "90"}})
    export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS", "credentials": {"token": "SECRET"}, "live_order_command": "BUY"}},
        now_ms=2,
        shared_liquidity={"status": "PASS", "callback_command": "RUN", "nested": {"api_key": "SECRET"}},
        tested_settings={"BTCUSDT": {"leverage": "3", "live_execution": {"command": "BUY", "secret": "SECRET"}}},
        exported_settings={"BTCUSDT": {"leverage": "3", "live_execution": {"command": "BUY", "secret": "SECRET"}}},
    )
    for path in (tmp_path / "out").rglob("*"):
        if path.is_file():
            content = path.read_text()
            assert "SECRET" not in content
            assert "BUY" not in content
            assert "RUN" not in content


def test_scalar_and_list_secret_commands_are_redacted_without_harming_research_text():
    payload = canonical_export_json(
        {
            "ordinary": "LONG SHORT BUY SELL status text",
            "hostile_value": "api_key=top-secret",
            "hostile_list": ["command=BUY", "--live", "SECRET"],
        }
    )
    assert "LONG" in payload and "SHORT" in payload and "BUY" in payload and "SELL" in payload
    assert "status text" in payload
    assert "top-secret" not in payload
    assert "command=BUY" not in payload
    assert "--live" not in payload
    assert "SECRET" not in payload


def test_report_required_fields_have_values(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    result = replace(result, accounts={"account-a": {"deposit": "100", "cap": "90"}})
    export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        shared_liquidity=True,
        tested_settings={"BTCUSDT": {"leverage": "3"}},
        exported_settings={"BTCUSDT": {"leverage": "3"}},
    )
    report = (tmp_path / "out" / "report.txt").read_text()
    for label in ("Account/scenario deposit", "Cap", "Limiter/priority/opposite mode", "Periods", "Candidate IDs", "Initial/final equity", "Primary result", "Realized PnL", "DD coverage", "Metrics", "Validation disposition", "Gates", "Selection reasons", "Limitations"):
        line = next(item for item in report.splitlines() if item.startswith(label + ":"))
        assert line.split(":", 1)[1].strip() not in {"", "null", "None", '"UNAVAILABLE"'}
    limiter_line = next(item for item in report.splitlines() if item.startswith("Limiter/priority/opposite mode:"))
    assert json.loads(limiter_line.split(":", 1)[1]) == {
        "limiter": 0,
        "opposite_policy": "KEEP_OPPOSITE",
        "priorities": 1,
    }


def test_injected_committed_facts_without_durable_run_do_not_refresh_evaluation(tmp_path: Path):
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    result = run(campaign(budget=2, grid=("10",)), [candidate()], store=store)
    before = _evaluation_count(store)
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        shared_liquidity=True,
        tested_settings={"BTCUSDT": {"leverage": "3"}},
        exported_settings={"BTCUSDT": {"leverage": "3"}},
        execution_facts={"status": "COMMITTED", "metrics": {}},
    )
    assert _evaluation_count(store) == before
    assert exported.evaluation_id == result.evaluation_id
    assert exported.manifest["gates"]["execution"]["source"] == "injected_facts"
    assert "EXECUTION_EVIDENCE_MISSING" in exported.reasons


def test_callback_internal_type_error_is_not_retried():
    calls = 0

    def callback(value):
        nonlocal calls
        calls += 1
        raise TypeError("callback failure")

    with pytest.raises(TypeError, match="callback failure"):
        _invoke(callback, "one", "two")
    assert calls == 1


def test_replay_tick_availability_comes_from_persisted_evidence(tmp_path: Path):
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    result = run(campaign(budget=2, grid=("10",)), [candidate()], store=store)
    tick_file = tmp_path / "ticks.bin"
    tick_file.write_bytes(b"fixture")
    report = normalize_report(fixture_report(run_id=result.run_id, attempt_id="attempt", member="A"))
    store.publish_portfolio_run(
        result.run_id,
        (report,),
        executable_identity={"binary_identity": "binary-1", "tick_identity": "ticks-1", "artifacts": [str(tick_file)]},
    )
    replayed = replay_decision(store, result.evaluation_id)
    assert replayed["exact_tick_replay"] == {"available": True, "reason": None}


def test_repeated_selected_variants_keep_order_independent_slots(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    variants = [attempt.variant for attempt in result.attempts if attempt.variant is not None]
    kwargs = {
        "store": store,
        "reference": {"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        "now_ms": 2,
        "shared_liquidity": True,
        "tested_settings": {"BTCUSDT": {"leverage": "3"}},
        "exported_settings": {"BTCUSDT": {"leverage": "3"}},
    }
    first = export_portfolio(result, tmp_path / "first", selected_variants=variants, **kwargs)
    second = export_portfolio(result, tmp_path / "second", selected_variants=tuple(reversed(variants)), **kwargs)
    strategy = json.loads((tmp_path / "first" / "strategies" / "BTCUSDT.json").read_text())
    assert strategy["slot_count"] == len(variants)
    assert len(strategy["slots"]) == len(variants)
    assert all("candidate_ids" in slot and "result_ids" in slot for slot in strategy["slots"])
    assert first.manifest_digest == second.manifest_digest


def test_omitted_attempt_id_uses_durable_readback_attempt_in_manifest_and_gate(tmp_path: Path):
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    result = run(campaign(budget=2, grid=("10",)), [candidate()], store=store)
    for attempt in ("first", "second"):
        store.publish_portfolio_run(
            result.run_id,
            (normalize_report(fixture_report(run_id=result.run_id, attempt_id=attempt, member="A")),),
            attempt_id=attempt,
            executable_identity={"binary": "fixture"},
        )
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        shared_liquidity=True,
        tested_settings={"BTCUSDT": {"leverage": "3"}},
        exported_settings={"BTCUSDT": {"leverage": "3"}},
    )
    assert exported.manifest["attempt_id"] == "second"
    assert exported.manifest["gates"]["execution"]["attempt_id"] == "second"


def test_secret_only_settings_difference_requires_retest_but_is_redacted(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        shared_liquidity=True,
        tested_settings={"BTCUSDT": {"leverage": "3", "token": "tested-secret"}},
        exported_settings={"BTCUSDT": {"leverage": "3", "token": "exported-secret"}},
    )
    assert exported.status == "NEEDS_RETEST"
    assert "EXECUTABLE_PAYLOAD_CHANGED" in exported.reasons
    assert "secret" not in (tmp_path / "out" / "strategies" / "BTCUSDT.json").read_text().casefold()


def test_missing_shared_liquidity_blocks_evaluation_refresh(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    before = _evaluation_count(store)
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        tested_settings={"BTCUSDT": {"leverage": "3"}},
        exported_settings={"BTCUSDT": {"leverage": "3"}},
    )
    assert _evaluation_count(store) == before
    assert exported.evaluation_id == result.evaluation_id
    assert exported.manifest["gates"]["shared_liquidity"]["status"] == "UNKNOWN"
    assert exported.status == "RESEARCH_ONLY"


def test_write_failure_leaves_target_and_evaluation_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store, result = _committed_result(tmp_path)
    before = _evaluation_count(store)
    import mrs3.portfolio.export as export_module
    original_write = export_module._write

    def fail(path, value, root):
        if path.name == "portfolio.json":
            raise OSError("forced export write failure")
        return original_write(path, value, root)

    monkeypatch.setattr(export_module, "_write", fail)
    with pytest.raises(OSError, match="forced export write failure"):
        export_portfolio(
            result,
            tmp_path / "out",
            store=store,
            reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
            now_ms=2,
            shared_liquidity=True,
            tested_settings={"BTCUSDT": {"leverage": "3"}},
            exported_settings={"BTCUSDT": {"leverage": "3"}},
        )
    assert not (tmp_path / "out").exists()
    assert _evaluation_count(store) == before

    monkeypatch.undo()
    target = tmp_path / "swap"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    before_swap = _evaluation_count(store), _portfolio_set_count(store)
    original_replace = Path.replace

    def fail_swap(source: Path, destination: Path):
        if source.name.startswith(".swap.stage-") and destination == target.resolve():
            raise OSError("forced replacement failure")
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", fail_swap)
    with pytest.raises(OSError, match="forced replacement failure"):
        export_portfolio(
            result,
            target,
            store=store,
            reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
            now_ms=2,
            shared_liquidity=True,
            tested_settings={"BTCUSDT": {"leverage": "3"}},
            exported_settings={"BTCUSDT": {"leverage": "3"}},
        )
    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".swap.backup-*"))
    assert (_evaluation_count(store), _portfolio_set_count(store)) == before_swap


def test_hostile_symbol_is_rejected_before_output_writes(tmp_path: Path):
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    result = run(campaign(budget=2, grid=("10",)), [candidate(symbol="../evil")], store=store)

    with pytest.raises(ExportError, match="unsafe"):
        export_portfolio(result, tmp_path / "out", store=store)

    assert not (tmp_path / "out").exists()


def test_casefold_colliding_symbols_are_rejected_before_output_writes(tmp_path: Path):
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    result = run(campaign(budget=2, grid=("10",)), [candidate(symbol="BTCUSDT"), candidate(symbol="btcusdt")], store=store)
    selected = [attempt.variant for attempt in result.attempts if attempt.variant is not None]

    with pytest.raises(ExportError, match="case-insensitively"):
        export_portfolio(result, tmp_path / "out", store=store, selected_variants=selected)

    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".out.stage-*"))


def test_unknown_portfolio_set_probe_never_deletes_preexisting_children(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store, result = _committed_result(tmp_path)
    selected_composition = {"members": [{"portfolio_id": "member-1", "load": "1"}]}
    set_digest = composition_digest(selected_composition)
    set_id = f"portfolio-set-{set_digest[:32]}"
    store.create_portfolio_set(set_id, set_digest, {"composition": selected_composition}, ("member-1",))
    store.insert_portfolio_set_series(set_id, [("2026-01-01T00:00:00Z", "100")], "equity")

    class ProbeFailureStore(type(store)):
        def get_portfolio_set(self, *_args, **_kwargs):
            raise OSError("forced existence probe failure")

    probing_store = ProbeFailureStore(store.path)
    assert _store_row_exists(probing_store, "get_portfolio_set", set_id, table="portfolio_sets", key="portfolio_set_id") is None
    original_create = probing_store.create_portfolio_set

    def fail_create(*args, **kwargs):
        original_create(*args, **kwargs)
        raise RuntimeError("forced PortfolioSet failure")

    monkeypatch.setattr(probing_store, "create_portfolio_set", fail_create)
    with pytest.raises(ExportError, match="PortfolioSet"):
        export_portfolio(
            result,
            tmp_path / "out",
            store=probing_store,
            reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
            now_ms=2,
            shared_liquidity=True,
            tested_settings={"BTCUSDT": {"leverage": "3"}},
            exported_settings={"BTCUSDT": {"leverage": "3"}},
            portfolio_set=selected_composition,
        )

    with duckdb.connect(str(store.path), read_only=True) as db:
        assert db.execute("SELECT member_ordinal, member_id FROM portfolio_set_members WHERE portfolio_set_id = ?", [set_id]).fetchall() == [(0, "member-1")]
        assert db.execute("SELECT series_name, timestamp_utc, source_ordinal, numeric_value FROM portfolio_set_series WHERE portfolio_set_id = ?", [set_id]).fetchall() == [("equity", "2026-01-01T00:00:00Z", 0,  Decimal("100.000000000000"))]


def test_portfolio_set_failure_rolls_back_refreshed_evaluation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store, result = _committed_result(tmp_path)
    before = _evaluation_count(store), _portfolio_set_count(store)
    original_create = store.create_portfolio_set
    def fail(*args, **kwargs):
        original_create(*args, **kwargs)
        raise RuntimeError("forced PortfolioSet failure")
    monkeypatch.setattr(store, "create_portfolio_set", fail)
    with pytest.raises(ExportError, match="PortfolioSet"):
        export_portfolio(
            result,
            tmp_path / "out",
            store=store,
            reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
            now_ms=2,
            shared_liquidity=True,
            tested_settings={"BTCUSDT": {"leverage": "3"}},
            exported_settings={"BTCUSDT": {"leverage": "3"}},
        )
    assert not (tmp_path / "out").exists()
    assert (_evaluation_count(store), _portfolio_set_count(store)) == before


def test_refresh_export_is_idempotent_for_evaluation_rows_and_id(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    kwargs = {
        "store": store,
        "reference": {"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        "now_ms": 2,
        "shared_liquidity": True,
        "tested_settings": {"BTCUSDT": {"leverage": "3"}},
        "exported_settings": {"BTCUSDT": {"leverage": "3"}},
    }
    first = export_portfolio(result, tmp_path / "out", **kwargs)
    count = _evaluation_count(store)
    second = export_portfolio(result, tmp_path / "out", **kwargs)
    assert second.evaluation_id == first.evaluation_id
    assert _evaluation_count(store) == count
    assert not list(tmp_path.glob(".out.backup-*"))


def test_composition_change_is_explicit_needs_rescreen_and_keeps_run(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        shared_liquidity=True,
        tested_settings={"BTCUSDT": {"leverage": "3"}},
        exported_settings={"BTCUSDT": {"leverage": "3"}},
        portfolio_set={"members": [{"portfolio_id": "one", "load": "2"}]},
        previous_composition={"members": [{"portfolio_id": "one", "load": "1"}]},
    )
    assert exported.status == "NEEDS_RESCREEN"
    assert exported.manifest["run_id"] == result.run_id
    assert exported.manifest["execution_campaign_id"] == result.execution_campaign_id
    assert exported.manifest["decision_campaign_id"] != result.execution_campaign_id
    assert exported.evaluation_id != result.evaluation_id


def test_symbol_less_accounts_remain_in_portfolio_scope_for_multi_symbol_export(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    other = run(campaign(budget=2, grid=("10",)), [candidate(symbol="ETHUSDT")])
    selected = [
        next(attempt.variant for attempt in result.attempts if attempt.variant is not None),
        next(attempt.variant for attempt in other.attempts if attempt.variant is not None),
    ]
    result = replace(
        result,
        accounts={"portfolio": {"deposit": "100"}, "btc": {"symbol": "BTCUSDT", "deposit": "100"}, "eth": {"symbol": "ETHUSDT", "deposit": "100"}},
    )
    export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        selected_variants=selected,
        reference={
            "BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"},
            "ETHUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"},
        },
        now_ms=2,
        shared_liquidity=True,
        tested_settings={"BTCUSDT": {"leverage": "3"}, "ETHUSDT": {"leverage": "3"}},
        exported_settings={"BTCUSDT": {"leverage": "3"}, "ETHUSDT": {"leverage": "3"}},
    )
    manifest = json.loads((tmp_path / "out" / "portfolio.json").read_text())
    assert "portfolio" in manifest["scenario"]["accounts"]
    for symbol in ("BTCUSDT", "ETHUSDT"):
        strategy = json.loads((tmp_path / "out" / "strategies" / f"{symbol}.json").read_text())
        assert all(account["account_id"] != "portfolio" for account in strategy.get("accounts", ()))


def test_symbol_keyed_settings_do_not_leak_into_missing_symbol_artifact(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    other = run(campaign(budget=2, grid=("10",)), [candidate(symbol="ETHUSDT")])
    selected = [
        next(attempt.variant for attempt in result.attempts if attempt.variant is not None),
        next(attempt.variant for attempt in other.attempts if attempt.variant is not None),
    ]
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        selected_variants=selected,
        reference={
            "BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"},
            "ETHUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"},
        },
        now_ms=2,
        shared_liquidity=True,
        tested_settings={"BTCUSDT": {"leverage": "3"}},
        exported_settings={"BTCUSDT": {"leverage": "3"}},
    )
    btc = json.loads((tmp_path / "out" / "strategies" / "BTCUSDT.json").read_text())
    eth = json.loads((tmp_path / "out" / "strategies" / "ETHUSDT.json").read_text())
    assert btc["settings"] == {"leverage": "3"}
    assert "settings" not in eth
    assert exported.status == "NEEDS_RETEST"
    assert "EXECUTABLE_PAYLOAD_UNVERIFIED" in exported.reasons


def test_global_tested_settings_remain_global_when_exported_shape_is_symbol_keyed(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    other = run(campaign(budget=2, grid=("10",)), [candidate(symbol="ETHUSDT")])
    selected = [
        next(attempt.variant for attempt in result.attempts if attempt.variant is not None),
        next(attempt.variant for attempt in other.attempts if attempt.variant is not None),
    ]
    export_portfolio(
        result,
        tmp_path / "out",
        store=store,
        selected_variants=selected,
        reference={
            "BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"},
            "ETHUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"},
        },
        now_ms=2,
        shared_liquidity=True,
        tested_settings={"leverage": "3"},
        exported_settings={"BTCUSDT": {"leverage": "3"}},
    )
    for symbol in ("BTCUSDT", "ETHUSDT"):
        strategy = json.loads((tmp_path / "out" / "strategies" / f"{symbol}.json").read_text())
        assert strategy["settings"] == {"leverage": "3"}


def test_unreadable_durable_evidence_has_distinct_reason(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    class UnreadableStore(type(store)):
        def read_portfolio_run(self, *args, **kwargs):
            raise RuntimeError("forced durable readback failure")
    exported = export_portfolio(
        result,
        tmp_path / "out",
        store=UnreadableStore(store.path),
        reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
        now_ms=2,
        shared_liquidity=True,
        tested_settings={"BTCUSDT": {"leverage": "3"}},
        exported_settings={"BTCUSDT": {"leverage": "3"}},
    )
    assert "EXECUTION_EVIDENCE_UNREADABLE" in exported.reasons
    assert exported.manifest["gates"]["execution"]["reason"] == "EXECUTION_EVIDENCE_UNREADABLE"


def test_campaign_lookup_failure_is_wrapped(tmp_path: Path):
    store, result = _committed_result(tmp_path)
    class BrokenCampaignStore(type(store)):
        def get_campaign(self, *args, **kwargs):
            raise RuntimeError("forced campaign read failure")
    with pytest.raises(ExportError, match="campaign lookup failed"):
        export_portfolio(
            result,
            tmp_path / "out",
            store=BrokenCampaignStore(store.path),
            reference={"BTCUSDT": {"captured_at_ms": 1, "expires_at_ms": 10, "quality": "PASS"}},
            now_ms=2,
            shared_liquidity=True,
            tested_settings={"BTCUSDT": {"leverage": "3"}},
            exported_settings={"BTCUSDT": {"leverage": "3"}},
        )

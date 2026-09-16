import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import inspect
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
import mrs3.portfolio.adapter as adapter_module
import mrs3.portfolio.input as portfolio_input
from mrs3.portfolio import candidate_search, market_snapshot, minute_capacity, position_sizing, spread_screen
from mrs3.portfolio.config import RESEARCH_RISK_POLICY
from mrs3.portfolio.liquidity import ReferenceReader
from mrs3.portfolio.weighted_search import weighted_search as real_weighted_search

weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")

from mrs3.portfolio.adapter import (
    CAMPAIGN_CONTRACT_VERSION,
    CAMPAIGN_SEARCH_MODE,
    CAMPAIGN_WEIGHTED_ALGO_VERSION,
    CampaignContractError,
    MARGIN_BOUND_UNAVAILABLE,
    WEIGHTED_INPUT_PREPARATION_FAILED,
    WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE,
    WEIGHTED_EXECUTABLE_IDENTITY_COLLISION,
    _prepare_frozen_weighted_input,
    build_weighted_strategy_payload,
    build_portfolio_candidates,
    run_portfolio_adapter,
    validate_campaign_contract,
)


def _weighted_search_bridge_campaign():
    return {
        "config_document": {
            "search": {
                "seed": 17,
                "weighted_search": {
                    "max_targets": 4,
                    "bootstrap_scenarios_per_block": 101,
                    "bootstrap_diagnostic_scenarios": 7,
                    "wall_time_seconds": 12,
                    "solver_time_seconds": 3,
                },
            }
        }
    }


def _margin_evidence(*strategy_ids):
    return {
        strategy_id: {
            "a": Decimal("0.10"),
            "b": Decimal("0.01"),
            "evidence_class": "CALCULATED",
            "max_notional": Decimal("1000"),
        }
        for strategy_id in strategy_ids
    }


def test_run_weighted_search_forwards_profile_settings_and_capacities(monkeypatch):
    calls = []
    prepared = object()
    members = (
        {
            "strategy_id": 11,
            "symbol": "BTCUSDT",
            "position_size_usdt": Decimal("123"),
        },
    )
    profile = {
        "profile_id": "BALANCED",
        "scenario_id": "SCENARIO-1",
        "equity_usdt": Decimal("1000"),
        "max_candidates": 9,
    }
    margin = _margin_evidence(11)

    def fake_weighted_search(*args, **kwargs):
        calls.append((args, kwargs))
        return "search-result"

    monkeypatch.setattr(adapter_module, "weighted_search", fake_weighted_search)

    result = adapter_module._run_weighted_search(
        prepared,
        members,
        _weighted_search_bridge_campaign(),
        profile,
        margin,
        workers=3,
    )

    assert result == "search-result"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert len(args) == 2
    assert args[0] is prepared
    assert args[1] == {11: Decimal("123")}
    assert kwargs["members"] is members
    assert kwargs == {
        "members": members,
        "bank_available": Decimal("1000"),
        "profile_id": "BALANCED",
        "scenario_id": "SCENARIO-1",
        "margin_coefficients": margin,
        "max_candidates": 9,
        "max_dd": Decimal("0.10"),
        "margin_kwargs": {
            "reserve": Decimal("0.40"),
            "max_mm_load": Decimal("0.35"),
        },
        "max_targets": 4,
        "seed": 17,
        "bootstrap_scenarios": 101,
        "screening_scenarios": 7,
        "workers": 3,
        "wall_time": 12,
        "solver_time": 3,
    }


def test_run_weighted_search_requires_margin_before_calling_search(monkeypatch):
    calls = []
    monkeypatch.setattr(adapter_module, "weighted_search", lambda *args, **kwargs: calls.append(True))

    with pytest.raises(CampaignContractError) as error:
        adapter_module._run_weighted_search(
            object(),
            ({"strategy_id": 11, "position_size_usdt": Decimal("123")},),
            _weighted_search_bridge_campaign(),
            {"profile_id": "BALANCED", "scenario_id": "SCENARIO-1", "equity_usdt": Decimal("1000")},
            None,
            workers=1,
        )

    assert error.value.code == MARGIN_BOUND_UNAVAILABLE
    assert calls == []


@pytest.mark.parametrize(
    "margin, members",
    (
        ({}, ({"strategy_id": 11, "position_size_usdt": Decimal("123")},)),
        ((), ({"strategy_id": 11, "position_size_usdt": Decimal("123")},)),
        ({11: None}, ({"strategy_id": 11, "position_size_usdt": Decimal("123")},)),
        ({11: "not-a-coefficient"}, ({"strategy_id": 11, "position_size_usdt": Decimal("123")},)),
        ({11: Decimal("NaN")}, ({"strategy_id": 11, "position_size_usdt": Decimal("123")},)),
        ({11: {}}, ({"strategy_id": 11, "position_size_usdt": Decimal("123")},)),
        (_margin_evidence(11), (
            {"strategy_id": 11, "position_size_usdt": Decimal("123")},
            {"strategy_id": 22, "position_size_usdt": Decimal("123")},
        )),
    ),
)
def test_run_weighted_search_requires_covered_margin_evidence(margin, members, monkeypatch):
    calls = []
    monkeypatch.setattr(adapter_module, "weighted_search", lambda *args, **kwargs: calls.append(True))

    with pytest.raises(CampaignContractError) as error:
        adapter_module._run_weighted_search(
            object(), members, _weighted_search_bridge_campaign(),
            {"profile_id": "BALANCED", "scenario_id": "SCENARIO-1", "equity_usdt": Decimal("1000")},
            margin, workers=1,
        )

    assert error.value.code == MARGIN_BOUND_UNAVAILABLE
    assert calls == []


def test_run_weighted_search_propagates_search_error(monkeypatch):
    def fail_search(*args, **kwargs):
        raise ValueError("search exploded")

    monkeypatch.setattr(adapter_module, "weighted_search", fail_search)
    with pytest.raises(ValueError, match="search exploded"):
        adapter_module._run_weighted_search(
            object(), ({"strategy_id": 11, "position_size_usdt": Decimal("123")},),
            _weighted_search_bridge_campaign(),
            {"profile_id": "BALANCED", "scenario_id": "SCENARIO-1", "equity_usdt": Decimal("1000")},
            _margin_evidence(11), workers=1,
        )


def test_run_weighted_search_kwargs_bind_real_signature_with_and_without_max_candidates(monkeypatch):
    calls = []
    monkeypatch.setattr(adapter_module, "weighted_search", lambda *args, **kwargs: calls.append((args, kwargs)) or "ok")
    for profile in (
        {"profile_id": "BALANCED", "scenario_id": "SCENARIO-1", "equity_usdt": Decimal("1000"), "max_candidates": 9},
        {"profile_id": "BALANCED", "scenario_id": "SCENARIO-1", "equity_usdt": Decimal("1000")},
    ):
        prepared = object()
        result = adapter_module._run_weighted_search(
            prepared, ({"strategy_id": 11, "position_size_usdt": Decimal("123")},),
            _weighted_search_bridge_campaign(), profile, _margin_evidence(11), workers=1,
        )
        assert result == "ok"
        args, kwargs = calls[-1]
        inspect.signature(real_weighted_search).bind(args[0], args[1], **kwargs)


def test_run_weighted_search_maps_missing_margin_validator_to_margin_blocker(monkeypatch):
    calls = []
    monkeypatch.setattr(adapter_module, "weighted_search", lambda *args, **kwargs: calls.append(True))
    monkeypatch.delattr(weighted_search_module, "_public_margin_coefficients_are_known")

    with pytest.raises(CampaignContractError) as error:
        adapter_module._run_weighted_search(
            object(), ({"strategy_id": 11, "position_size_usdt": Decimal("123")},),
            _weighted_search_bridge_campaign(),
            {"profile_id": "BALANCED", "scenario_id": "SCENARIO-1", "equity_usdt": Decimal("1000")},
            _margin_evidence(11), workers=1,
        )

    assert error.value.code == MARGIN_BOUND_UNAVAILABLE
    assert calls == []


@pytest.mark.parametrize("equity", (None, Decimal("-1")))
def test_run_weighted_search_rejects_invalid_equity(equity, monkeypatch):
    calls = []
    monkeypatch.setattr(adapter_module, "weighted_search", lambda *args, **kwargs: calls.append(True))
    with pytest.raises(CampaignContractError) as error:
        adapter_module._run_weighted_search(
            object(), ({"strategy_id": 11, "position_size_usdt": Decimal("123")},),
            _weighted_search_bridge_campaign(),
            {"profile_id": "BALANCED", "scenario_id": "SCENARIO-1", "equity_usdt": equity},
            _margin_evidence(11), workers=1,
        )
    assert error.value.code == "WEIGHTED_SEARCH_CONFIG_INVALID"
    assert calls == []


def test_run_weighted_search_rejects_non_decimal_position_size(monkeypatch):
    calls = []
    monkeypatch.setattr(adapter_module, "weighted_search", lambda *args, **kwargs: calls.append(True))
    with pytest.raises(CampaignContractError) as error:
        adapter_module._run_weighted_search(
            object(), ({"strategy_id": 11, "position_size_usdt": "123"},),
            _weighted_search_bridge_campaign(),
            {"profile_id": "BALANCED", "scenario_id": "SCENARIO-1", "equity_usdt": Decimal("1000")},
            _margin_evidence(11), workers=1,
        )
    assert error.value.code == "WEIGHTED_SEARCH_CONFIG_INVALID"
    assert calls == []


def test_run_weighted_search_rejects_missing_prepared_input(monkeypatch):
    calls = []
    monkeypatch.setattr(adapter_module, "weighted_search", lambda *args, **kwargs: calls.append(True))
    with pytest.raises(CampaignContractError) as error:
        adapter_module._run_weighted_search(
            None, ({"strategy_id": 11, "position_size_usdt": Decimal("123")},),
            _weighted_search_bridge_campaign(),
            {"profile_id": "BALANCED", "scenario_id": "SCENARIO-1", "equity_usdt": Decimal("1000")},
            _margin_evidence(11), workers=1,
        )
    assert error.value.code == WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE
    assert calls == []


@pytest.mark.parametrize("profile_id", tuple(RESEARCH_RISK_POLICY))
def test_run_weighted_search_forwards_each_research_policy_fraction(profile_id, monkeypatch):
    calls = []
    monkeypatch.setattr(adapter_module, "weighted_search", lambda *args, **kwargs: calls.append((args, kwargs)) or "ok")
    prepared = object()
    members = ({"strategy_id": 11, "position_size_usdt": Decimal("123")},)
    result = adapter_module._run_weighted_search(
        prepared, members,
        _weighted_search_bridge_campaign(),
        {"profile_id": profile_id, "scenario_id": "SCENARIO-1", "equity_usdt": Decimal("1000")},
        _margin_evidence(11), workers=1,
    )
    assert result == "ok"
    policy = RESEARCH_RISK_POLICY[profile_id]
    assert all(isinstance(policy[key], Decimal) for key in policy)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] is prepared
    assert args[1] == {11: Decimal("123")}
    assert kwargs["members"] is members
    assert kwargs["max_dd"] == policy["max_actual_equity_dd_pct"] / Decimal("100")
    assert kwargs["margin_kwargs"] == {
        "reserve": policy["min_calculated_free_margin_reserve_pct"] / Decimal("100"),
        "max_mm_load": policy["max_calculated_account_mm_load_pct"] / Decimal("100"),
    }


@pytest.mark.parametrize("case", (
    "duplicate_id", "strategy_id_bool", "strategy_id_string", "strategy_id_zero", "strategy_id_negative", "unknown_profile", "non_string_profile", "unhashable_profile", "empty_profile",
    "missing_scenario", "empty_scenario", "non_string_scenario", "max_candidates_low", "max_candidates_high",
    "max_candidates_bool", "workers_zero", "workers_bool", "empty_members", "nonsequence_members",
    "nonmapping_member", "diagnostic_gt_bootstrap", "seed_bool", "seed_negative",
))
def test_run_weighted_search_rejects_malformed_inputs(case, monkeypatch):
    calls = []
    monkeypatch.setattr(adapter_module, "weighted_search", lambda *args, **kwargs: calls.append(True))
    campaign = _weighted_search_bridge_campaign()
    profile = {"profile_id": "BALANCED", "scenario_id": "SCENARIO-1", "equity_usdt": Decimal("1000"), "max_candidates": 9}
    members = ({"strategy_id": 11, "position_size_usdt": Decimal("123")},)
    workers = 1
    margin = _margin_evidence(11)
    if case == "duplicate_id":
        members = members + members
        margin = _margin_evidence(11)
    elif case == "strategy_id_bool":
        members = (dict(members[0], strategy_id=True),)
    elif case == "strategy_id_string":
        members = (dict(members[0], strategy_id="11"),)
    elif case == "strategy_id_zero":
        members = (dict(members[0], strategy_id=0),)
    elif case == "strategy_id_negative":
        members = (dict(members[0], strategy_id=-1),)
    elif case == "unknown_profile":
        profile["profile_id"] = "NOPE"
    elif case == "non_string_profile":
        profile["profile_id"] = 1
    elif case == "unhashable_profile":
        profile["profile_id"] = []
    elif case == "empty_profile":
        profile["profile_id"] = ""
    elif case == "missing_scenario":
        del profile["scenario_id"]
    elif case == "empty_scenario":
        profile["scenario_id"] = ""
    elif case == "non_string_scenario":
        profile["scenario_id"] = 1
    elif case == "max_candidates_low":
        profile["max_candidates"] = 0
    elif case == "max_candidates_high":
        profile["max_candidates"] = 51
    elif case == "max_candidates_bool":
        profile["max_candidates"] = True
    elif case == "workers_zero":
        workers = 0
    elif case == "workers_bool":
        workers = True
    elif case == "empty_members":
        members = ()
        margin = {}
    elif case == "nonsequence_members":
        members = "members"
        margin = {}
    elif case == "nonmapping_member":
        members = (None,)
        margin = {}
    elif case == "diagnostic_gt_bootstrap":
        campaign["config_document"]["search"]["weighted_search"]["bootstrap_diagnostic_scenarios"] = 102
    elif case == "seed_bool":
        campaign["config_document"]["search"]["seed"] = True
    elif case == "seed_negative":
        campaign["config_document"]["search"]["seed"] = -1

    with pytest.raises(CampaignContractError) as error:
        adapter_module._run_weighted_search(object(), members, campaign, profile, margin, workers=workers)
    assert error.value.code == "WEIGHTED_SEARCH_CONFIG_INVALID"
    assert calls == []


@pytest.mark.parametrize(
    "campaign",
    (
        {},
        {"config_document": None},
        {"config_document": {"search": None}},
        {"config_document": {"search": {"weighted_search": None}}},
        {"config_document": {"search": {"weighted_search": {"seed": 17}}}},
    ),
)
def test_run_weighted_search_rejects_malformed_frozen_config(campaign, monkeypatch):
    calls = []
    monkeypatch.setattr(adapter_module, "weighted_search", lambda *args, **kwargs: calls.append(True))

    with pytest.raises(CampaignContractError) as error:
        adapter_module._run_weighted_search(
            object(),
            ({"strategy_id": 11, "position_size_usdt": Decimal("123")},),
            campaign,
            {"profile_id": "BALANCED", "scenario_id": "SCENARIO-1", "equity_usdt": Decimal("1000")},
            {11: {"a": Decimal("1"), "b": Decimal("0")}},
            workers=1,
        )

    assert error.value.code == "WEIGHTED_SEARCH_CONFIG_INVALID"
    assert calls == []


def test_run_weighted_search_uses_stable_config_error_code(monkeypatch):
    monkeypatch.setattr(adapter_module, "weighted_search", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("search reached")))
    with pytest.raises(CampaignContractError) as error:
        adapter_module._run_weighted_search(
            object(), ({"strategy_id": 11, "position_size_usdt": Decimal("123")},), {},
            {"profile_id": "BALANCED", "scenario_id": "SCENARIO-1", "equity_usdt": Decimal("1000")},
            _margin_evidence(11), workers=1,
        )
    assert error.value.code == "WEIGHTED_SEARCH_CONFIG_INVALID"


def weighted_campaign():
    return {
        "campaign_contract_version": CAMPAIGN_CONTRACT_VERSION,
        "search_mode": CAMPAIGN_SEARCH_MODE,
        "weighted_algo_version": CAMPAIGN_WEIGHTED_ALGO_VERSION,
        "versions": {
            "campaign_contract_version": CAMPAIGN_CONTRACT_VERSION,
            "search_mode": CAMPAIGN_SEARCH_MODE,
            "weighted_algo_version": CAMPAIGN_WEIGHTED_ALGO_VERSION,
        },
    }


def _weighted_build_campaign():
    campaign = weighted_campaign()
    campaign.update({
        "weighted_input_rows": ({
            "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
            "actions": (), "equity": (),
        },),
        "config_document": {
            "liquidity": {"maximum_age_hours": 2},
            "search": {
                "seed": 17,
                "weighted_search": {
                    "history_step_minutes": 5,
                    "max_targets": 4,
                    "bootstrap_scenarios_per_block": 101,
                    "bootstrap_diagnostic_scenarios": 7,
                    "wall_time_seconds": 12,
                    "solver_time_seconds": 3,
                },
                "composition": {"parameters": {"minimum_common_days": 1}},
            },
        },
        "launch": {"profiles": ({"profile_id": "BALANCED", "equity_usdt": Decimal("1000")},)},
    })
    return campaign


def _runtime_weighted_campaign():
    campaign = weighted_campaign()
    campaign.update({
        "created_at_utc": "2026-09-16T12:00:00Z",
        "weighted_input_rows": ({
            "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
            "actions": (), "equity": (),
        },),
        "strategy_template": {"template": "frozen"},
        "config_document": {
            "inputs": {
                "bybit_minute_data_root": "tester/data/bybit",
                "collector_root": "collector",
            },
            "liquidity": {
                "parameters": {"close_volume_participation_pct": 30},
                "round_down_usdt": Decimal("50"),
                "minimum_coverage_pct": 90,
                "maximum_age_hours": 2,
                "weekend_start_utc": "SATURDAY 00:00",
                "weekend_end_utc": "MONDAY 00:00",
                "archive_publication_lag_hours": 6,
                "backfill_write_enabled": False,
            },
        },
    })
    return campaign


def test_build_adapter_derives_margin_coefficients_from_frozen_reference(monkeypatch):
    campaign = _weighted_build_campaign()
    campaign["config_document"]["margin"] = {
        "policy_id": "margin-policy-v1",
        "parameters": {"open_fee_rate": "0.001", "close_fee_rate": "0.002"},
    }
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    enriched = ({**selected[0], "position_size_usdt": Decimal("200"), "planned_leverage": Decimal("10")},)
    reference = ReferenceReader.from_records(
        instruments=[{"symbol": "BTCUSDT", "status": "Trading", "contract_type": "LinearPerpetual", "tick_size": "0.1", "qty_step": "0.001", "min_qty": "0.001", "max_qty": "100000", "leverage_step": "1", "max_leverage": "50"}],
        risk_tiers=[{"symbol": "BTCUSDT", "risk_limit_value": "500", "max_leverage": "20", "initial_margin": "0.05", "maintenance_margin": "0.025"}],
        captured_at_ms=123,
    )
    seen = []
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(adapter_module, "enrich_finalist_rows", lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=enriched, exclusions=()))
    monkeypatch.setattr(adapter_module, "_run_weighted_search", lambda _prepared, _members, _campaign, _profile, margin, *, workers: seen.append(margin) or SimpleNamespace(status="PASS", mode=CAMPAIGN_SEARCH_MODE, candidates=(_weighted_candidate(members=enriched),), warnings=()))
    result = build_portfolio_candidates(
        selected, campaign, capacities={}, reference=reference, mark_prices={}, spread_observations={},
        spread_history_statuses={"BTCUSDT": "READY"}, now_ms=123,
    )
    assert result.status == "PASS"
    assert seen and seen[0][11].a == Decimal("0.103")


def test_build_adapter_keeps_margin_blocker_without_explicit_reference_policy(monkeypatch):
    campaign = _weighted_build_campaign()
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    enriched = ({**selected[0], "position_size_usdt": Decimal("200"), "planned_leverage": Decimal("10")},)
    reference = ReferenceReader.from_records(
        instruments=[{"symbol": "BTCUSDT", "status": "Trading", "contract_type": "LinearPerpetual", "tick_size": "0.1", "qty_step": "0.001", "min_qty": "0.001", "max_qty": "100000", "leverage_step": "1", "max_leverage": "50"}],
        risk_tiers=[{"symbol": "BTCUSDT", "risk_limit_value": "500", "max_leverage": "20", "initial_margin": "0.05", "maintenance_margin": "0.025"}],
        captured_at_ms=123,
    )
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(adapter_module, "enrich_finalist_rows", lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=enriched, exclusions=()))
    result = build_portfolio_candidates(
        selected, campaign, capacities={}, reference=reference, mark_prices={}, spread_observations={},
        spread_history_statuses={"BTCUSDT": "READY"}, now_ms=123,
    )
    assert result.status == "FAIL" and result.blockers == ("PROFILE:MARGIN_BOUND_UNAVAILABLE",)


def _adapter_margin_failure_case(monkeypatch, reference, members, expected):
    campaign = _weighted_build_campaign()
    campaign["config_document"]["margin"] = {
        "policy_id": "margin-policy-v1",
        "parameters": {"open_fee_rate": "0.001", "close_fee_rate": "0.002"},
    }
    selected = tuple({"symbol": row["symbol"], "side": "LONG", "strategy_id": row["strategy_id"], "result_id": row["result_id"]} for row in members)
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(adapter_module, "enrich_finalist_rows", lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=()))
    result = build_portfolio_candidates(
        selected, campaign, capacities={}, reference=reference, mark_prices={}, spread_observations={},
        spread_history_statuses={row["symbol"]: "READY" for row in members}, now_ms=123,
    )
    assert result.status == "FAIL" and result.blockers == ("PROFILE:" + expected,)


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        ("missing_initial", "MARGIN_TIER_RATE_UNKNOWN"),
        ("overleverage", "MARGIN_LEVERAGE_EXCEEDS_REFERENCE"),
        ("duplicate_id", "MARGIN_STRATEGY_ID_NOT_UNIQUE"),
    ),
)
def test_build_adapter_blocks_reference_margin_failures(monkeypatch, failure, expected):
    reference = ReferenceReader.from_records(
        instruments=[{"symbol": "BTCUSDT", "status": "Trading", "contract_type": "LinearPerpetual", "tick_size": "0.1", "qty_step": "0.001", "min_qty": "0.001", "max_qty": "100000", "leverage_step": "1", "max_leverage": "50"}],
        risk_tiers=[{"symbol": "BTCUSDT", "risk_limit_value": "500", "max_leverage": "20", "maintenance_margin": "0.025", **({} if failure == "missing_initial" else {"initial_margin": "0.05"})}],
        captured_at_ms=123,
    )
    members = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101, "position_size_usdt": Decimal("200"), "planned_leverage": Decimal("21" if failure == "overleverage" else "10")},)
    if failure == "duplicate_id":
        members = members + ({**members[0], "result_id": 102},)
    _adapter_margin_failure_case(monkeypatch, reference, members, expected)


def test_build_adapter_malformed_margin_policy_stays_generic_blocker(monkeypatch):
    campaign = _weighted_build_campaign()
    campaign["config_document"]["margin"] = {"policy_id": "margin-policy-v1", "parameters": {"open_fee_rate": "0.001"}}
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    members = ({**selected[0], "position_size_usdt": Decimal("200"), "planned_leverage": Decimal("10")},)
    reference = ReferenceReader.from_records(
        instruments=[{"symbol": "BTCUSDT", "status": "Trading", "contract_type": "LinearPerpetual", "tick_size": "0.1", "qty_step": "0.001", "min_qty": "0.001", "max_qty": "100000", "leverage_step": "1", "max_leverage": "50"}],
        risk_tiers=[{"symbol": "BTCUSDT", "risk_limit_value": "500", "max_leverage": "20", "initial_margin": "0.05", "maintenance_margin": "0.025"}],
        captured_at_ms=123,
    )
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(adapter_module, "enrich_finalist_rows", lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=()))
    result = build_portfolio_candidates(
        selected, campaign, capacities={}, reference=reference, mark_prices={}, spread_observations={},
        spread_history_statuses={"BTCUSDT": "READY"}, now_ms=123,
    )
    assert result.status == "FAIL" and result.blockers == ("PROFILE:MARGIN_BOUND_UNAVAILABLE",)


def test_build_adapter_preserves_non_usdt_margin_reason(monkeypatch):
    reference = ReferenceReader.from_records(
        instruments=[{"symbol": "BTCUSD", "status": "Trading", "contract_type": "LinearPerpetual", "tick_size": "0.1", "qty_step": "0.001", "min_qty": "0.001", "max_qty": "100000", "leverage_step": "1", "max_leverage": "50"}],
        risk_tiers=[{"symbol": "BTCUSD", "risk_limit_value": "500", "max_leverage": "20", "initial_margin": "0.05", "maintenance_margin": "0.025"}],
        captured_at_ms=123,
    )
    members = ({"symbol": "BTCUSD", "side": "LONG", "strategy_id": 11, "result_id": 101, "position_size_usdt": Decimal("200"), "planned_leverage": Decimal("10")},)
    _adapter_margin_failure_case(monkeypatch, reference, members, "MARGIN_NON_USDT_SYMBOL")


def _weighted_candidate(profile_id="BALANCED", *, members=None, scenario_id=None, identity=None):
    return candidate_search.PortfolioCandidate(
        schema_version="portfolio_candidate_v1",
        profile_id=profile_id,
        scenario_id=scenario_id or profile_id,
        identity=identity or f"candidate-{profile_id}",
        members=tuple(members or ({
            "symbol": "BTCUSDT",
            "side": "LONG",
            "strategy_id": 11,
            "result_id": 101,
            "position_size_usdt": Decimal("123"),
        },)),
        metrics={"limiter_L": Decimal("2")},
    )


@pytest.mark.parametrize(
    ("campaign_value", "code"),
    (
        (None, "CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED"),
        ({"stage1_mode": None}, "CAMPAIGN_LEGACY_STAGE1_MODE_UNSUPPORTED"),
        ({"versions": {"stage1_mode": None}}, "CAMPAIGN_LEGACY_STAGE1_MODE_UNSUPPORTED"),
        ({"campaign_contract_version": None}, "CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED"),
        ({"campaign_contract_version": ""}, "CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED"),
        ({"campaign_contract_version": "other"}, "CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED"),
    ),
)
def test_campaign_contract_validation_short_circuits_early(campaign_value, code):
    with pytest.raises(CampaignContractError) as error:
        validate_campaign_contract(campaign_value)
    assert error.value.code == code


@pytest.mark.parametrize("value", (None, "", 1, True, []))
def test_campaign_contract_requires_search_mode(value):
    request = weighted_campaign()
    request["search_mode"] = value
    with pytest.raises(CampaignContractError) as error:
        validate_campaign_contract(request)
    assert error.value.code == ("CAMPAIGN_SEARCH_MODE_REQUIRED" if not isinstance(value, str) or not value else "CAMPAIGN_SEARCH_MODE_UNSUPPORTED")


def test_campaign_contract_rejects_legacy_and_unknown_search_modes():
    for value, code in (("PRETEST_PROXY", "CAMPAIGN_LEGACY_SEARCH_MODE_UNSUPPORTED"), ("OTHER", "CAMPAIGN_SEARCH_MODE_UNSUPPORTED")):
        request = weighted_campaign()
        request["search_mode"] = value
        with pytest.raises(CampaignContractError) as error:
            validate_campaign_contract(request)
        assert error.value.code == code


@pytest.mark.parametrize("value", (None, "", 1, True, []))
def test_campaign_contract_requires_weighted_algorithm_version(value):
    request = weighted_campaign()
    request["weighted_algo_version"] = value
    with pytest.raises(CampaignContractError) as error:
        validate_campaign_contract(request)
    assert error.value.code == ("CAMPAIGN_WEIGHTED_ALGO_VERSION_REQUIRED" if not isinstance(value, str) or not value else "CAMPAIGN_WEIGHTED_ALGO_VERSION_UNSUPPORTED")


def test_campaign_contract_versions_require_exact_parity_but_ignore_extras():
    request = weighted_campaign()
    request["versions"]["provenance"] = "ignored"
    validate_campaign_contract(request)
    for key in ("campaign_contract_version", "search_mode", "weighted_algo_version"):
        missing = weighted_campaign()
        del missing["versions"][key]
        with pytest.raises(CampaignContractError) as error:
            validate_campaign_contract(missing)
        assert error.value.code == "CAMPAIGN_VERSIONS_MISMATCH"
    for versions in (None, [], {"campaign_contract_version": "wrong", "search_mode": CAMPAIGN_SEARCH_MODE, "weighted_algo_version": CAMPAIGN_WEIGHTED_ALGO_VERSION}):
        invalid = weighted_campaign()
        invalid["versions"] = versions
        with pytest.raises(CampaignContractError) as error:
            validate_campaign_contract(invalid)
        assert error.value.code == "CAMPAIGN_VERSIONS_MISMATCH"

    mismatch = weighted_campaign()
    mismatch["versions"]["search_mode"] = "OTHER"
    with pytest.raises(CampaignContractError) as error:
        validate_campaign_contract(mismatch)
    assert error.value.code == "CAMPAIGN_VERSIONS_MISMATCH"


def test_build_adapter_does_not_publish_partial_profile_variants_when_later_candidate_is_malformed(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    members = (dict(selected[0], position_size_usdt=Decimal("123")),)
    campaign = _weighted_build_campaign()
    valid = _weighted_candidate(members=members)
    malformed = SimpleNamespace(
        profile_id="BALANCED",
        scenario_id="BALANCED",
        schema_version="portfolio_candidate_v1",
        members=members,
        metrics={},
    )

    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module,
        "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module,
        "_run_weighted_search",
        lambda *_args, **_kwargs: candidate_search.SearchResult(
            status="PASS", candidates=(valid, malformed), mode=CAMPAIGN_SEARCH_MODE,
        ),
    )

    result = build_portfolio_candidates(
        selected, campaign, capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert (result.status, result.blockers, result.variants) == (
        "FAIL", ("PROFILE:WEIGHTED_SEARCH_CONFIG_INVALID",), (),
    )


@pytest.mark.parametrize("profile", ({"equity_usdt": Decimal("1000")}, {"profile_id": "", "equity_usdt": Decimal("1000")}, {"profile_id": 1, "equity_usdt": Decimal("1000")}))
def test_build_adapter_validates_profile_id_before_search_call(monkeypatch, profile):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    members = (dict(selected[0], position_size_usdt=Decimal("123")),)
    campaign = _weighted_build_campaign()
    campaign["launch"] = {"profiles": (profile,)}
    calls = []

    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module,
        "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module,
        "_run_weighted_search",
        lambda *_args, **_kwargs: calls.append(True) or candidate_search.SearchResult(status="FAIL", reason="SEARCH_FAIL"),
    )

    result = build_portfolio_candidates(
        selected, campaign, capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert (result.status, result.blockers, calls) == (
        "FAIL", ("PROFILE:WEIGHTED_SEARCH_CONFIG_INVALID",), [],
    )


@pytest.mark.parametrize(
    ("mode", "candidate_profile"),
    (("PRETEST_PROXY", "BALANCED"), (CAMPAIGN_SEARCH_MODE, "AGGRESSIVE")),
)
def test_build_adapter_publishes_only_weighted_candidates_for_requested_profile(monkeypatch, mode, candidate_profile):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    members = (dict(selected[0], position_size_usdt=Decimal("123")),)
    campaign = _weighted_build_campaign()
    candidate = _weighted_candidate(candidate_profile, members=members)

    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module,
        "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module,
        "_run_weighted_search",
        lambda *_args, **_kwargs: candidate_search.SearchResult(
            status="PASS", candidates=(candidate,), mode=mode,
        ),
    )

    result = build_portfolio_candidates(
        selected, campaign, capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert (result.status, result.blockers, result.variants) == (
        "FAIL", ("PROFILE:WEIGHTED_SEARCH_CONFIG_INVALID",), (),
    )


@pytest.mark.parametrize(
    ("candidate_profile", "candidate_scenario"),
    (("BALANCED", "OTHER"), ("AGGRESSIVE", "AGGRESSIVE")),
)
def test_build_adapter_requires_candidate_profile_and_scenario_to_match_request(
    monkeypatch, candidate_profile, candidate_scenario,
):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    members = (dict(selected[0], position_size_usdt=Decimal("123")),)
    candidate = _weighted_candidate(
        candidate_profile, members=members, scenario_id=candidate_scenario,
    )

    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module,
        "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module,
        "_run_weighted_search",
        lambda *_args, **_kwargs: candidate_search.SearchResult(
            status="PASS", candidates=(candidate,), mode=CAMPAIGN_SEARCH_MODE,
        ),
    )

    result = build_portfolio_candidates(
        selected, _weighted_build_campaign(), capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert (result.status, result.blockers, result.variants) == (
        "FAIL", ("PROFILE:WEIGHTED_SEARCH_CONFIG_INVALID",), (),
    )


def test_build_adapter_rejects_duplicate_launch_profiles_before_enrichment_or_search(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    members = (dict(selected[0], position_size_usdt=Decimal("123")),)
    campaign = _weighted_build_campaign()
    campaign["launch"] = {"profiles": (
        {"profile_id": "BALANCED", "equity_usdt": Decimal("1000")},
        {"profile_id": "BALANCED", "equity_usdt": Decimal("1000")},
    )}
    calls = []

    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: calls.append("prepare") or object())
    monkeypatch.setattr(
        adapter_module,
        "enrich_finalist_rows",
        lambda *_args, **_kwargs: calls.append("enrich") or SimpleNamespace(
            status="PASS", rows=members, exclusions=(), reason=None,
        ),
    )
    monkeypatch.setattr(adapter_module, "_run_weighted_search", lambda *_args, **_kwargs: calls.append("search"))

    result = build_portfolio_candidates(
        selected, campaign, capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert (result.status, result.blockers, result.variants, calls) == (
        "FAIL", ("PROFILE:WEIGHTED_SEARCH_CONFIG_INVALID",), (), ["prepare"],
    )


def test_build_adapter_reports_typed_candidate_identity_collision(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    members = (dict(selected[0], position_size_usdt=Decimal("123")),)
    campaign = _weighted_build_campaign()
    campaign["launch"] = {"profiles": (
        {"profile_id": "BALANCED", "equity_usdt": Decimal("1000")},
        {"profile_id": "AGGRESSIVE", "equity_usdt": Decimal("1000")},
    )}
    candidates = {
        "BALANCED": _weighted_candidate("BALANCED", members=members, identity="duplicate"),
        "AGGRESSIVE": _weighted_candidate("AGGRESSIVE", members=members, identity="duplicate"),
    }

    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module,
        "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module,
        "_run_weighted_search",
        lambda *_args, **_kwargs: candidate_search.SearchResult(
            status="PASS", candidates=(candidates[_args[3]["profile_id"]],), mode=CAMPAIGN_SEARCH_MODE,
        ),
    )

    result = build_portfolio_candidates(
        selected, campaign, capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert (result.status, result.blockers, result.variants) == (
        "FAIL", (f"PROFILE:{WEIGHTED_EXECUTABLE_IDENTITY_COLLISION}",), (),
    )


def test_build_adapter_rejects_invalid_search_warning_without_coercion(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    members = (dict(selected[0], position_size_usdt=Decimal("123")),)
    candidate = _weighted_candidate(members=members)

    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module,
        "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module,
        "_run_weighted_search",
        lambda *_args, **_kwargs: candidate_search.SearchResult(
            status="PASS", candidates=(candidate,), warnings=(object(),), mode=CAMPAIGN_SEARCH_MODE,
        ),
    )

    result = build_portfolio_candidates(
        selected, _weighted_build_campaign(), capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert (result.status, result.blockers, result.variants, result.warnings) == (
        "FAIL", ("PROFILE:WEIGHTED_SEARCH_CONFIG_INVALID",), (), (),
    )


def test_build_adapter_rejects_pass_search_with_no_candidates(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    members = (dict(selected[0], position_size_usdt=Decimal("123")),)

    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module,
        "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module,
        "_run_weighted_search",
        lambda *_args, **_kwargs: candidate_search.SearchResult(
            status="PASS", candidates=(), mode=CAMPAIGN_SEARCH_MODE,
        ),
    )

    result = build_portfolio_candidates(
        selected, _weighted_build_campaign(), capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert (result.status, result.blockers, result.variants) == (
        "FAIL", ("PROFILE:WEIGHTED_SEARCH_FAILED",), (),
    )


def test_build_adapter_scenario_injection_does_not_mutate_campaign_profile(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    members = (dict(selected[0], position_size_usdt=Decimal("123")),)
    profile = {"profile_id": "BALANCED", "equity_usdt": Decimal("1000")}
    campaign = _weighted_build_campaign()
    campaign["launch"] = {"profiles": (profile,)}
    captured = []

    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module,
        "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module,
        "_run_weighted_search",
        lambda *_args, **_kwargs: captured.append(_args[3]) or candidate_search.SearchResult(
            status="FAIL", reason="SEARCH_FAIL", mode=CAMPAIGN_SEARCH_MODE,
        ),
    )

    build_portfolio_candidates(
        selected, campaign, capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert profile == {"profile_id": "BALANCED", "equity_usdt": Decimal("1000")}
    assert captured == [{"profile_id": "BALANCED", "equity_usdt": Decimal("1000"), "scenario_id": "BALANCED"}]


def test_build_adapter_runs_all_profiles_and_keeps_only_failed_profile_blocker(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    members = (dict(selected[0], position_size_usdt=Decimal("123")),)
    campaign = _weighted_build_campaign()
    campaign["launch"] = {"profiles": (
        {"profile_id": "BALANCED", "equity_usdt": Decimal("1000")},
        {"profile_id": "AGGRESSIVE", "equity_usdt": Decimal("1000")},
    )}
    calls = []
    candidate = _weighted_candidate(members=members)

    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module,
        "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None),
    )

    def search(*args, **kwargs):
        profile_id = args[3]["profile_id"]
        calls.append(profile_id)
        if profile_id == "BALANCED":
            return candidate_search.SearchResult(
                status="PASS", candidates=(candidate,), warnings=("pass-warning",), mode=CAMPAIGN_SEARCH_MODE,
            )
        return candidate_search.SearchResult(
            status="FAIL", reason="SEARCH_FAIL", warnings=("failed-warning",), mode=CAMPAIGN_SEARCH_MODE,
        )

    monkeypatch.setattr(adapter_module, "_run_weighted_search", search)

    result = build_portfolio_candidates(
        selected, campaign, capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert result.status == "FAIL"
    assert result.blockers == ("PROFILE:SEARCH_FAIL",)
    assert result.warnings == ("pass-warning", "failed-warning")
    assert result.variants == ()
    assert calls == ["BALANCED", "AGGRESSIVE"]


@pytest.mark.parametrize(
    "members",
    (
        (
            {"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},
            {"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 12, "result_id": 102},
        ),
        ({"symbol": "BTCUSDT", "side": "SHORT", "strategy_id": 11, "result_id": 101},),
    ),
)
def test_safe_weighted_variant_requires_one_nonempty_long_member_per_symbol(members):
    candidate = _weighted_candidate(members=members)

    with pytest.raises(CampaignContractError) as error:
        adapter_module._safe_weighted_variant(candidate, SimpleNamespace(mode=CAMPAIGN_SEARCH_MODE))
    assert error.value.code == "WEIGHTED_SEARCH_CONFIG_INVALID"


def test_safe_weighted_variant_rejects_whitespace_padded_duplicate_symbol():
    candidate = _weighted_candidate(members=(
        {"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},
        {"symbol": "BTCUSDT ", "side": "LONG", "strategy_id": 12, "result_id": 102},
    ))

    with pytest.raises(CampaignContractError) as error:
        adapter_module._safe_weighted_variant(candidate, SimpleNamespace(mode=CAMPAIGN_SEARCH_MODE))
    assert error.value.code == "WEIGHTED_SEARCH_CONFIG_INVALID"


@pytest.mark.parametrize("raw_location", ("member", "metrics"))
def test_safe_weighted_variant_rejects_known_raw_candidate_fields(raw_location):
    member = {"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101}
    metrics = {"limiter_L": Decimal("2")}
    if raw_location == "member":
        member["actions"] = ({"minute": 1},)
    else:
        metrics["equity"] = ({"minute": 1, "value": "1000"},)
    candidate = candidate_search.PortfolioCandidate(
        schema_version="portfolio_candidate_v1",
        profile_id="BALANCED",
        scenario_id="BALANCED",
        identity="candidate-raw",
        members=(member,),
        metrics=metrics,
    )

    with pytest.raises(CampaignContractError) as error:
        adapter_module._safe_weighted_variant(candidate, SimpleNamespace(mode=CAMPAIGN_SEARCH_MODE))
    assert error.value.code == "WEIGHTED_SEARCH_CONFIG_INVALID"


@pytest.mark.parametrize(
    ("mutate", "code"),
    (
        (lambda value: value.update(stage1_mode=None), "CAMPAIGN_LEGACY_STAGE1_MODE_UNSUPPORTED"),
        (lambda value: value.update(campaign_contract_version="OTHER"), "CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED"),
        (lambda value: value.update(search_mode=None), "CAMPAIGN_SEARCH_MODE_REQUIRED"),
        (lambda value: value.update(search_mode="PRETEST_PROXY"), "CAMPAIGN_LEGACY_SEARCH_MODE_UNSUPPORTED"),
        (lambda value: value.update(search_mode="OTHER"), "CAMPAIGN_SEARCH_MODE_UNSUPPORTED"),
        (lambda value: value.update(weighted_algo_version=None), "CAMPAIGN_WEIGHTED_ALGO_VERSION_REQUIRED"),
        (lambda value: value.update(weighted_algo_version="OTHER"), "CAMPAIGN_WEIGHTED_ALGO_VERSION_UNSUPPORTED"),
        (lambda value: value.update(versions={}), "CAMPAIGN_VERSIONS_MISMATCH"),
    ),
)
def test_adapter_returns_one_exact_contract_blocker(mutate, code):
    request = weighted_campaign()
    mutate(request)
    result = run_portfolio_adapter((), request, workspace_root=".")
    assert (result.status, result.blockers) == ("FAIL", (code,))


@pytest.mark.parametrize("campaign", (None, "not-a-campaign"))
def test_adapter_rejects_nonmapping_campaign_without_throw(campaign):
    result = run_portfolio_adapter((), campaign, workspace_root=".")
    assert (result.status, result.blockers) == ("FAIL", ("CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED",))


def test_valid_weighted_campaign_is_blocked_before_any_adapter_fact_work(monkeypatch):
    calls = []
    monkeypatch.setattr(candidate_search, "search_portfolio_candidates", lambda *args, **kwargs: calls.append("search"))
    monkeypatch.setattr(market_snapshot, "load_market_snapshot", lambda *args, **kwargs: calls.append("market"))
    monkeypatch.setattr(minute_capacity, "calculate_minute_capacity", lambda *args, **kwargs: calls.append("capacity"))
    monkeypatch.setattr(minute_capacity, "backfill_missing_days", lambda *args, **kwargs: calls.append("backfill"))
    monkeypatch.setattr(spread_screen, "read_spread_history", lambda *args, **kwargs: calls.append("spread"))
    result = run_portfolio_adapter((), weighted_campaign(), workspace_root=".")
    assert result.status == "FAIL"
    assert result.blockers == ("WEIGHTED_SEARCH_CONFIG_INVALID",)
    assert calls == []


def test_runtime_adapter_reaches_builder_with_injected_local_facts(monkeypatch, tmp_path):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    campaign = _runtime_weighted_campaign()
    market_fetcher = lambda feed, params: (feed, params)
    archive_fetcher = lambda symbol, day: (symbol, day)
    calls = {}
    invocation = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    clock_calls = []

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            clock_calls.append(tz)
            return invocation

    monkeypatch.setattr(adapter_module, "datetime", FixedDateTime)
    monkeypatch.setattr(market_snapshot, "_http_fetch", lambda *_args, **_kwargs: pytest.fail("network fallback touched"))
    monkeypatch.setattr(minute_capacity, "fetch_bybit_trade_archive", lambda *_args, **_kwargs: pytest.fail("archive fallback touched"))

    monkeypatch.setattr(
        adapter_module,
        "backfill_missing_days",
        lambda root, symbol, days, **kwargs: calls.update(backfill=(root, symbol, tuple(days), kwargs)) or SimpleNamespace(failed={}),
    )
    monkeypatch.setattr(
        adapter_module,
        "calculate_minute_capacity",
        lambda root, symbol, **kwargs: calls.update(capacity=(root, symbol, kwargs)) or "capacity",
    )
    monkeypatch.setattr(
        adapter_module,
        "load_market_snapshot",
        lambda symbols, **kwargs: calls.update(market=(tuple(symbols), kwargs)) or SimpleNamespace(reference="reference", mark_prices={"BTCUSDT": "100"}),
    )
    monkeypatch.setattr(
        adapter_module,
        "read_spread_history",
        lambda root, symbols, **kwargs: calls.update(spread=(root, tuple(symbols), kwargs)) or SimpleNamespace(observations={"BTCUSDT": ({"spread_bps_p95": "1"},)}, statuses={"BTCUSDT": "READY"}),
    )

    def fake_build(rows, frozen_campaign, **kwargs):
        calls["build"] = (rows, frozen_campaign, kwargs)
        return adapter_module.AdapterResult("PASS", variants=({"candidate_id": "candidate"},))

    monkeypatch.setattr(adapter_module, "build_portfolio_candidates", fake_build)

    result = run_portfolio_adapter(
        selected,
        campaign,
        workspace_root=tmp_path,
        market_fetcher=market_fetcher,
        archive_fetcher=archive_fetcher,
        workers=3,
    )

    assert result.status == "PASS"
    assert result.variants[0]["candidate_id"] == "candidate"
    assert calls["backfill"][0] == tmp_path / "tester/data/bybit"
    assert calls["backfill"][3]["fetch_day"] is archive_fetcher
    assert calls["market"][1]["fetcher"] is market_fetcher
    assert calls["market"][1]["captured_at_ms"] == 1893553445000
    assert clock_calls == [timezone.utc]
    build_rows, build_campaign, build_kwargs = calls["build"]
    assert build_rows is selected
    assert build_campaign is campaign
    observed_now_ms = build_kwargs.pop("now_ms")
    assert observed_now_ms == 1893553445000
    assert build_kwargs == {
        "capacities": {"BTCUSDT": "capacity"},
        "reference": "reference",
        "mark_prices": {"BTCUSDT": "100"},
        "spread_observations": {"BTCUSDT": ({"spread_bps_p95": "1"},)},
        "spread_history_statuses": {"BTCUSDT": "READY"},
        "workers": 3,
        "strategy_template": {"template": "frozen"},
    }


def test_runtime_adapter_uses_official_fact_loaders_without_overrides(monkeypatch, tmp_path):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    campaign = _runtime_weighted_campaign()
    campaign["config_document"]["liquidity"]["backfill_write_enabled"] = True
    official_archive = lambda symbol, day: (symbol, day)
    calls = {}
    monkeypatch.setattr(minute_capacity, "fetch_bybit_trade_archive", official_archive)
    monkeypatch.setattr(
        adapter_module,
        "backfill_missing_days",
        lambda root, symbol, days, **kwargs: calls.update(backfill=(root, symbol, tuple(days), kwargs)) or SimpleNamespace(failed={}),
    )
    monkeypatch.setattr(adapter_module, "calculate_minute_capacity", lambda *_args, **_kwargs: "capacity")
    monkeypatch.setattr(
        adapter_module,
        "load_market_snapshot",
        lambda symbols, **kwargs: calls.update(market=(tuple(symbols), kwargs)) or SimpleNamespace(reference="reference", mark_prices={"BTCUSDT": "100"}),
    )
    monkeypatch.setattr(
        adapter_module,
        "read_spread_history",
        lambda *_args, **_kwargs: SimpleNamespace(observations={"BTCUSDT": ({"spread_bps_p95": "1"},)}, statuses={"BTCUSDT": "READY"}),
    )
    monkeypatch.setattr(adapter_module, "build_portfolio_candidates", lambda *_args, **_kwargs: adapter_module.AdapterResult("PASS", variants=({"candidate_id": "candidate"},)))

    result = run_portfolio_adapter(selected, campaign, workspace_root=tmp_path)

    assert result.status == "PASS"
    assert calls["backfill"][3]["fetch_day"] is official_archive
    assert isinstance(calls["market"][1]["limiter"], market_snapshot.ApiRateLimiter)
    assert calls["market"][1]["limiter"].state_path == tmp_path / ".mrs3-market-api-cooldown.json"


@pytest.mark.parametrize(
    ("override", "blocker"),
    (("market_fetcher", "MARKET_SNAPSHOT_UNAVAILABLE"), ("archive_fetcher", "MINUTE_CAPACITY_UNAVAILABLE")),
)
def test_runtime_adapter_rejects_malformed_fact_override_before_fact_work(monkeypatch, tmp_path, override, blocker):
    calls = []
    monkeypatch.setattr(adapter_module, "backfill_missing_days", lambda *_args, **_kwargs: calls.append("backfill"))
    monkeypatch.setattr(adapter_module, "load_market_snapshot", lambda *_args, **_kwargs: calls.append("market"))

    result = run_portfolio_adapter(
        ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},),
        _runtime_weighted_campaign(),
        workspace_root=tmp_path,
        **{override: object()},
    )

    assert (result.status, result.blockers, calls) == ("FAIL", (blocker,), [])


def test_runtime_adapter_maps_official_archive_failure_before_market(monkeypatch, tmp_path):
    campaign = _runtime_weighted_campaign()
    campaign["config_document"]["liquidity"]["backfill_write_enabled"] = True
    archive_days = []
    fact_calls = []
    errors = []

    def fail_archive(_symbol, day):
        archive_days.append(day)
        raise RuntimeError("archive unavailable")

    class CapturingMinuteCapacityError(ValueError):
        def __init__(self, message):
            errors.append(message)
            super().__init__(message)

    monkeypatch.setattr(minute_capacity, "fetch_bybit_trade_archive", fail_archive)
    monkeypatch.setattr(adapter_module, "MinuteCapacityError", CapturingMinuteCapacityError)
    monkeypatch.setattr(adapter_module, "calculate_minute_capacity", lambda *_args, **_kwargs: fact_calls.append("capacity"))
    monkeypatch.setattr(adapter_module, "load_market_snapshot", lambda *_args, **_kwargs: fact_calls.append("market"))
    monkeypatch.setattr(adapter_module, "build_portfolio_candidates", lambda *_args, **_kwargs: fact_calls.append("builder"))

    result = run_portfolio_adapter(
        ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},),
        campaign,
        workspace_root=tmp_path,
    )

    assert len(archive_days) == 21
    failed_days = ", ".join(day.isoformat() for day in sorted(set(archive_days)))
    assert errors == [f"Bybit archive backfill failed for BTCUSDT: {failed_days}"]
    assert (result.status, result.blockers, fact_calls) == ("FAIL", ("MINUTE_CAPACITY_UNAVAILABLE",), [])


def test_runtime_adapter_maps_unexpected_builder_failure_separately(monkeypatch, tmp_path):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    campaign = _runtime_weighted_campaign()
    monkeypatch.setattr(adapter_module, "backfill_missing_days", lambda *_args, **_kwargs: SimpleNamespace(failed={}))
    monkeypatch.setattr(adapter_module, "calculate_minute_capacity", lambda *_args, **_kwargs: "capacity")
    monkeypatch.setattr(
        adapter_module,
        "load_market_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(reference="reference", mark_prices={"BTCUSDT": "100"}),
    )
    monkeypatch.setattr(
        adapter_module,
        "read_spread_history",
        lambda *_args, **_kwargs: SimpleNamespace(observations={"BTCUSDT": ({"spread_bps_p95": "1"},)}, statuses={"BTCUSDT": "READY"}),
    )
    monkeypatch.setattr(adapter_module, "build_portfolio_candidates", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("build exploded")))

    result = run_portfolio_adapter(
        selected,
        campaign,
        workspace_root=tmp_path,
        market_fetcher=lambda *_args, **_kwargs: {},
        archive_fetcher=lambda *_args, **_kwargs: b"archive",
    )

    assert (result.status, result.blockers) == ("FAIL", ("ADAPTER_BUILD_FAILED",))


def test_runtime_adapter_maps_minute_capacity_error(monkeypatch, tmp_path):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    monkeypatch.setattr(adapter_module, "backfill_missing_days", lambda *_args, **_kwargs: SimpleNamespace(failed={}))
    monkeypatch.setattr(adapter_module, "calculate_minute_capacity", lambda *_args, **_kwargs: (_ for _ in ()).throw(adapter_module.MinuteCapacityError("capacity")))
    monkeypatch.setattr(adapter_module, "load_market_snapshot", lambda *_args, **_kwargs: pytest.fail("market reached"))

    result = run_portfolio_adapter(
        selected,
        _runtime_weighted_campaign(),
        workspace_root=tmp_path,
        archive_fetcher=lambda *_args, **_kwargs: b"archive",
    )

    assert (result.status, result.blockers) == ("FAIL", ("MINUTE_CAPACITY_UNAVAILABLE",))


def test_runtime_adapter_preserves_fact_contract_error_after_one_clock_sample(monkeypatch, tmp_path):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    invocation = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    clock_calls = []

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            clock_calls.append(tz)
            return invocation

    monkeypatch.setattr(adapter_module, "datetime", FixedDateTime)
    monkeypatch.setattr(adapter_module, "backfill_missing_days", lambda *_args, **_kwargs: SimpleNamespace(failed={}))
    monkeypatch.setattr(
        adapter_module,
        "calculate_minute_capacity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            adapter_module.CampaignContractError("WEIGHTED_SEARCH_CONFIG_INVALID")
        ),
    )
    monkeypatch.setattr(adapter_module, "load_market_snapshot", lambda *_args, **_kwargs: pytest.fail("market reached"))

    result = run_portfolio_adapter(
        selected,
        _runtime_weighted_campaign(),
        workspace_root=tmp_path,
        market_fetcher=lambda *_args, **_kwargs: {},
        archive_fetcher=lambda *_args, **_kwargs: b"archive",
    )

    assert (result.status, result.blockers, clock_calls) == (
        "FAIL",
        ("WEIGHTED_SEARCH_CONFIG_INVALID",),
        [timezone.utc],
    )


def test_runtime_adapter_maps_market_snapshot_error(monkeypatch, tmp_path):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    monkeypatch.setattr(adapter_module, "backfill_missing_days", lambda *_args, **_kwargs: SimpleNamespace(failed={}))
    monkeypatch.setattr(adapter_module, "calculate_minute_capacity", lambda *_args, **_kwargs: "capacity")
    monkeypatch.setattr(adapter_module, "load_market_snapshot", lambda *_args, **_kwargs: (_ for _ in ()).throw(adapter_module.MarketSnapshotError("market")))
    monkeypatch.setattr(adapter_module, "build_portfolio_candidates", lambda *_args, **_kwargs: pytest.fail("builder reached"))

    result = run_portfolio_adapter(
        selected,
        _runtime_weighted_campaign(),
        workspace_root=tmp_path,
        market_fetcher=lambda *_args, **_kwargs: {},
        archive_fetcher=lambda *_args, **_kwargs: b"archive",
    )

    assert (result.status, result.blockers) == ("FAIL", ("MARKET_SNAPSHOT_UNAVAILABLE",))


def test_runtime_adapter_rejects_empty_market_facts_before_builder(monkeypatch, tmp_path):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    calls = []
    monkeypatch.setattr(adapter_module, "backfill_missing_days", lambda *_args, **_kwargs: SimpleNamespace(failed={}))
    monkeypatch.setattr(adapter_module, "calculate_minute_capacity", lambda *_args, **_kwargs: "capacity")
    monkeypatch.setattr(adapter_module, "load_market_snapshot", lambda *_args, **_kwargs: SimpleNamespace(reference="reference", mark_prices={}))
    monkeypatch.setattr(
        adapter_module,
        "read_spread_history",
        lambda *_args, **_kwargs: SimpleNamespace(observations={"BTCUSDT": ()}, statuses={"BTCUSDT": "READY"}),
    )
    monkeypatch.setattr(adapter_module, "build_portfolio_candidates", lambda *_args, **_kwargs: calls.append(True))

    result = run_portfolio_adapter(
        selected,
        _runtime_weighted_campaign(),
        workspace_root=tmp_path,
        market_fetcher=lambda *_args, **_kwargs: {},
        archive_fetcher=lambda *_args, **_kwargs: b"archive",
    )

    assert (result.status, result.blockers, calls) == ("FAIL", ("MARKET_SNAPSHOT_UNAVAILABLE",), [])


def test_runtime_adapter_rejects_empty_spread_facts_before_builder(monkeypatch, tmp_path):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    calls = []
    monkeypatch.setattr(adapter_module, "backfill_missing_days", lambda *_args, **_kwargs: SimpleNamespace(failed={}))
    monkeypatch.setattr(adapter_module, "calculate_minute_capacity", lambda *_args, **_kwargs: "capacity")
    monkeypatch.setattr(adapter_module, "load_market_snapshot", lambda *_args, **_kwargs: SimpleNamespace(reference="reference", mark_prices={"BTCUSDT": "100"}))
    monkeypatch.setattr(
        adapter_module,
        "read_spread_history",
        lambda *_args, **_kwargs: SimpleNamespace(observations={"BTCUSDT": ()}, statuses={"BTCUSDT": "READY"}),
    )
    monkeypatch.setattr(adapter_module, "build_portfolio_candidates", lambda *_args, **_kwargs: calls.append(True))

    result = run_portfolio_adapter(
        selected,
        _runtime_weighted_campaign(),
        workspace_root=tmp_path,
        market_fetcher=lambda *_args, **_kwargs: {},
        archive_fetcher=lambda *_args, **_kwargs: b"archive",
    )

    assert (result.status, result.blockers, calls) == ("FAIL", ("ADAPTER_FACTS_UNAVAILABLE",), [])


@pytest.mark.parametrize("template", (None, [], "not-a-template"))
def test_runtime_adapter_requires_frozen_strategy_template_before_fact_work(monkeypatch, tmp_path, template):
    campaign = _runtime_weighted_campaign()
    campaign["strategy_template"] = template
    calls = []
    monkeypatch.setattr(adapter_module, "load_market_snapshot", lambda *_args, **_kwargs: calls.append("market"))

    result = run_portfolio_adapter(
        ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},),
        campaign,
        workspace_root=tmp_path,
        market_fetcher=lambda *_args, **_kwargs: {},
    )

    assert (result.status, result.blockers, calls) == ("FAIL", ("WEIGHTED_SEARCH_CONFIG_INVALID",), [])


@pytest.mark.parametrize(
    ("search_mode", "blocker"),
    (("PRETEST_PROXY", "CAMPAIGN_LEGACY_SEARCH_MODE_UNSUPPORTED"), ("OTHER", "CAMPAIGN_SEARCH_MODE_UNSUPPORTED")),
)
def test_runtime_adapter_contract_validation_precedes_missing_fetcher(tmp_path, search_mode, blocker):
    campaign = _runtime_weighted_campaign()
    campaign["search_mode"] = search_mode
    campaign.pop("strategy_template")

    result = run_portfolio_adapter((), campaign, workspace_root=tmp_path)

    assert (result.status, result.blockers) == ("FAIL", (blocker,))


def test_runtime_adapter_keeps_legacy_campaign_gate_failure(tmp_path):
    campaign = _runtime_weighted_campaign()
    campaign["search_mode"] = "PRETEST_PROXY"

    result = run_portfolio_adapter((), campaign, workspace_root=tmp_path)

    assert (result.status, result.blockers) == ("FAIL", ("CAMPAIGN_LEGACY_SEARCH_MODE_UNSUPPORTED",))


def test_build_adapter_blocks_valid_weighted_campaign_before_legacy_search(monkeypatch):
    calls = []
    monkeypatch.setattr(candidate_search, "search_portfolio_candidates", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy search reached")))
    monkeypatch.setattr(adapter_module, "enrich_finalist_rows", lambda *_args, **_kwargs: calls.append("enrich"))
    monkeypatch.setattr(adapter_module, "_run_weighted_search", lambda *_args, **_kwargs: calls.append("search"))
    result = build_portfolio_candidates(
        (), weighted_campaign(), capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
    )
    assert (result.status, result.blockers) == ("FAIL", (WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE,))
    assert calls == []


def test_build_adapter_bridges_frozen_weighted_search_result(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    members = (dict(
        selected[0],
        position_size_usdt=Decimal("123"),
        k=9,
        size_composition_vector=(Decimal("0.5"),),
    ),)
    prepared = object()
    margin = _margin_evidence(11)
    campaign = weighted_campaign()
    campaign.update({
        "weighted_input_rows": (dict(selected[0], actions=(), equity=()),),
        "config_document": {
            "liquidity": {"maximum_age_hours": 2},
            "search": {
                "seed": 17,
                "weighted_search": {
                    "history_step_minutes": 5,
                    "max_targets": 4,
                    "bootstrap_scenarios_per_block": 101,
                    "bootstrap_diagnostic_scenarios": 7,
                    "wall_time_seconds": 12,
                    "solver_time_seconds": 3,
                },
                "composition": {"parameters": {"minimum_common_days": 1}},
            },
        },
        "launch": {"profiles": ({"profile_id": "BALANCED", "equity_usdt": Decimal("1000")},)},
    })
    calls = {}
    candidate = candidate_search.PortfolioCandidate(
        schema_version="portfolio_candidate_v1",
        profile_id="BALANCED",
        scenario_id="BALANCED",
        identity="candidate-1",
        members=members,
        metrics={
            "limiter_L": Decimal("2"),
            "proxy_pnl_usdt": Decimal("1"),
            "k": 7,
            "size_composition_vector": (Decimal("0.5"),),
        },
    )
    search_result = candidate_search.SearchResult(
        status="PASS", candidates=(candidate,), warnings=("search-warning",), mode="WEIGHTED_V1"
    )

    def capture_enrich(*args, **kwargs):
        calls["enrich"] = (args, kwargs)
        return SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None)

    def capture_prepare(*args):
        calls["prepare"] = args
        return prepared

    def capture_search(*args, **kwargs):
        calls["search"] = (args, kwargs)
        return search_result

    monkeypatch.setattr(candidate_search, "search_portfolio_candidates", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy search reached")))
    monkeypatch.setattr(position_sizing, "size_composition_vector", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy sizing reached")))
    monkeypatch.setattr(spread_screen, "screen_spread", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy spread reached")))
    monkeypatch.setattr(adapter_module, "enrich_finalist_rows", capture_enrich)
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", capture_prepare)
    monkeypatch.setattr(adapter_module, "_run_weighted_search", capture_search)

    result = build_portfolio_candidates(
        selected,
        campaign,
        capacities={},
        reference=None,
        mark_prices={},
        spread_observations={},
        spread_history_statuses={"BTCUSDT": "READY"},
        now_ms=0,
        workers=3,
        margin_coefficients=margin,
    )

    assert result.status == "PASS"
    assert calls["enrich"] == (
        (selected, {}, None, {}),
        {"now_ms": 0, "maximum_age_hours": 2},
    )
    assert calls["prepare"] == (selected, campaign)
    search_args, search_kwargs = calls["search"]
    assert search_args[0] is prepared
    assert search_args[1] is members
    assert search_args[2] is campaign
    assert search_args[3] == {"profile_id": "BALANCED", "equity_usdt": Decimal("1000"), "scenario_id": "BALANCED"}
    assert search_args[4] is margin
    assert search_kwargs == {"workers": 3}
    assert result.warnings == ("search-warning",)
    variant = result.variants[0]
    assert variant["candidate_id"] == "candidate-1"
    assert variant["profile"] == "BALANCED"
    assert variant["scenario_id"] == "BALANCED"
    assert variant["schema_version"] == "portfolio_candidate_v1"
    assert variant["member_count"] == 1
    assert variant["pair_count"] == 1
    assert variant["limiter_L"] == Decimal("2")
    assert variant["search_mode"] == "WEIGHTED_V1"
    assert variant["gate"] == "PASS"
    assert variant["members"][0]["k"] == 9
    assert variant["members"][0]["size_composition_vector"] == (Decimal("0.5"),)
    assert variant["metrics"]["k"] == 7
    assert variant["metrics"]["size_composition_vector"] == (Decimal("0.5"),)
    with pytest.raises(TypeError):
        result.variants[0]["profile_id"] = "AGGRESSIVE"
    with pytest.raises(TypeError):
        result.variants[0]["members"][0]["symbol"] = "ETHUSDT"
    with pytest.raises(TypeError):
        result.variants[0]["metrics"]["limiter_L"] = Decimal("9")


def _weighted_strategy_template_fixture():
    return {
        "exchange": {"use_upnl": True, "use_frozen_balance": True},
        "basic": {
            "strategy": "mrs3", "symbol": "PLACEHOLDER", "time_frame": "1h",
            "use_long": True, "use_short": False, "use_fix": False,
            "balance_percentage_long": 100, "risk_long": 1, "max_balance": 0,
            "leverage": 20.0,
        },
        "mrs": {"position_priority": 1},
        "mrs3": {
            "ma_long": [{"id": 0, "len": 1, "multiplier": 1.0, "lot_x": 1.0}],
            "ma_short": [{"id": 0, "len": 1, "multiplier": 1.0, "lot_x": 1.0}],
            "ma_close_long": {"len": 1, "multiplier": 1.003},
            "ma_close_short": {"len": 1, "multiplier": 0.997},
        },
    }


def _weighted_required_evidence(row):
    return dict(
        row,
        reference_digest=f"reference-{row['strategy_id']}-{row['result_id']}",
        sizing_digest=f"sizing-{row['strategy_id']}-{row['result_id']}",
        capacity_digest=f"capacity-{row['strategy_id']}-{row['result_id']}",
        report_start_utc="2026-01-01T00:00:00Z",
        report_end_utc="2026-01-15T00:00:00Z",
    )


def test_build_adapter_attaches_real_source_geometry_payloads_by_member_identity(monkeypatch):
    source_rows = (
        {
            "symbol": "ETHUSDT", "side": "LONG", "strategy_id": 22, "result_id": 202,
            "timeframe": "15m", "close_ma_len": 21, "order_count": 2,
            "actions": ({"event": "open"},), "equity": ({"value": "100"},),
            "strategy_orders": (
                {"order_id": 1, "open_ma_len": 17, "shift_bp": 100, "lot_x": Decimal("0.4")},
                {"order_id": 2, "open_ma_len": 19, "shift_bp": 250, "lot_x": Decimal("0.6")},
            ),
        },
        {
            "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
            "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
            "actions": ({"event": "open"},), "equity": ({"value": "100"},),
            "strategy_orders": (
                {"order_id": 1, "open_ma_len": 34, "shift_bp": 50, "lot_x": Decimal("1")},
            ),
        },
        {
            "symbol": "SOLUSDT", "side": "LONG", "strategy_id": 33, "result_id": 303,
            "timeframe": "1h", "close_ma_len": 13, "order_count": 1,
            "actions": ({"event": "open"},), "equity": ({"value": "100"},),
            "strategy_orders": (
                {"order_id": 1, "open_ma_len": 8, "shift_bp": 75, "lot_x": Decimal("1")},
            ),
        },
    )
    selected = source_rows
    enriched = tuple(
        _weighted_required_evidence(dict(row, position_size_usdt=capacity, planned_leverage=leverage))
        for row, capacity, leverage in zip(source_rows, (Decimal("800"), Decimal("400"), Decimal("100")), (Decimal("7"), Decimal("9"), Decimal("5")))
    )
    candidate_members = (
        {"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101, "x_usdt": Decimal("100"), "capacity_usdt": Decimal("400"), "priority": 3},
        {"symbol": "ETHUSDT", "side": "LONG", "strategy_id": 22, "result_id": 202, "x_usdt": Decimal("200"), "capacity_usdt": Decimal("800"), "priority": 2},
        {"symbol": "SOLUSDT", "side": "LONG", "strategy_id": 33, "result_id": 303, "x_usdt": Decimal("0"), "capacity_usdt": Decimal("100"), "priority": 1},
    )
    candidate = candidate_search.PortfolioCandidate(
        schema_version="portfolio_candidate_v1", profile_id="BALANCED", scenario_id="BALANCED",
        identity="candidate-BALANCED", members=candidate_members, metrics={"limiter_L": 2},
    )
    campaign = _weighted_build_campaign()
    template = _weighted_strategy_template_fixture()
    template_before = deepcopy(template)
    source_before = deepcopy(source_rows)
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module, "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=enriched, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module, "_run_weighted_search",
        lambda *_args, **_kwargs: candidate_search.SearchResult(
            status="PASS", candidates=(candidate,), mode=CAMPAIGN_SEARCH_MODE,
        ),
    )

    result = build_portfolio_candidates(
        selected, campaign, capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY", "ETHUSDT": "READY", "SOLUSDT": "READY"},
        now_ms=0, margin_coefficients=_margin_evidence(11), strategy_template=template,
    )

    assert result.status == "PASS", (result.blockers, result.excluded)
    payloads = result.variants[0]["strategy_payloads"]
    by_symbol = {payload["strategy"]["basic"]["symbol"]: payload for payload in payloads}
    assert set(by_symbol) == {"BTCUSDT", "ETHUSDT"}
    btc = by_symbol["BTCUSDT"]["strategy"]
    eth = by_symbol["ETHUSDT"]["strategy"]
    assert btc["basic"]["time_frame"] == "3h"
    assert btc["basic"]["leverage"] == 9
    assert btc["name"] == "PORTFOLIO_BTCUSDT_11_101"
    assert [dict(entry) for entry in btc["mrs3"]["ma_long"]] == [{"id": 1, "len": 34, "multiplier": 0.995, "lot_x": 1.0}]
    assert btc["mrs3"]["ma_close_long"]["len"] == 55
    assert eth["basic"]["time_frame"] == "15m"
    assert eth["basic"]["leverage"] == 7
    assert [entry["len"] for entry in eth["mrs3"]["ma_long"]] == [17, 19]
    assert [entry["multiplier"] for entry in eth["mrs3"]["ma_long"]] == [0.99, 0.975]
    assert [entry["lot_x"] for entry in eth["mrs3"]["ma_long"]] == [0.4, 0.6]
    assert eth["mrs3"]["ma_close_long"]["len"] == 21
    assert by_symbol["BTCUSDT"]["facts"] == {"B": "1000", "C": "400", "q": "0.1", "x": "100"}
    assert by_symbol["ETHUSDT"]["facts"] == {"B": "1000", "C": "800", "q": "0.2", "x": "200"}
    assert by_symbol["BTCUSDT"]["strategy"]["basic"]["max_balance"] == 4000
    assert by_symbol["ETHUSDT"]["strategy"]["basic"]["max_balance"] == 4000
    assert by_symbol["BTCUSDT"]["strategy"]["mrs"]["position_priority"] == 3
    assert by_symbol["ETHUSDT"]["strategy"]["mrs"]["position_priority"] == 2
    assert by_symbol["BTCUSDT"]["account"]["open_positions_limiter"] == 2
    assert by_symbol["ETHUSDT"]["account"]["open_positions_limiter"] == 2
    payload_copy = adapter_module._copy_candidate_fields(by_symbol["BTCUSDT"])
    decoded_payload = json.loads(json.dumps(payload_copy))
    assert decoded_payload["facts"] == payload_copy["facts"]
    assert decoded_payload["strategy"]["mrs"]["position_priority"] == 3
    payload_keys = set()
    def collect_keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                payload_keys.add(key)
                collect_keys(item)
        elif isinstance(value, list):
            for item in value:
                collect_keys(item)
    collect_keys(decoded_payload)
    assert not payload_keys.intersection(adapter_module._RAW_SERIES_KEYS | {"raw"})
    assert source_rows == source_before
    assert template == template_before


def test_build_adapter_rebinds_identity_after_executable_payload_assembly(monkeypatch):
    source_rows = (
        {
            "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
            "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
            "actions": ({"event": "open"},), "equity": ({"value": "100"},),
            "strategy_orders": (
                {"order_id": 1, "open_ma_len": 34, "shift_bp": 50, "lot_x": Decimal("1")},
            ),
        },
    )
    enriched = tuple(
        _weighted_required_evidence(dict(row, position_size_usdt=Decimal("400"), planned_leverage=Decimal("9")))
        for row in source_rows
    )
    candidate = candidate_search.PortfolioCandidate(
        schema_version="portfolio_candidate_v1", profile_id="BALANCED", scenario_id="BALANCED",
        identity="candidate-BALANCED", members=((
            {"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
             "x_usdt": Decimal("100"), "capacity_usdt": Decimal("400"), "priority": 3},
        )),
        metrics={"limiter_L": 2},
    )
    campaign = _weighted_build_campaign()
    template = _weighted_strategy_template_fixture()
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module, "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=enriched, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module, "_run_weighted_search",
        lambda *_args, **_kwargs: candidate_search.SearchResult(
            status="PASS", candidates=(candidate,), mode=CAMPAIGN_SEARCH_MODE,
        ),
    )

    result = build_portfolio_candidates(
        source_rows, campaign, capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11), strategy_template=template,
    )

    assert result.status == "PASS", (result.blockers, result.excluded)
    variant = result.variants[0]
    assert variant["identity"] == variant["candidate_id"]
    assert variant["identity"] != "candidate-BALANCED"
    assert variant["search_identity"] == "candidate-BALANCED"
    assert len(variant["identity"]) == 64


def test_build_adapter_reports_true_executable_identity_collision(monkeypatch):
    source_rows = ({
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
        "strategy_orders": ({"order_id": 1, "open_ma_len": 34, "shift_bp": 50, "lot_x": Decimal("1")},),
    },)
    enriched = tuple(
        _weighted_required_evidence(dict(row, position_size_usdt=Decimal("400"), planned_leverage=Decimal("9")))
        for row in source_rows
    )
    member = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "x_usdt": Decimal("100"), "capacity_usdt": Decimal("400"), "priority": 3,
    }
    candidates = tuple(
        candidate_search.PortfolioCandidate(
            schema_version="portfolio_candidate_v1", profile_id="BALANCED", scenario_id="BALANCED",
            identity=identity, members=(member,), metrics={"limiter_L": 2},
        )
        for identity in ("search-a", "search-b")
    )
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module, "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=enriched, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module, "_run_weighted_search",
        lambda *_args, **_kwargs: candidate_search.SearchResult(
            status="PASS", candidates=candidates, mode=CAMPAIGN_SEARCH_MODE,
        ),
    )

    result = build_portfolio_candidates(
        source_rows, _weighted_build_campaign(), capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11), strategy_template=_weighted_strategy_template_fixture(),
    )

    assert (result.status, result.blockers, result.variants) == (
        "FAIL", (f"PROFILE:{WEIGHTED_EXECUTABLE_IDENTITY_COLLISION}",), (),
    )


def test_weighted_payload_readback_checks_explicit_a0_and_bounded_full_position():
    template, member = _weighted_payload_inputs()
    template["basic"]["initial_balance"] = "1000"
    template["mrs3"]["order_geometry"] = {
        "sizing_base": "1000",
        "entry": [{"price": "100", "qty": "0.9", "qty_step": "0.1"}],
    }

    payload = build_weighted_strategy_payload(template, member, Decimal("1000"), Decimal("400"), 3)

    assert payload["readback"] == {
        "status": "PASS",
        "A0": "1000",
        "sizing_base": "1000",
        "expected_full_position_usdt": "100",
        "full_position_usdt": "90",
        "epsilon_usdt": "10",
    }


def test_weighted_payload_readback_rejects_explicit_geometry_mismatch():
    template, member = _weighted_payload_inputs()
    template["basic"]["initial_balance"] = "999"
    template["mrs3"]["order_geometry"] = {
        "sizing_base": "1000",
        "entry": [{"price": "100", "qty": "0.9", "qty_step": "0.1"}],
    }

    with pytest.raises(CampaignContractError) as error:
        build_weighted_strategy_payload(template, member, Decimal("1000"), Decimal("400"), 3)

    assert error.value.code == "WEIGHTED_PAYLOAD_INVALID"


def test_weighted_payload_readback_rejects_full_position_outside_step_epsilon():
    template, member = _weighted_payload_inputs()
    template["basic"]["initial_balance"] = "1000"
    template["mrs3"]["order_geometry"] = {
        "sizing_base": "1000",
        "entry": [{"price": "100", "qty": "0.7", "qty_step": "0.1"}],
    }

    with pytest.raises(CampaignContractError) as error:
        build_weighted_strategy_payload(template, member, Decimal("1000"), Decimal("400"), 3)

    assert error.value.code == "WEIGHTED_PAYLOAD_INVALID"


def test_weighted_payload_readback_rejects_unaligned_quantity():
    template, member = _weighted_payload_inputs()
    template["basic"]["initial_balance"] = "1000"
    template["mrs3"]["order_geometry"] = {
        "sizing_base": "1000",
        "entry": [{"price": "100", "qty": "0.99", "qty_step": "0.1"}],
    }

    with pytest.raises(CampaignContractError) as error:
        build_weighted_strategy_payload(template, member, Decimal("1000"), Decimal("400"), 3)

    assert error.value.code == "WEIGHTED_PAYLOAD_INVALID"


def test_weighted_payload_readback_rejects_missing_explicit_sizing_base():
    template, member = _weighted_payload_inputs()
    template["basic"]["initial_balance"] = "1000"
    template["mrs3"]["order_geometry"] = {
        "entry": [{"price": "100", "qty": "0.9", "qty_step": "0.1"}],
    }

    with pytest.raises(CampaignContractError) as error:
        build_weighted_strategy_payload(template, member, Decimal("1000"), Decimal("400"), 3)

    assert error.value.code == "WEIGHTED_PAYLOAD_INVALID"


def test_weighted_executable_identity_is_stable_and_binds_mutable_inputs():
    campaign = weighted_campaign()
    campaign.update({
        "input_digest": "input-a",
        "config_digest": "config-a",
        "strategy_template_digest": "template-a",
    })
    member = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "x_usdt": "100",
    }
    enriched = dict(
        member,
        reference_digest="reference-a", sizing_digest="sizing-a", capacity_digest="capacity-a",
        report_start_utc="2026-01-01T00:00:00Z", report_end_utc="2026-01-15T00:00:00Z",
    )
    payload = {
        "strategy": {"name": "PORTFOLIO_BTCUSDT_11_101", "basic": {"symbol": "BTCUSDT", "max_balance": 4000}},
        "account": {"open_positions_limiter": 2},
        "facts": {"B": "1000", "x": "100"},
    }

    identity = adapter_module._weighted_executable_identity(
        campaign, "BALANCED", "BALANCED", Decimal("1000"), (member,), (enriched,), (payload,)
    )
    assert identity == adapter_module._weighted_executable_identity(
        campaign, "BALANCED", "BALANCED", Decimal("1000"), (member,), (enriched,), (payload,)
    )
    assert len(identity) == 64 and all(char in "0123456789abcdef" for char in identity)

    changed_payload = deepcopy(payload)
    changed_payload["facts"]["x"] = "101"
    assert identity != adapter_module._weighted_executable_identity(
        campaign, "BALANCED", "BALANCED", Decimal("1000"), (member,), (enriched,), (changed_payload,)
    )
    changed_campaign = dict(campaign, config_digest="config-b")
    assert identity != adapter_module._weighted_executable_identity(
        changed_campaign, "BALANCED", "BALANCED", Decimal("1000"), (member,), (enriched,), (payload,)
    )
    changed_enriched = dict(enriched, reference_digest="reference-b")
    assert identity != adapter_module._weighted_executable_identity(
        campaign, "BALANCED", "BALANCED", Decimal("1000"), (member,), (changed_enriched,), (payload,)
    )
    changed_period = dict(enriched, report_end_utc="2026-01-16T00:00:00Z")
    assert identity != adapter_module._weighted_executable_identity(
        campaign, "BALANCED", "BALANCED", Decimal("1000"), (member,), (changed_period,), (payload,)
    )

    mutations = (
        (dict(campaign, weighted_algo_version="WS1.2"), Decimal("1000"), enriched, payload),
        (dict(campaign, input_digest="input-b"), Decimal("1000"), enriched, payload),
        (dict(campaign, strategy_template_digest="template-b"), Decimal("1000"), enriched, payload),
        (campaign, Decimal("1001"), enriched, payload),
    )
    for changed_campaign, changed_bank, changed_evidence, changed_payload in mutations:
        assert identity != adapter_module._weighted_executable_identity(
            changed_campaign, "BALANCED", "BALANCED", changed_bank,
            (member,), (changed_evidence,), (changed_payload,),
        )

    for path, value in (
        (("facts", "q"), "0.11"),
        (("facts", "C"), "401"),
        (("facts", "priority"), 4),
        (("strategy", "basic", "max_balance"), 4001),
        (("account", "open_positions_limiter"), 3),
    ):
        changed_payload = deepcopy(payload)
        target = changed_payload
        for key in path[:-1]:
            target = target.setdefault(key, {})
        target[path[-1]] = value
        assert identity != adapter_module._weighted_executable_identity(
            campaign, "BALANCED", "BALANCED", Decimal("1000"),
            (member,), (enriched,), (changed_payload,),
        )


def test_weighted_executable_identity_rejects_reversed_payload_association():
    campaign = weighted_campaign()
    first = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101, "x_usdt": "100",
    }
    second = {
        "symbol": "ETHUSDT", "side": "LONG", "strategy_id": 22, "result_id": 202, "x_usdt": "200",
    }
    enriched = tuple(_weighted_required_evidence(row) for row in (first, second))
    payloads = tuple(
        {
            "strategy": {"name": f"PORTFOLIO_{row['symbol']}_{row['strategy_id']}_{row['result_id']}", "basic": {"symbol": row["symbol"]}},
            "account": {}, "facts": {},
        }
        for row in (first, second)
    )

    with pytest.raises(CampaignContractError) as error:
        adapter_module._weighted_executable_identity(
            campaign, "BALANCED", "BALANCED", Decimal("1000"), (first, second), enriched,
            (payloads[1], payloads[0]),
        )

    assert error.value.code == "WEIGHTED_SEARCH_CONFIG_INVALID"


def test_weighted_executable_identity_rejects_missing_or_conflicting_evidence():
    campaign = weighted_campaign()
    member = {"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101, "x_usdt": "100"}
    payload = {
        "strategy": {"name": "PORTFOLIO_BTCUSDT_11_101", "basic": {"symbol": "BTCUSDT"}},
        "account": {}, "facts": {},
    }
    enriched = _weighted_required_evidence(member)
    missing = dict(enriched)
    del missing["capacity_digest"]
    with pytest.raises(CampaignContractError) as missing_error:
        adapter_module._weighted_executable_identity(
            campaign, "BALANCED", "BALANCED", Decimal("1000"), (member,), (missing,), (payload,)
        )
    assert missing_error.value.code == "WEIGHTED_SEARCH_CONFIG_INVALID"

    conflict = dict(enriched, report_end_utc="2026-01-16T00:00:00Z")
    with pytest.raises(CampaignContractError) as conflict_error:
        adapter_module._weighted_executable_identity(
            campaign, "BALANCED", "BALANCED", Decimal("1000"), (member,), (enriched,), (payload,),
            source_rows=(conflict,),
        )
    assert conflict_error.value.code == "WEIGHTED_SEARCH_CONFIG_INVALID"


def test_weighted_payload_invalid_member_x_preserves_payload_error_code():
    template, member = _weighted_payload_inputs()
    member["x_usdt"] = "-1"

    with pytest.raises(CampaignContractError) as error:
        build_weighted_strategy_payload(template, member, Decimal("1000"), Decimal("400"), 3)

    assert error.value.code == "WEIGHTED_PAYLOAD_INVALID"


@pytest.mark.parametrize(
    "geometry_case",
    (
        "missing", "nonmapping", "count_mismatch", "fractional_open_ma", "boolean_open_ma",
        "negative_shift", "zero_close_ma", "boolean_order_count",
    ),
)
def test_build_adapter_rejects_template_payload_without_exact_source_geometry(monkeypatch, geometry_case):
    source = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
    }
    source["strategy_orders"] = (
        {"order_id": 1, "open_ma_len": 34, "shift_bp": 50, "lot_x": Decimal("1")},
    )
    if geometry_case == "missing":
        source.pop("strategy_orders")
    elif geometry_case == "nonmapping":
        source["strategy_orders"] = (
            {"order_id": 1, "open_ma_len": 34, "shift_bp": 50, "lot_x": Decimal("1")}, object(),
        )
    elif geometry_case == "count_mismatch":
        source["order_count"] = 2
    elif geometry_case == "fractional_open_ma":
        source["strategy_orders"][0]["open_ma_len"] = Decimal("34.5")
    elif geometry_case == "boolean_open_ma":
        source["strategy_orders"][0]["open_ma_len"] = True
    elif geometry_case == "negative_shift":
        source["strategy_orders"][0]["shift_bp"] = -1
    elif geometry_case == "zero_close_ma":
        source["close_ma_len"] = 0
    elif geometry_case == "boolean_order_count":
        source["order_count"] = True
    selected = (source,)
    members = (dict(selected[0], position_size_usdt=Decimal("400")),)
    candidate = candidate_search.PortfolioCandidate(
        schema_version="portfolio_candidate_v1", profile_id="BALANCED", scenario_id="BALANCED",
        identity="candidate-BALANCED", members=(dict(selected[0], x_usdt=Decimal("100"), capacity_usdt=Decimal("400"), priority=3),),
        metrics={"limiter_L": 2},
    )
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module, "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module, "_run_weighted_search",
        lambda *_args, **_kwargs: candidate_search.SearchResult(
            status="PASS", candidates=(candidate,), mode=CAMPAIGN_SEARCH_MODE,
        ),
    )

    result = build_portfolio_candidates(
        selected, _weighted_build_campaign(), capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11), strategy_template=_weighted_strategy_template_fixture(),
    )

    assert (result.status, result.blockers, result.variants) == (
        "FAIL", ("PROFILE:" + WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE,), (),
    )


def test_build_adapter_rejects_negative_x_for_template_payload(monkeypatch):
    selected = ({
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
        "strategy_orders": ({"order_id": 1, "open_ma_len": 34, "shift_bp": 50, "lot_x": Decimal("1")},),
    },)
    members = (dict(selected[0], position_size_usdt=Decimal("400")),)
    candidate = candidate_search.PortfolioCandidate(
        schema_version="portfolio_candidate_v1", profile_id="BALANCED", scenario_id="BALANCED",
        identity="candidate-BALANCED", members=(dict(selected[0], x_usdt=Decimal("-1"), capacity_usdt=Decimal("400"), priority=3),),
        metrics={"limiter_L": 2},
    )
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module, "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module, "_run_weighted_search",
        lambda *_args, **_kwargs: candidate_search.SearchResult(
            status="PASS", candidates=(candidate,), mode=CAMPAIGN_SEARCH_MODE,
        ),
    )

    result = build_portfolio_candidates(
        selected, _weighted_build_campaign(), capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11), strategy_template=_weighted_strategy_template_fixture(),
    )

    assert (result.status, result.blockers, result.variants) == (
        "FAIL", ("PROFILE:WEIGHTED_PAYLOAD_INVALID",), (),
    )


def test_build_adapter_rejects_ambiguous_template_source_geometry(monkeypatch):
    source = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
        "strategy_orders": ({"order_id": 1, "open_ma_len": 34, "shift_bp": 50, "lot_x": Decimal("1")},),
    }
    selected = (source, dict(source))
    members = tuple(dict(row, position_size_usdt=Decimal("400")) for row in selected)
    candidate = candidate_search.PortfolioCandidate(
        schema_version="portfolio_candidate_v1", profile_id="BALANCED", scenario_id="BALANCED",
        identity="candidate-BALANCED", members=(dict(source, x_usdt=Decimal("100"), capacity_usdt=Decimal("400"), priority=3),),
        metrics={"limiter_L": 2},
    )
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module, "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module, "_run_weighted_search",
        lambda *_args, **_kwargs: candidate_search.SearchResult(
            status="PASS", candidates=(candidate,), mode=CAMPAIGN_SEARCH_MODE,
        ),
    )

    result = build_portfolio_candidates(
        selected, _weighted_build_campaign(), capacities={}, reference=None, mark_prices={},
        spread_observations={"BTCUSDT": ()}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11), strategy_template=_weighted_strategy_template_fixture(),
    )

    assert (result.status, result.blockers, result.variants) == (
        "FAIL", ("PROFILE:" + WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE,), (),
    )


def test_build_adapter_rejects_candidate_capacity_mismatch_with_enriched_source(monkeypatch):
    source = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
        "strategy_orders": ({"order_id": 1, "open_ma_len": 34, "shift_bp": 50, "lot_x": Decimal("1")},),
    }
    selected = (source,)
    enriched = (dict(source, position_size_usdt=Decimal("401"), planned_leverage=Decimal("9")),)
    candidate = candidate_search.PortfolioCandidate(
        schema_version="portfolio_candidate_v1", profile_id="BALANCED", scenario_id="BALANCED",
        identity="candidate-BALANCED", members=(
            {"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
             "x_usdt": Decimal("100"), "capacity_usdt": Decimal("400"), "priority": 3},
        ), metrics={"limiter_L": 2},
    )
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module, "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=enriched, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module, "_run_weighted_search",
        lambda *_args, **_kwargs: candidate_search.SearchResult(
            status="PASS", candidates=(candidate,), mode=CAMPAIGN_SEARCH_MODE,
        ),
    )

    result = build_portfolio_candidates(
        selected, _weighted_build_campaign(), capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11), strategy_template=_weighted_strategy_template_fixture(),
    )

    assert (result.status, result.blockers, result.variants) == (
        "FAIL", ("PROFILE:" + WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE,), (),
    )


@pytest.mark.parametrize(
    "planned_leverage",
    (None, Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")),
)
def test_build_strategy_payloads_requires_positive_joined_planned_leverage(planned_leverage):
    source = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
        "strategy_orders": ({"order_id": 1, "open_ma_len": 34, "shift_bp": 50, "lot_x": Decimal("1")},),
    }
    enriched = dict(source, position_size_usdt=Decimal("400"))
    if planned_leverage is not None:
        enriched["planned_leverage"] = planned_leverage
    member = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "x_usdt": Decimal("100"), "capacity_usdt": Decimal("400"), "priority": 3,
    }

    with pytest.raises(CampaignContractError) as error:
        adapter_module._build_strategy_payloads(
            _weighted_strategy_template_fixture(), (member,), (source,), (enriched,), Decimal("1000"), 2,
        )

    assert error.value.code == WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE


def test_build_strategy_payloads_rejects_nonmapping_generated_basic(monkeypatch):
    source = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
        "strategy_orders": ({"order_id": 1, "open_ma_len": 34, "shift_bp": 50, "lot_x": Decimal("1")},),
    }
    member = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "x_usdt": Decimal("100"), "capacity_usdt": Decimal("400"), "priority": 3,
    }
    template = _weighted_strategy_template_fixture()
    template["basic"] = []
    monkeypatch.setattr(
        adapter_module,
        "generate_strategy",
        lambda *_args, **_kwargs: {"basic": [], "exchange": {}, "mrs": {}},
    )

    with pytest.raises(CampaignContractError) as error:
        adapter_module._build_strategy_payloads(
            template, (member,), (source,),
            (dict(source, position_size_usdt=Decimal("400"), planned_leverage=Decimal("9")),),
            Decimal("1000"), 2,
        )

    assert error.value.code == WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE


@pytest.mark.parametrize("lot_x", (Decimal("0"), Decimal("-1"), Decimal("NaN")))
def test_build_strategy_payloads_requires_positive_finite_source_lot(lot_x):
    source = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
        "strategy_orders": ({"order_id": 1, "open_ma_len": 34, "shift_bp": 50, "lot_x": lot_x},),
    }
    member = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "x_usdt": Decimal("100"), "capacity_usdt": Decimal("400"), "priority": 3,
    }

    with pytest.raises(CampaignContractError) as error:
        adapter_module._build_strategy_payloads(
            _weighted_strategy_template_fixture(), (member,), (source,),
            (dict(source, position_size_usdt=Decimal("400"), planned_leverage=Decimal("9")),),
            Decimal("1000"), 2,
        )

    assert error.value.code == WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE


@pytest.mark.parametrize("shift_bp", (10000, 10001))
def test_build_strategy_payloads_rejects_nonpositive_long_entry_multiplier(shift_bp):
    source = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
        "strategy_orders": ({"order_id": 1, "open_ma_len": 34, "shift_bp": shift_bp, "lot_x": Decimal("1")},),
    }
    member = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "x_usdt": Decimal("100"), "capacity_usdt": Decimal("400"), "priority": 3,
    }

    with pytest.raises(CampaignContractError) as error:
        adapter_module._build_strategy_payloads(
            _weighted_strategy_template_fixture(), (member,), (source,),
            (dict(source, position_size_usdt=Decimal("400"), planned_leverage=Decimal("9")),),
            Decimal("1000"), 2,
        )

    assert error.value.code == WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE


def test_build_strategy_payloads_rejects_repeated_positive_x_member_identity():
    source = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
        "strategy_orders": ({"order_id": 1, "open_ma_len": 34, "shift_bp": 50, "lot_x": Decimal("1")},),
    }
    member = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "x_usdt": Decimal("100"), "capacity_usdt": Decimal("400"), "priority": 3,
    }

    with pytest.raises(CampaignContractError) as error:
        adapter_module._build_strategy_payloads(
            _weighted_strategy_template_fixture(), (member, dict(member)), (source,),
            (dict(source, position_size_usdt=Decimal("400"), planned_leverage=Decimal("9")),),
            Decimal("1000"), 2,
        )

    assert error.value.code == WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE


def test_build_adapter_rejects_template_branch_with_no_positive_allocations(monkeypatch):
    source = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
        "strategy_orders": ({"order_id": 1, "open_ma_len": 34, "shift_bp": 50, "lot_x": Decimal("1")},),
    }
    member = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "x_usdt": Decimal("0"), "capacity_usdt": Decimal("400"), "priority": 3,
    }
    candidate = candidate_search.PortfolioCandidate(
        schema_version="portfolio_candidate_v1", profile_id="BALANCED", scenario_id="BALANCED",
        identity="candidate-BALANCED", members=(member,), metrics={"limiter_L": 2},
    )
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module, "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="PASS", rows=(dict(source, position_size_usdt=Decimal("400"), planned_leverage=Decimal("9")),),
            exclusions=(), reason=None,
        ),
    )
    monkeypatch.setattr(
        adapter_module, "_run_weighted_search",
        lambda *_args, **_kwargs: candidate_search.SearchResult(
            status="PASS", candidates=(candidate,), mode=CAMPAIGN_SEARCH_MODE,
        ),
    )

    result = build_portfolio_candidates(
        (source,), _weighted_build_campaign(), capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11), strategy_template=_weighted_strategy_template_fixture(),
    )

    assert (result.status, result.blockers, result.variants) == (
        "FAIL", ("PROFILE:WEIGHTED_PAYLOAD_INVALID",), (),
    )


def test_build_adapter_preserves_limiter_zero_as_off_in_strategy_payload(monkeypatch):
    source = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
        "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
        "strategy_orders": ({"order_id": 1, "open_ma_len": 34, "shift_bp": 50, "lot_x": Decimal("1")},),
    }
    member = {**source, "x_usdt": Decimal("100"), "capacity_usdt": Decimal("400"), "priority": 3}
    payload = adapter_module._build_strategy_payloads(
        _weighted_strategy_template_fixture(), (member,),
        (source,), (dict(source, position_size_usdt=Decimal("400"), planned_leverage=Decimal("9")),),
        Decimal("1000"), 0,
    )[0]

    assert payload["account"]["open_positions_limiter"] == 0


def test_build_adapter_rejects_duplicate_symbol_candidate_deterministically(monkeypatch):
    source_rows = (
        {
            "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101,
            "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
            "strategy_orders": ({"order_id": 1, "open_ma_len": 34, "shift_bp": 50, "lot_x": Decimal("1")},),
        },
        {
            "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 12, "result_id": 102,
            "timeframe": "3h", "close_ma_len": 55, "order_count": 1,
            "strategy_orders": ({"order_id": 1, "open_ma_len": 35, "shift_bp": 60, "lot_x": Decimal("1")},),
        },
    )
    candidate = candidate_search.PortfolioCandidate(
        schema_version="portfolio_candidate_v1", profile_id="BALANCED", scenario_id="BALANCED",
        identity="candidate-BALANCED", members=tuple({
            "symbol": "BTCUSDT", "side": "LONG", "strategy_id": strategy_id, "result_id": result_id,
            "x_usdt": Decimal("100"), "capacity_usdt": Decimal("400"), "priority": 3,
        } for strategy_id, result_id in ((11, 101), (12, 102))), metrics={"limiter_L": 2},
    )
    enriched = tuple(dict(row, position_size_usdt=Decimal("400"), planned_leverage=Decimal("9")) for row in source_rows)
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module, "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=enriched, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module, "_run_weighted_search",
        lambda *_args, **_kwargs: candidate_search.SearchResult(
            status="PASS", candidates=(candidate,), mode=CAMPAIGN_SEARCH_MODE,
        ),
    )

    result = build_portfolio_candidates(
        source_rows, _weighted_build_campaign(), capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(11), strategy_template=_weighted_strategy_template_fixture(),
    )

    assert (result.status, result.blockers, result.variants) == (
        "FAIL", ("PROFILE:WEIGHTED_SEARCH_CONFIG_INVALID",), (),
    )


def test_build_adapter_prepares_frozen_input_before_enrichment(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    campaign = weighted_campaign()
    campaign.update({
        "weighted_input_rows": (dict(selected[0], actions=(), equity=()),),
        "config_document": {
            "liquidity": {"maximum_age_hours": 2},
            "search": {
                "weighted_search": {"history_step_minutes": 5},
                "composition": {"parameters": {"minimum_common_days": 1}},
            },
        },
        "launch": {"profiles": ({"profile_id": "BALANCED", "equity_usdt": Decimal("1000")},)},
    })
    order = []

    def capture_prepare(*args):
        order.append("prepare")
        return object()

    def capture_enrich(*args, **kwargs):
        assert order == ["prepare"]
        order.append("enrich")
        return SimpleNamespace(status="FAIL", exclusions=(), reason="NO_ENRICHED_ROWS")

    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", capture_prepare)
    monkeypatch.setattr(adapter_module, "enrich_finalist_rows", capture_enrich)

    result = build_portfolio_candidates(
        selected,
        campaign,
        capacities={},
        reference=None,
        mark_prices={},
        spread_observations={},
        spread_history_statuses={"BTCUSDT": "READY"},
        now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert (result.status, result.blockers, order) == ("FAIL", ("NO_ENRICHED_ROWS",), ["prepare", "enrich"])


def test_build_adapter_missing_margin_blocks_profile_without_search(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    members = (dict(selected[0], position_size_usdt=Decimal("123")),)
    calls = []
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module,
        "enrich_finalist_rows",
        lambda *args, **kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None),
    )
    monkeypatch.setattr(adapter_module, "_run_weighted_search", lambda *_args, **_kwargs: calls.append(True))

    result = build_portfolio_candidates(
        selected,
        _weighted_build_campaign(),
        capacities={},
        reference=None,
        mark_prices={},
        spread_observations={},
        spread_history_statuses={"BTCUSDT": "READY"},
        now_ms=0,
        workers=3,
        margin_coefficients=None,
    )

    assert result.status == "FAIL"
    assert result.blockers == ("PROFILE:MARGIN_BOUND_UNAVAILABLE",)
    assert calls == []


def test_build_adapter_prefixes_search_failure_and_never_falls_back(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    members = (dict(selected[0], position_size_usdt=Decimal("123")),)
    calls = []
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module,
        "enrich_finalist_rows",
        lambda *args, **kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module,
        "_run_weighted_search",
        lambda *_args, **_kwargs: calls.append(True) or candidate_search.SearchResult(
            status="FAIL", reason="TARGET_INFEASIBLE", mode="WEIGHTED_V1"
        ),
    )
    monkeypatch.setattr(candidate_search, "search_portfolio_candidates", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy fallback reached")))

    result = build_portfolio_candidates(
        selected,
        _weighted_build_campaign(),
        capacities={},
        reference=None,
        mark_prices={},
        spread_observations={},
        spread_history_statuses={"BTCUSDT": "READY"},
        now_ms=0,
        workers=3,
        margin_coefficients=_margin_evidence(11),
    )

    assert (result.status, result.blockers, result.variants) == (
        "FAIL", ("PROFILE:TARGET_INFEASIBLE",), ()
    )
    assert calls == [True]


def test_build_adapter_maps_unexpected_weighted_search_exception_to_profile_blocker(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    members = (dict(selected[0], position_size_usdt=Decimal("123")),)

    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module,
        "enrich_finalist_rows",
        lambda *_args, **_kwargs: SimpleNamespace(status="PASS", rows=members, exclusions=(), reason=None),
    )
    monkeypatch.setattr(
        adapter_module,
        "_run_weighted_search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("search exploded")),
    )

    result = build_portfolio_candidates(
        selected,
        _weighted_build_campaign(),
        capacities={},
        reference=None,
        mark_prices={},
        spread_observations={},
        spread_history_statuses={"BTCUSDT": "READY"},
        now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert (result.status, result.blockers, result.variants) == (
        "FAIL", ("PROFILE:WEIGHTED_SEARCH_FAILED",), (),
    )


def test_build_adapter_maps_unexpected_enrichment_exception_to_single_sizing_blocker(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)

    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: object())
    monkeypatch.setattr(
        adapter_module,
        "enrich_finalist_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("enrichment exploded")),
    )
    campaign = _weighted_build_campaign()
    campaign["launch"] = {"profiles": (
        {"profile_id": "BALANCED", "equity_usdt": Decimal("1000")},
        {"profile_id": "AGGRESSIVE", "equity_usdt": Decimal("1000")},
    )}

    result = build_portfolio_candidates(
        selected,
        campaign,
        capacities={},
        reference=None,
        mark_prices={},
        spread_observations={},
        spread_history_statuses={"BTCUSDT": "READY"},
        now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert (result.status, result.blockers, result.variants) == (
        "FAIL", ("POSITION_SIZING_FAILED",), (),
    )


@pytest.mark.parametrize("statuses", (None, []), ids=("none", "list"))
def test_build_adapter_rejects_non_mapping_spread_history_statuses_before_preparation(monkeypatch, statuses):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    calls = []
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: calls.append("prepare"))
    monkeypatch.setattr(adapter_module, "enrich_finalist_rows", lambda *_args, **_kwargs: calls.append("enrich"))

    result = build_portfolio_candidates(
        selected,
        _weighted_build_campaign(),
        capacities={},
        reference=None,
        mark_prices={},
        spread_observations={},
        spread_history_statuses=statuses,
        now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert (result.status, result.blockers) == ("FAIL", ("SPREAD_HISTORY_STATUS_UNKNOWN",))
    assert result.excluded == ()
    assert result.variants == ()
    assert calls == []


def test_build_adapter_excludes_symbols_with_non_ok_spread_history_before_preparation(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    calls = []
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: calls.append("prepare"))

    result = build_portfolio_candidates(
        selected,
        _weighted_build_campaign(),
        capacities={},
        reference=None,
        mark_prices={},
        spread_observations={},
        spread_history_statuses={"BTCUSDT": "UNKNOWN"},
        now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert result.status == "FAIL"
    assert result.blockers == ("SPREAD_HISTORY_STATUS_UNKNOWN",)
    assert result.excluded == ({
        "strategy_id": 11,
        "result_id": 101,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "reason": "SPREAD_HISTORY_STATUS_UNKNOWN",
    },)
    assert result.variants == ()
    assert calls == []


def test_build_adapter_excludes_symbols_missing_spread_history_status_before_preparation(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    calls = []
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: calls.append("prepare"))

    result = build_portfolio_candidates(
        selected,
        _weighted_build_campaign(),
        capacities={},
        reference=None,
        mark_prices={},
        spread_observations={},
        spread_history_statuses={},
        now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert result.status == "FAIL"
    assert result.blockers == ("SPREAD_HISTORY_STATUS_UNKNOWN",)
    assert result.excluded[0]["symbol"] == "BTCUSDT"
    assert result.excluded[0]["reason"] == "SPREAD_HISTORY_STATUS_UNKNOWN"
    assert calls == []


def test_build_adapter_preserves_known_spread_history_status_reason(monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 101},)
    monkeypatch.setattr(adapter_module, "_prepare_frozen_weighted_input", lambda *_args: pytest.fail("prepare reached"))

    result = build_portfolio_candidates(
        selected,
        _weighted_build_campaign(),
        capacities={},
        reference=None,
        mark_prices={},
        spread_observations={},
        spread_history_statuses={"BTCUSDT": "OVERLAPS_SPREAD"},
        now_ms=0,
        margin_coefficients=_margin_evidence(11),
    )

    assert result.blockers == ("SPREAD_HISTORY_STATUS_OVERLAPS_SPREAD",)
    assert result.excluded[0]["reason"] == "SPREAD_HISTORY_STATUS_OVERLAPS_SPREAD"


def test_runtime_entrypoints_remain_gate_only_when_weighted_search_is_patched(monkeypatch, tmp_path):
    monkeypatch.setattr(adapter_module, "weighted_search", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("weighted search reached")))
    request = weighted_campaign()

    build_result = build_portfolio_candidates(
        (), request, capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
    )
    run_result = run_portfolio_adapter((), request, workspace_root=tmp_path)

    assert (build_result.status, build_result.blockers) == ("FAIL", (WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE,))
    assert (run_result.status, run_result.blockers) == ("FAIL", ("WEIGHTED_SEARCH_CONFIG_INVALID",))


def test_build_adapter_rejects_legacy_campaign_before_any_work(monkeypatch):
    monkeypatch.setattr(candidate_search, "search_portfolio_candidates", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy search reached")))
    request = weighted_campaign()
    request["stage1_mode"] = None
    result = build_portfolio_candidates(
        (), request, capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
    )
    assert (result.status, result.blockers) == ("FAIL", ("CAMPAIGN_LEGACY_STAGE1_MODE_UNSUPPORTED",))


def test_runtime_adapter_blocks_weighted_campaign_before_fact_loading(tmp_path):
    request = weighted_campaign()
    result = run_portfolio_adapter(
        (), request, workspace_root=tmp_path,
    )

    assert result.status == "FAIL"
    assert result.blockers == ("WEIGHTED_SEARCH_CONFIG_INVALID",)


def test_weighted_payload_maps_one_long_member_without_legacy_or_raw_fields(monkeypatch):
    template = {
        "exchange": {"use_upnl": True, "use_frozen_balance": True, "symbol": "BTCUSDT"},
        "basic": {"use_fix": False, "use_long": True, "use_short": False, "balance_percentage_long": 100, "risk_long": "1", "max_balance": 0},
        "mrs": {"position_priority": 1, "nested": {"open_positions_limiter": 9}},
        "mrs3": {"legacy": {"k": 9, "size_composition_vector": [1, 2]}, "order_geometry": {"entry": [{"price": "100", "qty": "0.1"}], "exit": [{"price": "110", "qty": "0.1"}]}},
        "raw": {"actions": [{"minute": 1}], "equity": [{"minute": 1, "value": "1000"}], "returns": [{"value": "0.1"}]},
    }
    member = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "strategy_id": "strategy-1",
        "result_id": "result-1",
        "x_usdt": "100",
        "priority": 3,
        "actions": [{"minute": 1}],
        "equity": [{"minute": 1, "value": "1000"}],
    }

    def fail_legacy_sizing(*args, **kwargs):
        raise AssertionError("legacy sizing reached")

    monkeypatch.setattr(position_sizing, "size_composition_vector", fail_legacy_sizing)
    payload = build_weighted_strategy_payload(
        template,
        member,
        bank_usdt=Decimal("2000"),
        capacity_usdt=Decimal("400"),
        open_positions_limiter=3,
    )

    assert Decimal(payload["facts"]["q"]) == Decimal("0.05")
    assert type(payload["strategy"]["basic"]["balance_percentage_long"]) is int
    assert payload["strategy"]["basic"]["balance_percentage_long"] == 5
    assert Decimal(payload["strategy"]["basic"]["risk_long"]) == Decimal("1")
    assert type(payload["strategy"]["basic"]["max_balance"]) is int
    assert payload["strategy"]["basic"]["max_balance"] == 8000
    assert payload["strategy"]["mrs"]["position_priority"] == 3
    assert "position_priority" not in payload["strategy"]["basic"]
    assert payload["account"]["open_positions_limiter"] == 3
    assert "open_positions_limiter" not in payload["strategy"]
    assert payload["facts"] == {"B": "2000", "C": "400", "q": "0.05", "x": "100"}
    assert payload["strategy"]["mrs3"]["order_geometry"] == template["mrs3"]["order_geometry"]
    payload["strategy"]["mrs3"]["order_geometry"]["entry"][0]["qty"] = "9"
    assert template["mrs3"]["order_geometry"]["entry"][0]["qty"] == "0.1"
    assert template["basic"] == {"use_fix": False, "use_long": True, "use_short": False, "balance_percentage_long": 100, "risk_long": "1", "max_balance": 0}
    assert template["mrs"]["position_priority"] == 1

    def keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)

    payload_keys = set(keys(payload))
    strategy_keys = set(keys(payload["strategy"]))
    assert "k" not in payload_keys
    assert not payload_keys.intersection({"actions", "action_series", "minute_actions", "equity", "equity_series"})
    assert "raw" not in strategy_keys
    assert "open_positions_limiter" not in strategy_keys
    assert json.loads(json.dumps(payload)) == payload


def _weighted_payload_inputs():
    return (
        {
            "exchange": {"use_upnl": True, "use_frozen_balance": True, "symbol": "BTCUSDT"},
            "basic": {"use_fix": False, "use_long": True, "use_short": False, "balance_percentage_long": 100, "risk_long": "1", "max_balance": 0},
            "mrs": {"position_priority": 1},
            "mrs3": {"order_geometry": {"entry": [{"price": "100", "qty": "0.1"}]}},
        },
        {"symbol": "BTCUSDT", "side": "LONG", "x_usdt": "100", "priority": 3},
    )


def test_weighted_payload_uses_direct_formula_for_nonterminating_ratio():
    template, member = _weighted_payload_inputs()
    payload = build_weighted_strategy_payload(template, member, Decimal("3000"), Decimal("400"), 3)

    assert Decimal(payload["facts"]["q"]) == Decimal("1") / Decimal("30")
    assert isinstance(payload["strategy"]["basic"]["max_balance"], (int, float))
    assert isinstance(payload["strategy"]["basic"]["balance_percentage_long"], (int, float))
    assert payload["strategy"]["basic"]["balance_percentage_long"] == pytest.approx(float(Decimal("100") / Decimal("30")))


def test_weighted_payload_rejects_serialized_capacity_mismatch(monkeypatch):
    template, member = _weighted_payload_inputs()
    original_json_number = adapter_module._weighted_json_number

    def tamper_max_balance(value):
        converted = original_json_number(value)
        return converted + 1 if value == Decimal("12000") else converted

    monkeypatch.setattr(adapter_module, "_weighted_json_number", tamper_max_balance)
    with pytest.raises(CampaignContractError) as error:
        build_weighted_strategy_payload(template, member, Decimal("3000"), Decimal("400"), 3)
    assert error.value.code == "WEIGHTED_PAYLOAD_INVALID"


@pytest.mark.parametrize(("field", "value"), (("use_long", 1), ("use_short", 0), ("use_long", False), ("use_short", True)))
def test_weighted_payload_requires_exact_long_only_basic_flags(field, value):
    template, member = _weighted_payload_inputs()
    template["basic"][field] = value

    with pytest.raises(CampaignContractError) as error:
        build_weighted_strategy_payload(template, member, Decimal("2000"), Decimal("400"), 3)
    assert error.value.code == "WEIGHTED_PAYLOAD_INVALID"


@pytest.mark.parametrize("symbol_case", ("missing", "empty"))
def test_weighted_payload_rejects_missing_or_empty_member_symbol(symbol_case):
    template, member = _weighted_payload_inputs()
    if symbol_case == "missing":
        member.pop("symbol")
    else:
        member["symbol"] = ""

    with pytest.raises(CampaignContractError) as error:
        build_weighted_strategy_payload(template, member, Decimal("2000"), Decimal("400"), 3)
    assert error.value.code == "WEIGHTED_PAYLOAD_INVALID"


def test_weighted_payload_rewrites_basic_symbol_for_member_without_exchange_symbol_requirement():
    template, member = _weighted_payload_inputs()
    template["exchange"]["symbol"] = "PLACEHOLDER"
    member["symbol"] = "ETHUSDT"

    payload = build_weighted_strategy_payload(template, member, Decimal("2000"), Decimal("400"), 3)

    assert payload["strategy"]["basic"]["symbol"] == "ETHUSDT"
    assert payload["strategy"]["exchange"]["symbol"] == "PLACEHOLDER"


def test_weighted_payload_accepts_new_disk_template_for_btcusdt_fixture():
    template_path = Path(__file__).parents[1] / "templates" / "strategies" / "portfolio-weighted-mrs" / "base.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))
    member = {"symbol": "BTCUSDT", "side": "LONG", "x_usdt": "100", "priority": 3}

    payload = build_weighted_strategy_payload(template, member, Decimal("2000"), Decimal("400"), 3)

    assert payload["strategy"]["basic"]["symbol"] == "BTCUSDT"
    assert payload["strategy"]["mrs"]["position_priority"] == 3
    assert type(payload["strategy"]["mrs"]["position_priority"]) is int
    assert payload["strategy"]["exchange"]["use_upnl"] is True
    assert payload["strategy"]["exchange"]["use_frozen_balance"] is True
    assert "readback" not in payload


def test_weighted_payload_recomputes_decoded_facts_on_json_readback(monkeypatch):
    template, member = _weighted_payload_inputs()
    original_loads = adapter_module.json.loads

    def tampered_loads(value):
        decoded = original_loads(value)
        decoded["facts"]["B"] = "4000"
        return decoded

    monkeypatch.setattr(adapter_module.json, "loads", tampered_loads)
    with pytest.raises(CampaignContractError) as error:
        build_weighted_strategy_payload(template, member, Decimal("2000"), Decimal("400"), 3)
    assert error.value.code == "WEIGHTED_PAYLOAD_INVALID"


def _frozen_weighted_input_row(result_id=101):
    return {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "strategy_id": 1,
        "result_id": result_id,
        "actions": ("frozen-actions",),
        "equity": ("frozen-equity",),
        "metadata": "frozen-metadata",
    }


def _frozen_input_campaign(rows):
    return {
        "weighted_input_rows": rows,
        "config_document": {
            "search": {
                "weighted_search": {"history_step_minutes": 5},
                "composition": {"parameters": {"minimum_common_days": 1}},
            }
        },
    }


def test_prepare_frozen_weighted_input_uses_only_exact_snapshot_rows(monkeypatch):
    source = _frozen_weighted_input_row()
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 1, "result_id": 101, "equity": "mutable"},)
    calls = {}
    prepared = object()

    def capture_prepare(rows, **kwargs):
        calls["rows"] = rows
        calls["kwargs"] = kwargs
        return prepared

    monkeypatch.setattr(portfolio_input, "prepare_weighted_input", capture_prepare)
    payload = _prepare_frozen_weighted_input(selected, _frozen_input_campaign((source,)))

    assert payload is prepared
    assert calls == {"rows": (source,), "kwargs": {"history_step_minutes": 5, "minimum_common_days": 1}}
    assert isinstance(calls["rows"][0], MappingProxyType)
    assert isinstance(calls["rows"][0]["actions"], tuple)
    assert isinstance(calls["rows"][0]["equity"], tuple)
    assert source["equity"] == ("frozen-equity",)


def test_prepare_frozen_weighted_input_reads_minimum_days_from_composition_parameters(monkeypatch):
    source = _frozen_weighted_input_row()
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 1, "result_id": 101},)
    calls = {}

    def capture_prepare(rows, **kwargs):
        calls["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(portfolio_input, "prepare_weighted_input", capture_prepare)
    _prepare_frozen_weighted_input(
        selected,
        {
            "weighted_input_rows": (source,),
            "config_document": {
                "search": {
                    "weighted_search": {"history_step_minutes": 5},
                    "composition": {"parameters": {"minimum_common_days": 7}},
                }
            },
        },
    )

    assert calls["kwargs"] == {"history_step_minutes": 5, "minimum_common_days": 7}


def _real_frozen_weighted_input_row():
    return {
        **_frozen_weighted_input_row(),
        "report_start_utc": "2026-01-01T00:00:00Z",
        "report_end_utc": "2026-01-15T00:00:00Z",
        "actions": (
            {
                "action_index": 0,
                "timestamp_utc": "2026-01-01T00:05:00Z",
                "symbol": "BTCUSDT",
                "action": "opened",
                "post_size": "1",
                "post_side": "LONG",
                "balance": "100",
                "pnl": "0",
                "fee": "0",
            },
        ),
        "equity": (
            {"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},
            {"timestamp_utc": "2026-01-01T00:05:00Z", "equity": "110"},
        ),
    }


def test_prepare_frozen_weighted_input_reaches_real_preparer_and_accepts_full_campaign():
    source = _real_frozen_weighted_input_row()
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 1, "result_id": 101},)
    campaign = weighted_campaign()
    frozen_campaign = _frozen_input_campaign((source,))
    campaign["weighted_input_rows"] = frozen_campaign["weighted_input_rows"]
    campaign["config_document"] = {
        "search": {
            "weighted_search": {
                "history_step_minutes": frozen_campaign["config_document"]["search"]["weighted_search"]["history_step_minutes"],
            },
            "composition": {
                "parameters": {
                    "minimum_common_days": frozen_campaign["config_document"]["search"]["composition"]["parameters"]["minimum_common_days"],
                }
            },
        }
    }

    validate_campaign_contract(campaign)
    assert isinstance(campaign["config_document"]["search"]["weighted_search"]["history_step_minutes"], int)
    assert campaign["config_document"]["search"]["weighted_search"]["history_step_minutes"] > 0
    assert isinstance(campaign["config_document"]["search"]["composition"]["parameters"]["minimum_common_days"], int)
    assert campaign["config_document"]["search"]["composition"]["parameters"]["minimum_common_days"] > 0
    prepared = _prepare_frozen_weighted_input(selected, campaign)

    parameters = inspect.signature(portfolio_input.prepare_weighted_input).parameters
    assert "history_step_minutes" in parameters
    assert "minimum_common_days" in parameters
    assert isinstance(prepared, portfolio_input.PreparedWeightedInput)
    assert prepared.strategy_ids == (1,)


def test_build_adapter_rejects_selected_identity_mismatch_before_enrichment_or_search(monkeypatch):
    source = _real_frozen_weighted_input_row()
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 1, "result_id": 999},)
    campaign = weighted_campaign()
    campaign.update({
        "weighted_input_rows": (source,),
        "config_document": {
            "liquidity": {"maximum_age_hours": 2},
            "search": {
                "weighted_search": {"history_step_minutes": 5},
                "composition": {"parameters": {"minimum_common_days": 1}},
            },
        },
        "launch": {"profiles": ({"profile_id": "BALANCED", "equity_usdt": Decimal("1000")},)},
    })
    calls = []
    monkeypatch.setattr(adapter_module, "enrich_finalist_rows", lambda *_args, **_kwargs: calls.append("enrich"))
    monkeypatch.setattr(adapter_module, "_run_weighted_search", lambda *_args, **_kwargs: calls.append("search"))

    result = build_portfolio_candidates(
        selected, campaign, capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={"BTCUSDT": "READY"}, now_ms=0,
        margin_coefficients=_margin_evidence(1),
    )

    assert (result.status, result.blockers, result.variants, calls) == (
        "FAIL", (WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE,), (), [],
    )


@pytest.mark.parametrize("artifact", ((), (_frozen_weighted_input_row(), _frozen_weighted_input_row()), (_frozen_weighted_input_row(999),)))
def test_prepare_frozen_weighted_input_rejects_missing_duplicate_or_mismatched_rows(artifact, monkeypatch):
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 1, "result_id": 101},)
    monkeypatch.setattr(portfolio_input, "prepare_weighted_input", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("prepare reached")))

    with pytest.raises(CampaignContractError) as error:
        _prepare_frozen_weighted_input(selected, _frozen_input_campaign(artifact))
    assert error.value.code == WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE


def test_real_weighted_input_preparer_is_available_and_callable():
    assert callable(portfolio_input.prepare_weighted_input)


def test_weighted_input_snapshot_error_code_is_public_and_stable():
    assert adapter_module.WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE == "WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE"
    assert "WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE" in adapter_module.__all__
    assert adapter_module.WEIGHTED_INPUT_PREPARATION_FAILED == "WEIGHTED_INPUT_PREPARATION_FAILED"
    assert "WEIGHTED_INPUT_PREPARATION_FAILED" in adapter_module.__all__


def test_prepare_frozen_weighted_input_reports_preparation_failure_separately(monkeypatch):
    source = _frozen_weighted_input_row()
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 1, "result_id": 101},)

    def fail_prepare(*args, **kwargs):
        raise portfolio_input.PortfolioInputError("preparation failed", code="INVALID_SOURCE_VALUE")

    monkeypatch.setattr(portfolio_input, "prepare_weighted_input", fail_prepare)
    with pytest.raises(CampaignContractError) as error:
        _prepare_frozen_weighted_input(selected, _frozen_input_campaign((source,)))
    assert error.value.code == WEIGHTED_INPUT_PREPARATION_FAILED


def test_prepare_frozen_weighted_input_maps_ordinary_preparation_exception(monkeypatch):
    source = _frozen_weighted_input_row()
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 1, "result_id": 101},)

    monkeypatch.setattr(
        portfolio_input,
        "prepare_weighted_input",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("preparation exploded")),
    )
    with pytest.raises(CampaignContractError) as error:
        _prepare_frozen_weighted_input(selected, _frozen_input_campaign((source,)))
    assert error.value.code == WEIGHTED_INPUT_PREPARATION_FAILED


def test_prepare_frozen_weighted_input_maps_injected_campaign_contract_exception(monkeypatch):
    source = _frozen_weighted_input_row()
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 1, "result_id": 101},)

    monkeypatch.setattr(
        portfolio_input,
        "prepare_weighted_input",
        lambda *args, **kwargs: (_ for _ in ()).throw(CampaignContractError("preparer contract failure")),
    )
    with pytest.raises(CampaignContractError) as error:
        _prepare_frozen_weighted_input(selected, _frozen_input_campaign((source,)))
    assert error.value.code == WEIGHTED_INPUT_PREPARATION_FAILED


def test_prepare_frozen_weighted_input_does_not_catch_base_exception(monkeypatch):
    class Abort(BaseException):
        pass

    source = _frozen_weighted_input_row()
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 1, "result_id": 101},)
    monkeypatch.setattr(
        portfolio_input,
        "prepare_weighted_input",
        lambda *args, **kwargs: (_ for _ in ()).throw(Abort()),
    )
    with pytest.raises(Abort):
        _prepare_frozen_weighted_input(selected, _frozen_input_campaign((source,)))


@pytest.mark.parametrize(
    "config_document",
    (
        None,
        {},
        {"search": None},
        {"search": {}},
        {"search": {"weighted_search": None, "composition": {"parameters": {"minimum_common_days": 1}}}},
        {"search": {"weighted_search": {"history_step_minutes": 5}, "composition": None}},
        {"search": {"weighted_search": {"history_step_minutes": 5}, "composition": {}}},
        {"search": {"weighted_search": {"history_step_minutes": 5}, "composition": {"parameters": None}}},
        {"search": {"weighted_search": {"history_step_minutes": 5}, "composition": {"parameters": {}}}},
        {"search": {"history_step_minutes": 5, "minimum_common_days": 1}},
        {"search": {"weighted_search": {"history_step_minutes": 5, "minimum_common_days": 1}}},
    ),
)
def test_prepare_frozen_weighted_input_rejects_missing_strict_config(config_document, monkeypatch):
    source = _frozen_weighted_input_row()
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 1, "result_id": 101},)
    called = []
    monkeypatch.setattr(portfolio_input, "prepare_weighted_input", lambda *args, **kwargs: called.append(True))

    with pytest.raises(CampaignContractError) as error:
        _prepare_frozen_weighted_input(selected, {"weighted_input_rows": (source,), "config_document": config_document})
    assert error.value.code == WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE
    assert called == []


@pytest.mark.parametrize(
    "campaign",
    (
        {"weighted_input_rows": (), "history_step_minutes": 5, "minimum_common_days": 1},
        {
            "weighted_input_rows": (),
            "config_document": {"search": {"weighted_search": {"history_step_minutes": 5, "minimum_common_days": 1}}},
        },
    ),
)
def test_prepare_frozen_weighted_input_does_not_use_config_aliases(campaign, monkeypatch):
    source = _frozen_weighted_input_row()
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 1, "result_id": 101},)
    called = []
    monkeypatch.setattr(portfolio_input, "prepare_weighted_input", lambda *args, **kwargs: called.append(True))
    campaign["weighted_input_rows"] = (source,)

    with pytest.raises(CampaignContractError) as error:
        _prepare_frozen_weighted_input(selected, campaign)
    assert error.value.code == WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE
    assert called == []


@pytest.mark.parametrize(
    ("location", "key"),
    (
        ("weighted_search", "minimum_common_days"),
        ("composition_parameters", "history_step_minutes"),
        ("search", "history_step_minutes"),
        ("search", "minimum_common_days"),
        ("composition", "history_step_minutes"),
        ("composition", "minimum_common_days"),
        ("config_document", "history_step_minutes"),
        ("config_document", "minimum_common_days"),
        ("campaign", "history_step_minutes"),
        ("campaign", "minimum_common_days"),
    ),
)
def test_prepare_frozen_weighted_input_rejects_canonical_plus_config_alias(location, key, monkeypatch):
    source = _frozen_weighted_input_row()
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 1, "result_id": 101},)
    campaign = _frozen_input_campaign((source,))
    search = campaign["config_document"]["search"]
    if location == "weighted_search":
        search[location][key] = 99
    elif location == "composition_parameters":
        search["composition"]["parameters"][key] = 99
    elif location == "composition":
        search[location][key] = 99
    elif location == "config_document":
        campaign["config_document"][key] = 99
    elif location == "campaign":
        campaign[key] = 99
    else:
        search[key] = 99
    called = []
    monkeypatch.setattr(portfolio_input, "prepare_weighted_input", lambda *args, **kwargs: called.append(True))

    with pytest.raises(CampaignContractError) as error:
        _prepare_frozen_weighted_input(selected, campaign)
    assert error.value.code == WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE
    assert called == []


@pytest.mark.parametrize("location", ("history_step_minutes", "minimum_common_days"))
@pytest.mark.parametrize("invalid_value", (0, -1, True, "5", 5.0))
def test_prepare_frozen_weighted_input_rejects_invalid_strict_config_value(location, invalid_value, monkeypatch):
    source = _frozen_weighted_input_row()
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 1, "result_id": 101},)
    weighted = {"history_step_minutes": 5}
    composition_parameters = {"minimum_common_days": 1}
    if location == "history_step_minutes":
        weighted[location] = invalid_value
    else:
        composition_parameters[location] = invalid_value
    called = []
    monkeypatch.setattr(portfolio_input, "prepare_weighted_input", lambda *args, **kwargs: called.append(True))

    with pytest.raises(CampaignContractError) as error:
        _prepare_frozen_weighted_input(
            selected,
            {
                "weighted_input_rows": (source,),
                "config_document": {
                    "search": {
                        "weighted_search": weighted,
                        "composition": {"parameters": composition_parameters},
                    }
                },
            },
        )
    assert error.value.code == WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE
    assert called == []


@pytest.mark.parametrize(
    "case",
    (
        "selected_nonmapping", "selected_short", "selected_bool_id", "selected_string_id", "selected_blank_symbol",
        "selected_str", "selected_bytes",
        "frozen_nonmapping", "frozen_missing_actions", "frozen_invalid_actions", "frozen_missing_equity", "frozen_invalid_equity",
        "frozen_short", "frozen_bool_id", "frozen_string_id", "frozen_blank_symbol",
        "artifact_absent", "artifact_none", "artifact_string", "selected_duplicate",
    ),
)
def test_prepare_frozen_weighted_input_rejects_invalid_snapshot_or_selection(case, monkeypatch):
    source = _frozen_weighted_input_row()
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 1, "result_id": 101},)
    artifact = (source,)
    if case == "selected_nonmapping":
        selected = (None,)
    elif case == "selected_short":
        selected = (dict(selected[0], side="SHORT"),)
    elif case == "selected_bool_id":
        selected = (dict(selected[0], strategy_id=True),)
    elif case == "selected_string_id":
        selected = (dict(selected[0], result_id="101"),)
    elif case == "selected_blank_symbol":
        selected = (dict(selected[0], symbol=" "),)
    elif case == "selected_str":
        selected = "rows"
    elif case == "selected_bytes":
        selected = b"rows"
    elif case == "frozen_nonmapping":
        artifact = (None,)
    elif case == "frozen_missing_actions":
        artifact = (dict(source, actions=None),)
    elif case == "frozen_invalid_actions":
        artifact = (dict(source, actions="actions"),)
    elif case == "frozen_missing_equity":
        artifact = (dict(source, equity=None),)
    elif case == "frozen_invalid_equity":
        artifact = (dict(source, equity="equity"),)
    elif case == "frozen_short":
        artifact = (dict(source, side="SHORT"),)
    elif case == "frozen_bool_id":
        artifact = (dict(source, strategy_id=True),)
    elif case == "frozen_string_id":
        artifact = (dict(source, result_id="101"),)
    elif case == "frozen_blank_symbol":
        artifact = (dict(source, symbol=" "),)
    elif case == "artifact_none":
        artifact = None
    elif case == "artifact_string":
        artifact = "rows"
    elif case == "selected_duplicate":
        selected = (selected[0], dict(selected[0]))
    campaign = _frozen_input_campaign(artifact)
    if case == "artifact_absent":
        del campaign["weighted_input_rows"]
    if case == "artifact_string":
        campaign["weighted_input_rows"] = artifact
    called = []
    monkeypatch.setattr(portfolio_input, "prepare_weighted_input", lambda *args, **kwargs: called.append(True))

    with pytest.raises(CampaignContractError) as error:
        _prepare_frozen_weighted_input(selected, campaign)
    assert error.value.code == WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE
    assert called == []


def test_prepare_frozen_weighted_input_rejects_empty_selection_before_preparer(monkeypatch):
    called = []
    monkeypatch.setattr(portfolio_input, "prepare_weighted_input", lambda *args, **kwargs: called.append(True))

    with pytest.raises(CampaignContractError) as error:
        _prepare_frozen_weighted_input((), _frozen_input_campaign((_frozen_weighted_input_row(),)))
    assert error.value.code == WEIGHTED_INPUT_SNAPSHOT_UNAVAILABLE
    assert called == []


def test_prepare_frozen_weighted_input_freezes_series_before_preparer(monkeypatch):
    source = _frozen_weighted_input_row()
    source["actions"] = [{"event": "open"}]
    source["equity"] = [{"value": "100"}]
    selected = ({"symbol": "BTCUSDT", "side": "LONG", "strategy_id": 1, "result_id": 101},)

    def mutate_prepared(rows, **kwargs):
        with pytest.raises(TypeError):
            rows[0]["actions"][0]["event"] = "close"
        with pytest.raises(TypeError):
            rows[0]["equity"] += ({"value": "0"},)
        return object()

    monkeypatch.setattr(portfolio_input, "prepare_weighted_input", mutate_prepared)
    _prepare_frozen_weighted_input(selected, _frozen_input_campaign((source,)))
    assert source["actions"] == [{"event": "open"}]
    assert source["equity"] == [{"value": "100"}]


def test_weighted_payload_allows_allocation_above_bank():
    template, member = _weighted_payload_inputs()
    payload = build_weighted_strategy_payload(template, dict(member, x_usdt="3000"), Decimal("2000"), Decimal("400"), 3)

    assert Decimal(payload["facts"]["q"]) == Decimal("1.5")
    assert Decimal(payload["strategy"]["basic"]["balance_percentage_long"]) == Decimal("150")
    assert payload["strategy"]["basic"]["max_balance"] == pytest.approx(float(Decimal("400") / Decimal("1.5")))


@pytest.mark.parametrize(
    ("bank", "capacity", "x"),
    (
        ("1e39", "400", "100"),
        ("2000", "1e39", "100"),
        ("2000", "400", "1e39"),
        ("1e-39", "400", "100"),
        ("2000", "1e-39", "100"),
        ("2000", "400", "1e-39"),
    ),
)
def test_weighted_payload_rejects_extreme_decimal_exponents(bank, capacity, x):
    template, member = _weighted_payload_inputs()

    with pytest.raises(CampaignContractError) as error:
        build_weighted_strategy_payload(template, dict(member, x_usdt=x), Decimal(bank), Decimal(capacity), 3)
    assert error.value.code == "WEIGHTED_PAYLOAD_INVALID"


@pytest.mark.parametrize("field", ("bank", "capacity", "x"))
def test_weighted_decimal_rejects_pathological_low_exponent_before_formatting(field):
    value_by_field = {"bank": "1e-999999999", "capacity": "1e-999999999", "x": "1e-999999999"}
    with pytest.raises(CampaignContractError) as error:
        adapter_module._weighted_decimal(value_by_field[field])
    assert error.value.code == "WEIGHTED_PAYLOAD_INVALID"


def test_weighted_payload_rejects_derived_value_out_of_range_before_formatting(monkeypatch):
    template, member = _weighted_payload_inputs()
    original_format = adapter_module._weighted_decimal_text

    def fail_pathological_format(value):
        if value.adjusted() > 38:
            raise AssertionError("pathological derived value reached formatting")
        return original_format(value)

    monkeypatch.setattr(adapter_module, "_weighted_decimal_text", fail_pathological_format)
    with pytest.raises(CampaignContractError) as error:
        build_weighted_strategy_payload(template, dict(member, x_usdt="1"), Decimal("1e30"), Decimal("1e30"), 3)
    assert error.value.code == "WEIGHTED_PAYLOAD_INVALID"

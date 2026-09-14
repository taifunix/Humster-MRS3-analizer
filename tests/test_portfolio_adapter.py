import pytest
from mrs3.portfolio import candidate_search, market_snapshot, minute_capacity, spread_screen

from mrs3.portfolio.adapter import (
    CAMPAIGN_CONTRACT_VERSION,
    CAMPAIGN_SEARCH_MODE,
    CAMPAIGN_WEIGHTED_ALGO_VERSION,
    CampaignContractError,
    build_portfolio_candidates,
    run_portfolio_adapter,
    validate_campaign_contract,
)
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
    assert result.blockers == ("WEIGHTED_SEARCH_NOT_IMPLEMENTED",)
    assert calls == []


def test_build_adapter_blocks_valid_weighted_campaign_before_legacy_search(monkeypatch):
    monkeypatch.setattr(candidate_search, "search_portfolio_candidates", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy search reached")))
    result = build_portfolio_candidates(
        (), weighted_campaign(), capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={}, now_ms=0,
    )
    assert (result.status, result.blockers) == ("FAIL", ("WEIGHTED_SEARCH_NOT_IMPLEMENTED",))


def test_build_adapter_rejects_legacy_campaign_before_any_work(monkeypatch):
    monkeypatch.setattr(candidate_search, "search_portfolio_candidates", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy search reached")))
    request = weighted_campaign()
    request["stage1_mode"] = None
    result = build_portfolio_candidates(
        (), request, capacities={}, reference=None, mark_prices={},
        spread_observations={}, spread_history_statuses={}, now_ms=0,
    )
    assert (result.status, result.blockers) == ("FAIL", ("CAMPAIGN_LEGACY_STAGE1_MODE_UNSUPPORTED",))


def test_runtime_adapter_blocks_weighted_campaign_before_fact_loading(tmp_path):
    request = weighted_campaign()
    result = run_portfolio_adapter(
        (), request, workspace_root=tmp_path,
    )

    assert result.status == "FAIL"
    assert result.blockers == ("WEIGHTED_SEARCH_NOT_IMPLEMENTED",)

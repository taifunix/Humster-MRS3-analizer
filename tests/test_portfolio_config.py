import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
import mrs3.portfolio.config as config_module

from mrs3.portfolio.config import (
    INDIVIDUAL_DD_DEFAULTS,
    POLICY_VERSION,
    RANKING_METRICS,
    SCHEMA_VERSION,
    SIZING_MODE,
    WEIGHTED_SEARCH_DEFAULTS,
    PortfolioConfigError,
    load_portfolio_config,
    migrate_portfolio_config_document,
)


def _money(amount, currency="USDT"):
    return {"amount": amount, "currency": currency}


def _profile():
    return {
        "pnl": {"policy_id": "operator_supplied_pnl_v1", "parameters": {"operator_supplied": True}},
        "liquidity": {"policy_id": "operator_supplied_liquidity_v1", "parameters": {"operator_supplied": True}},
        "ranking": {
            "id": "operator_supplied_ranking_v1",
            "parameters": {"operator_supplied": True},
            "top_n": 1,
        },
    }


def _v1_config():
    return {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "algorithm_versions": {"sizing": "portfolio_optimizer_sizing_v1", "ranking": "portfolio_optimizer_ranking_v1"},
        "inputs": {
            "performance_db": "./operator-supplied/performance.duckdb",
            "portfolio_db": "./operator-supplied/portfolio.duckdb",
            "collector_root": "./operator-supplied/collector",
            "approved_templates": ["./operator-supplied/templates/strategy.json"],
        },
        "scenarios": {
            name: {
                "account": f"operator-supplied-{name.lower()}",
                "deposit": _money(10000),
                "collateral": _money(10000),
                "max_balance": _money(10000),
                "sizing": {"upper_bound": _money(10000), "grid": [_money(1000), _money(5000), _money(10000)]},
            }
            for name in ("AGGRESSIVE", "BALANCED", "CONSERVATIVE")
        },
        "search": {
            "universe": {"policy_id": "operator_supplied_universe_v1", "parameters": {"operator_supplied": True}},
            "composition": {"policy_id": "operator_supplied_composition_v1", "parameters": {"operator_supplied": True}},
            "sizing": {"policy_id": "operator_supplied_sizing_v1", "parameters": {"operator_supplied": True}},
            "limiter": {"policy_id": "operator_supplied_limiter_v1", "parameters": {"operator_supplied": True}},
            "priority": {"policy_id": "operator_supplied_priority_v1", "parameters": {"operator_supplied": True}},
            "seed": 1,
            "rounds": 1,
            "total_test_budget": 1,
        },
        "research": {
            "development_window": {"value": 1, "unit": "days"},
            "validation_window": {"value": 1, "unit": "days"},
            "warmup": {"value": 0, "unit": "days"},
            "boundary": "operator-supplied",
            "evidence_minimum": {"value": 1, "unit": "count"},
        },
        "liquidity": {"policy_id": "operator_supplied_liquidity_v1", "parameters": {"operator_supplied": True}},
        "margin": {"policy_id": "operator_supplied_margin_v1", "parameters": {"operator_supplied": True}},
        "profiles": {name: _profile() for name in ("AGGRESSIVE", "BALANCED", "CONSERVATIVE")},
        "runner": {"target": "local", "root": "./operator-supplied/bot-root", "timeout": {"value": 1, "unit": "seconds"}, "retries": 0},
    }


def _v2_config():
    return migrate_portfolio_config_document(_v1_config())[0]


def test_v1_migration_adds_weighted_search_defaults_and_rejects_invalid_value(tmp_path):
    migrated, changed = migrate_portfolio_config_document(_v1_config())

    assert changed
    assert migrated["search"]["weighted_search"] == {
        "history_step_minutes": 5,
        "lp_solutions_per_profile": 20,
        "max_targets": 8,
        "bootstrap_scenarios_per_block": 1_000,
        "bootstrap_diagnostic_scenarios": 100,
        "wall_time_seconds": 900,
        "solver_time_seconds": 30,
        "limiter_step": 1,
        "limiter_controls": 2,
        "limiter_stress_pct": 1.5,
        "priority_groups": 5,
        "priority_beta": 0.5,
        "priority_close_ratio": 2,
    }

    migrated["search"]["weighted_search"]["bootstrap_scenarios_per_block"] = 0
    with pytest.raises(PortfolioConfigError, match="bootstrap_scenarios_per_block"):
        load_portfolio_config(_write(tmp_path, migrated))


def test_v2_loader_adds_missing_weighted_search_defaults_in_memory(tmp_path):
    value = _v2_config()
    value["search"].pop("weighted_search")
    path = _write(tmp_path, value)
    before = path.read_bytes()

    loaded = load_portfolio_config(path)

    expected = dict(WEIGHTED_SEARCH_DEFAULTS)
    assert loaded.search["weighted_search"] == expected
    assert "weighted_search" not in value["search"]
    assert path.read_bytes() == before


def test_v2_migration_discards_only_retired_weighted_search_keys(tmp_path):
    value = _v2_config()
    retired = {
        "repair_attempts": 3,
        "additional_passes": 1,
        "base_vectors": 2_000_000,
        "scenarios": 3_000_000,
        "cdar_pct": 80,
        "diagnostic_cdar_pct": 90,
        "alternatives_per_profile": 2,
        "p30_tolerance_pct": 5,
        "bootstrap_block_days": [1, 3, 7],
        "bootstrap_p95": True,
        "bootstrap_low_block_common_days": 10,
        "scale_warning_multiple": 10,
        "api_requests_per_second": 2,
        "api_concurrency": 1,
        "api_retries": 3,
        "reference_max_age_hours": 2,
        "csv_download_concurrency": 2,
    }
    value["search"]["weighted_search"].update(retired)

    migrated, changed = migrate_portfolio_config_document(value)

    assert not changed
    assert not (retired.keys() & migrated["search"]["weighted_search"].keys())
    loaded = load_portfolio_config(_write(tmp_path, value))
    assert loaded.search["weighted_search"] == WEIGHTED_SEARCH_DEFAULTS


def test_v2_migration_backfills_missing_retained_weighted_keys_and_preserves_values(tmp_path):
    value = _v2_config()
    value["search"]["weighted_search"] = {
        "history_step_minutes": 9,
        "repair_attempts": 3,
    }

    migrated, changed = migrate_portfolio_config_document(value)

    assert not changed
    assert migrated["search"]["weighted_search"] == {
        **WEIGHTED_SEARCH_DEFAULTS,
        "history_step_minutes": 9,
    }
    loaded = load_portfolio_config(_write(tmp_path, value))
    assert loaded.search["weighted_search"]["history_step_minutes"] == 9
    assert loaded.search["weighted_search"]["lp_solutions_per_profile"] == 20


@pytest.mark.parametrize("malformed_search", ["missing", None, []])
def test_v2_migration_preserves_missing_or_nonmapping_search_for_parser_rejection(tmp_path, malformed_search):
    original = _v2_config()
    if malformed_search == "missing":
        del original["search"]
    else:
        original["search"] = malformed_search
    before = json.loads(json.dumps(original))

    migrated, changed = migrate_portfolio_config_document(original)

    assert not changed
    assert original == before
    if malformed_search == "missing":
        assert "search" not in migrated
    else:
        assert migrated["search"] == malformed_search
    with pytest.raises(PortfolioConfigError, match="search"):
        load_portfolio_config(_write(tmp_path, migrated))


@pytest.mark.parametrize("malformed_search", ["missing", None, []])
def test_v1_migration_preserves_missing_or_nonmapping_search_for_parser_rejection(tmp_path, malformed_search):
    original = _v1_config()
    if malformed_search == "missing":
        del original["search"]
    else:
        original["search"] = malformed_search
    before = json.loads(json.dumps(original))

    migrated, changed = migrate_portfolio_config_document(original)

    assert changed
    assert original == before
    if malformed_search == "missing":
        assert "search" not in migrated
    else:
        assert migrated["search"] == malformed_search
    with pytest.raises(PortfolioConfigError, match="search"):
        load_portfolio_config(_write(tmp_path, migrated))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lp_solutions_per_profile", 21),
        ("limiter_stress_pct", 100),
        ("bootstrap_diagnostic_scenarios", 1001),
        ("limiter_controls", -1),
        ("limiter_controls", 3),
        ("priority_groups", 0),
        ("priority_groups", 6),
        ("priority_beta", -0.1),
        ("priority_beta", 1.1),
        ("priority_close_ratio", 1),
        ("max_targets", 0),
        ("max_targets", 9),
        ("limiter_stress_pct", -1),
    ],
)
def test_weighted_search_plan_bounds_fail_closed(tmp_path, field, value):
    config = _v2_config()
    config["search"]["weighted_search"][field] = value

    with pytest.raises(PortfolioConfigError, match=field):
        load_portfolio_config(_write(tmp_path, config))


@pytest.mark.parametrize("value", [0, 21, 1.5, "1.5"])
def test_weighted_search_solver_call_limit_rejects_out_of_range_or_non_integer(tmp_path, value):
    config = _v2_config()
    config["search"]["weighted_search"]["lp_solutions_per_profile"] = value

    with pytest.raises(PortfolioConfigError, match="lp_solutions_per_profile"):
        load_portfolio_config(_write(tmp_path, config))


@pytest.mark.parametrize("value", [1, 20])
def test_weighted_search_solver_call_limit_accepts_inclusive_boundaries(tmp_path, value):
    config = _v2_config()
    config["search"]["weighted_search"]["lp_solutions_per_profile"] = value

    loaded = load_portfolio_config(_write(tmp_path, config))

    assert loaded.search["weighted_search"]["lp_solutions_per_profile"] == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("limiter_stress_pct", 0),
        ("limiter_controls", 0),
        ("priority_beta", 0),
    ],
)
def test_weighted_search_plan_zero_bounds_are_explicit(tmp_path, field, value):
    config = _v2_config()
    config["search"]["weighted_search"][field] = value

    loaded = load_portfolio_config(_write(tmp_path, config))

    assert loaded.search["weighted_search"][field] == value


def test_stage1_seed_is_required_and_history_window_has_no_backend_upper_bound(tmp_path):
    missing_seed = _v2_config()
    missing_seed["search"].pop("seed")
    with pytest.raises(PortfolioConfigError, match="missing required field.*seed"):
        load_portfolio_config(_write(tmp_path, missing_seed))

    value = _v2_config()
    large = 10**30
    value["search"]["seed"] = large
    value["search"]["composition"]["parameters"]["minimum_common_days"] = large

    loaded = load_portfolio_config(_write(tmp_path, value))

    assert loaded.search["seed"] == large
    assert loaded.search["composition"]["parameters"]["minimum_common_days"] == large


def _write(tmp_path: Path, value):
    path = tmp_path / "portfolio_optimizer.local.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_v1_migration_is_deterministic_and_drops_legacy_grid():
    original = _v1_config()
    first, migrated = migrate_portfolio_config_document(original)
    second, migrated_again = migrate_portfolio_config_document(original)

    assert migrated and migrated_again
    assert first == second
    assert original["schema_version"] == 1
    assert first["schema_version"] == SCHEMA_VERSION == 2
    assert all("grid" not in item["sizing"] for item in first["scenarios"].values())
    assert first["search"]["sizing_mode"] == SIZING_MODE
    assert first["search"]["max_enumerated_combinations"] == 100000
    assert first["inputs"]["bybit_minute_data_root"] == "./operator-supplied/bot-root/tester/data/bybit"
    assert all(profile["ranking"]["id"] == "portfolio_preliminary_ranking_v1" for profile in first["profiles"].values())


def test_v1_migration_preserves_opaque_composition_parameters_under_compatibility_namespace(tmp_path):
    original = _v1_config()
    original["search"]["composition"]["parameters"]["operator_knob"] = {"value": 7}

    migrated, changed = migrate_portfolio_config_document(original)

    assert changed
    assert migrated["search"]["composition"]["parameters"]["legacy_parameters"] == {"operator_knob": {"value": 7}}
    assert load_portfolio_config(_write(tmp_path, migrated)).search["composition"]["parameters"]["legacy_parameters"] == {"operator_knob": {"value": 7}}


def test_v1_load_returns_v2_model_with_defaults(tmp_path):
    config = load_portfolio_config(_write(tmp_path, _v1_config()))

    assert config.schema_version == 2
    assert not hasattr(config.scenarios["AGGRESSIVE"], "sizing_grid")
    assert config.search["sizing_mode"] == SIZING_MODE
    assert config.liquidity["parameters"]["close_volume_participation_pct"] == 200
    assert config.liquidity["round_down_usdt"] == Decimal("10")
    assert config.liquidity["archive_publication_lag_hours"] == 6
    assert config.profiles["AGGRESSIVE"].individual_max_dd_pct == INDIVIDUAL_DD_DEFAULTS["AGGRESSIVE"]
    assert config.profiles["BALANCED"].individual_net_pnl_min_exclusive == Decimal("0")


def test_v2_unknown_keys_are_rejected(tmp_path):
    value = _v2_config()
    value["unexpected"] = True
    with pytest.raises(PortfolioConfigError, match="unknown"):
        load_portfolio_config(_write(tmp_path, value))

    value = _v2_config()
    value["scenarios"]["AGGRESSIVE"]["sizing"]["grid"] = []
    with pytest.raises(PortfolioConfigError, match="unknown"):
        load_portfolio_config(_write(tmp_path, value))

    value = _v2_config()
    value["profiles"]["AGGRESSIVE"]["ranking"]["id"] = "operator_supplied_ranking_v1"
    with pytest.raises(PortfolioConfigError, match="unknown"):
        load_portfolio_config(_write(tmp_path, value))

    value = _v2_config()
    value["search"]["weighted_search"]["unexpected"] = 1
    with pytest.raises(PortfolioConfigError, match="unknown"):
        load_portfolio_config(_write(tmp_path, value))


@pytest.mark.parametrize("value", [1, 30, 200])
def test_close_volume_participation_bounds_are_inclusive(tmp_path, value):
    config = _v2_config()
    config["liquidity"]["parameters"]["close_volume_participation_pct"] = value
    loaded = load_portfolio_config(_write(tmp_path, config))
    assert loaded.liquidity["parameters"]["close_volume_participation_pct"] == value


@pytest.mark.parametrize("value", [0, 201, 1.0, "30"])
def test_close_volume_participation_invalid_values_fail(tmp_path, value):
    config = _v2_config()
    config["liquidity"]["parameters"]["close_volume_participation_pct"] = value
    with pytest.raises(PortfolioConfigError):
        load_portfolio_config(_write(tmp_path, config))


@pytest.mark.parametrize("value", [0, 6, 48])
def test_archive_lag_bounds_are_inclusive(tmp_path, value):
    config = _v2_config()
    config["liquidity"]["archive_publication_lag_hours"] = value
    assert load_portfolio_config(_write(tmp_path, config)).liquidity["archive_publication_lag_hours"] == value


@pytest.mark.parametrize("value", [-1, 49, 6.0, "6"])
def test_archive_lag_invalid_values_fail(tmp_path, value):
    config = _v2_config()
    config["liquidity"]["archive_publication_lag_hours"] = value
    with pytest.raises(PortfolioConfigError):
        load_portfolio_config(_write(tmp_path, config))


def test_weekend_boundaries_accept_nonempty_weekly_utc_interval(tmp_path):
    config = _v2_config()
    config["liquidity"]["weekend_start_utc"] = "FRIDAY 18:30"
    config["liquidity"]["weekend_end_utc"] = "MONDAY 06:00"
    loaded = load_portfolio_config(_write(tmp_path, config))
    assert loaded.liquidity["weekend_start_utc"] == "FRIDAY 18:30"
    assert loaded.liquidity["weekend_end_utc"] == "MONDAY 06:00"


@pytest.mark.parametrize("field,value", [("weekend_start_utc", "not-a-time"), ("weekend_end_utc", "MONDAY 25:00")])
def test_weekend_boundaries_reject_invalid_utc_times(tmp_path, field, value):
    config = _v2_config()
    config["liquidity"][field] = value
    with pytest.raises(PortfolioConfigError):
        load_portfolio_config(_write(tmp_path, config))


def test_concrete_profile_descriptors_and_single_size_mode(tmp_path):
    config = load_portfolio_config(_write(tmp_path, _v2_config()))

    for name in ("AGGRESSIVE", "BALANCED", "CONSERVATIVE"):
        assert tuple(config.profiles[name].ranking_parameters["metrics"]) == tuple(RANKING_METRICS[name])
        assert config.profiles[name].individual_net_pnl_min_exclusive == Decimal("0")
    assert config.search["sizing_mode"] == SIZING_MODE
    assert all(not hasattr(scenario, "sizing_grid") for scenario in config.scenarios.values())


def test_v2_values_are_preserved_and_money_stays_decimal(tmp_path):
    value = _v2_config()
    value["search"]["seed"] = 17
    value["profiles"]["BALANCED"]["individual_max_dd_pct"] = "21.25"
    value["scenarios"]["AGGRESSIVE"]["deposit"]["amount"] = "10000.125"
    loaded = load_portfolio_config(_write(tmp_path, value))

    assert loaded.search["seed"] == 17
    assert loaded.profiles["BALANCED"].individual_max_dd_pct == Decimal("21.25")
    assert loaded.scenarios["AGGRESSIVE"].deposit.amount == Decimal("10000.125")
    with pytest.raises(TypeError):
        loaded.algorithm_versions["new"] = "v1"


def test_existing_v2_v1_algorithm_versions_resolve_in_memory_without_rewrite(tmp_path):
    value = _v2_config()
    value["algorithm_versions"] = {
        "sizing": "portfolio_optimizer_sizing_v1",
        "ranking": "portfolio_optimizer_ranking_v1",
    }
    path = _write(tmp_path, value)
    before = path.read_bytes()

    loaded = load_portfolio_config(path)

    assert dict(loaded.algorithm_versions) == {
        "sizing": "portfolio_optimizer_sizing_v2",
        "ranking": "portfolio_optimizer_ranking_v2",
    }
    assert path.read_bytes() == before


def test_v2_migration_resolves_legacy_algorithm_versions_in_document_without_rewrite():
    value = _v2_config()
    value["algorithm_versions"] = {
        "sizing": "portfolio_optimizer_sizing_v1",
        "ranking": "portfolio_optimizer_ranking_v1",
    }

    resolved, changed = migrate_portfolio_config_document(value)

    assert not changed
    assert resolved["algorithm_versions"] == {
        "sizing": "portfolio_optimizer_sizing_v2",
        "ranking": "portfolio_optimizer_ranking_v2",
    }
    assert value["algorithm_versions"]["sizing"].endswith("_v1")


def test_nonfinite_and_binary_float_money_fail(tmp_path):
    value = _v2_config()
    value["scenarios"]["AGGRESSIVE"]["deposit"]["amount"] = "NaN"
    with pytest.raises(PortfolioConfigError, match="finite"):
        load_portfolio_config(_write(tmp_path, value))

    value = _v2_config()
    value["scenarios"]["AGGRESSIVE"]["deposit"]["amount"] = 10000.125
    with pytest.raises(PortfolioConfigError, match="finite decimal"):
        load_portfolio_config(_write(tmp_path, value))


def test_repository_v1_example_migrates():
    config = load_portfolio_config(Path(__file__).parents[1] / "portfolio_optimizer.local.json.example")
    assert config.schema_version == 2
    assert set(config.profiles) == {"AGGRESSIVE", "BALANCED", "CONSERVATIVE"}


def test_effective_profile_risk_defaults_are_immutable_and_do_not_mutate_source():
    source = {"individual_max_dd_pct": "20"}
    before = deepcopy(source)

    effective = config_module.effective_profile_risk(source, "BALANCED")

    assert dict(effective) == {
        "max_actual_equity_dd_pct": Decimal("10"),
        "min_calculated_free_margin_reserve_pct": Decimal("40"),
        "max_calculated_account_mm_load_pct": Decimal("35"),
    }
    assert source == before
    with pytest.raises(TypeError):
        effective["max_actual_equity_dd_pct"] = Decimal("1")


def test_effective_profile_risk_preserves_custom_values_without_mutating_source():
    source = {
        "max_actual_equity_dd_pct": "10.000000000001",
        "min_calculated_free_margin_reserve_pct": "40",
        "max_calculated_account_mm_load_pct": 35,
    }
    before = deepcopy(source)

    effective = config_module.effective_profile_risk(source, "BALANCED")

    assert effective["max_actual_equity_dd_pct"] == Decimal("10.000000000001")
    assert effective["min_calculated_free_margin_reserve_pct"] == Decimal("40")
    assert effective["max_calculated_account_mm_load_pct"] == Decimal("35")
    assert source == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_actual_equity_dd_pct", True),
        ("min_calculated_free_margin_reserve_pct", "NaN"),
        ("max_calculated_account_mm_load_pct", "-0.1"),
        ("max_actual_equity_dd_pct", "100.000000000001"),
        ("min_calculated_free_margin_reserve_pct", "123456789012345678901234567.0"),
        ("max_calculated_account_mm_load_pct", "0.1234567890123"),
    ],
)
def test_effective_profile_risk_rejects_invalid_present_values(field, value):
    with pytest.raises(PortfolioConfigError, match=rf"profiles\.BALANCED\.{field}"):
        config_module.effective_profile_risk({field: value}, "BALANCED")


def test_v2_profile_risk_values_are_parsed_and_materialized(tmp_path):
    value = _v2_config()
    value["profiles"]["BALANCED"].update({
        "max_actual_equity_dd_pct": "10.000000000001",
        "min_calculated_free_margin_reserve_pct": "40.000000000001",
        "max_calculated_account_mm_load_pct": "35.000000000001",
    })

    loaded = load_portfolio_config(_write(tmp_path, value))

    assert loaded.profiles["BALANCED"].max_actual_equity_dd_pct == Decimal("10.000000000001")
    assert loaded.profiles["BALANCED"].min_calculated_free_margin_reserve_pct == Decimal("40.000000000001")
    assert loaded.profiles["BALANCED"].max_calculated_account_mm_load_pct == Decimal("35.000000000001")


def test_schema_v2_migration_defaults_spread_history_pretest_bypass_off_and_materializes_it():
    value = _v2_config()
    value["liquidity"].pop("spread_history_bypass_pretest", None)

    migrated, changed = migrate_portfolio_config_document(value)

    assert changed is False
    assert migrated["liquidity"]["spread_history_bypass_pretest"] is False


def test_load_portfolio_config_materializes_absent_v2_spread_history_bypass_in_memory(tmp_path):
    value = _v2_config()
    value["liquidity"].pop("spread_history_bypass_pretest", None)

    loaded = load_portfolio_config(_write(tmp_path, value))

    assert loaded.liquidity["spread_history_bypass_pretest"] is False


def test_spread_history_pretest_bypass_requires_strict_boolean(tmp_path):
    value = _v2_config()
    value["liquidity"]["spread_history_bypass_pretest"] = "false"

    with pytest.raises(PortfolioConfigError, match="spread_history_bypass_pretest must be a boolean"):
        load_portfolio_config(_write(tmp_path, value))


def test_margin_fee_rates_preserve_exact_decimal_strings(tmp_path):
    value = _v2_config()
    value["margin"]["parameters"].update({"open_fee_rate": "0", "close_fee_rate": "0.0002"})

    loaded = load_portfolio_config(_write(tmp_path, value))

    assert loaded.margin["parameters"]["open_fee_rate"] == "0"
    assert loaded.margin["parameters"]["close_fee_rate"] == "0.0002"

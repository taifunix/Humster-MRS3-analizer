import json
from decimal import Decimal
from pathlib import Path

import pytest

from mrs3.portfolio.config import (
    INDIVIDUAL_DD_DEFAULTS,
    POLICY_VERSION,
    RANKING_METRICS,
    SCHEMA_VERSION,
    SIZING_MODE,
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


def test_v1_load_returns_v2_model_with_defaults(tmp_path):
    config = load_portfolio_config(_write(tmp_path, _v1_config()))

    assert config.schema_version == 2
    assert not hasattr(config.scenarios["AGGRESSIVE"], "sizing_grid")
    assert config.search["sizing_mode"] == SIZING_MODE
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

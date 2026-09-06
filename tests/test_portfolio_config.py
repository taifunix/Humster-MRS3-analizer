import json
from decimal import Decimal
from pathlib import Path

import pytest

from mrs3.portfolio.config import (
    POLICY_VERSION,
    PortfolioConfigError,
    load_portfolio_config,
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


def _config():
    return {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "algorithm_versions": {
            "sizing": "portfolio_optimizer_sizing_v1",
            "ranking": "portfolio_optimizer_ranking_v1",
        },
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
                "sizing": {
                    "upper_bound": _money(10000),
                    "grid": [_money(1000), _money(5000), _money(10000)],
                },
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
        "runner": {
            "target": "local",
            "root": "./operator-supplied/tester",
            "timeout": {"value": 1, "unit": "seconds"},
            "retries": 0,
        },
        "profiles": {name: _profile() for name in ("AGGRESSIVE", "BALANCED", "CONSERVATIVE")},
    }


def _write(tmp_path: Path, value=None):
    path = tmp_path / "portfolio_optimizer.local.json"
    path.write_text(json.dumps(_config() if value is None else value), encoding="utf-8")
    return path


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(PortfolioConfigError):
        load_portfolio_config(tmp_path / "missing.json")


def test_unknown_top_level_field_is_rejected(tmp_path):
    value = _config()
    value["unexpected"] = True
    with pytest.raises(PortfolioConfigError, match="unknown"):
        load_portfolio_config(_write(tmp_path, value))


def test_schema_policy_and_algorithm_versions_are_checked(tmp_path):
    for field, invalid in (("schema_version", 2), ("policy_version", "other_policy_v1")):
        value = _config()
        value[field] = invalid
        with pytest.raises(PortfolioConfigError, match="version"):
            load_portfolio_config(_write(tmp_path, value))

    value = _config()
    value["algorithm_versions"]["sizing"] = "other_sizing_v1"
    with pytest.raises(PortfolioConfigError, match="algorithm"):
        load_portfolio_config(_write(tmp_path, value))


def test_invalid_units_and_nonfinite_sizing_are_rejected(tmp_path):
    value = _config()
    value["research"]["development_window"]["unit"] = "months"
    with pytest.raises(PortfolioConfigError, match="unit"):
        load_portfolio_config(_write(tmp_path, value))

    value = _config()
    value["scenarios"]["AGGRESSIVE"]["sizing"]["upper_bound"]["amount"] = "NaN"
    with pytest.raises(PortfolioConfigError, match="finite"):
        load_portfolio_config(_write(tmp_path, value))


def test_unknown_ranking_and_missing_required_ranking_are_rejected(tmp_path):
    value = _config()
    value["profiles"]["BALANCED"]["ranking"]["id"] = "unknown_ranking_v99"
    with pytest.raises(PortfolioConfigError, match="ranking"):
        load_portfolio_config(_write(tmp_path, value))

    value = _config()
    del value["profiles"]["BALANCED"]["ranking"]["parameters"]
    with pytest.raises(PortfolioConfigError, match="parameters"):
        load_portfolio_config(_write(tmp_path, value))


def test_risk_policy_is_exact_and_cannot_be_overridden(tmp_path):
    config = load_portfolio_config(_write(tmp_path))
    assert config.policy_version == POLICY_VERSION
    assert config.profiles["AGGRESSIVE"].max_actual_equity_dd_pct == Decimal("20")
    assert config.profiles["BALANCED"].min_calculated_free_margin_reserve_pct == Decimal("40")
    assert config.profiles["CONSERVATIVE"].max_calculated_account_mm_load_pct == Decimal("20")
    assert isinstance(config.profiles["AGGRESSIVE"].max_actual_equity_dd_pct, Decimal)

    value = _config()
    value["profiles"]["AGGRESSIVE"]["max_actual_equity_dd_pct"] = {"value": 1, "unit": "percent"}
    with pytest.raises(PortfolioConfigError, match="override"):
        load_portfolio_config(_write(tmp_path, value))


def test_sizing_grid_is_finite_nonempty_and_ordered(tmp_path):
    value = _config()
    value["scenarios"]["AGGRESSIVE"]["sizing"]["grid"] = [_money(5000), _money(1000)]
    with pytest.raises(PortfolioConfigError, match="ordered"):
        load_portfolio_config(_write(tmp_path, value))


def test_money_accepts_exact_json_numbers_and_rejects_binary_float(tmp_path):
    value = _config()
    value["scenarios"]["AGGRESSIVE"]["deposit"]["amount"] = "10000.125"
    config = load_portfolio_config(_write(tmp_path, value))
    assert config.scenarios["AGGRESSIVE"].deposit.amount == Decimal("10000.125")
    assert isinstance(config.scenarios["AGGRESSIVE"].deposit.amount, Decimal)

    value = _config()
    value["scenarios"]["AGGRESSIVE"]["deposit"]["amount"] = 10000.125
    with pytest.raises(PortfolioConfigError, match="finite decimal"):
        load_portfolio_config(_write(tmp_path, value))

    value = _config()
    value["scenarios"]["AGGRESSIVE"]["sizing"]["grid"] = []
    with pytest.raises(PortfolioConfigError, match="nonempty"):
        load_portfolio_config(_write(tmp_path, value))


def test_valid_example_is_loadable_and_immutable(tmp_path):
    config = load_portfolio_config(_write(tmp_path))
    assert tuple(config.scenarios["AGGRESSIVE"].sizing_grid)[0].amount == Decimal("1000")
    with pytest.raises(TypeError):
        config.algorithm_versions["new"] = "v1"
    with pytest.raises(AttributeError):
        config.policy_version = "other"


def test_repository_example_is_valid():
    example = Path(__file__).parents[1] / "portfolio_optimizer.local.json.example"
    config = load_portfolio_config(example)
    assert set(config.profiles) == {"AGGRESSIVE", "BALANCED", "CONSERVATIVE"}

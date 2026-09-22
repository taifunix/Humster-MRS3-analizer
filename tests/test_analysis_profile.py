from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest


def _config(tmp_path: Path) -> Path:
    document = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
    document["duckdb_import"] = {"workers": 15, "transaction_batch_size": 2000}
    document["remote_runner"] = {"host": "private-host", "password": "private-secret"}
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_profile_projects_only_fresh_analysis_fields_and_keeps_unknown_config(tmp_path: Path) -> None:
    from mrs3.analysis_profile import load_analysis_profile

    config = _config(tmp_path)

    profile = load_analysis_profile(config)

    assert profile["economics"]["min_pnl_pct"] == "0"
    assert profile["ready"]["base_min_points"] == 3
    assert profile["ready"]["multi_min_events_per_month"] == 20
    assert "workers" not in profile
    assert "canonical_shifts_bp" not in profile["geometry"]
    assert "isolated_peak_relative" not in profile["plateau"]
    assert "close_core_min" not in profile["plateau"]
    assert "close_supported_min" not in profile["plateau"]
    assert "target_dd_pct" not in profile["structures"]
    assert "private-host" not in json.dumps(profile)
    assert "private-secret" not in json.dumps(profile)


def test_profile_rejects_unknown_or_invalid_values_without_writing(tmp_path: Path) -> None:
    from mrs3.analysis_profile import load_analysis_profile, validate_analysis_profile

    config = _config(tmp_path)
    before = config.read_text(encoding="utf-8")
    profile = load_analysis_profile(config)

    with pytest.raises(ValueError, match="unknown analysis profile field"):
        validate_analysis_profile(config, {**profile, "remote_runner": {}})
    profile["ready"]["base_min_points"] = 1
    with pytest.raises(ValueError, match="min_plateau_points"):
        validate_analysis_profile(config, profile)

    assert config.read_text(encoding="utf-8") == before


def test_profile_save_updates_only_whitelisted_values(tmp_path: Path) -> None:
    from mrs3.analysis_profile import load_analysis_profile, save_analysis_profile
    from mrs3.config import AlgorithmConfig

    config = _config(tmp_path)
    before = json.loads(config.read_text(encoding="utf-8"))
    profile = load_analysis_profile(config)
    profile["economics"]["min_pnl_pct"] = "7"

    saved = save_analysis_profile(config, profile)
    after = json.loads(config.read_text(encoding="utf-8"))

    assert saved["economics"]["min_pnl_pct"] == "7"
    assert after["economic_min_pnl_pct"] == 7
    assert after["remote_runner"] == before["remote_runner"]
    assert after["duckdb_import"]["transaction_batch_size"] == 2000
    assert AlgorithmConfig.from_json(config).economic_min_pnl_pct == Decimal("7")


def test_profile_save_preserves_hidden_analysis_values(tmp_path: Path) -> None:
    from mrs3.analysis_profile import load_analysis_profile, save_analysis_profile

    config = _config(tmp_path)
    before = json.loads(config.read_text(encoding="utf-8"))
    profile = load_analysis_profile(config)

    save_analysis_profile(config, profile)
    after = json.loads(config.read_text(encoding="utf-8"))

    assert after["canonical_shifts_bp"] == before["canonical_shifts_bp"]
    assert after["plateau"]["isolated_peak_relative"] == before["plateau"]["isolated_peak_relative"]
    assert after["close_support"] == before["close_support"]
    assert after["target_dd"] == before["target_dd"]


def test_profile_rejects_legacy_worker_field(tmp_path: Path) -> None:
    from mrs3.analysis_profile import load_analysis_profile, validate_analysis_profile

    config = _config(tmp_path)
    profile = load_analysis_profile(config)

    with pytest.raises(ValueError, match="unknown analysis profile field"):
        validate_analysis_profile(config, {**profile, "workers": 3})


def test_analysis_settings_load_and_atomically_save_listing_dates_path(tmp_path: Path) -> None:
    from mrs3.analysis_profile import load_analysis_settings, save_analysis_settings

    config = _config(tmp_path)
    before = json.loads(config.read_text(encoding="utf-8"))
    settings = load_analysis_settings(config)

    assert settings["listing_dates_path"] == "input/dates.xlsx"

    settings["profile"]["economics"]["min_pnl_pct"] = "7"
    saved = save_analysis_settings(
        config,
        settings["profile"],
        "input/bybit_tradfi_liquidity.xlsx",
    )
    after = json.loads(config.read_text(encoding="utf-8"))

    assert saved["listing_dates_path"] == "input/bybit_tradfi_liquidity.xlsx"
    assert saved["profile"]["economics"]["min_pnl_pct"] == "7"
    assert after["panel_workflow"]["listing_dates_path"] == "input/bybit_tradfi_liquidity.xlsx"
    assert after["remote_runner"] == before["remote_runner"]


@pytest.mark.parametrize("value", ["", "   ", None, 7])
def test_analysis_settings_reject_invalid_listing_dates_path_without_writing(
    tmp_path: Path, value: object
) -> None:
    from mrs3.analysis_profile import load_analysis_profile, save_analysis_settings

    config = _config(tmp_path)
    before = config.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="listing dates path"):
        save_analysis_settings(config, load_analysis_profile(config), value)

    assert config.read_text(encoding="utf-8") == before

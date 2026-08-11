from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest

import mrs3.config as config_module
from mrs3.config import (
    AlgorithmConfig,
    DuckDBImportSettings,
    load_duckdb_import_settings,
    save_duckdb_import_settings,
)


def test_duckdb_import_settings_round_trip_preserves_other_local_config(tmp_path) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(
        json.dumps({"panel": {"theme": "dark"}, "duckdb_import": {"future": "keep"}}),
        encoding="utf-8",
    )
    settings = DuckDBImportSettings(
        source_duckdb_path=Path("source.duckdb"),
        analysis_duckdb_path=Path("analysis.duckdb"),
        default_html_root=Path("reports"),
        audit_root=Path("audit"),
        workers=2,
        transaction_batch_size=50,
    )

    save_duckdb_import_settings(path, settings)

    assert load_duckdb_import_settings(path) == settings
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["panel"] == {"theme": "dark"}
    assert saved["duckdb_import"]["future"] == "keep"


def test_duckdb_import_settings_missing_section_uses_safe_defaults(tmp_path) -> None:
    settings = load_duckdb_import_settings(tmp_path / "missing.json")

    assert settings == DuckDBImportSettings()


def test_duckdb_import_settings_failed_atomic_replace_preserves_original(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "config.local.json"
    original = b'{"panel":{"theme":"dark"}}\n'
    path.write_bytes(original)

    def fail_replace(source, destination) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(config_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        save_duckdb_import_settings(path, DuckDBImportSettings(workers=2))

    assert path.read_bytes() == original
    assert tuple(tmp_path.iterdir()) == (path,)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"duckdb_import": []}, "duckdb_import must be an object"),
        ({"duckdb_import": {"workers": True}}, "duckdb_import.workers must be a positive integer"),
        ({"duckdb_import": {"transaction_batch_size": 0}}, "duckdb_import.transaction_batch_size must be a positive integer"),
        ({"duckdb_import": {"audit_root": 7}}, "duckdb_import.audit_root must be a string or null"),
    ],
)
def test_duckdb_import_settings_reject_malformed_values(tmp_path, payload, message: str) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_duckdb_import_settings(path)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"shift_domain_min_bp": 470, "shift_domain_max_bp": 30},
            "shift domain",
        ),
        ({"max_orders": 5}, "max_orders"),
        (
            {
                "shift_factors": (
                    (200, Decimal("0.9")),
                    (150, Decimal("1.0")),
                )
            },
            "shift factor boundaries",
        ),
        (
            {
                "core_link_min": Decimal("0.70"),
                "plateau_envelope_min": Decimal("0.75"),
            },
            "plateau thresholds",
        ),
        (
            {
                "close_core_min": Decimal("0.70"),
                "close_supported_min": Decimal("0.75"),
            },
            "close support",
        ),
        (
            {"gap_mid_start_bp": 410, "deep_gap_boundary_bp": 400},
            "gap boundaries",
        ),
        ({"initial_lot_sum": Decimal("0")}, "initial_lot_sum"),
        ({"base_columns": {}}, "base column mappings"),
    ],
)
def test_algorithm_config_rejects_invalid_semantics(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(AlgorithmConfig.defaults(), **changes)


def test_shift_domain_can_extend_beyond_calibrated_shift_factors() -> None:
    config = replace(AlgorithmConfig.defaults(), shift_domain_max_bp=700)

    assert config.shift_domain_max_bp == 700


@pytest.mark.parametrize(
    ("base_rate_tf", "message"),
    [
        (None, "base_rate_tf must be an object"),
        ({"2h": None}, "base_rate_tf.2h"),
        ([], "base_rate_tf must be an object"),
    ],
)
def test_from_json_rejects_null_or_non_object_base_rate_config(
    tmp_path, base_rate_tf: object, message: str
) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"base_rate_tf": base_rate_tf}), encoding="utf-8")

    with pytest.raises(ValueError, match=message) as error:
        AlgorithmConfig.from_json(path)

    assert error.value.__cause__ is not None


@pytest.mark.parametrize("rate", [[], {}, True])
def test_from_json_rejects_non_scalar_base_rate_before_decimal(
    tmp_path, rate: object
) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"base_rate_tf": {"2h": rate}}), encoding="utf-8")

    with pytest.raises(ValueError, match="base_rate_tf.2h") as error:
        AlgorithmConfig.from_json(path)

    assert isinstance(error.value.__cause__, TypeError)

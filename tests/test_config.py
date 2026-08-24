from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import re

import pytest

import mrs3.config as config_module
from mrs3.config import (
    AlgorithmConfig,
    DirectMaterializationSettings,
    DuckDBImportSettings,
    SourceV6ImportSettings,
    load_duckdb_import_settings,
    load_direct_materialization_settings,
    load_source_v6_import_settings,
    save_duckdb_import_settings,
    save_direct_materialization_settings,
    PanelPathSettings,
    load_panel_path_settings,
)


def test_panel_path_defaults_are_fixed_and_relative(tmp_path) -> None:
    settings = load_panel_path_settings(tmp_path / "missing.json")

    assert settings == PanelPathSettings()
    assert settings.analysis_root == Path("data/Analysis")
    assert settings.strategies_root == Path("Output/strategies")
    assert settings.performance_db_root == Path("data/performanceDB")
    assert settings.workbooks_root == Path("data/workbooks")
    assert settings.tester_report_dir == Path("tester/report/my_test")
    assert settings.tester_strategy_dir == Path("settings_strategy")
    assert settings.tester_config == Path("config_tester.json")


def test_panel_path_defaults_keep_legacy_config_values(tmp_path) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps({
        "tester_runner": {
            "report_dir": "old/reports",
            "strategy_dir": "old/strategies",
            "tester_config": "old/config.json",
        },
        "duckdb_import": {"analysis_duckdb_path": "old/Analysis/analysis.duckdb"},
    }), encoding="utf-8")

    settings = load_panel_path_settings(path)

    assert settings.tester_report_dir == Path("old/reports")
    assert settings.tester_strategy_dir == Path("old/strategies")
    assert settings.tester_config == Path("old/config.json")
    assert settings.analysis_root == Path("old/Analysis")


def test_panel_path_defaults_reject_traversal(tmp_path) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps({"panel_paths": {"analysis_root": "../outside"}}), encoding="utf-8")

    with pytest.raises(ValueError, match="panel_paths.analysis_root"):
        load_panel_path_settings(path)


def test_panel_path_defaults_match_tracked_examples() -> None:
    expected = {
        "analysis_root": "data/Analysis",
        "strategies_root": "Output/strategies",
        "performance_db_root": "data/performanceDB",
        "workbooks_root": "data/workbooks",
        "tester_report_dir": "tester/report/my_test",
        "tester_strategy_dir": "settings_strategy",
        "tester_config": "config_tester.json",
    }
    for filename in ("config.example.json", "config.local.json.example"):
        raw = json.loads(Path(filename).read_text(encoding="utf-8"))
        assert raw["panel_paths"] == expected


def test_source_v6_import_batch_size_uses_its_own_bounded_setting(tmp_path) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps({"source_v6_import": {"write_batch_size": 8}}), encoding="utf-8")

    assert load_source_v6_import_settings(path) == SourceV6ImportSettings(write_batch_size=8)


@pytest.mark.parametrize("value", [0, 33, True])
def test_source_v6_import_rejects_unsafe_batch_size(tmp_path, value: object) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps({"source_v6_import": {"write_batch_size": value}}), encoding="utf-8")

    with pytest.raises(ValueError, match="source_v6_import.write_batch_size must be an integer from 1 to 32"):
        load_source_v6_import_settings(path)


def test_source_v6_import_settings_defaults(tmp_path) -> None:
    settings = SourceV6ImportSettings()

    assert settings == SourceV6ImportSettings(
        write_batch_size=32,
        worker_chunk_size=64,
        max_in_flight_chunks=60,
        segment_writer_limit=4,
    )
    assert load_source_v6_import_settings(tmp_path / "missing.json") == settings


@pytest.mark.parametrize(
    ("field", "minimum", "maximum"),
    [
        ("write_batch_size", 1, 32),
        ("worker_chunk_size", 1, 256),
        ("max_in_flight_chunks", 1, 240),
        ("segment_writer_limit", 1, 8),
    ],
)
def test_source_v6_import_settings_accepts_exact_field_boundaries(
    field: str, minimum: int, maximum: int
) -> None:
    defaults = SourceV6ImportSettings()

    assert getattr(replace(defaults, **{field: minimum}), field) == minimum
    assert getattr(replace(defaults, **{field: maximum}), field) == maximum


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("write_batch_size", 0, "source_v6_import.write_batch_size must be an integer from 1 to 32"),
        ("write_batch_size", 33, "source_v6_import.write_batch_size must be an integer from 1 to 32"),
        ("worker_chunk_size", 0, "source_v6_import.worker_chunk_size must be an integer from 1 to 256"),
        ("worker_chunk_size", 257, "source_v6_import.worker_chunk_size must be an integer from 1 to 256"),
        ("max_in_flight_chunks", 0, "source_v6_import.max_in_flight_chunks must be an integer from 1 to 240"),
        ("max_in_flight_chunks", 241, "source_v6_import.max_in_flight_chunks must be an integer from 1 to 240"),
        ("segment_writer_limit", 0, "source_v6_import.segment_writer_limit must be an integer from 1 to 8"),
        ("segment_writer_limit", 9, "source_v6_import.segment_writer_limit must be an integer from 1 to 8"),
        ("worker_chunk_size", True, "source_v6_import.worker_chunk_size must be an integer from 1 to 256"),
        ("max_in_flight_chunks", 1.5, "source_v6_import.max_in_flight_chunks must be an integer from 1 to 240"),
        ("segment_writer_limit", "4", "source_v6_import.segment_writer_limit must be an integer from 1 to 8"),
    ],
)
def test_source_v6_import_settings_rejects_invalid_fields(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        SourceV6ImportSettings(**{field: value})


@pytest.mark.parametrize(
    ("changes", "workers", "message"),
    [
        (
            {"max_in_flight_chunks": 2},
            3,
            "source_v6_import.max_in_flight_chunks must be at least workers",
        ),
        (
            {"worker_chunk_size": 256, "max_in_flight_chunks": 65},
            1,
            "source_v6_import.worker_chunk_size * max_in_flight_chunks must be at most 16384",
        ),
        (
            {"segment_writer_limit": 4},
            3,
            "source_v6_import.segment_writer_limit must be at most workers",
        ),
    ],
)
def test_source_v6_import_settings_validates_worker_dependent_limits(
    changes: dict[str, object], workers: int, message: str
) -> None:
    settings = SourceV6ImportSettings(**changes)

    with pytest.raises(ValueError, match=re.escape(message)):
        settings.validate_for_workers(workers)


def test_source_v6_import_settings_loads_all_overrides(tmp_path) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(
        json.dumps(
            {
                "source_v6_import": {
                    "write_batch_size": 8,
                    "worker_chunk_size": 128,
                    "max_in_flight_chunks": 120,
                    "segment_writer_limit": 3,
                }
            }
        ),
        encoding="utf-8",
    )

    assert load_source_v6_import_settings(path) == SourceV6ImportSettings(
        write_batch_size=8,
        worker_chunk_size=128,
        max_in_flight_chunks=120,
        segment_writer_limit=3,
    )


def test_source_v6_import_settings_partial_section_uses_dataclass_defaults(tmp_path) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(
        json.dumps({"source_v6_import": {"worker_chunk_size": 128}}),
        encoding="utf-8",
    )

    assert load_source_v6_import_settings(path) == replace(
        SourceV6ImportSettings(), worker_chunk_size=128
    )


def test_source_v6_import_settings_rejects_unknown_keys(tmp_path) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(
        json.dumps({"source_v6_import": {"worker_chunk_sze": 64}}),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=re.escape("source_v6_import contains unknown keys: worker_chunk_sze"),
    ):
        load_source_v6_import_settings(path)


def test_source_v6_import_defaults_match_tracked_example() -> None:
    raw = json.loads(Path("config.example.json").read_text(encoding="utf-8"))

    assert raw["source_v6_import"] == {
        "write_batch_size": 32,
        "worker_chunk_size": 64,
        "max_in_flight_chunks": 60,
        "segment_writer_limit": 4,
    }


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


def test_direct_materialization_settings_defaults(tmp_path) -> None:
    settings = DirectMaterializationSettings()

    assert settings.workers == 15
    assert settings.fetch_batch_size == 256
    assert settings.worker_chunk_size == 16
    assert settings.max_in_flight_chunks == 30
    assert load_direct_materialization_settings(tmp_path / "missing.json") == settings


def test_direct_materialization_settings_round_trip_preserves_other_local_config(
    tmp_path,
) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(
        json.dumps(
            {
                "panel": {"theme": "dark"},
                "direct_materialization": {"future": "keep"},
            }
        ),
        encoding="utf-8",
    )
    settings = DirectMaterializationSettings(
        workers=4, fetch_batch_size=128, worker_chunk_size=8, max_in_flight_chunks=16
    )

    save_direct_materialization_settings(path, settings)

    assert load_direct_materialization_settings(path) == settings
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["panel"] == {"theme": "dark"}
    assert saved["direct_materialization"]["future"] == "keep"


def test_direct_materialization_settings_valid_overrides(tmp_path) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(
        json.dumps(
            {
                "direct_materialization": {
                    "workers": 8,
                    "fetch_batch_size": 64,
                    "worker_chunk_size": 4,
                    "max_in_flight_chunks": 12,
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = load_direct_materialization_settings(path)

    assert loaded == DirectMaterializationSettings(
        workers=8, fetch_batch_size=64, worker_chunk_size=4, max_in_flight_chunks=12
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"direct_materialization": []},
            "direct_materialization must be an object",
        ),
        (
            {"direct_materialization": {"workers": True}},
            "direct_materialization.workers must be a positive integer",
        ),
        (
            {"direct_materialization": {"fetch_batch_size": True}},
            "direct_materialization.fetch_batch_size must be a positive integer",
        ),
        (
            {"direct_materialization": {"worker_chunk_size": True}},
            "direct_materialization.worker_chunk_size must be a positive integer",
        ),
        (
            {"direct_materialization": {"max_in_flight_chunks": True}},
            "direct_materialization.max_in_flight_chunks must be a positive integer",
        ),
        (
            {"direct_materialization": {"workers": 0}},
            "direct_materialization.workers must be a positive integer",
        ),
        (
            {"direct_materialization": {"fetch_batch_size": -1}},
            "direct_materialization.fetch_batch_size must be a positive integer",
        ),
        (
            {"direct_materialization": {"worker_chunk_size": 0}},
            "direct_materialization.worker_chunk_size must be a positive integer",
        ),
        (
            {"direct_materialization": {"max_in_flight_chunks": 0}},
            "direct_materialization.max_in_flight_chunks must be a positive integer",
        ),
        (
            {"direct_materialization": {"workers": 15, "max_in_flight_chunks": 14}},
            "direct_materialization.max_in_flight_chunks must be at least workers",
        ),
    ],
)
def test_direct_materialization_settings_reject_malformed_values(
    tmp_path, payload: dict[str, object], message: str
) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_direct_materialization_settings(path)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"workers": True},
            "direct_materialization.workers must be a positive integer",
        ),
        (
            {"fetch_batch_size": 0},
            "direct_materialization.fetch_batch_size must be a positive integer",
        ),
        (
            {"max_in_flight_chunks": 2, "workers": 3},
            "direct_materialization.max_in_flight_chunks must be at least workers",
        ),
    ],
)
def test_direct_materialization_settings_reject_invalid_construction(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(DirectMaterializationSettings(), **changes)


def test_direct_materialization_defaults_match_tracked_example() -> None:
    raw = json.loads(Path("config.example.json").read_text(encoding="utf-8"))

    defaults = DirectMaterializationSettings()

    assert raw["direct_materialization"] == {
        "workers": defaults.workers,
        "fetch_batch_size": defaults.fetch_batch_size,
        "worker_chunk_size": defaults.worker_chunk_size,
        "max_in_flight_chunks": defaults.max_in_flight_chunks,
    }


def test_direct_materialization_settings_do_not_alter_algorithm_config_identity() -> None:
    algorithm = AlgorithmConfig.defaults()
    algorithm_fields = frozenset(algorithm.__dataclass_fields__)

    settings = DirectMaterializationSettings()

    assert isinstance(settings, DirectMaterializationSettings)
    assert settings != algorithm
    assert not isinstance(settings, AlgorithmConfig)
    assert algorithm_fields.isdisjoint(
        {
            "workers",
            "fetch_batch_size",
            "worker_chunk_size",
            "max_in_flight_chunks",
        }
    )


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


@pytest.mark.parametrize(
    "canonical_shifts_bp",
    [
        (),
        (30, 40, True),
        (30, 40, 40, 550),
        (40, 550),
        (30, 510),
    ],
)
def test_algorithm_config_rejects_invalid_canonical_shifts(
    canonical_shifts_bp: tuple[int, ...],
) -> None:
    with pytest.raises(ValueError, match="canonical shifts"):
        replace(
            AlgorithmConfig.defaults(), canonical_shifts_bp=canonical_shifts_bp
        )


@pytest.mark.parametrize(
    "gap_rules",
    [
        (),
        ((30, 80, 80), (80, 200, 100), (200, 300, 130)),
        ((30, 80, 0), (80, 200, 100), (200, 300, 130), (300, 551, 150)),
        ((30, 90, 80), (80, 200, 100), (200, 300, 130), (300, 551, 150)),
    ],
)
def test_algorithm_config_rejects_invalid_gap_rules(
    gap_rules: tuple[tuple[int, int, int], ...],
) -> None:
    with pytest.raises(ValueError, match="gap rules"):
        replace(AlgorithmConfig.defaults(), gap_rules=gap_rules)


def test_defaults_match_tracked_example() -> None:
    raw = json.loads(Path("config.example.json").read_text(encoding="utf-8"))

    loaded = AlgorithmConfig.from_json(Path("config.example.json"))
    default = AlgorithmConfig.defaults()

    assert default.canonical_shifts_bp == loaded.canonical_shifts_bp
    assert default.canonical_shifts_bp == tuple(raw["canonical_shifts_bp"])
    assert default.shift_domain_min_bp == raw["shift_domain"]["min_bp"]
    assert default.shift_domain_max_bp == raw["shift_domain"]["max_bp"]
    assert default.shift_domain_max_bp == 550
    assert default.gap_rules == tuple(
        (
            rule["lower_min_bp"],
            rule["lower_max_exclusive_bp"],
            rule["min_gap_bp"],
        )
        for rule in raw["gap_rules"]
    )
    assert default.close_supported_min == Decimal("0.60")
    assert default.close_core_min == Decimal("0.90")
    assert default.supported_link_min == Decimal("0.75")
    assert default.plateau_envelope_min == Decimal("0.75")
    assert default.shift_factors[-1][0] == 550


def test_from_json_accepts_canonical_fields(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "canonical_shifts_bp": [190, 230, 270],
                "shift_domain": {"min_bp": 190, "max_bp": 270},
                "gap_rules": [
                    {"lower_min_bp": 190, "lower_max_exclusive_bp": 230, "min_gap_bp": 30},
                    {"lower_min_bp": 230, "lower_max_exclusive_bp": 271, "min_gap_bp": 40},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    loaded = AlgorithmConfig.from_json(path)

    assert loaded.canonical_shifts_bp == (190, 230, 270)
    assert loaded.gap_rules == ((190, 230, 30), (230, 271, 40))


def test_from_json_missing_canonical_fields_uses_defaults(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")

    loaded = AlgorithmConfig.from_json(path)

    assert loaded.canonical_shifts_bp == AlgorithmConfig.defaults().canonical_shifts_bp
    assert loaded.gap_rules == AlgorithmConfig.defaults().gap_rules


def test_shift_domain_can_extend_beyond_calibrated_shift_factors() -> None:
    config = replace(
        AlgorithmConfig.defaults(),
        shift_domain_max_bp=700,
        canonical_shifts_bp=(
            30,
            40,
            50,
            60,
            70,
            90,
            110,
            140,
            170,
            200,
            230,
            270,
            310,
            350,
            390,
            430,
            470,
            510,
            550,
            700,
        ),
        gap_rules=(
            (30, 80, 80),
            (80, 200, 100),
            (200, 300, 130),
            (300, 551, 150),
            (551, 701, 150),
        ),
    )

    assert config.shift_domain_max_bp == 700


@pytest.mark.parametrize("shift_bp", [470, 510, 550])
def test_final_shift_factor_covers_canonical_tail(shift_bp: int) -> None:
    config = AlgorithmConfig.defaults()

    assert next(
        factor for maximum, factor in config.shift_factors if shift_bp <= maximum
    ) == Decimal("0.20")


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

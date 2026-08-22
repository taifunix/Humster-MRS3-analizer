from __future__ import annotations

from pathlib import Path

import pytest

from mrs3.panel_provenance import (
    validate_analysis_identity,
    validate_artifact_gate,
    validate_performance_evidence,
    validate_selected_surface,
    validate_strategy_batch,
)


SURFACE = {
    "surface_id": "surface-1",
    "manifest_sha256": "m" * 64,
    "frozen_facts_sha256": "f" * 64,
    "surface_schema_version": 6,
    "event_mode": "real_independent_events",
    "status": "COMMITTED",
    "canonical": True,
    "path": r"C:\private\data\surface-1.duckdb",
}
IDENTITY = {
    "analysis_run_id": "run-1",
    "surface_id": "surface-1",
    "source_manifest_sha256": "m" * 64,
    "state": "COMMITTED",
    "event_mode": "real_independent_events",
    "canonical_identity_sha256": "run-1",
    "listing_dates_sha256": "do-not-return",
}
BATCH = {
    "batch_id": "batch-1",
    "status": "READY",
    "analysis_run_id": "run-1",
    "generation_manifest_sha256": "g" * 64,
    "manifest_path": "/private/strategies/strategy_manifest.json",
    "manifest": {
        "generation_manifest_sha256": "g" * 64,
        "analysis_run_id": "run-1",
        "strategy_count": 2,
        "strategy_json_sha256": {"A.json": "a" * 64, "B.json": "b" * 64},
    },
    "artifacts": [
        {"filename": "/private/strategies/A.json", "status": "READY"},
        {"filename": "/private/strategies/B.json", "status": "READY"},
    ],
}
PERFORMANCE = {
    "batch_id": "batch-1",
    "analysis_run_id": "run-1",
    "import_id": "import-1",
    "status": "COMMITTED",
    "quarantined_count": 0,
    "tester_evidence": {"status": "RECONCILED", "batch_id": "batch-1", "tested_count": 2},
    "dd5": {"dd5_run_id": "dd5-1", "status": "CALCULATION_ONLY"},
}


def test_valid_gate_returns_only_safe_lineage_ids_and_counts() -> None:
    result = validate_artifact_gate(
        surface_loader=lambda _key: SURFACE,
        analysis_loader=lambda _key: IDENTITY,
        strategy_loader=lambda _key: BATCH,
        performance_loader=lambda _key: PERFORMANCE,
        surface_key="surface-1",
        analysis_key="run-1",
        batch_key="batch-1",
        performance_key="import-1",
    )

    assert result == {
        "status": "READY",
        "surface_id": "surface-1",
        "analysis_run_id": "run-1",
        "batch_id": "batch-1",
        "import_id": "import-1",
        "dd5_run_id": "dd5-1",
        "strategy_count": 2,
        "tested_count": 2,
        "dd5_mode": "CALCULATION_ONLY",
    }
    assert not any(
        isinstance(value, str) and (Path(value).is_absolute() or "\\" in value)
        for value in result.values()
    )
    assert "listing_dates_sha256" not in result


def test_surface_rejects_noncanonical_or_direct_lineage() -> None:
    with pytest.raises(ValueError, match="canonical"):
        validate_selected_surface({**SURFACE, "canonical": False})
    with pytest.raises(ValueError, match="DUCKDB_DIRECT"):
        validate_selected_surface({**SURFACE, "build_mode": "DUCKDB_DIRECT"})


def test_analysis_identity_must_still_point_at_selected_surface() -> None:
    surface = validate_selected_surface(SURFACE)
    with pytest.raises(ValueError, match="surface"):
        validate_analysis_identity(surface, {**IDENTITY, "surface_id": "other-surface"})


def test_strategy_batch_rejects_non_ready_candidate() -> None:
    with pytest.raises(ValueError, match="READY"):
        validate_strategy_batch(
            IDENTITY,
            {
                **BATCH,
                "artifacts": [
                    {"filename": "/private/strategies/A.json", "status": "DEFERRED"},
                    {"filename": "/private/strategies/B.json", "status": "READY"},
                ],
            },
        )


def test_performance_evidence_requires_committed_zero_quarantine() -> None:
    with pytest.raises(ValueError, match="zero-quarantine"):
        validate_performance_evidence(BATCH, {**PERFORMANCE, "quarantined_count": 1})


@pytest.mark.parametrize("mode", ["TICK_TEST", "DD5_RETEST", "tick-test"])
def test_performance_evidence_rejects_dd5_retest_modes(mode: str) -> None:
    with pytest.raises(ValueError, match="CALCULATION_ONLY"):
        validate_performance_evidence(
            BATCH,
            {**PERFORMANCE, "dd5": {"dd5_run_id": "dd5-1", "status": mode}},
        )


def test_performance_evidence_rejects_source_pnl_as_tested_pnl() -> None:
    with pytest.raises(ValueError, match="Source PnL"):
        validate_performance_evidence(
            BATCH,
            {**PERFORMANCE, "tested_pnl_kind": "SOURCE_PNL"},
        )


def test_strategy_batch_redacts_absolute_paths_and_csv_inputs() -> None:
    result = validate_strategy_batch(IDENTITY, BATCH)
    assert result["manifest"] == "strategy_manifest.json"
    assert result["artifacts"] == ["A.json", "B.json"]
    assert "path" not in result
    with pytest.raises(ValueError, match="CSV"):
        validate_strategy_batch(IDENTITY, {**BATCH, "artifact_type": "LEGACY_CSV"})

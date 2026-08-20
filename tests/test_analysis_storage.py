from __future__ import annotations

from hashlib import sha256
import json
from dataclasses import replace
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from mrs3.analysis_storage import ANALYSIS_SCHEMA_VERSION, AnalysisSchemaError, ensure_analysis_schema
from mrs3.duckdb_direct import (
    CANONICAL_GRID_VERSION,
    CANONICAL_MATERIALIZER_VERSION,
    DEFAULT_CANONICAL_SHIFTS_BP,
    NORMALIZATION_CONTRACT_VERSION,
    POINT_MATERIALIZATION_SEMANTICS_VERSION,
    READINESS_CONTRACT_VERSION,
    READINESS_MAX_SHIFT_BP,
    V2_GRID_CONTRACT_KIND,
    CoverageIssue,
    DirectBuildRequest,
    DirectPoint,
    DirectPreflight,
    DirectSurface,
    canonical_point_materialization_config_hash,
    point_evidence_jsonl_bytes,
)


REQUIRED_TABLES = {
    "schema_info",
    "surfaces",
    "surface_sources",
    "surface_pairs",
    "surface_timeframes",
    "surface_points",
    "surface_point_events",
    "coverage_issues",
    "dedup_decisions",
    "analysis_runs",
    "plateaus",
    "plateau_members",
    "candidates",
    "candidate_plateaus",
    "plateau_lineage",
    "analysis_run_facts",
}


def _create_v1_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Committed Task 10 v1 DDL, retained solely as a migration fixture."""
    connection.execute(
        """
        create table schema_info(key varchar primary key, value varchar not null);
        create table surfaces(surface_id varchar primary key, parent_surface_id varchar references surfaces(surface_id), build_mode varchar not null, period_start_utc timestamp not null, period_end_utc timestamp not null, side varchar not null check(side in ('LONG', 'SHORT')), grid_contract_json varchar, normalization_contract_version varchar, materializer_version varchar, point_materialization_config_hash varchar, created_at_utc timestamp not null default current_timestamp, check(period_end_utc > period_start_utc));
        create table surface_sources(surface_id varchar not null references surfaces(surface_id), source_database_id varchar, source_hash varchar not null check(length(source_hash) = 64), primary key(surface_id, source_hash));
        create table surface_pairs(surface_id varchar not null references surfaces(surface_id), pair_key varchar not null, symbol varchar, side varchar check(side is null or side in ('LONG', 'SHORT')), shift_bp integer check(shift_bp is null or shift_bp >= 0), open_ma integer, close_ma integer, primary key(surface_id, pair_key));
        create table surface_timeframes(surface_id varchar not null, pair_key varchar not null, timeframe varchar not null, coverage_status varchar, primary key(surface_id, pair_key, timeframe), foreign key(surface_id, pair_key) references surface_pairs(surface_id, pair_key));
        create table surface_points(surface_id varchar not null, canonical_point_key varchar not null, pair_key varchar not null, timeframe varchar not null, point_event_count bigint not null check(point_event_count >= 0), source_report_id varchar not null, source_hash varchar not null check(length(source_hash) = 64), provenance_state varchar not null, metrics_json varchar not null, primary key(surface_id, canonical_point_key), foreign key(surface_id, pair_key, timeframe) references surface_timeframes(surface_id, pair_key, timeframe));
        create table coverage_issues(issue_id varchar primary key, surface_id varchar not null references surfaces(surface_id), symbol varchar, timeframe varchar, issue_code varchar not null, detail_json varchar);
        create table dedup_decisions(decision_id varchar primary key, surface_id varchar not null references surfaces(surface_id), canonical_point_key varchar, decision varchar not null, detail_json varchar);
        create table analysis_runs(run_id varchar primary key, surface_id varchar not null references surfaces(surface_id), algorithm_version varchar not null, algorithm_config_json varchar not null, created_at_utc timestamp not null default current_timestamp, unique(surface_id, algorithm_version, algorithm_config_json), unique(run_id, surface_id));
        create table plateaus(run_id varchar not null, plateau_id varchar not null, surface_id varchar not null, metrics_json varchar, primary key(run_id, plateau_id), unique(run_id, plateau_id, surface_id), foreign key(run_id, surface_id) references analysis_runs(run_id, surface_id));
        create table plateau_members(run_id varchar not null, plateau_id varchar not null, surface_id varchar not null, canonical_point_key varchar not null, primary key(run_id, plateau_id, canonical_point_key), foreign key(run_id, plateau_id, surface_id) references plateaus(run_id, plateau_id, surface_id), foreign key(surface_id, canonical_point_key) references surface_points(surface_id, canonical_point_key));
        create table candidates(candidate_id varchar primary key, run_id varchar not null, plateau_id varchar not null, surface_id varchar not null, candidate_json varchar, foreign key(run_id, plateau_id, surface_id) references plateaus(run_id, plateau_id, surface_id));
        create table plateau_lineage(lineage_id varchar primary key, child_run_id varchar, child_plateau_id varchar, parent_run_id varchar, parent_plateau_id varchar, relation varchar not null check(relation in ('CONTINUED', 'SPLIT', 'MERGED', 'NEW', 'DROPPED')), detail_json varchar, foreign key(child_run_id, child_plateau_id) references plateaus(run_id, plateau_id), foreign key(parent_run_id, parent_plateau_id) references plateaus(run_id, plateau_id), check((child_run_id is null) = (child_plateau_id is null)), check((parent_run_id is null) = (parent_plateau_id is null)), check((relation = 'NEW' and child_run_id is not null and parent_run_id is null) or (relation = 'DROPPED' and child_run_id is null and parent_run_id is not null) or (relation in ('CONTINUED', 'SPLIT', 'MERGED') and child_run_id is not null and parent_run_id is not null)));
        create index surface_points_by_surface on surface_points(surface_id);
        create index analysis_runs_by_surface on analysis_runs(surface_id);
        create index plateaus_by_run on plateaus(run_id);
        create index lineage_by_child on plateau_lineage(child_run_id, child_plateau_id);
        """
    )


def _v1_with_one_candidate(connection: duckdb.DuckDBPyConnection) -> None:
    from mrs3 import analysis_storage

    _create_v1_schema(connection)
    connection.executemany("insert into schema_info values (?, ?)", [("schema_version", "1"), ("schema_fingerprint", analysis_storage._V1_FINGERPRINT)])
    connection.execute("insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')")
    connection.execute("insert into surface_pairs(surface_id, pair_key) values ('S1', 'PAIR')")
    connection.execute("insert into surface_timeframes values ('S1', 'PAIR', '1h', 'COMPLETE')")
    connection.execute("insert into surface_points values ('S1', 'BTCUSDT|LONG|1h|100|3|9', 'PAIR', '1h', 7, 'report', ?, 'REPRODUCIBLE_AT_PUBLICATION', '{}')", ["a" * 64])
    connection.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{}')")
    connection.execute("insert into plateaus(run_id, plateau_id, surface_id) values ('R1', 'P1', 'S1')")
    connection.execute("insert into plateau_members values ('R1', 'P1', 'S1', 'BTCUSDT|LONG|1h|100|3|9')")
    connection.execute("insert into candidates values ('C1', 'R1', 'P1', 'S1', '{\"legacy\":true}')")


@pytest.fixture
def connections(tmp_path: Path):
    source = duckdb.connect(str(tmp_path / "source.duckdb"))
    analysis = duckdb.connect(str(tmp_path / "analysis.duckdb"))
    try:
        yield source, analysis
    finally:
        source.close()
        analysis.close()


def _tables(connection: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "select table_name from information_schema.tables where table_schema = 'main'"
        ).fetchall()
    }


def _surface(*, source_hash: str = "a" * 64) -> DirectSurface:
    request = DirectBuildRequest(
        "2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z", "LONG", ("BTCUSDT",),
        (100,), "v1", "c" * 64,
    )
    preflight = DirectPreflight(
        {"BTCUSDT": ("1h",)}, {}, (),
        {"kind": "OBSERVED_GRID_CONTRACT", "required_shifts_bp": (100,), "pairs": ("100|3|9",), "normalization_contract_version": "v1"},
        (source_hash,), (("report", source_hash),), ("BTCUSDT|LONG|1h|100|3|9",),
    )
    return DirectSurface(
        request, preflight, "legacy_trades_proxy",
        (DirectPoint("BTCUSDT|LONG|1h|100|3|9", "report", source_hash, 7, {"TotalTrades": 7, "TotalPnLPercent": 10.0, "MaxDrawdownPercent": 2.0, "Win": 5, "Los": 2, "WinRate": 71.4, "ProfitFactor": 2.0}),),
    )


def _real_surface(*, source_hash: str = "b" * 64) -> DirectSurface:
    legacy = _surface(source_hash=source_hash)
    point = replace(
        legacy.points[0], point_event_count=2, event_ids=("1" * 64, "2" * 64)
    )
    return replace(
        legacy,
        event_mode="real_independent_events",
        request=replace(
            legacy.request,
            materializer_version="v2-real-events",
            point_materialization_config_hash="d" * 64,
        ),
        points=(point,),
    )


def _v2_surface(
    *,
    source_hash: str = "a" * 64,
    report_id: str = "report",
    point_key: str = "BTCUSDT|LONG|1h|100|3|9",
    audit_sha256: str = sha256(b"pair,side\nBTC,LONG\n").hexdigest(),
    audit_schema_version: int = 1,
    audit_row_count: int = 1,
    audit_bytes: bytes = b"pair,side\nBTC,LONG\n",
) -> DirectSurface:
    legacy = replace(_surface(source_hash=source_hash), event_mode="real_independent_events")
    point = replace(
        legacy.points[0],
        source_report_id=report_id,
        event_ids=tuple(
            f'{index:064x}' for index in range(1, legacy.points[0].point_event_count + 1)
        ),
    )
    evidence = point_evidence_jsonl_bytes((point,))
    witness = [
        {
            'symbol': 'BTCUSDT',
            'side': 'LONG',
            'timeframe': '1h',
            'open_ma': 3,
            'close_ma': close_ma,
            'shifts_bp': list(DEFAULT_CANONICAL_SHIFTS_BP),
            'contract_version': READINESS_CONTRACT_VERSION,
            'max_shift_bp': READINESS_MAX_SHIFT_BP,
        }
        for close_ma in range(2, 8)
    ]
    grid_contract = {
        "kind": V2_GRID_CONTRACT_KIND,
        "canonical_grid_version": CANONICAL_GRID_VERSION,
        "canonical_shifts_bp": list(DEFAULT_CANONICAL_SHIFTS_BP),
        "point_materialization_semantics_version": POINT_MATERIALIZATION_SEMANTICS_VERSION,
        "point_materialization_config_hash": canonical_point_materialization_config_hash(DEFAULT_CANONICAL_SHIFTS_BP),
        "selected_scopes": ["BTCUSDT|1h"],
        "readiness_contract_version": READINESS_CONTRACT_VERSION,
        "readiness_max_shift_bp": READINESS_MAX_SHIFT_BP,
        "normalization_contract_version": NORMALIZATION_CONTRACT_VERSION,
        "witnesses": {
            'BTCUSDT|1h': witness,
        },
        "point_evidence": evidence.decode("utf-8"),
        "point_evidence_sha256": sha256(evidence).hexdigest(),
        "audit_artifact_name": "surface_coverage_audit_LONG.csv",
        "audit_schema_version": audit_schema_version,
        "audit_size_bytes": len(audit_bytes),
        "audit_row_count": audit_row_count,
        "audit_sha256": audit_sha256,
    }
    request = replace(
        legacy.request,
        selected_scopes=("BTCUSDT|1h",),
        required_shifts_bp=DEFAULT_CANONICAL_SHIFTS_BP,
        materializer_version=CANONICAL_MATERIALIZER_VERSION,
        point_materialization_config_hash=canonical_point_materialization_config_hash(DEFAULT_CANONICAL_SHIFTS_BP),
        grid_contract_kind=V2_GRID_CONTRACT_KIND,
        readiness_contract_version=READINESS_CONTRACT_VERSION,
        readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
        audit_artifact_name="surface_coverage_audit_LONG.csv",
        audit_schema_version=audit_schema_version,
        audit_size_bytes=len(audit_bytes),
        audit_row_count=audit_row_count,
        audit_sha256=audit_sha256,
        audit_bytes=audit_bytes,
    )
    preflight = DirectPreflight(
        legacy.preflight.usable_timeframes,
        {},
        (),
        grid_contract,
        (source_hash,),
        ((report_id, source_hash),),
        (point_key,),
        legacy.preflight.coverage_rows,
        witnesses={'BTCUSDT|1h': witness},
        point_evidence_sha256=sha256(evidence).hexdigest(),
        audit_artifact_name="surface_coverage_audit_LONG.csv",
        audit_schema_version=audit_schema_version,
        audit_size_bytes=len(audit_bytes),
        audit_row_count=audit_row_count,
        audit_sha256=audit_sha256,
        audit_bytes=audit_bytes,
    )
    return replace(
        legacy,
        request=request,
        preflight=preflight,
        points=(point,),
        event_mode='real_independent_events',
    )


def test_publish_surface_is_deterministic_deduplicated_and_persists_materialized_provenance(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    first = publish_surface(analysis, _surface())
    second = publish_surface(analysis, _surface())

    assert first.surface_id == second.surface_id
    assert second.created is False
    assert analysis.execute("select count(*) from surfaces").fetchone() == (1,)
    assert analysis.execute("select provenance_state from surface_points").fetchone() == ("REPRODUCIBLE_AT_PUBLICATION",)


def test_real_surface_persists_exact_event_membership_and_has_distinct_identity(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    legacy = publish_surface(analysis, _surface(source_hash="b" * 64))
    real = publish_surface(analysis, _real_surface())

    assert real.surface_id != legacy.surface_id
    assert analysis.execute(
        "select event_id from surface_point_events where surface_id=? order by event_id",
        [real.surface_id],
    ).fetchall() == [("1" * 64,), ("2" * 64,)]
    assert analysis.execute(
        "select event_mode from surfaces where surface_id=?", [real.surface_id]
    ).fetchone() == ("real_independent_events",)


def test_publish_analysis_run_is_deterministic_and_does_not_copy_surface_points(connections) -> None:
    from mrs3.analysis_storage import publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    surface = publish_surface(analysis, _surface())
    result = PipelineResult(surface.surface_id, "0.6", {"minimum": 3}, pd.DataFrame(), pd.DataFrame())

    first = publish_analysis_run(analysis, result)
    second = publish_analysis_run(analysis, result)

    assert first.run_id == second.run_id
    assert not second.created
    assert analysis.execute("select count(*) from surface_points").fetchone() == (1,)
    assert analysis.execute("select count(*) from plateau_lineage").fetchone() == (0,)


def test_v3_stores_computed_run_facts_and_legacy_is_not_zero(connections) -> None:
    from mrs3.analysis_storage import publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    surface = publish_surface(analysis, _surface())
    legacy = publish_analysis_run(analysis, PipelineResult(surface.surface_id, "v1", {}, pd.DataFrame(), pd.DataFrame()))
    assert analysis.execute("select facts_state, unique_point_count from analysis_run_facts where run_id=?", [legacy.run_id]).fetchone() == ("UNAVAILABLE_LEGACY", None)
    computed = publish_analysis_run(analysis, PipelineResult(surface.surface_id, "v2", {}, pd.DataFrame(), pd.DataFrame(), None, {"unique_point_count": 1, "economic_eligible_point_count": 1, "event_eligible_point_count": 1, "plateau_count": 0, "ready_candidate_count": 0}))
    assert analysis.execute("select facts_state, unique_point_count, economic_eligible_point_count from analysis_run_facts where run_id=?", [computed.run_id]).fetchone() == ("COMPUTED", 1, 1)
    with pytest.raises(ValueError, match="facts"):
        publish_analysis_run(analysis, replace(PipelineResult(surface.surface_id, "v2", {}, pd.DataFrame(), pd.DataFrame(), None, {"unique_point_count": 2, "economic_eligible_point_count": 1, "event_eligible_point_count": 1, "plateau_count": 0, "ready_candidate_count": 0})))


def test_library_filters_and_compare_keep_run_facts_separate(connections) -> None:
    from mrs3.analysis_storage import compare_analysis_runs, list_surface_library, publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    first = publish_surface(analysis, _surface())
    second = publish_surface(analysis, _surface(source_hash="b" * 64))
    facts = {"unique_point_count": 1, "economic_eligible_point_count": 1, "event_eligible_point_count": 1, "plateau_count": 0, "ready_candidate_count": 0}
    left = publish_analysis_run(analysis, PipelineResult(first.surface_id, "v1", {}, pd.DataFrame(), pd.DataFrame(), None, facts))
    right = publish_analysis_run(analysis, PipelineResult(second.surface_id, "v1", {}, pd.DataFrame(), pd.DataFrame(), None, {**facts, "economic_eligible_point_count": 0}))
    rows = list_surface_library(analysis, symbol="BTCUSDT", source_hash="b" * 64)
    assert [row["surface_id"] for row in rows] == [second.surface_id]
    assert rows[0]["runs"][0]["facts"].economic_eligible_point_count == 0
    before = analysis.execute("select count(*) from plateau_lineage").fetchone()
    comparison = compare_analysis_runs(analysis, left.run_id, right.run_id)
    assert comparison["left"]["facts"].economic_eligible_point_count == 1
    assert comparison["right"]["facts"].economic_eligible_point_count == 0
    assert analysis.execute("select count(*) from plateau_lineage").fetchone() == before


def test_repeated_run_publishes_each_explicit_lineage_comparison(connections) -> None:
    from mrs3.analysis_storage import publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    parent_a = publish_surface(analysis, _surface(source_hash="a" * 64))
    parent_b = publish_surface(analysis, _surface(source_hash="b" * 64))
    child = publish_surface(analysis, _surface(source_hash="c" * 64))

    def plateau(identifier: str) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "plateau_id": identifier,
                    "all_point_ids": ("BTCUSDT|LONG|1h|100|3|9",),
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "timeframe": "1h",
                    "min_shift_bp": 100,
                    "max_shift_bp": 100,
                    "open_ma_min": 3,
                    "open_ma_max": 3,
                    "close_ma_min": 9,
                    "close_ma_max": 9,
                }
            ]
        )

    empty = pd.DataFrame()
    run_a = publish_analysis_run(
        analysis, PipelineResult(parent_a.surface_id, "v1", {"period": "A"}, plateau("PA"), empty)
    )
    run_b = publish_analysis_run(
        analysis, PipelineResult(parent_b.surface_id, "v1", {"period": "B"}, plateau("PB"), empty)
    )
    child_result = PipelineResult(
        child.surface_id, "v1", {"period": "C"}, plateau("PC"), empty, run_a.run_id
    )
    published = publish_analysis_run(analysis, child_result)
    repeated = publish_analysis_run(
        analysis, replace(child_result, comparison_run_id=run_b.run_id)
    )

    assert repeated.run_id == published.run_id
    assert not repeated.created
    assert {
        row[0]
        for row in analysis.execute(
            """select parent_run_id from plateau_lineage
                 where child_run_id=? and relation='CONTINUED'""",
            [published.run_id],
        ).fetchall()
    } == {run_a.run_id, run_b.run_id}


def test_publish_analysis_run_persists_2ord_and_4ord_candidates_through_junction(connections) -> None:
    from mrs3.analysis_storage import publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    surface = publish_surface(analysis, _surface())
    plateaus = pd.DataFrame([{
        "plateau_id": "P1", "all_point_ids": (surface.points[0].canonical_point_key,),
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "min_shift_bp": 100, "max_shift_bp": 100,
        "open_ma_min": 3, "open_ma_max": 3, "close_ma_min": 9, "close_ma_max": 9,
    }, {
        "plateau_id": "P2", "all_point_ids": (surface.points[0].canonical_point_key,),
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "min_shift_bp": 100, "max_shift_bp": 100,
        "open_ma_min": 3, "open_ma_max": 3, "close_ma_min": 9, "close_ma_max": 9,
    }, {
        "plateau_id": "P3", "all_point_ids": (surface.points[0].canonical_point_key,),
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "min_shift_bp": 100, "max_shift_bp": 100,
        "open_ma_min": 3, "open_ma_max": 3, "close_ma_min": 9, "close_ma_max": 9,
    }, {
        "plateau_id": "P4", "all_point_ids": (surface.points[0].canonical_point_key,),
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "min_shift_bp": 100, "max_shift_bp": 100,
        "open_ma_min": 3, "open_ma_max": 3, "close_ma_min": 9, "close_ma_max": 9,
    }])
    candidates = pd.DataFrame([{"structure_id": "C2", "plateau_ids": ("P1", "P2")}, {"structure_id": "C4", "plateau_ids": ("P1", "P2", "P3", "P4")}])

    publish_analysis_run(analysis, PipelineResult(surface.surface_id, "0.6", {}, plateaus, candidates))

    assert analysis.execute("select count(*) from candidates").fetchone() == (2,)
    assert analysis.execute("select count(*) from candidate_plateaus").fetchone() == (6,)
    assert analysis.execute("""select count(*) from candidate_plateaus cp
        join plateaus p using(run_id, plateau_id, surface_id)
        join plateau_members pm using(run_id, plateau_id, surface_id)
        join surface_points sp using(surface_id, canonical_point_key)
        join surface_sources ss using(surface_id, source_hash)""").fetchone() == (6,)


def _analysis_surface(keys: tuple[str, ...]) -> DirectSurface:
    source_hash = "a" * 64
    points = tuple(
        DirectPoint(
            key,
            f"report{index}",
            source_hash,
            7,
            {
                "TotalTrades": 7,
                "TotalPnLPercent": 10.0,
                "MaxDrawdownPercent": 2.0,
                "Win": 5,
                "Los": 2,
                "WinRate": 71.4,
                "ProfitFactor": 2.0,
            },
        )
        for index, key in enumerate(keys)
    )
    pairs = tuple("|".join(key.split("|")[3:]) for key in keys)
    request = DirectBuildRequest(
        "2024-01-01T00:00:00Z",
        "2024-01-01T02:00:00Z",
        "LONG",
        ("BTCUSDT",),
        (100,),
        "v1",
        "c" * 64,
    )
    preflight = DirectPreflight(
        {"BTCUSDT": ("1h",)},
        {},
        (),
        {
            "kind": "OBSERVED_GRID_CONTRACT",
            "required_shifts_bp": (100,),
            "pairs": pairs,
            "normalization_contract_version": "v1",
        },
        (source_hash,),
        tuple((f"report{index}", source_hash) for index in range(len(keys))),
        keys,
    )
    return DirectSurface(request, preflight, "legacy_trades_proxy", points)


def _frozen_facts(key4: str, key5: str) -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "operational_facts_version": "cma_representatives_v1",
        "primary_close_ma": 8,
        "cma_representatives": [
            {
                "close_ma": 8,
                "point_id": key4,
                "support": 1.0,
                "support_status": "PRIMARY_CLOSE",
                "continuity_status": "USABLE",
                "usable": True,
            },
            {
                "close_ma": 9,
                "point_id": key5,
                "support": 0.8,
                "support_status": "SUPPORTED_CLOSE",
                "continuity_status": "USABLE",
                "usable": True,
            },
        ],
        "base_1ord_point_id": key4,
        "standalone_eligible_point_ids": (key4,),
    }


def _fact_plateau(key4: str, key5: str, facts: dict[str, object]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "plateau_id": "P1",
            "all_point_ids": (key4, key5),
            "standalone_eligible_point_ids": (key4,),
            "symbol": "BTCUSDT",
            "side": "LONG",
            "timeframe": "1h",
            "min_shift_bp": 100,
            "max_shift_bp": 100,
            "open_ma_min": 3,
            "open_ma_max": 3,
            "close_ma_min": 8,
            "close_ma_max": 9,
            "ready": True,
            **facts,
        }
    ])


def test_publish_analysis_run_persists_validated_frozen_facts(connections) -> None:
    from mrs3.analysis_storage import publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    key4 = "BTCUSDT|LONG|1h|100|3|8"
    key5 = "BTCUSDT|LONG|1h|100|3|9"
    surface = publish_surface(analysis, _analysis_surface((key4, key5)))
    facts = _frozen_facts(key4, key5)
    result = PipelineResult(
        surface.surface_id, "0.7", {"frozen": True}, _fact_plateau(key4, key5, facts), pd.DataFrame()
    )

    published = publish_analysis_run(analysis, result)

    stored = json.loads(
        analysis.execute(
            "select metrics_json from plateaus where run_id=? and plateau_id='P1'",
            [published.run_id],
        ).fetchone()[0]
    )
    assert stored["operational_facts_version"] == "cma_representatives_v1"
    assert stored["primary_close_ma"] == 8
    assert stored["cma_representatives"] == facts["cma_representatives"]
    assert stored["base_1ord_point_id"] == key4


def test_publish_analysis_run_rejects_facts_with_invalid_base(connections) -> None:
    from mrs3.analysis_storage import publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    key4 = "BTCUSDT|LONG|1h|100|3|8"
    key5 = "BTCUSDT|LONG|1h|100|3|9"
    surface = publish_surface(analysis, _analysis_surface((key4, key5)))
    facts = _frozen_facts(key4, key5)
    facts["base_1ord_point_id"] = "BTCUSDT|LONG|1h|100|3|99"
    result = PipelineResult(
        surface.surface_id, "0.7", {"frozen": True}, _fact_plateau(key4, key5, facts), pd.DataFrame()
    )

    with pytest.raises(ValueError, match="base"):
        publish_analysis_run(analysis, result)


def test_publish_analysis_run_rejects_representative_outside_surface(connections) -> None:
    from mrs3.analysis_storage import publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    key4 = "BTCUSDT|LONG|1h|100|3|8"
    key5 = "BTCUSDT|LONG|1h|100|3|9"
    surface = publish_surface(analysis, _analysis_surface((key4, key5)))
    facts = _frozen_facts(key4, key5)
    facts["cma_representatives"][0]["point_id"] = "BTCUSDT|LONG|1h|200|3|8"
    result = PipelineResult(
        surface.surface_id, "0.7", {"frozen": True}, _fact_plateau(key4, key5, facts), pd.DataFrame()
    )

    with pytest.raises(ValueError, match="outside the surface"):
        publish_analysis_run(analysis, result)


def test_publish_analysis_run_rejects_unknown_facts_version(connections) -> None:
    from mrs3.analysis_storage import publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    key4 = "BTCUSDT|LONG|1h|100|3|8"
    key5 = "BTCUSDT|LONG|1h|100|3|9"
    surface = publish_surface(analysis, _analysis_surface((key4, key5)))
    facts = _frozen_facts(key4, key5)
    facts["operational_facts_version"] = "bogus_v2"
    result = PipelineResult(
        surface.surface_id, "0.7", {"frozen": True}, _fact_plateau(key4, key5, facts), pd.DataFrame()
    )

    with pytest.raises(ValueError, match="version"):
        publish_analysis_run(analysis, result)


def test_publish_analysis_run_rejects_contradictory_support_status(connections) -> None:
    from mrs3.analysis_storage import publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    key4 = "BTCUSDT|LONG|1h|100|3|8"
    key5 = "BTCUSDT|LONG|1h|100|3|9"
    surface = publish_surface(analysis, _analysis_surface((key4, key5)))
    facts = _frozen_facts(key4, key5)
    facts["cma_representatives"][1]["support_status"] = "CORE_CLOSE"
    result = PipelineResult(
        surface.surface_id, "0.7", {"frozen": True}, _fact_plateau(key4, key5, facts), pd.DataFrame()
    )

    with pytest.raises(ValueError, match="support_status"):
        publish_analysis_run(analysis, result)


def test_publish_analysis_run_rejects_computed_ready_plateau_without_facts(connections) -> None:
    from mrs3.analysis_storage import publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    key4 = "BTCUSDT|LONG|1h|100|3|8"
    key5 = "BTCUSDT|LONG|1h|100|3|9"
    surface = publish_surface(analysis, _analysis_surface((key4, key5)))
    result = PipelineResult(
        surface.surface_id,
        "0.7",
        {"frozen": True},
        _fact_plateau(key4, key5, {}),
        pd.DataFrame(),
        None,
        {"unique_point_count": 2, "economic_eligible_point_count": 2, "event_eligible_point_count": 2, "plateau_count": 1, "ready_candidate_count": 0},
    )

    with pytest.raises(ValueError, match="frozen operational facts"):
        publish_analysis_run(analysis, result)


def test_publish_analysis_run_allows_legacy_ready_plateau_without_facts(connections) -> None:
    from mrs3.analysis_storage import publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    key4 = "BTCUSDT|LONG|1h|100|3|8"
    key5 = "BTCUSDT|LONG|1h|100|3|9"
    surface = publish_surface(analysis, _analysis_surface((key4, key5)))
    result = PipelineResult(
        surface.surface_id,
        "0.6",
        {"legacy": True},
        _fact_plateau(key4, key5, {}),
        pd.DataFrame(),
    )

    published = publish_analysis_run(analysis, result)

    stored = json.loads(
        analysis.execute(
            "select metrics_json from plateaus where run_id=? and plateau_id='P1'",
            [published.run_id],
        ).fetchone()[0]
    )
    assert "operational_facts_version" not in stored
    assert stored["ready"] is True


def test_lineage_classification_requires_common_points_and_geometry() -> None:
    from mrs3.analysis_storage import classify_plateau_lineage

    geometry = {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "min_shift_bp": 100, "max_shift_bp": 100,
                "open_ma_min": 3, "open_ma_max": 3, "close_ma_min": 9, "close_ma_max": 9}
    assert classify_plateau_lineage({"old": ({"point"}, geometry)}, {"new": ({"point"}, geometry)})[0][2] == "CONTINUED"
    shifted = {**geometry, "min_shift_bp": 200, "max_shift_bp": 200}
    assert classify_plateau_lineage({"old": ({"point"}, geometry)}, {"new": ({"point"}, shifted)})[0][2] == "NEW"


def test_lineage_classifies_split_merge_new_and_dropped_without_metrics() -> None:
    from mrs3.analysis_storage import classify_plateau_lineage

    geometry = {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "min_shift_bp": 100, "max_shift_bp": 100,
                "open_ma_min": 3, "open_ma_max": 3, "close_ma_min": 9, "close_ma_max": 9, "pnl_pct": 999}
    split = classify_plateau_lineage({"old": ({"a", "b"}, geometry)}, {"new-a": ({"a"}, geometry), "new-b": ({"b"}, geometry)})
    merged = classify_plateau_lineage({"old-a": ({"a"}, geometry), "old-b": ({"b"}, geometry)}, {"new": ({"a", "b"}, geometry)})
    isolated = classify_plateau_lineage({"dropped": ({"d"}, geometry)}, {"new": ({"n"}, geometry)})

    assert {row[2] for row in split} == {"SPLIT"}
    assert {row[2] for row in merged} == {"MERGED"}
    assert {(row[0], row[1], row[2]) for row in isolated} == {("new", None, "NEW"), (None, "dropped", "DROPPED")}
    assert all("pnl_pct" not in row[3] for row in (*split, *merged, *isolated))


def test_publish_surface_creates_immutable_child_and_rolls_back_invalid_materialized_input(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    parent = publish_surface(analysis, _surface())
    same_period_child = publish_surface(analysis, _surface(source_hash="b" * 64))
    assert same_period_child.parent_surface_id == parent.surface_id
    parent_dedup = publish_surface(analysis, _surface())
    assert parent_dedup.created is False
    assert parent_dedup.parent_surface_id is None
    same_period_dedup = publish_surface(analysis, _surface(source_hash="b" * 64))
    assert same_period_dedup.created is False
    assert same_period_dedup.parent_surface_id == parent.surface_id
    child = publish_surface(analysis, replace(_surface(source_hash="c" * 64), parent_surface_id=parent.surface_id))
    assert child.parent_surface_id == parent.surface_id
    assert analysis.execute("select provenance_state from surface_points where surface_id=?", [child.surface_id]).fetchone() == ("REPRODUCIBLE_AT_PUBLICATION",)

    invalid = _surface(source_hash="not-a-hash")
    with pytest.raises(ValueError, match="source hash"):
        publish_surface(analysis, invalid)
    assert analysis.execute("select count(*) from surfaces").fetchone() == (3,)


@pytest.mark.parametrize(
    "alter",
    [
        lambda point: replace(point, metrics={"TotalTrades": 7, "TotalPnL": 1}),
        lambda point: replace(point, source_report_id="other-report"),
    ],
)
def test_dedup_rejects_facts_that_do_not_match_immutable_stored_points(connections, alter) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    publish_surface(analysis, _surface())
    incoming = _surface()
    changed = alter(incoming.points[0])
    tampered = replace(incoming, points=(changed,), preflight=replace(incoming.preflight, manifest=((changed.source_report_id, changed.source_hash),)))

    with pytest.raises(ValueError, match="immutable|incomplete"):
        publish_surface(analysis, tampered)


def test_bootstrap_creates_only_analysis_tables_in_a_distinct_database(connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection]) -> None:
    source, analysis = connections
    source.execute("create table report_payloads(report_id varchar primary key, actions_zlib blob)")

    assert ensure_analysis_schema(analysis) == ANALYSIS_SCHEMA_VERSION
    assert _tables(analysis) == REQUIRED_TABLES
    assert _tables(source) == {"report_payloads"}
    assert not {"raw_payloads", "actions", "equity", "wallet", "report_payloads"}.intersection(
        _tables(analysis)
    )
    metadata = dict(analysis.execute("select key, value from schema_info").fetchall())
    assert metadata["schema_version"] == str(ANALYSIS_SCHEMA_VERSION)
    assert len(metadata["schema_fingerprint"]) == 64


def test_existing_schema_is_idempotent_but_other_versions_fail(connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection]) -> None:
    _, analysis = connections
    assert ensure_analysis_schema(analysis) == ANALYSIS_SCHEMA_VERSION
    assert ensure_analysis_schema(analysis) == ANALYSIS_SCHEMA_VERSION

    analysis.execute("update schema_info set value = '999' where key = 'schema_version'")
    with pytest.raises(AnalysisSchemaError, match="version"):
        ensure_analysis_schema(analysis)


def test_v1_migration_preserves_candidate_link_and_published_surface_bytes() -> None:
    connection = duckdb.connect(":memory:")
    try:
        _v1_with_one_candidate(connection)
        surface_columns = "surface_id,parent_surface_id,build_mode,period_start_utc,period_end_utc,side,grid_contract_json,normalization_contract_version,materializer_version,point_materialization_config_hash,created_at_utc"
        before = connection.execute(f"select {surface_columns} from surfaces").fetchall(), connection.execute("select * from surface_points").fetchall()

        assert ensure_analysis_schema(connection) == ANALYSIS_SCHEMA_VERSION

        assert connection.execute("select candidate_id, run_id, surface_id, candidate_json from candidates").fetchall() == [("C1", "R1", "S1", '{"legacy":true}')]
        assert connection.execute("select candidate_id, run_id, plateau_id, surface_id from candidate_plateaus").fetchall() == [("C1", "R1", "P1", "S1")]
        assert connection.execute("select facts_state, unique_point_count, economic_eligible_point_count, event_eligible_point_count, plateau_count, ready_candidate_count, final_state from analysis_run_facts").fetchall() == [("UNAVAILABLE_LEGACY", None, None, None, None, None, "COMMITTED")]
        assert (connection.execute(f"select {surface_columns} from surfaces").fetchall(), connection.execute("select * from surface_points").fetchall()) == before
        assert connection.execute("select event_mode from surfaces").fetchall() == [("legacy_trades_proxy",)]
    finally:
        connection.close()


def test_v3_migration_preserves_data_and_marks_existing_surfaces_legacy() -> None:
    from mrs3 import analysis_storage

    connection = duckdb.connect(":memory:")
    try:
        _v1_with_one_candidate(connection)
        analysis_storage._migrate_v1_to_v2(connection)
        analysis_storage._migrate_v2_to_v3(connection)
        assert dict(connection.execute("select key, value from schema_info").fetchall())["schema_version"] == "3"
        before = connection.execute(
            "select candidate_id, run_id, surface_id, candidate_json from candidates"
        ).fetchall()

        assert ensure_analysis_schema(connection) == ANALYSIS_SCHEMA_VERSION

        assert connection.execute(
            "select candidate_id, run_id, surface_id, candidate_json from candidates"
        ).fetchall() == before
        assert connection.execute("select event_mode from surfaces").fetchall() == [
            ("legacy_trades_proxy",)
        ]
        assert connection.execute("select * from surface_point_events").fetchall() == []
        assert dict(connection.execute("select key, value from schema_info").fetchall())[
            "schema_version"
        ] == str(ANALYSIS_SCHEMA_VERSION)
    finally:
        connection.close()


def test_v1_migration_rolls_back_on_injected_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3 import analysis_storage

    connection = duckdb.connect(":memory:")
    try:
        _v1_with_one_candidate(connection)
        monkeypatch.setattr(analysis_storage, "EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT", "not-the-result")

        with pytest.raises(AnalysisSchemaError, match="migration DDL"):
            ensure_analysis_schema(connection)

        assert dict(connection.execute("select key, value from schema_info").fetchall())["schema_version"] == "1"
        assert "candidate_plateaus" not in _tables(connection)
        assert connection.execute("select candidate_id, plateau_id from candidates").fetchall() == [("C1", "P1")]
    finally:
        connection.close()


def test_read_only_library_rejects_legacy_schema_without_migrating(tmp_path: Path) -> None:
    from mrs3.analysis_storage import list_surface_library

    database = tmp_path / "legacy.duckdb"
    writable = duckdb.connect(str(database))
    _v1_with_one_candidate(writable)
    writable.close()

    read_only = duckdb.connect(str(database), read_only=True)
    try:
        with pytest.raises(AnalysisSchemaError):
            list_surface_library(read_only)
    finally:
        read_only.close()
    check = duckdb.connect(str(database), read_only=True)
    try:
        assert dict(check.execute("select key, value from schema_info").fetchall())["schema_version"] == "1"
    finally:
        check.close()


def test_claimed_v1_with_expected_table_names_but_wrong_schema_is_rejected(
    tmp_path: Path,
) -> None:
    from mrs3 import analysis_storage

    malformed = duckdb.connect(str(tmp_path / "malformed.duckdb"))
    try:
        malformed.execute("create table schema_info(key varchar, value varchar)")
        for table in sorted(REQUIRED_TABLES - {"schema_info"}):
            malformed.execute(f'create table "{table}"(wrong_column integer)')
        malformed.executemany(
            "insert into schema_info values (?, ?)",
            [
                ("schema_version", str(ANALYSIS_SCHEMA_VERSION)),
                ("schema_fingerprint", analysis_storage._schema_fingerprint(malformed)),
            ],
        )

        with pytest.raises(AnalysisSchemaError, match="fingerprint"):
            ensure_analysis_schema(malformed)
    finally:
        malformed.close()


def test_surface_and_run_constraints_preserve_required_lineage(connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection]) -> None:
    _, analysis = connections
    ensure_analysis_schema(analysis)
    analysis.execute(
        "insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')"
    )
    with pytest.raises(duckdb.ConstraintException):
        analysis.execute(
            "insert into surfaces(surface_id, parent_surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S2', 'missing', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')"
        )
    with pytest.raises(duckdb.ConstraintException):
        analysis.execute(
            "insert into surface_sources(surface_id, source_hash) values ('missing', ?)", ["a" * 64]
        )
    analysis.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{}')")
    with pytest.raises(duckdb.ConstraintException):
        analysis.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R2', 'missing', 'v1', '{}')")


def test_unique_point_and_plateau_member_identities_are_enforced(connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection]) -> None:
    _, analysis = connections
    ensure_analysis_schema(analysis)
    analysis.execute(
        "insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')"
    )
    analysis.execute("insert into surface_pairs(surface_id, pair_key) values ('S1', 'BTCUSDT|LONG|100|3|9')")
    analysis.execute("insert into surface_timeframes(surface_id, pair_key, timeframe) values ('S1', 'BTCUSDT|LONG|100|3|9', '15m')")
    analysis.execute("insert into surface_points(surface_id, canonical_point_key, pair_key, timeframe, point_event_count, source_report_id, source_hash, provenance_state, metrics_json) values ('S1', 'BTCUSDT|LONG|15m|100|3|9', 'BTCUSDT|LONG|100|3|9', '15m', 1, 'REPORT1', ?, 'REPRODUCIBLE', '{}')", ["a" * 64])
    with pytest.raises(duckdb.ConstraintException):
        analysis.execute("insert into surface_points(surface_id, canonical_point_key, pair_key, timeframe, point_event_count, source_report_id, source_hash, provenance_state, metrics_json) values ('S1', 'BTCUSDT|LONG|15m|100|3|9', 'BTCUSDT|LONG|100|3|9', '15m', 1, 'REPORT1', ?, 'REPRODUCIBLE', '{}')", ["a" * 64])
    analysis.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{}')")
    analysis.execute("insert into plateaus(run_id, plateau_id, surface_id) values ('R1', 'P1', 'S1')")
    analysis.execute("insert into plateau_members(run_id, plateau_id, surface_id, canonical_point_key) values ('R1', 'P1', 'S1', 'BTCUSDT|LONG|15m|100|3|9')")
    with pytest.raises(duckdb.ConstraintException):
        analysis.execute("insert into plateau_members(run_id, plateau_id, surface_id, canonical_point_key) values ('R1', 'P1', 'S1', 'BTCUSDT|LONG|15m|100|3|9')")


@pytest.mark.parametrize(
    "missing_field",
    ["source_report_id", "source_hash", "provenance_state", "metrics_json"],
)
def test_surface_points_require_final_provenance_and_metric_facts(
    connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection],
    missing_field: str,
) -> None:
    _, analysis = connections
    ensure_analysis_schema(analysis)
    analysis.execute("insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')")
    analysis.execute("insert into surface_pairs(surface_id, pair_key) values ('S1', 'PAIR')")
    analysis.execute("insert into surface_timeframes(surface_id, pair_key, timeframe) values ('S1', 'PAIR', '15m')")
    facts: dict[str, object] = {
        "source_report_id": "REPORT1",
        "source_hash": "a" * 64,
        "provenance_state": "REPRODUCIBLE",
        "metrics_json": "{}",
    }
    facts[missing_field] = None

    with pytest.raises(duckdb.ConstraintException):
        analysis.execute(
            """insert into surface_points(
                   surface_id, canonical_point_key, pair_key, timeframe,
                   point_event_count, source_report_id, source_hash,
                   provenance_state, metrics_json)
               values ('S1', 'POINT', 'PAIR', '15m', 1, ?, ?, ?, ?)""",
            list(facts.values()),
        )


def test_members_cannot_reference_a_point_from_another_run_surface(
    connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection],
) -> None:
    _, analysis = connections
    ensure_analysis_schema(analysis)
    for surface_id in ("S1", "S2"):
        analysis.execute(
            "insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values (?, 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')",
            [surface_id],
        )
        analysis.execute(
            "insert into surface_pairs(surface_id, pair_key) values (?, 'PAIR')", [surface_id]
        )
        analysis.execute(
            "insert into surface_timeframes(surface_id, pair_key, timeframe) values (?, 'PAIR', '15m')",
            [surface_id],
        )
        analysis.execute(
            "insert into surface_points(surface_id, canonical_point_key, pair_key, timeframe, point_event_count, source_report_id, source_hash, provenance_state, metrics_json) values (?, 'POINT', 'PAIR', '15m', 1, ?, ?, 'REPRODUCIBLE', '{}')",
            [surface_id, f"REPORT-{surface_id}", (surface_id.lower() * 32)],
        )
    analysis.execute("insert into analysis_runs values ('R1', 'S1', 'v1', '{}', current_timestamp)")
    analysis.execute("insert into plateaus(run_id, plateau_id, surface_id) values ('R1', 'P1', 'S1')")

    with pytest.raises(duckdb.ConstraintException):
        analysis.execute(
            "insert into plateau_members values ('R1', 'P1', 'S2', 'POINT')"
        )


def test_candidates_require_a_valid_plateau(
    connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection],
) -> None:
    _, analysis = connections
    ensure_analysis_schema(analysis)
    analysis.execute("insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')")
    analysis.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{}')")
    analysis.execute("insert into plateaus(run_id, plateau_id, surface_id) values ('R1', 'P1', 'S1')")

    with pytest.raises(duckdb.ConstraintException):
        analysis.execute("insert into candidates values ('C1', 'R1', 'missing', '{}')")
    analysis.execute("insert into candidates values ('C2', 'R1', 'S1', '{}')")
    with pytest.raises(duckdb.ConstraintException):
        analysis.execute("insert into candidate_plateaus values ('C2', 'R1', 'missing', 'S1')")
    analysis.execute("insert into candidate_plateaus values ('C2', 'R1', 'P1', 'S1')")


def test_lineage_endpoint_constraints_model_new_dropped_and_cross_run_relations(
    connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection],
) -> None:
    _, analysis = connections
    ensure_analysis_schema(analysis)
    for surface_id, run_id, plateau_id in (("S1", "R1", "P1"), ("S2", "R2", "P2")):
        analysis.execute("insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values (?, 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')", [surface_id])
        analysis.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values (?, ?, 'v1', '{}')", [run_id, surface_id])
        analysis.execute("insert into plateaus(run_id, plateau_id, surface_id) values (?, ?, ?)", [run_id, plateau_id, surface_id])

    analysis.execute("insert into plateau_lineage(lineage_id, child_run_id, child_plateau_id, relation) values ('L1', 'R2', 'P2', 'NEW')")
    analysis.execute("insert into plateau_lineage(lineage_id, parent_run_id, parent_plateau_id, relation) values ('L2', 'R1', 'P1', 'DROPPED')")
    analysis.execute("insert into plateau_lineage values ('L3', 'R2', 'P2', 'R1', 'P1', 'CONTINUED', null)")

    invalid = (
        "insert into plateau_lineage values ('X1', 'R2', 'P2', 'R1', 'P1', 'NEW', null)",
        "insert into plateau_lineage values ('X2', 'R2', 'P2', 'R1', 'P1', 'DROPPED', null)",
        "insert into plateau_lineage(lineage_id, child_run_id, child_plateau_id, relation) values ('X3', 'R2', 'P2', 'MERGED')",
        "insert into plateau_lineage(lineage_id, child_run_id, relation) values ('X4', 'R2', 'NEW')",
    )
    for statement in invalid:
        with pytest.raises(duckdb.ConstraintException):
            analysis.execute(statement)


def test_bootstrap_rolls_back_on_creation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3 import analysis_storage

    analysis = duckdb.connect(str(tmp_path / "analysis.duckdb"))
    try:
        def fail_after_create(connection: duckdb.DuckDBPyConnection) -> None:
            connection.execute("create table transient_table(id integer)")
            raise duckdb.TransactionException("injected failure")

        monkeypatch.setattr(analysis_storage, "_create_tables", fail_after_create)
        with pytest.raises(duckdb.TransactionException, match="injected failure"):
            ensure_analysis_schema(analysis)
        assert _tables(analysis) == set()
    finally:
        analysis.close()


def test_v1_rectangular_identity_and_validation_remain_literal(connections) -> None:
    from mrs3.analysis_storage import _surface_identity, publish_surface
    _, analysis = connections
    surface = _surface()
    surface_id, identity = _surface_identity(surface)
    assert identity["grid_contract"]["kind"] == "OBSERVED_GRID_CONTRACT"
    assert publish_surface(analysis, surface).surface_id == surface_id


def test_v1_allows_distinct_points_sharing_provenance_but_v2_rejects(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    v1 = _surface()
    shared = (v1.points[0].source_report_id, v1.points[0].source_hash)
    second_key = "BTCUSDT|LONG|1h|200|3|9"
    empty_metrics = {
        "TotalTrades": 0,
        "TotalPnLPercent": 0,
        "MaxDrawdownPercent": 0,
        "Win": 0,
        "Los": 0,
        "WinRate": 0,
        "ProfitFactor": None,
    }
    shared_points = (
        v1.points[0],
        DirectPoint(second_key, shared[0], shared[1], 0, empty_metrics),
    )
    shared_v1 = replace(
        v1,
        request=replace(v1.request, required_shifts_bp=(100, 200)),
        preflight=replace(
            v1.preflight,
            manifest=(shared,),
            source_hashes=(shared[1],),
            accepted_point_keys=(v1.points[0].canonical_point_key, second_key),
            grid_contract={
                **v1.preflight.grid_contract,
                "required_shifts_bp": (100, 200),
                "pairs": ("100|3|9", "200|3|9"),
            },
        ),
        points=shared_points,
    )
    assert publish_surface(analysis, shared_v1).created is True

    v2 = _v2_surface()
    evidence = point_evidence_jsonl_bytes(shared_points)
    shared_v2 = replace(
        v2,
        points=shared_points,
        preflight=replace(
            v2.preflight,
            manifest=(shared,),
            source_hashes=(shared[1],),
            accepted_point_keys=(v2.points[0].canonical_point_key, second_key),
            grid_contract={
                **v2.preflight.grid_contract,
                "point_evidence": evidence.decode("utf-8"),
                "point_evidence_sha256": sha256(evidence).hexdigest(),
            },
        ),
    )
    with pytest.raises(ValueError, match="one-to-one|manifest"):
        publish_surface(analysis, shared_v2)


def test_v2_evidence_persists_in_existing_schema_v4_grid_contract_json(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    surface = _v2_surface()
    published = publish_surface(analysis, surface)

    assert analysis.execute("select value from schema_info where key = 'schema_version'").fetchone() == ("4",)
    assert _tables(analysis) == REQUIRED_TABLES
    stored = json.loads(
        analysis.execute(
            "select grid_contract_json from surfaces where surface_id = ?",
            [published.surface_id],
        ).fetchone()[0]
    )
    expected_evidence = point_evidence_jsonl_bytes(surface.points).decode("utf-8")
    assert stored["kind"] == V2_GRID_CONTRACT_KIND
    assert stored["selected_scopes"] == ["BTCUSDT|1h"]
    assert stored["witnesses"]["BTCUSDT|1h"][0]["open_ma"] == 3
    assert stored["point_evidence"] == expected_evidence
    assert stored["point_evidence_sha256"] == sha256(expected_evidence.encode("utf-8")).hexdigest()
    assert stored["audit_artifact_name"] == "surface_coverage_audit_LONG.csv"
    assert stored["audit_schema_version"] == 1
    assert stored["audit_row_count"] == 1
    assert stored["audit_sha256"] == sha256(b"pair,side\nBTC,LONG\n").hexdigest()


def test_v2_identity_changes_with_witness_point_assignment_and_audit_hash(connections) -> None:
    from mrs3.analysis_storage import _surface_identity

    _, analysis = connections
    surface = _v2_surface()
    baseline = _surface_identity(surface)[0]

    witness_changed = replace(
        surface,
        preflight=replace(
            surface.preflight,
            grid_contract={
                **surface.preflight.grid_contract,
                "witnesses": {
                    "BTCUSDT|1h": {
                        "open_ma": 4,
                        "close_ma": 10,
                        "shifts_bp": list(tuple(range(30, 151, 10)) + tuple(range(190, 431, 40))),
                    },
                },
            },
        ),
    )
    assert _surface_identity(witness_changed)[0] != baseline

    assigned_point = replace(
        surface.points[0],
        source_report_id="other-report",
        source_hash="b" * 64,
    )
    assigned_evidence = point_evidence_jsonl_bytes((assigned_point,))
    assigned = replace(
        surface,
        points=(assigned_point,),
        preflight=replace(
            surface.preflight,
            source_hashes=("b" * 64,),
            manifest=(("other-report", "b" * 64),),
            grid_contract={
                **surface.preflight.grid_contract,
                "point_evidence": assigned_evidence.decode("utf-8"),
                "point_evidence_sha256": sha256(assigned_evidence).hexdigest(),
            },
        ),
    )
    assert _surface_identity(assigned)[0] != baseline

    audit_changed = replace(
        surface,
        preflight=replace(
            surface.preflight,
            grid_contract={**surface.preflight.grid_contract, "audit_sha256": "b" * 64},
        ),
    )
    assert _surface_identity(audit_changed)[0] != baseline


def test_v2_rejects_malformed_duplicate_and_non_roundtripping_point_evidence(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    surface = _v2_surface()

    malformed_key = "BTCUSDT|LONG|1h|100"
    malformed_point = replace(surface.points[0], canonical_point_key=malformed_key)
    malformed = replace(
        surface,
        points=(malformed_point,),
        preflight=replace(
            surface.preflight,
            accepted_point_keys=(malformed_key,),
        ),
    )
    with pytest.raises(ValueError, match="six fields"):
        publish_surface(analysis, malformed)

    duplicate_point = replace(surface.points[0], source_report_id="duplicate", source_hash="b" * 64)
    duplicate_evidence = point_evidence_jsonl_bytes((surface.points[0], duplicate_point))
    duplicate = replace(
        surface,
        points=(surface.points[0], duplicate_point),
        preflight=replace(
            surface.preflight,
            source_hashes=("a" * 64, "b" * 64),
            manifest=(("report", "a" * 64), ("duplicate", "b" * 64)),
            accepted_point_keys=(surface.points[0].canonical_point_key,) * 2,
            grid_contract={
                **surface.preflight.grid_contract,
                "point_evidence": duplicate_evidence.decode("utf-8"),
                "point_evidence_sha256": sha256(duplicate_evidence).hexdigest(),
            },
        ),
    )
    with pytest.raises(ValueError, match="duplicate|uniqueness"):
        publish_surface(analysis, duplicate)

    non_roundtrip_key = "BTCUSDT|LONG|1h|0100|3|9"
    non_roundtrip_point = replace(surface.points[0], canonical_point_key=non_roundtrip_key)
    non_roundtrip = replace(
        surface,
        points=(non_roundtrip_point,),
        preflight=replace(
            surface.preflight,
            accepted_point_keys=(non_roundtrip_key,),
        ),
    )
    with pytest.raises(ValueError, match="round-trip|roundtrip"):
        publish_surface(analysis, non_roundtrip)


def test_v2_publish_verifies_audit_bytes_hash_and_row_count(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    audit = b"pair,side\nBTC,LONG\n"
    surface = _v2_surface(
        audit_bytes=audit,
        audit_row_count=1,
        audit_sha256=sha256(audit).hexdigest(),
    )
    assert publish_surface(analysis, surface).created is True

    bad_hash = replace(surface.preflight, audit_sha256="b" * 64)
    with pytest.raises(ValueError, match="audit"):
        publish_surface(analysis, replace(surface, preflight=bad_hash))

    bad_row_count = replace(surface.preflight, audit_row_count=999)
    with pytest.raises(ValueError, match="audit"):
        publish_surface(analysis, replace(surface, preflight=bad_row_count))

    bad_bytes = replace(surface.preflight, audit_bytes=b"pair,side\nOTHER,LONG\n")
    with pytest.raises(ValueError, match="audit"):
        publish_surface(analysis, replace(surface, preflight=bad_bytes))


def test_v2_publish_requires_audit_evidence_and_valid_schema(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections

    missing = _v2_surface(audit_bytes=b"", audit_sha256="", audit_row_count=0)
    with pytest.raises(ValueError, match="audit"):
        publish_surface(analysis, missing)

    invalid_schema = _v2_surface(audit_schema_version=0)
    with pytest.raises(ValueError, match="audit|schema"):
        publish_surface(analysis, invalid_schema)


def test_v2_audit_metadata_is_exactly_bound_across_request_preflight_and_contract(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    base = _v2_surface()

    missing_preflight_size = replace(
        base,
        preflight=replace(base.preflight, audit_size_bytes=0),
    )
    with pytest.raises(ValueError, match="audit metadata"):
        publish_surface(analysis, missing_preflight_size)

    missing_request_name = replace(
        base,
        request=replace(base.request, audit_artifact_name=""),
    )
    with pytest.raises(ValueError, match="audit metadata|artifact name"):
        publish_surface(analysis, missing_request_name)

    wrong_name = "surface_coverage_audit_SHORT.csv"
    renamed = replace(
        base,
        request=replace(base.request, audit_artifact_name=wrong_name),
        preflight=replace(base.preflight, audit_artifact_name=wrong_name),
    )
    renamed_contract = {
        **renamed.preflight.grid_contract,
        "audit_artifact_name": wrong_name,
    }
    renamed = replace(renamed, preflight=replace(renamed.preflight, grid_contract=renamed_contract))
    with pytest.raises(ValueError, match="artifact name|metadata"):
        publish_surface(analysis, renamed)

    wrong_schema = _v2_surface(audit_schema_version=2)
    with pytest.raises(ValueError, match="exactly 1"):
        publish_surface(analysis, wrong_schema)


def test_v2_publish_defines_audit_row_count_as_data_rows_excluding_header(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    audit = b"pair,side\nrow1,LONG\nrow2,SHORT\n"

    header_inclusive = _v2_surface(
        audit_bytes=audit,
        audit_row_count=3,
        audit_sha256=sha256(audit).hexdigest(),
    )
    with pytest.raises(ValueError, match="row count"):
        publish_surface(analysis, header_inclusive)

    data_rows = _v2_surface(
        audit_bytes=audit,
        audit_row_count=2,
        audit_sha256=sha256(audit).hexdigest(),
    )
    assert publish_surface(analysis, data_rows).created is True


def test_v2_rejects_two_point_keys_sharing_one_report_source_assignment(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    surface = _v2_surface()
    shared = (surface.points[0].source_report_id, surface.points[0].source_hash)
    second_key = "BTCUSDT|LONG|1h|200|3|9"
    empty_metrics = {
        "TotalTrades": 0,
        "TotalPnLPercent": 0,
        "MaxDrawdownPercent": 0,
        "Win": 0,
        "Los": 0,
        "WinRate": 0,
        "ProfitFactor": None,
    }
    second_point = DirectPoint(second_key, shared[0], shared[1], 0, empty_metrics)
    points = (surface.points[0], second_point)
    evidence = point_evidence_jsonl_bytes(points)
    preflight = replace(
        surface.preflight,
        manifest=(shared,),
        source_hashes=(shared[1],),
        accepted_point_keys=(surface.points[0].canonical_point_key, second_key),
        grid_contract={
            **surface.preflight.grid_contract,
            "point_evidence": evidence.decode("utf-8"),
            "point_evidence_sha256": sha256(evidence).hexdigest(),
        },
    )

    with pytest.raises(ValueError, match="one-to-one|manifest"):
        publish_surface(analysis, replace(surface, points=points, preflight=preflight))


def test_v2_rejects_non_canonical_grid_contract_json(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections

    nested_float = replace(
        _v2_surface(),
        preflight=replace(
            _v2_surface().preflight,
            grid_contract={
                **_v2_surface().preflight.grid_contract,
                "extra": {"value": 1.5},
            },
        ),
    )
    with pytest.raises(ValueError, match="float|canonical"):
        publish_surface(analysis, nested_float)

    non_string_key = replace(
        _v2_surface(),
        preflight=replace(
            _v2_surface().preflight,
            grid_contract={
                **_v2_surface().preflight.grid_contract,
                "extra": {42: "not-a-string-key"},
            },
        ),
    )
    with pytest.raises(ValueError, match="key|canonical"):
        publish_surface(analysis, non_string_key)


def test_v2_rejects_noncanonical_witnesses_and_scope_identity(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    def with_witness(
        surface: DirectSurface,
        witness_value: object,
        scopes=None,
        extra_scope: str | None = None,
    ) -> DirectSurface:
        selected = scopes or surface.preflight.grid_contract["selected_scopes"]
        base_vector = surface.preflight.grid_contract["witnesses"]["BTCUSDT|1h"]
        if isinstance(witness_value, dict):
            vector = [dict(item) for item in base_vector]
            vector[0] = dict(witness_value)
        else:
            vector = witness_value
        witnesses = {"BTCUSDT|1h": vector}
        if extra_scope is not None:
            witnesses[extra_scope] = vector
        return replace(
            surface,
            preflight=replace(
                surface.preflight,
                grid_contract={
                    **surface.preflight.grid_contract,
                    "selected_scopes": selected,
                    "witnesses": witnesses,
                },
            ),
        )

    _, analysis = connections
    base = _v2_surface()
    valid_shifts = DEFAULT_CANONICAL_SHIFTS_BP
    witness = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "open_ma": 3,
        "close_ma": 2,
        "shifts_bp": list(valid_shifts),
        "contract_version": READINESS_CONTRACT_VERSION,
        "max_shift_bp": READINESS_MAX_SHIFT_BP,
    }

    cases = (
        (with_witness(base, {**witness, "extra_field": 1}), "exact|keys"),
        (with_witness(base, {key: value for key, value in witness.items() if key != "symbol"}), "exact|keys"),
        (with_witness(base, {key: value for key, value in witness.items() if key != "timeframe"}), "exact|keys"),
        (with_witness(base, {key: value for key, value in witness.items() if key != "side"}), "exact|keys"),
        (with_witness(base, {key: value for key, value in witness.items() if key != "contract_version"}), "exact|keys"),
        (with_witness(base, {**witness, "symbol": "ETHUSDT"}), "symbol|scope"),
        (with_witness(base, {**witness, "timeframe": "4h"}), "timeframe|scope"),
        (with_witness(base, {**witness, "side": "SHORT"}), "scope|side"),
        (with_witness(base, {**witness, "contract_version": "shift_readiness_v2"}), "contract"),
        (with_witness(base, {**witness, "max_shift_bp": 700}), "contract|invalid"),
        (with_witness(base, {**witness, "max_shift_bp": True}), "contract|invalid"),
        (with_witness(base, {**witness, "open_ma": True}), "integer"),
        (with_witness(base, {**witness, "shifts_bp": [30, True, 150, 430]}), "shifts_bp is invalid"),
        (with_witness(base, {**witness, "shifts_bp": [30, 50, 150, 190, 230, 270, 310, 350, 390, 430]}), "canonical shifts"),
        (with_witness(base, {**witness, "shifts_bp": [30, 50, 100, 150, 190, 230, 270, 310, 350, 390, 430]}), "canonical shifts"),
        (with_witness(base, {**witness, "shifts_bp": [30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150, 200, 430]}), "canonical shifts"),
        (with_witness(base, {**witness, "shifts_bp": [30, 150]}), "canonical shifts"),
        (with_witness(base, witness, extra_scope="BTCUSDT|4h"), "exactly one"),
        (with_witness(base, witness, scopes=["BTCUSDT|1h", "BTCUSDT|1h"]), "unique|exactly one"),
    )

    for surface, pattern in cases:
        with pytest.raises(ValueError, match=pattern):
            publish_surface(analysis, surface)


def test_v2_admission_requires_canonical_grid_and_six_witness_vector(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    base = _v2_surface()
    missing_grid = replace(
        base,
        preflight=replace(
            base.preflight,
            grid_contract={key: value for key, value in base.preflight.grid_contract.items() if key != "canonical_grid_version"},
        ),
    )
    with pytest.raises(ValueError, match="canonical grid version"):
        publish_surface(analysis, missing_grid)

    singular = replace(
        base,
        preflight=replace(
            base.preflight,
            grid_contract={
                **base.preflight.grid_contract,
                "witnesses": {"BTCUSDT|1h": base.preflight.grid_contract["witnesses"]["BTCUSDT|1h"][0]},
            },
        ),
    )
    with pytest.raises(ValueError, match="six entries"):
        publish_surface(analysis, singular)


@pytest.mark.parametrize("request_kind, preflight_kind", [
    (V2_GRID_CONTRACT_KIND, "OBSERVED_GRID_CONTRACT"),
    ("OBSERVED_GRID_CONTRACT", V2_GRID_CONTRACT_KIND),
])
def test_v2_admission_requires_matching_request_and_preflight_contract_kinds(
    connections, request_kind: str, preflight_kind: str
) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    base = _v2_surface()
    mismatched = replace(
        base,
        request=replace(base.request, grid_contract_kind=request_kind),
        preflight=replace(
            base.preflight,
            grid_contract={**base.preflight.grid_contract, "kind": preflight_kind},
        ),
    )

    with pytest.raises(ValueError, match="V2 request and preflight grid contracts must agree"):
        publish_surface(analysis, mismatched)


def test_v2_rejects_self_consistent_non_default_canonical_shift_tuple(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    non_default = tuple(range(30, 151, 10)) + tuple(range(190, 431, 40))
    non_default_hash = canonical_point_materialization_config_hash(non_default)
    base = _v2_surface()
    witness = [
        {**item, "shifts_bp": list(non_default)}
        for item in base.preflight.grid_contract["witnesses"]["BTCUSDT|1h"]
    ]
    contract = {
        **base.preflight.grid_contract,
        "canonical_shifts_bp": list(non_default),
        "point_materialization_config_hash": non_default_hash,
        "witnesses": {"BTCUSDT|1h": witness},
    }
    surface = replace(
        base,
        request=replace(
            base.request,
            required_shifts_bp=non_default,
            point_materialization_config_hash=non_default_hash,
        ),
        preflight=replace(base.preflight, grid_contract=contract),
    )

    with pytest.raises(ValueError, match="canonical shifts"):
        publish_surface(analysis, surface)


def test_v2_rejects_non_canonical_materializer_version(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    surface = replace(
        _v2_surface(),
        request=replace(_v2_surface().request, materializer_version="v2-real-events"),
    )

    with pytest.raises(ValueError, match="materializer"):
        publish_surface(analysis, surface)


def test_v2_rejects_non_canonical_normalization_contract_version(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    surface = replace(
        _v2_surface(),
        preflight=replace(
            _v2_surface().preflight,
            grid_contract={
                **_v2_surface().preflight.grid_contract,
                "normalization_contract_version": "v0",
            },
        ),
    )

    with pytest.raises(ValueError, match="normalization"):
        publish_surface(analysis, surface)


def test_v2_grid_witnesses_are_not_restored_from_preflight_fallback(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    base = _v2_surface()
    contract = {
        key: value for key, value in base.preflight.grid_contract.items() if key != "witnesses"
    }
    missing_grid_witnesses = replace(
        base,
        preflight=replace(base.preflight, grid_contract=contract),
    )

    with pytest.raises(ValueError, match="exactly one witness"):
        publish_surface(analysis, missing_grid_witnesses)


def test_v2_audit_metadata_is_not_restored_from_preflight_fallback(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    base = _v2_surface()
    contract = {
        key: value for key, value in base.preflight.grid_contract.items()
        if key not in {
            "audit_artifact_name",
            "audit_schema_version",
            "audit_size_bytes",
            "audit_row_count",
            "audit_sha256",
        }
    }
    missing_audit_metadata = replace(
        base,
        preflight=replace(base.preflight, grid_contract=contract),
    )

    with pytest.raises(ValueError, match="audit metadata|audit size mismatch"):
        publish_surface(analysis, missing_audit_metadata)


def test_v2_admission_rejects_persisted_legacy_trades_proxy_event_mode(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    downgraded = replace(_v2_surface(), event_mode="legacy_trades_proxy")

    with pytest.raises(ValueError, match="real_independent_events"):
        publish_surface(analysis, downgraded)


def _published_v2(
    analysis: duckdb.DuckDBPyConnection, period_index: int = 0
) -> tuple[str, dict[str, object]]:
    from mrs3.analysis_storage import publish_surface

    base = _v2_surface()
    day = 1 + period_index
    surface = publish_surface(
        analysis,
        replace(
            base,
            request=replace(
                base.request,
                start_utc=f"2024-01-{day:02d}T00:00:00Z",
                end_utc=f"2024-01-{day:02d}T02:00:00Z",
            ),
        ),
    )
    contract = json.loads(
        analysis.execute(
            "select grid_contract_json from surfaces where surface_id=?", [surface.surface_id]
        ).fetchone()[0]
    )
    return surface.surface_id, contract


def _set_contract(
    analysis: duckdb.DuckDBPyConnection, surface_id: str, contract: dict[str, object]
) -> None:
    analysis.execute(
        "update surfaces set grid_contract_json=? where surface_id=?",
        [json.dumps(contract), surface_id],
    )


def test_require_canonical_operational_surface_accepts_fresh_v2_surface(connections) -> None:
    from mrs3.analysis_storage import require_canonical_operational_surface

    _, analysis = connections
    surface_id, _ = _published_v2(analysis)

    require_canonical_operational_surface(analysis, surface_id)


def test_require_canonical_operational_surface_rejects_missing_or_wrong_grid_fields(connections) -> None:
    from mrs3.analysis_storage import require_canonical_operational_surface

    _, analysis = connections

    surface_id, _ = _published_v2(analysis)
    analysis.execute("update surfaces set grid_contract_json=NULL where surface_id=?", [surface_id])
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, contract = _published_v2(analysis, 1)
    _set_contract(
        analysis, surface_id,
        {key: value for key, value in contract.items() if key != "kind"},
    )
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, contract = _published_v2(analysis, 2)
    _set_contract(analysis, surface_id, {**contract, "kind": "OBSERVED_GRID_CONTRACT"})
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)


def test_require_canonical_operational_surface_rejects_missing_or_wrong_grid_version(connections) -> None:
    from mrs3.analysis_storage import require_canonical_operational_surface

    _, analysis = connections

    surface_id, contract = _published_v2(analysis)
    _set_contract(
        analysis, surface_id,
        {key: value for key, value in contract.items() if key != "canonical_grid_version"},
    )
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, contract = _published_v2(analysis, 1)
    _set_contract(analysis, surface_id, {**contract, "canonical_grid_version": "mrs3_shift_grid_30_430_v1"})
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)


def test_require_canonical_operational_surface_rejects_missing_or_wrong_canonical_shifts(connections) -> None:
    from mrs3.analysis_storage import require_canonical_operational_surface

    _, analysis = connections

    surface_id, contract = _published_v2(analysis)
    _set_contract(
        analysis, surface_id,
        {key: value for key, value in contract.items() if key != "canonical_shifts_bp"},
    )
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, contract = _published_v2(analysis, 1)
    wrong_order = list(DEFAULT_CANONICAL_SHIFTS_BP)
    wrong_order[0], wrong_order[1] = wrong_order[1], wrong_order[0]
    _set_contract(analysis, surface_id, {**contract, "canonical_shifts_bp": wrong_order})
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, contract = _published_v2(analysis, 2)
    wrong_value = [*DEFAULT_CANONICAL_SHIFTS_BP, 600]
    _set_contract(analysis, surface_id, {**contract, "canonical_shifts_bp": wrong_value})
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)


def test_require_canonical_operational_surface_rejects_missing_or_wrong_readiness_version(connections) -> None:
    from mrs3.analysis_storage import require_canonical_operational_surface

    _, analysis = connections

    surface_id, contract = _published_v2(analysis)
    _set_contract(
        analysis, surface_id,
        {key: value for key, value in contract.items() if key != "readiness_contract_version"},
    )
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, contract = _published_v2(analysis, 1)
    _set_contract(analysis, surface_id, {**contract, "readiness_contract_version": "close_ma_2_7_legacy_v1"})
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)


def test_require_canonical_operational_surface_rejects_old_or_arbitrary_materializer(connections) -> None:
    from mrs3.analysis_storage import require_canonical_operational_surface

    _, analysis = connections

    surface_id, _ = _published_v2(analysis)
    analysis.execute("update surfaces set materializer_version=NULL where surface_id=?", [surface_id])
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, _ = _published_v2(analysis, 1)
    analysis.execute("update surfaces set materializer_version='v2-real-events' where surface_id=?", [surface_id])
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, _ = _published_v2(analysis, 2)
    analysis.execute(
        "update surfaces set materializer_version='v4-canonical-grid-parallel-hotfix' where surface_id=?",
        [surface_id],
    )
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)


def test_require_canonical_operational_surface_rejects_missing_or_wrong_semantics_version(connections) -> None:
    from mrs3.analysis_storage import require_canonical_operational_surface

    _, analysis = connections

    surface_id, contract = _published_v2(analysis)
    _set_contract(
        analysis, surface_id,
        {key: value for key, value in contract.items() if key != "point_materialization_semantics_version"},
    )
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, contract = _published_v2(analysis, 1)
    _set_contract(
        analysis, surface_id,
        {**contract, "point_materialization_semantics_version": "legacy_point_materialization_v1"},
    )
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)


def test_require_canonical_operational_surface_rejects_wrong_or_variant_config_hash(connections) -> None:
    from mrs3.analysis_storage import require_canonical_operational_surface
    from mrs3.duckdb_direct import canonical_point_materialization_semantic_payload

    _, analysis = connections
    payload = canonical_point_materialization_semantic_payload(DEFAULT_CANONICAL_SHIFTS_BP)
    canonical_hash = canonical_point_materialization_config_hash(DEFAULT_CANONICAL_SHIFTS_BP)

    def variant_hash(label: str) -> str:
        if label == "pretty":
            text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True)
        elif label == "newline":
            text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        elif label == "omitted":
            subset = {key: value for key, value in payload.items() if key != "event_id_contract"}
            text = json.dumps(subset, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        else:
            text = json.dumps(
                {**payload, "extra_field": "x"}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
        return sha256(text.encode("ascii")).hexdigest()

    for label in ("pretty", "newline", "omitted", "extra"):
        digest = variant_hash(label)
        assert digest != canonical_hash
        surface_id, contract = _published_v2(analysis)
        analysis.execute(
            "update surfaces set point_materialization_config_hash=? where surface_id=?",
            [digest, surface_id],
        )
        _set_contract(analysis, surface_id, {**contract, "point_materialization_config_hash": digest})
        with pytest.raises(ValueError, match="config hash"):
            require_canonical_operational_surface(analysis, surface_id)

    surface_id, contract = _published_v2(analysis, 1)
    wrong = "f" * 64
    analysis.execute(
        "update surfaces set point_materialization_config_hash=? where surface_id=?",
        [wrong, surface_id],
    )
    _set_contract(analysis, surface_id, {**contract, "point_materialization_config_hash": wrong})
    with pytest.raises(ValueError, match="config hash"):
        require_canonical_operational_surface(analysis, surface_id)


def test_require_canonical_operational_surface_rejects_wrong_event_mode(connections) -> None:
    from mrs3.analysis_storage import require_canonical_operational_surface

    _, analysis = connections
    surface_id, _ = _published_v2(analysis)
    analysis.execute("update surfaces set event_mode='legacy_trades_proxy' where surface_id=?", [surface_id])

    with pytest.raises(ValueError, match="event_mode"):
        require_canonical_operational_surface(analysis, surface_id)


def test_require_canonical_operational_surface_rejects_malformed_witnesses(connections) -> None:
    from mrs3.analysis_storage import require_canonical_operational_surface

    _, analysis = connections

    surface_id, contract = _published_v2(analysis)
    vector = contract["witnesses"]["BTCUSDT|1h"]
    _set_contract(analysis, surface_id, {**contract, "witnesses": {"BTCUSDT|1h": vector[:5]}})
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, contract = _published_v2(analysis, 1)
    _set_contract(analysis, surface_id, {**contract, "witnesses": {}})
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, contract = _published_v2(analysis, 2)
    reordered = [dict(item) for item in contract["witnesses"]["BTCUSDT|1h"]]
    reordered[0], reordered[1] = reordered[1], reordered[0]
    _set_contract(analysis, surface_id, {**contract, "witnesses": {"BTCUSDT|1h": reordered}})
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, contract = _published_v2(analysis, 3)
    shifted = [dict(item) for item in contract["witnesses"]["BTCUSDT|1h"]]
    shifted[0]["shifts_bp"] = [*DEFAULT_CANONICAL_SHIFTS_BP[:-1], 600]
    _set_contract(analysis, surface_id, {**contract, "witnesses": {"BTCUSDT|1h": shifted}})
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)


def test_require_canonical_operational_surface_rejects_malformed_audit(connections) -> None:
    from mrs3.analysis_storage import require_canonical_operational_surface

    _, analysis = connections

    surface_id, contract = _published_v2(analysis)
    _set_contract(analysis, surface_id, {**contract, "audit_schema_version": 2})
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, contract = _published_v2(analysis, 1)
    _set_contract(analysis, surface_id, {**contract, "audit_row_count": -1})
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, contract = _published_v2(analysis, 2)
    _set_contract(analysis, surface_id, {**contract, "audit_size_bytes": 0})
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, contract = _published_v2(analysis, 3)
    _set_contract(analysis, surface_id, {**contract, "audit_sha256": "z" * 64})
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)


def test_require_canonical_operational_surface_rejects_malformed_point_evidence(connections) -> None:
    from mrs3.analysis_storage import require_canonical_operational_surface

    _, analysis = connections

    surface_id, contract = _published_v2(analysis)
    _set_contract(analysis, surface_id, {**contract, "point_evidence": "not-jsonl"})
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)

    surface_id, contract = _published_v2(analysis, 1)
    _set_contract(analysis, surface_id, {**contract, "point_evidence_sha256": "f" * 64})
    with pytest.raises(ValueError, match="fresh canonical"):
        require_canonical_operational_surface(analysis, surface_id)


def test_parent_materialization_rejects_legacy_parent_surface(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    legacy = publish_surface(analysis, _surface())

    with pytest.raises(ValueError, match="fresh canonical"):
        publish_surface(analysis, replace(_v2_surface(), parent_surface_id=legacy.surface_id))

"""W1..W6: materialize without hydrating the selection into the coordinator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_source_v6_empty_results import _grid_with_one_idle, _healthy_and_empty


def _source_db(tmp_path: Path, fragments) -> Path:
    from mrs3.source_v6 import encode_fragment
    from mrs3.source_v6_storage import create_v6_database, import_fragment_batch

    database = tmp_path / "source.duckdb"
    create_v6_database(database, database_id="db-worker-materialization")
    import_fragment_batch(
        database, [(item, encode_fragment(item)) for item in fragments]
    )
    return database


def _scope_of(fragment) -> str:
    point = fragment.point
    return f"{point.symbol}|{point.side}|{point.timeframe}"


@pytest.mark.parametrize("workers", [1, 4])
def test_the_database_path_materializes_exactly_what_the_hydrated_path_does(
    tmp_path: Path, workers: int
) -> None:
    """W1--W5: same ids, same order, same witnesses, same empty results.

    The point of the change is that this equivalence holds while no payload is
    decoded in this process, so it is the equivalence the whole design rests on.
    """
    from mrs3.source_v6_materializer import (
        materialize_source_v6,
        materialize_source_v6_from_database,
    )

    facts, idle_key = _grid_with_one_idle()
    database = _source_db(tmp_path, facts)
    scope = _scope_of(facts[0])

    hydrated = materialize_source_v6(facts, (scope,))
    ids = materialize_source_v6_from_database(
        database, (scope,), workers=workers,
        source_content_digest_value=hydrated.source_content_digest,
    )

    assert [item.scope_key for item in ids.scopes] == [item.scope_key for item in hydrated.scopes]
    assert [item.ready_witness for item in ids.scopes] == [
        item.ready_witness for item in hydrated.scopes
    ]
    assert [[fact.fragment_id for fact in item.facts] for item in ids.scopes] == [
        [fact.fragment_id for fact in item.facts] for item in hydrated.scopes
    ]
    assert [[fact.point_key for fact in item.facts] for item in ids.scopes] == [
        [fact.point.canonical_key for fact in item.facts] for item in hydrated.scopes
    ]
    assert ids.empty_result_points == hydrated.empty_result_points
    assert [item["point_key"] for item in ids.empty_result_points] == [idle_key]


@pytest.mark.parametrize("workers", [1, 4])
def test_the_published_surface_is_identical_whichever_path_built_it(
    tmp_path: Path, workers: int
) -> None:
    """W3 and W6: identity does not depend on the path or on the worker count."""
    import duckdb

    from mrs3.source_v6_materializer import (
        materialize_source_v6,
        materialize_source_v6_from_database,
    )
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface

    facts, _idle_key = _grid_with_one_idle()
    database = _source_db(tmp_path, facts)
    scope = _scope_of(facts[0])

    def manifest(path: Path) -> dict:
        connection = duckdb.connect(str(path), read_only=True)
        try:
            rows = dict(connection.execute("select key, value from manifest").fetchall())
            scopes = connection.execute(
                "select scope_key, scope_digest, witness_json from scope_manifests"
            ).fetchall()
            fragments = sorted(
                connection.execute(
                    "select scope_key, fragment_id, point_key, payload_sha256 from factual_fragments"
                ).fetchall()
            )
        finally:
            connection.close()
        return {"manifest": rows, "scopes": sorted(scopes), "fragments": fragments}

    hydrated = publish_multiscope_surface(
        tmp_path / "a", materialize_source_v6(facts, (scope,)), source_database=database,
    )
    from_ids = publish_multiscope_surface(
        tmp_path / "b",
        materialize_source_v6_from_database(
            database, (scope,), workers=workers,
            source_content_digest_value=materialize_source_v6(facts, (scope,)).source_content_digest,
        ),
        source_database=database, workers=workers,
    )

    fast, slow = manifest(from_ids), manifest(hydrated)
    # W8: both paths carry the same precomputed measurements and digest.
    assert fast["manifest"]["analysis_input_digest"]
    assert slow["manifest"]["analysis_input_digest"] == fast["manifest"][
        "analysis_input_digest"
    ]
    assert fast == slow


def test_no_hydrated_fragment_crosses_the_worker_boundary(tmp_path: Path) -> None:
    """W2: the worker returns a verdict, never the fragments it measured."""
    from mrs3.source_v6 import SourceV6Fragment
    from mrs3.source_v6_materializer import _witness_window, measure_point_group
    from mrs3.source_v6_coverage import canonical_ready_intervals

    facts, idle_key = _grid_with_one_idle()
    database = _source_db(tmp_path, facts)
    witness = canonical_ready_intervals(facts)[0]
    idle_ids = tuple(
        item.fragment_id for item in facts if item.point.canonical_key == idle_key
    )

    verdict = measure_point_group(
        str(database), idle_key, idle_ids, _witness_window(witness)
    )

    point_key, returned_ids, empty, row = verdict
    assert point_key == idle_key
    assert returned_ids == idle_ids
    assert empty is not None and empty["point_key"] == idle_key
    # W7: the row is scalars and event ids. The balance and equity series stay
    # in the worker, which is what keeps the boundary small.
    assert row["point_id"] == idle_key and row["trades"] == 0
    assert row["events_last_30d"] == 0
    assert set(row) == {
        "point_id", "symbol", "side", "timeframe", "shift_bp", "open_ma", "close_ma",
        "pnl_pct", "dd_pct", "trades", "wins", "losses", "win_rate_pct",
        "profit_factor", "event_ids", "event_ids_hash", "event_mode",
        "weighted_trades", "max_equity_drawdown", "max_equity_drawdown_source",
        "events_last_30d",
    }
    flattened = json.dumps(verdict, default=str)
    assert "SourceV6Fragment" not in flattened
    assert not any(isinstance(item, SourceV6Fragment) for item in verdict)


def test_a_window_that_hides_measurable_data_still_fails_and_names_the_point(
    tmp_path: Path,
) -> None:
    """W2 keeps E4: the worker's exception fails the publication."""
    from mrs3.source_v6_materializer import measure_point_group
    from mrs3.source_v6_stitch import SourceV6EmptySeriesError

    healthy, _idle = _healthy_and_empty()
    dropped = healthy[0]
    ids = (dropped.fragment_id,)
    hidden = (dropped.report_end_ms + 10_000_000, dropped.report_end_ms + 20_000_000)

    with pytest.raises(SourceV6EmptySeriesError) as raised:
        measure_point_group(
            str(_source_db(tmp_path, healthy)),
            dropped.point.canonical_key,
            ids,
            hidden,
        )
    assert raised.value.reason == "WINDOW_EXCLUDES_MEASURABLE_DATA"
    assert dropped.point.canonical_key in str(raised.value)


def test_a_verdict_that_changes_the_fragment_set_is_rejected(tmp_path: Path) -> None:
    """W4: the coordinator validates verdicts rather than trusting them."""
    from mrs3 import source_v6_materializer as module

    facts, _idle_key = _grid_with_one_idle()
    database = _source_db(tmp_path, facts)
    scope = _scope_of(facts[0])

    def lying(_database, point_key, fragment_ids, _window):
        return point_key, tuple(fragment_ids)[:-1] or ("phantom",), None, {"point_id": point_key}

    original = module.measure_point_group
    module.measure_point_group = lying
    try:
        with pytest.raises(ValueError, match="changed the fragment set"):
            module.materialize_source_v6_from_database(database, (scope,), workers=1)
    finally:
        module.measure_point_group = original


def test_publishing_id_only_facts_without_a_source_database_is_refused(
    tmp_path: Path,
) -> None:
    """W3: the payload-rebuilding branch cannot work from ids, and says so."""
    from mrs3.source_v6_materializer import materialize_source_v6_from_database
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface

    facts, _idle_key = _grid_with_one_idle()
    database = _source_db(tmp_path, facts)
    materialized = materialize_source_v6_from_database(
        database, (_scope_of(facts[0]),), workers=1
    )

    with pytest.raises(ValueError, match="requires source_database"):
        publish_multiscope_surface(tmp_path / "refused", materialized)


@pytest.mark.parametrize("workers", [1, 4])
def test_readback_validation_rejects_a_corrupted_payload_at_any_worker_count(
    tmp_path: Path, workers: int
) -> None:
    """W6: parallelising the check did not weaken it."""
    import duckdb

    from mrs3.source_v6_materializer import materialize_source_v6_from_database
    from mrs3.source_v6_surface_fresh import (
        _validate_published_payloads,
        publish_multiscope_surface,
    )

    facts, _idle_key = _grid_with_one_idle()
    database = _source_db(tmp_path, facts)
    scope = _scope_of(facts[0])
    materialized = materialize_source_v6_from_database(database, (scope,), workers=1)
    surface = publish_multiscope_surface(
        tmp_path / "surfaces", materialized, source_database=database, workers=workers,
    )

    _validate_published_payloads(str(surface), materialized.scopes, None, workers)

    victim = materialized.scopes[0].facts[0].fragment_id
    connection = duckdb.connect(str(surface))
    try:
        connection.execute(
            "update factual_fragments set payload_blob = ? where fragment_id = ?",
            [b"not a payload", victim],
        )
    finally:
        connection.close()

    with pytest.raises(ValueError, match="payload checksum mismatch"):
        _validate_published_payloads(str(surface), materialized.scopes, None, workers)

def test_the_surface_carries_its_own_measurements(tmp_path: Path) -> None:
    """W8: metrics and independent event ids, bound to the facts by a digest."""
    from mrs3.source_v6_materializer import materialize_source_v6_from_database
    from mrs3.source_v6_surface_fresh import (
        publish_multiscope_surface,
        read_multiscope_analysis_input,
        read_multiscope_surface,
    )

    facts, idle_key = _grid_with_one_idle()
    database = _source_db(tmp_path, facts)
    scope = _scope_of(facts[0])
    surface = publish_multiscope_surface(
        tmp_path / "surfaces",
        materialize_source_v6_from_database(database, (scope,), workers=1),
        source_database=database,
    )

    stored = read_multiscope_analysis_input(surface)
    assert set(stored) == {scope}
    rows = {str(row["point_id"]): row for row in stored[scope]["rows"]}
    assert len(rows) == len(facts), "one row per parameter combination"
    assert rows[idle_key]["trades"] == 0 and rows[idle_key]["event_ids"] == []
    assert rows[idle_key]["profit_factor"] is None
    traded = rows[_grid_with_one_idle()[0][1].point.canonical_key]
    assert traded["trades"] == len(traded["event_ids"])
    # The fixture's leading closes are one orphan realization run: size 2 over
    # its observed peak 1, with no invented entry action.
    assert traded["weighted_trades"] == "2"
    assert read_multiscope_surface(surface)["analysis_input_digest"] == (
        read_multiscope_surface(surface)["analysis_input_digest"]
    )


def test_measurements_that_do_not_belong_to_the_facts_are_refused(tmp_path: Path) -> None:
    """W8: the digest is what stops one surface's numbers being read as another's."""
    import duckdb

    from mrs3.source_v6_materializer import materialize_source_v6_from_database
    from mrs3.source_v6_surface_fresh import (
        publish_multiscope_surface,
        read_multiscope_analysis_input,
    )

    facts, idle_key = _grid_with_one_idle()
    database = _source_db(tmp_path, facts)
    surface = publish_multiscope_surface(
        tmp_path / "surfaces",
        materialize_source_v6_from_database(database, (_scope_of(facts[0]),), workers=1),
        source_database=database,
    )

    connection = duckdb.connect(str(surface))
    try:
        connection.execute(
            "update point_analysis_input set row_json = replace(row_json, '\"pnl_pct\":\"0\"', '\"pnl_pct\":\"1\"') "
            "where point_id = ?",
            [idle_key],
        )
    finally:
        connection.close()

    with pytest.raises(ValueError, match="surface analysis input does not match its facts"):
        read_multiscope_analysis_input(surface)


def test_analysis_reads_the_surface_instead_of_measuring_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W9: the fast path never decodes a payload, and agrees with the slow one.

    Identical `analysis_id` and identical stored frames are the whole claim: the
    surface's numbers must be the numbers analysis would have computed.
    """
    import duckdb

    from mrs3 import source_v6_analysis_fresh as analysis
    from mrs3.config import AlgorithmConfig
    from mrs3.source_v6_materializer import (
        materialize_source_v6,
        materialize_source_v6_from_database,
    )
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface

    facts, _idle_key = _grid_with_one_idle()
    database = _source_db(tmp_path, facts)
    scope = _scope_of(facts[0])
    listing = {facts[0].point.symbol: "2020-01-01"}
    config = AlgorithmConfig.defaults()

    slow_surface = publish_multiscope_surface(
        tmp_path / "slow", materialize_source_v6(facts, (scope,)), source_database=database,
    )
    fast_surface = publish_multiscope_surface(
        tmp_path / "fast",
        materialize_source_v6_from_database(database, (scope,), workers=1),
        source_database=database,
    )

    decoded: list[str] = []
    original = analysis.read_multiscope_scope

    def counting(path, scope_key):
        decoded.append(str(scope_key))
        return original(path, scope_key)

    monkeypatch.setattr(analysis, "read_multiscope_scope", counting)
    fast = analysis.run_multiscope_analysis(
        fast_surface, tmp_path / "out-fast", config, listing_dates=listing, workers=1
    )
    assert decoded == [], "the fast path must not decode any payload"

    slow = analysis.run_multiscope_analysis(
        slow_surface, tmp_path / "out-slow", config, listing_dates=listing, workers=1
    )
    assert decoded == [], "the stored analysis rows must cover both publication paths"

    def dump(path):
        connection = duckdb.connect(str(path), read_only=True)
        try:
            return {
                name: sorted(
                    connection.execute(f"select scope_key, payload_json from {name}").fetchall()
                )
                for name in analysis._TABLES
            }
        finally:
            connection.close()

    assert dump(fast) == dump(slow)

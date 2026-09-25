from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import importlib
import json

import duckdb
import pytest

from mrs3.performance_v2_equity_quality import (
    ALGORITHM_VERSION,
    EquitySample,
    calculate_equity_quality_facts,
)
from mrs3.performance_v2_import import _optimizer_source_metadata_json
from mrs3.performance_v2_store import initialize_performance_v2


def _cache_module():
    try:
        return importlib.import_module("mrs3.performance_v2_equity_cache")
    except ModuleNotFoundError:
        pytest.fail("Performance v2 equity cache adapter is not implemented")


def _facts(result_id: int = 1):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=28)
    return calculate_equity_quality_facts(
        result_id,
        start,
        end,
        [
            EquitySample(result_id, 0, start, Decimal("100.000000000001")),
            EquitySample(result_id, 1, end, Decimal("125.000000000001")),
        ],
    )


def _facts_for_points(start: datetime, end: datetime, points: list[tuple[datetime, object]]):
    return calculate_equity_quality_facts(
        1,
        start,
        end,
        [EquitySample(1, index, timestamp, equity) for index, (timestamp, equity) in enumerate(points)],
    )


def _canonical(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _source_metadata(result_id: int = 1) -> dict[str, object]:
    imported = datetime(2026, 2, 1, 12, tzinfo=UTC)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 29, tzinfo=UTC)
    return {
        "result_id": result_id,
        "imported_at_utc": imported,
        "report_start_utc": start,
        "report_end_utc": end,
        "effective_start_utc": start,
        "effective_end_utc": end,
        "optimizer_source_metadata_json": _optimizer_source_metadata_json(
            {"exchange": {"use_upnl": False}}, imported, "a" * 64
        ),
    }


def test_source_revision_is_deterministic_and_covers_import_window_and_source_hash() -> None:
    cache = _cache_module()
    metadata = _source_metadata()
    original = cache.equity_source_revision(metadata)

    assert original == cache.equity_source_revision(dict(metadata))
    assert len(original) == 64
    assert cache.equity_source_revision({**metadata, "imported_at_utc": datetime(2026, 2, 2, tzinfo=UTC)}) != original
    assert cache.equity_source_revision({**metadata, "report_end_utc": datetime(2026, 1, 28, tzinfo=UTC)}) != original
    assert cache.equity_source_revision({**metadata, "effective_start_utc": datetime(2026, 1, 2, tzinfo=UTC)}) != original
    assert cache.equity_source_revision({**metadata, "effective_end_utc": datetime(2026, 1, 28, tzinfo=UTC)}) != original
    assert cache.equity_source_revision({
        **metadata,
        "imported_at_utc": metadata["imported_at_utc"].astimezone(UTC),
    }) == original
    assert cache.equity_source_revision({
        **metadata,
        "optimizer_source_metadata_json": _optimizer_source_metadata_json(
            {"exchange": {"use_upnl": False}}, metadata["imported_at_utc"], "b" * 64
        ),
    }) != original


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not-json", id="malformed-json"),
        pytest.param("x" * 16_385, id="oversized-text"),
        pytest.param("\ud800", id="lone-surrogate"),
        pytest.param(b"\xff", id="non-utf8-bytes"),
        pytest.param(42, id="integer"),
        pytest.param({"source_report_sha256": "a" * 64}, id="object"),
        pytest.param(
            _optimizer_source_metadata_json(
                {"exchange": {"use_upnl": False}},
                datetime(2026, 2, 1, 12, tzinfo=UTC),
                "z" * 64,
            ),
            id="valid-shape-nonhex-sha256",
        ),
    ],
)
def test_malformed_optional_optimizer_metadata_does_not_block_source_revision(payload: object) -> None:
    cache = _cache_module()
    metadata = _source_metadata()
    without_optional_metadata = cache.equity_source_revision(
        {**metadata, "optimizer_source_metadata_json": None}
    )
    assert cache.equity_source_revision(
        {**metadata, "optimizer_source_metadata_json": payload}
    ) == without_optional_metadata


@pytest.mark.parametrize(
    "start_field,end_field,start,end,error_text",
    [
        (
            "report_start_utc",
            "report_end_utc",
            datetime(2026, 1, 1, 0, 0, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC),
            "report bounds",
        ),
        (
            "effective_start_utc",
            "effective_end_utc",
            datetime(2026, 1, 1, 0, 0, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC),
            "effective bounds",
        ),
    ],
)
def test_source_revision_compares_utc_bounds_at_microsecond_precision(
    start_field: str, end_field: str, start: datetime, end: datetime, error_text: str
) -> None:
    cache = _cache_module()
    metadata = _source_metadata()

    assert cache.equity_source_revision(
        {**metadata, start_field: start, end_field: end}
    )
    with pytest.raises(cache.EquityQualityCacheError, match=error_text):
        cache.equity_source_revision(
            {**metadata, start_field: end, end_field: start}
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("result_id", "1"),
        ("imported_at_utc", datetime(2026, 2, 1, 12)),
        ("report_start_utc", datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))),
    ],
)
def test_source_revision_rejects_malformed_or_non_utc_metadata(field: str, value: object) -> None:
    cache = _cache_module()
    with pytest.raises(cache.EquityQualityCacheError):
        cache.equity_source_revision({**_source_metadata(), field: value})


def test_facts_json_round_trips_canonically_and_checks_its_digest() -> None:
    cache = _cache_module()
    facts = _facts()
    payload = cache.encode_equity_facts(facts)

    assert payload == _canonical(facts.to_canonical_dict())
    assert cache.decode_equity_facts(payload, sha256(payload.encode("utf-8")).hexdigest()) == facts
    with pytest.raises(cache.EquityQualityCacheError, match="digest"):
        cache.decode_equity_facts(payload, "0" * 64)


def test_encoder_accepts_each_engine_state_and_decodes_it_exactly() -> None:
    cache = _cache_module()
    end = datetime(2026, 1, 29, tzinfo=UTC)
    start = end - timedelta(days=28)
    fixtures = [
        _facts_for_points(start, end, [(start, 100), (end, 125)]),
        _facts_for_points(start, end, [(start, 100), (end, 100)]),
        _facts_for_points(start, end, [(start, 100), (end, 80)]),
        _facts_for_points(
            end - timedelta(days=7),
            end,
            [(end - timedelta(days=7), 100), (end, Decimal("100.0000000047"))],
        ),
        _facts_for_points(
            start,
            end,
            [(start, 100), (end - timedelta(days=14), 170), (end - timedelta(days=7), 150), (end, 140)],
        ),
        _facts_for_points(start, end, [(start, 100), (end, 0)]),
        _facts_for_points(start, end, [(end + timedelta(seconds=1), 100)]),
        _facts_for_points(end, start, []),
        _facts_for_points(end - timedelta(days=6), end, [(end - timedelta(days=6), 100)]),
        _facts_for_points(end - timedelta(days=8), end, [(end - timedelta(days=1), 100)]),
    ]

    assert {facts.state for facts in fixtures} == {
        "GROWING",
        "FLAT",
        "DECLINING_OR_MIXED",
        "WEAKENING",
        "NONPOSITIVE_EQUITY",
        "UNKNOWN_INVALID_SOURCE",
        "INSUFFICIENT_HISTORY",
        "MISSING_BASELINE",
    }
    assert {
        (facts.state, facts.equity_class, facts.reason)
        for facts in fixtures
    } == {
        ("GROWING", 0, "H_UP_SHORTS_NONDECLINING"),
        ("FLAT", 2, "H_FLAT"),
        ("DECLINING_OR_MIXED", 3, "H_DECLINING_OR_MIXED"),
        ("DECLINING_OR_MIXED", 2, "H_NONDECLINING_NOT_UP"),
        ("WEAKENING", 1, "SHORT_WINDOW_DECLINE"),
        ("NONPOSITIVE_EQUITY", None, "NONPOSITIVE_IN_REPORT_EQUITY"),
        ("UNKNOWN_INVALID_SOURCE", None, "INVALID_OR_OUT_OF_INTERVAL_SOURCE"),
        ("INSUFFICIENT_HISTORY", None, "INSUFFICIENT_HISTORY"),
        ("MISSING_BASELINE", None, "MISSING_BASELINE"),
    }
    for facts in fixtures:
        assert cache.decode_equity_facts(
            cache.encode_equity_facts(facts),
            sha256(cache.encode_equity_facts(facts).encode("utf-8")).hexdigest(),
        ) == facts


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "range",
        "nonfinite",
        "noncanonical",
        "extra",
        "missing",
        "state_stray",
        "score_precision",
        "window_order",
    ],
)
def test_facts_decoder_rejects_corrupt_version_and_out_of_range_payloads(mutation: str) -> None:
    cache = _cache_module()
    payload = cache.encode_equity_facts(_facts())
    document = json.loads(payload)
    if mutation == "version":
        document["algo_version"] = "unknown"
    elif mutation == "range":
        document["drawdown"] = "1.1"
    elif mutation == "nonfinite":
        document["score12"] = "NaN"
    elif mutation == "extra":
        document["unexpected"] = "field"
    elif mutation == "missing":
        del document["score12"]
    elif mutation == "state_stray":
        document["state"] = "UNKNOWN_INVALID_SOURCE"
        document["invalid_reasons"] = ["BAD_ROW"]
        document["horizon_days"] = 28
    elif mutation == "score_precision":
        document["score12"] = "0.0000000000001"
    else:
        if mutation == "window_order":
            document["windows"] = list(reversed(document["windows"]))
        else:
            payload = " " + payload
            document = None
    altered = payload if mutation == "noncanonical" else _canonical(document)
    digest = sha256(altered.encode("utf-8")).hexdigest()

    with pytest.raises(cache.EquityQualityCacheError):
        cache.decode_equity_facts(altered, digest)


def test_cache_upsert_checks_current_source_revision_and_reads_by_revision() -> None:
    cache = _cache_module()
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        connection.execute("insert into analysis_plateaus values ('run', 'P1', 1, 1)")
        strategy_id = connection.execute(
            """insert into strategies (
                strategy_name, symbol, side, timeframe, close_ma_len, order_count,
                analysis_run_id, candidate_identity, lifecycle_status, created_at_utc, updated_at_utc
            ) values ('cache-row', 'BTCUSDT', 'LONG', '1h', 20, 1, 'run', 'cache-row', 'ACTIVE', now(), now())
            returning strategy_id"""
        ).fetchone()[0]
        result_id = connection.execute(
            """insert into strategy_results (
                strategy_id, report_start_utc, report_end_utc, exchange, commission_rate,
                initial_balance, final_balance, imported_at_utc, effective_start_utc,
                effective_end_utc, optimizer_source_metadata_json
            ) values (?, ?, ?, 'BYBIT', .001, 100, 125, ?, ?, ?, ?)
            returning result_id""",
            [
                strategy_id,
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 29, tzinfo=UTC),
                datetime(2026, 2, 1, 12, tzinfo=UTC),
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 29, tzinfo=UTC),
                json.dumps({"source_report_sha256": "a" * 64}),
            ],
        ).fetchone()[0]
        metadata = cache.current_equity_source_metadata(connection, result_id)
        facts = _facts(result_id)
        revision = cache.equity_source_revision(metadata)
        cache.publish_equity_quality_facts(connection, metadata, facts, calculated_at_utc=datetime(2026, 2, 2, tzinfo=UTC))

        assert cache.read_equity_quality_facts(connection, result_id, revision) == facts
        assert cache.read_equity_quality_facts(connection, result_id, "f" * 64) is None

        corrupt = cache.encode_equity_facts(facts) + " "
        connection.execute(
            "update equity_quality_metrics set facts_json = ? where result_id = ?",
            [corrupt, result_id],
        )
        with pytest.raises(cache.EquityQualityCacheError, match="digest"):
            cache.read_equity_quality_facts(connection, result_id, revision)

        document = json.loads(cache.encode_equity_facts(facts))
        document["algo_version"] = "unknown-version"
        bad_payload = _canonical(document)
        connection.execute(
            "update equity_quality_metrics set facts_json = ?, facts_sha256 = ? where result_id = ?",
            [bad_payload, sha256(bad_payload.encode("utf-8")).hexdigest(), result_id],
        )
        with pytest.raises(cache.EquityQualityCacheError, match="version"):
            cache.read_equity_quality_facts(connection, result_id, revision)

        connection.execute(
            "update strategy_results set imported_at_utc = imported_at_utc + interval '1 second' where result_id = ?",
            [result_id],
        )
        with pytest.raises(cache.EquitySourceChangedError):
            cache.publish_equity_quality_facts(connection, metadata, facts, calculated_at_utc=datetime(2026, 2, 3, tzinfo=UTC))
        current = cache.current_equity_source_metadata(connection, result_id)
        assert cache.equity_source_revision(current) != revision


def test_invalid_pure_facts_with_reversed_bounds_cannot_publish_to_real_result() -> None:
    cache = _cache_module()
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        connection.execute("insert into analysis_plateaus values ('run', 'P1', 1, 1)")
        result_id = _insert_result(connection, "reversed-bounds")
        metadata = cache.current_equity_source_metadata(connection, result_id)
        end = datetime(2026, 1, 29, tzinfo=UTC)
        reversed_facts = _facts_for_points(end, end - timedelta(days=28), [])
        # The fixture helper uses result_id 1, as does this fresh in-memory database.
        assert reversed_facts.result_id == result_id

        with pytest.raises(cache.EquityQualityCacheError, match="report bounds"):
            cache.publish_equity_quality_facts(
                connection,
                metadata,
                reversed_facts,
                calculated_at_utc=datetime(2026, 2, 2, tzinfo=UTC),
            )
        assert connection.execute("select count(*) from equity_quality_metrics").fetchone() == (0,)


def test_cache_facts_are_not_foreign_key_coupled_to_source_rows() -> None:
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        connection.execute(
            "insert into equity_quality_metrics values (999, 'revision', ?, '{}', 'digest', now())",
            [ALGORITHM_VERSION],
        )
        assert connection.execute("select result_id from equity_quality_metrics").fetchall() == [(999,)]


def test_v5_cache_read_reports_upgrade_required_without_selecting_new_table() -> None:
    cache = _cache_module()
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        connection.execute("drop table equity_quality_metrics")
        connection.execute("update schema_info set value = '5' where key = 'schema_version'")

        with pytest.raises(cache.EquityQualityCacheError, match="EQUITY_SCHEMA_UPGRADE_REQUIRED"):
            cache.read_equity_quality_facts(connection, 1, "any-revision")


def _insert_result(connection: duckdb.DuckDBPyConnection, name: str) -> int:
    strategy_id = connection.execute(
        """insert into strategies (
            strategy_name, symbol, side, timeframe, close_ma_len, order_count,
            analysis_run_id, candidate_identity, lifecycle_status, created_at_utc, updated_at_utc
        ) values (?, 'BTCUSDT', 'LONG', '1h', 20, 1, 'run', ?, 'ACTIVE', now(), now())
        returning strategy_id""",
        [name, name],
    ).fetchone()[0]
    return connection.execute(
        """insert into strategy_results (
            strategy_id, report_start_utc, report_end_utc, exchange, commission_rate,
            initial_balance, final_balance, imported_at_utc, effective_start_utc,
            effective_end_utc, optimizer_source_metadata_json
        ) values (?, ?, ?, 'BYBIT', .001, 100, 125, ?, ?, ?, ?)
        returning result_id""",
        [
            strategy_id,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 29, tzinfo=UTC),
            datetime(2026, 2, 1, 12, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 29, tzinfo=UTC),
            json.dumps({"source_report_sha256": "a" * 64}),
        ],
    ).fetchone()[0]


def test_batch_publish_rechecks_all_sources_before_writing_any_rows() -> None:
    cache = _cache_module()
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        connection.execute("insert into analysis_plateaus values ('run', 'P1', 1, 1)")
        result_ids = (_insert_result(connection, "batch-one"), _insert_result(connection, "batch-two"))
        items = [
            (cache.current_equity_source_metadata(connection, result_id), _facts(result_id))
            for result_id in result_ids
        ]
        calculated_at = datetime(2026, 2, 2, tzinfo=UTC)

        cache.publish_equity_quality_facts_batch(connection, items, calculated_at_utc=calculated_at)
        assert connection.execute(
            "select count(*) from equity_quality_metrics where result_id in (?, ?)", result_ids
        ).fetchone() == (2,)

        connection.execute("delete from equity_quality_metrics where result_id in (?, ?)", result_ids)
        connection.execute(
            "update strategy_results set imported_at_utc = imported_at_utc + interval '1 second' where result_id = ?",
            [result_ids[1]],
        )
        with pytest.raises(cache.EquitySourceChangedError):
            cache.publish_equity_quality_facts_batch(connection, items, calculated_at_utc=calculated_at)
        assert connection.execute(
            "select count(*) from equity_quality_metrics where result_id in (?, ?)", result_ids
        ).fetchone() == (0,)

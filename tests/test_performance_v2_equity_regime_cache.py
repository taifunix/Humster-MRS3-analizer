from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from dataclasses import replace

import duckdb
import pytest

from mrs3.performance_v2_equity_regime import EquityRegimeSample, classify_equity_regime


UTC_NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)


def _assessment(result_id: int = 7):
    end = datetime(2026, 10, 1, tzinfo=UTC)
    start = end - timedelta(days=28)
    samples = tuple(
        EquityRegimeSample(result_id, index, start + timedelta(hours=6 * index), 100)
        for index in range(113)
    )
    return classify_equity_regime(result_id, start, end, samples)


def _metadata(result_id: int = 7) -> dict[str, object]:
    start = datetime(2026, 9, 3, tzinfo=UTC)
    end = datetime(2026, 10, 1, tzinfo=UTC)
    return {
        "result_id": result_id,
        "imported_at_utc": datetime(2026, 10, 2, tzinfo=UTC),
        "report_start_utc": start,
        "report_end_utc": end,
        "effective_start_utc": start,
        "effective_end_utc": end,
        "optimizer_source_metadata_json": None,
    }


def _connection(metadata: dict[str, object]) -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect()
    connection.execute(
        """
        create table strategy_results (
            result_id bigint,
            imported_at_utc timestamptz,
            report_start_utc timestamptz,
            report_end_utc timestamptz,
            effective_start_utc timestamptz,
            effective_end_utc timestamptz,
            optimizer_source_metadata_json varchar
        )
        """
    )
    connection.execute(
        """
        create table equity_quality_metrics (
            result_id bigint not null,
            source_revision varchar not null,
            algo_version varchar not null,
            facts_json varchar not null,
            facts_sha256 varchar not null,
            calculated_at_utc timestamptz not null,
            primary key (result_id, algo_version)
        )
        """
    )
    connection.execute(
        "insert into strategy_results values (?, ?, ?, ?, ?, ?, ?)",
        [
            metadata["result_id"],
            metadata["imported_at_utc"],
            metadata["report_start_utc"],
            metadata["report_end_utc"],
            metadata["effective_start_utc"],
            metadata["effective_end_utc"],
            metadata["optimizer_source_metadata_json"],
        ],
    )
    return connection


def _cache_module():
    import mrs3.performance_v2_equity_regime_cache as cache

    return cache


def test_facts_codec_is_canonical_and_digest_bound() -> None:
    cache = _cache_module()
    facts = _assessment().facts

    payload = cache.encode_equity_regime_facts(facts)

    assert payload == json.dumps(facts.to_canonical_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert cache.decode_equity_regime_facts(payload, sha256(payload.encode()).hexdigest()) == facts
    with pytest.raises(cache.EquityRegimeCacheError, match="digest"):
        cache.decode_equity_regime_facts(payload, "0" * 64)


def test_facts_codec_preserves_stage_ath_evidence() -> None:
    cache = _cache_module()
    end = datetime(2026, 10, 1, tzinfo=UTC)
    start = end - timedelta(days=28)
    samples = tuple(
        EquityRegimeSample(7, index, start + timedelta(hours=6 * index), 100 + index)
        for index in range(113)
    )
    facts = classify_equity_regime(7, start, end, samples).facts
    payload = cache.encode_equity_regime_facts(facts)

    assert all(facts.ath_stage_event_values)
    assert cache.decode_equity_regime_facts(payload, sha256(payload.encode()).hexdigest()) == facts


def test_facts_codec_round_trips_unavailable_w28() -> None:
    cache = _cache_module()
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=14)
    facts = classify_equity_regime(
        7, start, end, (EquityRegimeSample(7, 0, start, 100),)
    ).facts
    payload = cache.encode_equity_regime_facts(facts)

    assert facts.windows_28 is None
    assert cache.decode_equity_regime_facts(payload, sha256(payload.encode()).hexdigest()) == facts


def test_facts_codec_round_trips_fractional_pre28_duration() -> None:
    cache = _cache_module()
    end = datetime(2026, 10, 1, tzinfo=UTC)
    start = end - timedelta(days=42, hours=2)
    facts = classify_equity_regime(
        7,
        start,
        end,
        (
            EquityRegimeSample(7, 0, start, 100),
            EquityRegimeSample(7, 1, end, 100),
        ),
    ).facts

    assert facts.pre28 is not None and facts.pre28.elapsed_days > 14
    payload = cache.encode_equity_regime_facts(facts)
    assert cache.decode_equity_regime_facts(payload, sha256(payload.encode()).hexdigest()) == facts


@pytest.mark.parametrize(
    "mutation",
    ["duplicate", "nonfinite", "oversize", "unknown", "version", "result_id", "reason_order"],
)
def test_facts_decoder_rejects_noncanonical_or_immutable_payloads(mutation: str) -> None:
    cache = _cache_module()
    payload = cache.encode_equity_regime_facts(_assessment().facts)
    document = json.loads(payload)
    if mutation == "duplicate":
        payload = payload[:-1] + ',"algo_version":"equity-regime-v1"}'
    elif mutation == "nonfinite":
        document["final_equity"] = float("nan")
        payload = json.dumps(document, separators=(",", ":"), allow_nan=True)
    elif mutation == "oversize":
        payload = payload + (" " * 65_537)
    elif mutation == "unknown":
        document["unexpected"] = 1
        payload = json.dumps(document, separators=(",", ":"))
    elif mutation == "version":
        document["algo_version"] = "equity-regime-v2"
        payload = json.dumps(document, separators=(",", ":"))
    elif mutation == "result_id":
        document["result_id"] = 8
        payload = json.dumps(document, separators=(",", ":"))
    else:
        document["invalid_reasons"] = ["W28_UNAVAILABLE", "INVALID_REPORT_RANGE"]
        payload = json.dumps(document, separators=(",", ":"))

    digest = sha256(payload.encode()).hexdigest()
    with pytest.raises(cache.EquityRegimeCacheError):
        cache.decode_equity_regime_facts(payload, digest, expected_result_id=7)


def test_versioned_read_is_a_miss_for_source_or_payload_mismatch() -> None:
    cache = _cache_module()
    metadata = _metadata()
    connection = _connection(metadata)
    facts = _assessment().facts
    revision = cache.equity_regime_source_revision(metadata)
    payload = cache.encode_equity_regime_facts(facts)
    connection.execute(
        "insert into equity_quality_metrics values (?, ?, ?, ?, ?, ?)",
        [7, revision, cache.ALGORITHM_VERSION, payload, sha256(payload.encode()).hexdigest(), UTC_NOW],
    )

    before = connection.execute("select * from equity_quality_metrics order by algo_version").fetchall()
    assert cache.read_equity_regime_facts(connection, 7, revision) == facts
    assert connection.execute("select * from equity_quality_metrics order by algo_version").fetchall() == before
    assert cache.read_equity_regime_facts(connection, 7, "f" * 64) is None
    assert connection.execute("select * from equity_quality_metrics order by algo_version").fetchall() == before
    wrong_result = replace(facts, result_id=8)
    wrong_payload = cache.encode_equity_regime_facts(wrong_result)
    connection.execute(
        "update equity_quality_metrics set facts_json = ?, facts_sha256 = ?",
        [wrong_payload, sha256(wrong_payload.encode()).hexdigest()],
    )
    assert cache.read_equity_regime_facts(connection, 7, revision) is None
    connection.execute(
        "update equity_quality_metrics set facts_json = ?, facts_sha256 = ?",
        ["{}", sha256(b"{}").hexdigest()],
    )
    assert cache.read_equity_regime_facts(connection, 7, revision) is None
    connection.execute("update equity_quality_metrics set facts_sha256 = ?", ["0" * 64])
    assert cache.read_equity_regime_facts(connection, 7, revision) is None


def test_explicit_upsert_rechecks_source_and_preserves_r73_rows() -> None:
    cache = _cache_module()
    metadata = _metadata()
    connection = _connection(metadata)
    connection.execute(
        "insert into equity_quality_metrics values (?, ?, ?, ?, ?, ?)",
        [7, "r73", "R7.3", "legacy", "a" * 64, UTC_NOW],
    )
    connection.execute(
        "insert into equity_quality_metrics values (?, ?, ?, ?, ?, ?)",
        [7, "stale", cache.ALGORITHM_VERSION, "{}", "0" * 64, UTC_NOW],
    )
    facts = _assessment().facts

    source_revision = cache.upsert_equity_regime_facts_checked(
        connection, metadata, facts, calculated_at_utc=UTC_NOW
    )

    assert source_revision == cache.equity_regime_source_revision(metadata)
    assert cache.read_equity_regime_facts(connection, 7, source_revision) == facts
    assert connection.execute(
        "select source_revision, facts_json from equity_quality_metrics where result_id = 7 and algo_version = 'R7.3'"
    ).fetchone() == ("r73", "legacy")

    stale = {**metadata, "report_end_utc": metadata["report_end_utc"] - timedelta(seconds=1)}
    with pytest.raises(cache.EquityRegimeSourceChangedError, match="EQUITY_SOURCE_CHANGED"):
        cache.upsert_equity_regime_facts_checked(connection, stale, facts, calculated_at_utc=UTC_NOW)


def test_assessment_codec_round_trips_for_selection_snapshot() -> None:
    cache = _cache_module()
    assessment = _assessment()
    payload = cache.encode_equity_regime_assessment(assessment)
    digest = sha256(payload.encode()).hexdigest()

    assert cache.decode_equity_regime_assessment(payload, digest) == assessment

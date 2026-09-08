from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import duckdb

import pytest
import mrs3.portfolio.liquidity as liquidity_module

from mrs3.portfolio.liquidity import (
    BANDS_BPS,
    COARSE_ESTIMATE,
    LiquidityError,
    LiquidityPolicy,
    LiquidityReader,
    ReferenceReader,
    SizingEnvelopeResult,
    TickerAdapter,
    TickerSnapshot,
    coarse_symbol_screen,
    directional_liquidity_ceiling,
    empirical_capacity,
    exit_liquidity_diagnostic,
    sizing_envelope,
)
from mrs3.bybit_collector.aggregation import LIQUIDITY_1M_COLUMNS, LIQUIDITY_1M_SCHEMA
from mrs3.bybit_collector.storage import PublishedHour, SQLiteSpool


EXPECTED_MINUTES = 7 * 24 * 60
WINDOW_START_MS = 0
WINDOW_END_MS = EXPECTED_MINUTES * 60_000


def _dense_rows(*records: dict[str, object]) -> list[dict[str, object]]:
    """Build the complete seven-day minute denominator used by the ceiling contract."""
    if not records:
        raise ValueError("at least one row template is required")
    result: list[dict[str, object]] = []
    for minute in range(EXPECTED_MINUTES):
        template = records[minute % len(records)]
        result.append({**template, "minute_ts_ms": minute * 60_000})
    return result


def _write_marked_fixture(root: Path, *, missing_hour: int | None = None, escape_hour: int | None = None, bad_count_hour: int | None = None, bad_schema_hour: int | None = None, wrong_hour: int | None = None) -> int:
    now_ms = 8 * 86_400_000
    with SQLiteSpool(root) as spool:
        for hour in range(86_400_000, now_ms, 3_600_000):
            hour_index = hour // 3_600_000
            if hour_index == missing_hour:
                continue
            file_name = f"liquidity_1m/date=fixture/part-{hour_index:04d}.parquet"
            if hour_index == escape_hour:
                file_name = "../outside.parquet"
            # Keep the fixture write inside tmp_path; the marker itself carries
            # the escaping path and the reader must reject it before opening.
            path = root / ("escape_placeholder.parquet" if hour_index == escape_hour else file_name)
            path.parent.mkdir(parents=True, exist_ok=True)
            values: list[object] = []
            for column, data_type, _nullable in LIQUIDITY_1M_SCHEMA:
                if column == "minute_ts_ms":
                    values.append(hour + 3_600_000 if hour_index == wrong_hour else hour)
                elif column == "symbol":
                    values.append("BTCUSDT")
                elif data_type == "DOUBLE":
                    values.append(100.0 if "depth" in column else 1.0)
                else:
                    values.append(1)
            connection = duckdb.connect()
            try:
                if hour_index == bad_schema_hour:
                    connection.execute("CREATE TABLE fixture (minute_ts_ms BIGINT, symbol VARCHAR)")
                    connection.execute("INSERT INTO fixture VALUES (?, ?)", [hour, "BTCUSDT"])
                    connection.execute(f"COPY fixture TO '{path.as_posix()}' (FORMAT PARQUET)")
                else:
                    defs = ", ".join(f'"{name}" {data_type}{" NOT NULL" if not nullable else ""}' for name, data_type, nullable in LIQUIDITY_1M_SCHEMA)
                    connection.execute(f"CREATE TABLE fixture ({defs})")
                    connection.execute(f"INSERT INTO fixture VALUES ({','.join('?' for _ in values)})", values)
                    connection.execute(
                        f"COPY fixture TO '{path.as_posix()}' (FORMAT PARQUET, KV_METADATA {{'schema_name':'bybit_liquidity_1m','schema_version':'2','collector_version':'fixture','exchange':'bybit','category':'linear','created_at_utc':'1970-01-01T00:00:00+00:00'}})"
                    )
            finally:
                connection.close()
            if hour_index == escape_hour:
                spool.mark_published(hour, file_name, 1, now_ms)
            else:
                spool.mark_published(hour, file_name, 2 if hour_index == bad_count_hour else 1, now_ms)
    return now_ms


def policy(**overrides: object) -> LiquidityPolicy:
    values = {
        "band_bps": 10,
        "lookback_days": 7,
        "distribution_quantile": Decimal("0.05"),
        "allowed_depth_share": Decimal("0.10"),
        "min_coverage": Decimal("0.90"),
        "min_completeness": Decimal("0.90"),
        "min_eligible_fraction": Decimal("1.0"),
        "max_age_ms": 86_400_000,
        "expiry_ms": 86_400_000,
    }
    values.update(overrides)
    return LiquidityPolicy(**values)


def test_policy_requires_explicit_values_and_only_published_bands() -> None:
    assert BANDS_BPS == (10, 25, 50, 100)
    with pytest.raises(TypeError):
        LiquidityPolicy()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="band"):
        policy(band_bps=15)
    with pytest.raises(ValueError, match="Decimal"):
        policy(distribution_quantile="0.05")


def test_directional_ceiling_uses_all_opening_levels_and_excludes_closes() -> None:
    rows = _dense_rows({
            "symbol": "BTCUSDT",
            "bid_depth_usdt_10bps_p05": 1000.0,
            "ask_depth_usdt_10bps_p05": 900.0,
            "bid_depth_10bps_complete_ratio": 1.0,
            "ask_depth_10bps_complete_ratio": 1.0,
            "coverage_ratio": 1.0,
        })
    geometry = [
        {"role": "opening", "reduce_only": False, "lot_x": "1", "notional": "100"},
        {"role": "opening", "reduce_only": False, "lot_x": "2", "notional": "200"},
        {"role": "closing", "reduce_only": True, "lot_x": "99", "notional": "99999"},
    ]
    result = directional_liquidity_ceiling(
        rows,
        geometry,
        symbol="BTCUSDT",
        side="LONG",
        policy=policy(),
            sizing_base=Decimal("1000"),
            captured_at_ms=86_400_000,
            now_ms=86_400_001,
            source="fixture",
        window_start_ms=WINDOW_START_MS,
        window_end_ms=WINDOW_END_MS,
        tier={"risk_limit_value": "1000", "max_leverage": "50"},
        exposure={"position": "0", "active_orders": "300"},
    )
    assert result.status == "PASS"
    assert result.liquidity_scalar_pct_max == Decimal("50.0")
    assert result.evidence_class == "CONSERVATIVE_BOUND"
    assert result.used_band_bps == 10
    assert result.content_digest
    assert result.expires_at_ms == 172_800_000


def test_changed_geometry_or_filters_invalidates_saved_ceiling() -> None:
    rows = _dense_rows({
        "symbol": "BTCUSDT",
        "bid_depth_usdt_10bps_p05": 1000.0,
        "bid_depth_10bps_complete_ratio": 1.0,
        "coverage_ratio": 1.0,
    })
    result = directional_liquidity_ceiling(
        rows,
        [{"role": "opening", "reduce_only": False, "notional": "100"}],
        symbol="BTCUSDT",
        side="LONG",
        policy=policy(),
        sizing_base=Decimal("1000"),
        captured_at_ms=86_400_000,
        source="fixture",
        window_start_ms=WINDOW_START_MS,
        window_end_ms=WINDOW_END_MS,
        filters={"qty_step": "0.1"},
        tier={"risk_limit_value": "1000", "max_leverage": "50"},
        exposure={"position": "0", "active_orders": "100"},
    )
    assert result.is_valid(
        now_ms=86_400_001,
        geometry=[{"role": "opening", "reduce_only": False, "notional": "101"}],
        filters={"qty_step": "0.1"},
        tier={"risk_limit_value": "1000", "max_leverage": "50"},
        exposure={"position": "0", "active_orders": "100"},
        policy=policy(), symbol="BTCUSDT", side="LONG",
        window_start_ms=WINDOW_START_MS, window_end_ms=WINDOW_END_MS,
    ) is False
    assert result.is_valid(
        now_ms=86_400_001,
        geometry=[{"role": "opening", "reduce_only": False, "notional": "100"}],
        filters={"qty_step": "0.2"},
        tier={"risk_limit_value": "1000", "max_leverage": "50"},
        exposure={"position": "0", "active_orders": "100"},
        policy=policy(), symbol="BTCUSDT", side="LONG",
        window_start_ms=WINDOW_START_MS, window_end_ms=WINDOW_END_MS,
    ) is False
    assert result.is_valid(
        now_ms=172_800_000,
        geometry=[{"role": "opening", "reduce_only": False, "notional": "100"}],
        filters={"qty_step": "0.1"},
        tier={"risk_limit_value": "1000", "max_leverage": "50"},
        exposure={"position": "0", "active_orders": "100"},
        policy=policy(), symbol="BTCUSDT", side="LONG",
        window_start_ms=WINDOW_START_MS, window_end_ms=WINDOW_END_MS,
    ) is False


def test_incomplete_or_stale_history_is_unknown_and_not_zero() -> None:
    rows = _dense_rows({
        "symbol": "BTCUSDT",
        "bid_depth_usdt_25bps_p05": 1000.0,
        "bid_depth_25bps_complete_ratio": 0.5,
        "coverage_ratio": 1.0,
    })
    result = directional_liquidity_ceiling(
        rows,
        [{"role": "opening", "reduce_only": False, "notional": "100"}],
        symbol="BTCUSDT",
        side="LONG",
        policy=policy(band_bps=25, min_completeness=Decimal("0.9")),
        sizing_base=Decimal("1000"),
        captured_at_ms=0,
        now_ms=86_400_001,
        source="fixture",
        window_start_ms=WINDOW_START_MS,
        window_end_ms=WINDOW_END_MS,
        tier={"risk_limit_value": "1000", "max_leverage": "50"},
        exposure={"position": "0", "active_orders": "100"},
    )
    assert result.status == "UNKNOWN"
    assert result.reason in {"LIQUIDITY_QUALITY_INSUFFICIENT", "LIQUIDITY_STALE"}
    assert result.liquidity_scalar_pct_max is None


def test_distribution_quantile_is_over_source_p05_and_fraction_is_explicit() -> None:
    rows = _dense_rows(
        {"symbol": "BTCUSDT", "bid_depth_usdt_10bps_p05": "100", "bid_depth_10bps_complete_ratio": "1", "coverage_ratio": "1"},
        {"symbol": "BTCUSDT", "bid_depth_usdt_10bps_p05": "300", "bid_depth_10bps_complete_ratio": "1", "coverage_ratio": "1"},
    )
    result = directional_liquidity_ceiling(
        rows,
        [{"role": "opening", "reduce_only": False, "notional": "100"}],
        symbol="BTCUSDT",
        side="LONG",
        policy=policy(distribution_quantile=Decimal("0.5")),
        sizing_base=Decimal("1000"),
        captured_at_ms=1,
        now_ms=2,
        source="fixture",
        window_start_ms=WINDOW_START_MS,
        window_end_ms=WINDOW_END_MS,
        tier={"risk_limit_value": "1000"},
        exposure={"position": "0", "active_orders": "100"},
    )
    assert result.status == "PASS"
    assert result.depth_reference == Decimal("200")
    assert result.liquidity_scalar_pct_max == Decimal("20")


def test_duplicate_in_window_minutes_cannot_inflate_eligible_fraction() -> None:
    row = {
        "symbol": "BTCUSDT",
        "minute_ts_ms": 0,
        "bid_depth_usdt_10bps_p05": "1000",
        "bid_depth_10bps_complete_ratio": "1",
        "coverage_ratio": "1",
    }
    result = directional_liquidity_ceiling(
        [dict(row) for _ in range(EXPECTED_MINUTES)],
        [{"role": "opening", "reduce_only": False, "notional": "100"}],
        symbol="BTCUSDT", side="LONG", policy=policy(), sizing_base=Decimal("1000"),
        captured_at_ms=1, now_ms=2, source="fixture",
        window_start_ms=WINDOW_START_MS, window_end_ms=WINDOW_END_MS,
        tier={"risk_limit_value": "1000"}, exposure={"position": "0", "active_orders": "100"},
    )
    assert result.status == "UNKNOWN"
    assert result.reason == "LIQUIDITY_QUALITY_INSUFFICIENT"


def test_sparse_unique_minutes_below_quality_threshold_are_unknown() -> None:
    rows = [
        {"symbol": "BTCUSDT", "minute_ts_ms": minute * 60_000,
         "bid_depth_usdt_10bps_p05": "1000", "bid_depth_10bps_complete_ratio": "1", "coverage_ratio": "1"}
        for minute in (0, 1)
    ]
    result = directional_liquidity_ceiling(
        rows,
        [{"role": "opening", "reduce_only": False, "notional": "100"}],
        symbol="BTCUSDT", side="LONG", policy=policy(min_eligible_fraction=Decimal("0.001")),
        sizing_base=Decimal("1000"), captured_at_ms=1, now_ms=2, source="fixture",
        window_start_ms=WINDOW_START_MS, window_end_ms=WINDOW_END_MS,
        tier={"risk_limit_value": "1000"}, exposure={"position": "0"},
    )
    assert result.status == "UNKNOWN"
    assert result.reason == "LIQUIDITY_QUALITY_INSUFFICIENT"


def test_eligible_minute_fraction_allows_partial_only_above_explicit_threshold() -> None:
    rows = _dense_rows(
        {"symbol": "BTCUSDT", "bid_depth_usdt_10bps_p05": "100", "bid_depth_10bps_complete_ratio": "1", "coverage_ratio": "1"},
        {"symbol": "BTCUSDT", "bid_depth_usdt_10bps_p05": None, "bid_depth_10bps_complete_ratio": "0", "coverage_ratio": "0"},
    )
    kwargs = dict(
        symbol="BTCUSDT", side="LONG", sizing_base=Decimal("1000"), captured_at_ms=1, now_ms=2,
        source="fixture", window_start_ms=WINDOW_START_MS, window_end_ms=WINDOW_END_MS,
        tier={"risk_limit_value": "1000"}, exposure={"position": "0", "active_orders": "100"},
    )
    partial = directional_liquidity_ceiling(
        rows, [{"role": "opening", "reduce_only": False, "notional": "100"}],
        policy=policy(min_eligible_fraction=Decimal("0.5")), **kwargs,
    )
    assert partial.status == "PASS"
    failed = directional_liquidity_ceiling(
        rows, [{"role": "opening", "reduce_only": False, "notional": "100"}],
        policy=policy(min_eligible_fraction=Decimal("0.75")), **kwargs,
    )
    assert failed.status == "UNKNOWN"
    assert failed.reason == "LIQUIDITY_QUALITY_INSUFFICIENT"


def test_tier_and_exposure_lineage_are_required_for_validity() -> None:
    geometry = [{"role": "opening", "reduce_only": False, "notional": "100"}]
    result = directional_liquidity_ceiling(
        _dense_rows({"symbol": "BTCUSDT", "bid_depth_usdt_10bps_p05": "1000", "bid_depth_10bps_complete_ratio": "1", "coverage_ratio": "1"}),
        geometry, symbol="BTCUSDT", side="LONG", policy=policy(), sizing_base=Decimal("1000"), captured_at_ms=1,
        now_ms=2,
        source="fixture", window_start_ms=WINDOW_START_MS, window_end_ms=WINDOW_END_MS,
        tier={"risk_limit_value": "1000", "max_leverage": "50"}, exposure={"position": "0", "active_orders": "100"},
    )
    validity = dict(now_ms=2, geometry=geometry, tier={"risk_limit_value": "1000", "max_leverage": "50"}, exposure={"position": "0", "active_orders": "100"}, policy=policy(), symbol="BTCUSDT", side="LONG", window_start_ms=WINDOW_START_MS, window_end_ms=WINDOW_END_MS)
    assert result.is_valid(**validity)
    assert not result.is_valid(**{**validity, "tier": {"risk_limit_value": "2000", "max_leverage": "50"}})
    assert not result.is_valid(**{**validity, "tier": {"risk_limit_value": "1000", "max_leverage": "25"}})
    assert not result.is_valid(**{**validity, "policy": policy(allowed_depth_share=Decimal("0.20"))})
    assert not result.is_valid(**{**validity, "symbol": "ETHUSDT"})
    assert not result.is_valid(**{**validity, "window_end_ms": WINDOW_END_MS + 1})
    assert not result.is_valid(now_ms=2, geometry=geometry)


def test_ceiling_requires_explicit_source_window_and_reference_lineage() -> None:
    rows = _dense_rows({"symbol": "BTCUSDT", "bid_depth_usdt_10bps_p05": "1000", "bid_depth_10bps_complete_ratio": "1", "coverage_ratio": "1"})
    common = dict(
        rows=rows,
        geometry=[{"role": "opening", "reduce_only": False, "notional": "100"}],
        symbol="BTCUSDT", side="LONG", policy=policy(), sizing_base=Decimal("1000"),
        captured_at_ms=1, now_ms=2, source="fixture", window_start_ms=WINDOW_START_MS,
        window_end_ms=WINDOW_END_MS, tier={"risk_limit_value": "1000"},
        exposure={"position": "0", "active_orders": "100"},
    )
    assert directional_liquidity_ceiling(**{**common, "source": None}).reason == "OPEN_POLICY"
    assert directional_liquidity_ceiling(**{**common, "window_end_ms": WINDOW_END_MS + 60_000}).reason == "OPEN_POLICY"
    assert directional_liquidity_ceiling(**{**common, "tier": None}).reason == "OPEN_POLICY"
    assert directional_liquidity_ceiling(**{**common, "exposure": None}).reason == "OPEN_POLICY"


def test_ceiling_filters_rows_to_half_open_window_before_quality_fraction() -> None:
    rows = _dense_rows({"symbol": "BTCUSDT", "bid_depth_usdt_10bps_p05": "1000", "bid_depth_10bps_complete_ratio": "1", "coverage_ratio": "1"})
    rows.append({"symbol": "BTCUSDT", "minute_ts_ms": WINDOW_END_MS, "bid_depth_usdt_10bps_p05": "1", "bid_depth_10bps_complete_ratio": "1", "coverage_ratio": "1"})
    result = directional_liquidity_ceiling(
        rows, [{"role": "opening", "reduce_only": False, "notional": "100"}],
        symbol="BTCUSDT", side="LONG", policy=policy(), sizing_base=Decimal("1000"),
        captured_at_ms=1, now_ms=2, source="fixture", window_start_ms=WINDOW_START_MS,
        window_end_ms=WINDOW_END_MS, tier={"risk_limit_value": "1000"},
        exposure={"position": "0", "active_orders": "100"},
    )
    assert result.status == "PASS"
    assert result.depth_reference == Decimal("1000")


def test_geometry_role_reduce_only_and_notional_are_explicit() -> None:
    rows = _dense_rows({"symbol": "BTCUSDT", "bid_depth_usdt_10bps_p05": "1000", "bid_depth_10bps_complete_ratio": "1", "coverage_ratio": "1"})
    kwargs = dict(symbol="BTCUSDT", side="LONG", policy=policy(), sizing_base=Decimal("1000"), captured_at_ms=1, now_ms=2, source="fixture", window_start_ms=WINDOW_START_MS, window_end_ms=WINDOW_END_MS, tier={"risk_limit_value": "1000"}, exposure={"position": "0", "active_orders": "100"})
    with pytest.raises(ValueError, match="role"):
        directional_liquidity_ceiling(rows, [{"notional": "100"}], **kwargs)
    with pytest.raises(ValueError, match="reduce_only"):
        directional_liquidity_ceiling(rows, [{"role": "opening", "notional": "100", "reduce_only": 0}], **kwargs)
    with pytest.raises(ValueError, match="notional"):
        directional_liquidity_ceiling(rows, [{"role": "opening", "reduce_only": False, "notional": "0"}], **kwargs)


def test_exit_mapping_is_opposite_and_empirical_capacity_stays_open() -> None:
    rows = [{"symbol": "BTCUSDT", "bid_depth_usdt_10bps_p05": "100", "ask_depth_usdt_10bps_p05": "200", "bid_depth_10bps_complete_ratio": "1", "ask_depth_10bps_complete_ratio": "1", "coverage_ratio": "1"}]
    result = exit_liquidity_diagnostic(rows, [], symbol="BTCUSDT", side="LONG", policy=policy(), captured_at_ms=1, now_ms=2)
    assert result.status == "PASS"
    assert result.depth_reference == Decimal("200")
    opening_short = directional_liquidity_ceiling(
        _dense_rows(*rows[:2]),
        [{"role": "opening", "reduce_only": False, "notional": "100"}],
        symbol="BTCUSDT", side="SHORT", policy=policy(), sizing_base=Decimal("1000"),
        captured_at_ms=1, now_ms=2, source="fixture", window_start_ms=WINDOW_START_MS, window_end_ms=WINDOW_END_MS,
        tier={"risk_limit_value": "1000"}, exposure={"position": "0", "active_orders": "100"},
    )
    assert opening_short.depth_reference == Decimal("200")
    capacity = empirical_capacity({"symbol": "BTCUSDT"})
    assert capacity.status == "UNKNOWN" and capacity.reason == "OPEN_POLICY" and capacity.content_digest


def test_empirical_capacity_decimal_proxy_replay_keeps_canonical_digest() -> None:
    decimal_facts = {
        "proxy_capacity": Decimal("100.25"),
        "borrow": Decimal("2.5"),
        "active_order_reserve": Decimal("10"),
    }
    before = empirical_capacity(decimal_facts)
    after = empirical_capacity(dict(decimal_facts))
    assert before.status == after.status == "UNKNOWN"
    assert before.content_digest == after.content_digest


def test_omitted_now_is_never_treated_as_fresh() -> None:
    rows = _dense_rows({"symbol": "BTCUSDT", "bid_depth_usdt_10bps_p05": "1000", "bid_depth_10bps_complete_ratio": "1", "coverage_ratio": "1"})
    kwargs = dict(
        symbol="BTCUSDT", side="LONG", policy=policy(), sizing_base=Decimal("1000"), captured_at_ms=1,
        source="fixture", window_start_ms=WINDOW_START_MS, window_end_ms=WINDOW_END_MS,
        tier={"risk_limit_value": "1000"}, exposure={"position": "0", "active_orders": "100"},
    )
    ceiling = directional_liquidity_ceiling(rows, [{"role": "opening", "reduce_only": False, "notional": "100"}], **kwargs)
    assert ceiling.status == "UNKNOWN"
    assert ceiling.reason == "LIQUIDITY_STALE"
    exit_result = exit_liquidity_diagnostic(
        [{"symbol": "BTCUSDT", "ask_depth_usdt_10bps_p05": "200", "ask_depth_10bps_complete_ratio": "1"}],
        [], symbol="BTCUSDT", side="LONG", policy=policy(), captured_at_ms=1,
    )
    assert exit_result.status == "UNKNOWN"
    assert exit_result.reason == "LIQUIDITY_STALE"


def test_exit_requires_exact_nonnegative_capture_timestamp() -> None:
    with pytest.raises(ValueError, match="captured_at_ms"):
        exit_liquidity_diagnostic([], [], symbol="BTCUSDT", side="LONG", policy=policy(), captured_at_ms=True, now_ms=2)
    with pytest.raises(ValueError, match="captured_at_ms"):
        exit_liquidity_diagnostic([], [], symbol="BTCUSDT", side="LONG", policy=policy(), captured_at_ms=-1, now_ms=2)


def test_reader_uses_exactly_168_marked_hours_and_ignores_unmarked_files(tmp_path: Path) -> None:
    now_ms = _write_marked_fixture(tmp_path)
    extra = tmp_path / "liquidity_1m/date=fixture/unmarked.parquet"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b"not a parquet file")
    history = LiquidityReader(tmp_path, policy()).read("BTCUSDT", now_ms=now_ms)
    assert len(history.rows) == 168
    assert history.source == "bybit_liquidity_1m/published_hours"
    assert history.content_digest


@pytest.mark.parametrize("kwargs, reason", [
    ({"missing_hour": 48}, "LIQUIDITY_MISSING"),
    ({"escape_hour": 48}, "LIQUIDITY_MISSING"),
    ({"bad_count_hour": 48}, "LIQUIDITY_QUALITY_INSUFFICIENT"),
    ({"bad_schema_hour": 48}, "LIQUIDITY_QUALITY_INSUFFICIENT"),
])
def test_reader_rejects_marked_missing_escape_count_or_schema(tmp_path: Path, kwargs: dict[str, int], reason: str) -> None:
    now_ms = _write_marked_fixture(tmp_path, **kwargs)
    with pytest.raises(LiquidityError) as error:
        LiquidityReader(tmp_path, policy()).read("BTCUSDT", now_ms=now_ms)
    assert error.value.reason == reason


def test_reader_content_digest_covers_rows_because_markers_have_no_checksum(tmp_path: Path) -> None:
    now_ms = _write_marked_fixture(tmp_path)
    reader = LiquidityReader(tmp_path, policy())
    first = reader.read("BTCUSDT", now_ms=now_ms)
    with SQLiteSpool.open_read_only(tmp_path) as spool:
        marker = spool.published_hours()[0]
        assert not hasattr(marker, "checksum")
    assert first.content_digest


def test_reader_rejects_wrong_hour_row(tmp_path: Path) -> None:
    now_ms = _write_marked_fixture(tmp_path, wrong_hour=48)
    with pytest.raises(LiquidityError, match="outside marked hour") as error:
        LiquidityReader(tmp_path, policy()).read("BTCUSDT", now_ms=now_ms)
    assert error.value.reason == "LIQUIDITY_QUALITY_INSUFFICIENT"


def test_reader_rejects_duplicate_published_hour_markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now_ms = _write_marked_fixture(tmp_path)
    with SQLiteSpool.open_read_only(tmp_path) as spool:
        marker = spool.published_hours()[0]

    class DuplicateMarkerSpool:
        def __enter__(self) -> "DuplicateMarkerSpool":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def published_hours(self) -> tuple[PublishedHour, ...]:
            return (marker, marker)

    monkeypatch.setattr(liquidity_module.SQLiteSpool, "open_read_only", lambda _root: DuplicateMarkerSpool())
    with pytest.raises(LiquidityError, match="duplicate hour") as error:
        LiquidityReader(tmp_path, policy()).read("BTCUSDT", now_ms=now_ms)
    assert error.value.reason == "LIQUIDITY_QUALITY_INSUFFICIENT"


def test_reference_reader_from_parquet_is_read_only_and_typed(tmp_path: Path) -> None:
    instruments = tmp_path / "instruments.parquet"
    risks = tmp_path / "risk_limits.parquet"
    connection = duckdb.connect()
    try:
        connection.execute("CREATE TABLE instruments (symbol VARCHAR, status VARCHAR, contract_type VARCHAR, tick_size DECIMAL(18,6), qty_step DECIMAL(18,6), min_order_qty DECIMAL(18,6), max_order_qty DECIMAL(18,6), leverage_step DECIMAL(18,6), max_leverage DECIMAL(18,6))")
        connection.execute("INSERT INTO instruments VALUES ('BTCUSDT', 'Trading', 'LinearPerpetual', 0.1, 0.001, 0.001, 100, 0.01, 100)")
        connection.execute(f"COPY instruments TO '{instruments.as_posix()}' (FORMAT PARQUET)")
        connection.execute("CREATE TABLE risks (symbol VARCHAR, risk_limit_value DECIMAL(18,6), max_leverage DECIMAL(18,6))")
        connection.execute("INSERT INTO risks VALUES ('BTCUSDT', 1000, 50)")
        connection.execute(f"COPY risks TO '{risks.as_posix()}' (FORMAT PARQUET)")
    finally:
        connection.close()
    snapshot = ReferenceReader.from_parquet(instruments, risks, captured_at_ms=10, symbol="BTCUSDT")
    assert snapshot.instrument("BTCUSDT").tick_size == Decimal("0.1")
    assert snapshot.maximum_symbol_leverage("BTCUSDT", "1", "2") == Decimal("50.00")


def test_reference_reader_selects_tier_and_rounds_leverage_down() -> None:
    snapshot = ReferenceReader.from_records(
        instruments=[
            {
                "symbol": "BTCUSDT",
                "status": "Trading",
                "contract_type": "LinearPerpetual",
                "qty_step": "0.001",
                "min_qty": "0.001",
                "max_qty": "100",
                "tick_size": "0.1",
                    "leverage_step": "0.01",
                    "max_leverage": "100",
            }
        ],
        risk_tiers=[
            {"symbol": "BTCUSDT", "risk_limit_value": "1000", "max_leverage": "50"},
            {"symbol": "BTCUSDT", "risk_limit_value": "10000", "max_leverage": "25"},
        ],
        captured_at_ms=1_000,
    )
    assert snapshot.instrument("BTCUSDT").qty_step == Decimal("0.001")
    tier = snapshot.applicable_tier("BTCUSDT", position_exposure="800", active_order_exposure="100")
    assert tier.risk_limit_value == Decimal("1000")
    assert snapshot.maximum_symbol_leverage("BTCUSDT", "800", "100") == Decimal("50.00")
    assert snapshot.maximum_symbol_leverage("BTCUSDT", "1000", "0") == Decimal("25.00")


def test_reference_reader_requires_trading_linear_perpetual_for_leverage() -> None:
    snapshot = ReferenceReader.from_records(
        instruments=[{
            "symbol": "BTCUSDT", "status": "PreLaunch", "contract_type": "LinearPerpetual",
            "qty_step": "0.001", "min_qty": "0.001", "max_qty": "100", "tick_size": "0.1",
            "leverage_step": "0.01", "max_leverage": "100",
        }],
        risk_tiers=[{"symbol": "BTCUSDT", "risk_limit_value": "1000", "max_leverage": "50"}],
        captured_at_ms=1,
    )
    with pytest.raises(LiquidityError, match="active linear perpetual"):
        snapshot.maximum_symbol_leverage("BTCUSDT", "0", "0")


def test_ticker_adapter_requests_exact_linear_symbol_and_fail_closes() -> None:
    calls: list[dict[str, str]] = []

    def fetch(params: dict[str, str]) -> dict:
        calls.append(params)
        return {
            "retCode": 0,
            "retMsg": "OK",
            "time": 1234,
            "result": {"category": "linear", "list": [{
                "symbol": "BTCUSDT", "turnover24h": "1000", "volume24h": "10"
            }]},
        }

    result = TickerAdapter(fetch).fetch("BTCUSDT", captured_at_ms=2_000)
    assert calls == [{"category": "linear", "symbol": "BTCUSDT"}]
    assert result.turnover24h == Decimal("1000")
    assert result.volume24h == Decimal("10")
    assert result.unit == "USDT"
    assert result.server_time_ms == 1234
    assert result.capture_time_ms == 2_000
    assert result.content_digest

    failed = TickerAdapter(lambda _params: (_ for _ in ()).throw(TimeoutError())).fetch(
        "BTCUSDT", captured_at_ms=2_000
    )
    assert failed.status == "UNKNOWN"
    assert failed.reason == "TURNOVER_REQUEST_FAILED"


@pytest.mark.parametrize("payload", [
    {"retCode": 0, "result": {"category": "linear", "list": []}},
    {"retCode": 0, "result": {"category": "linear", "list": [{"symbol": "ETHUSDT", "turnover24h": "1", "volume24h": "1"}]}},
    {"retCode": 0, "result": {"category": "inverse", "list": [{"symbol": "BTCUSDT", "turnover24h": "1", "volume24h": "1"}]}},
    {"retCode": 0, "result": {"category": "linear", "list": [{"symbol": "BTCUSDT", "turnover24h": "bad", "volume24h": "1"}]}},
])
def test_ticker_adapter_malformed_payload_is_unknown_with_failure_digest(payload: dict[str, object]) -> None:
    result = TickerAdapter(lambda _params: payload).fetch("BTCUSDT", captured_at_ms=2_000)
    assert result.status == "UNKNOWN"
    assert result.reason == "TURNOVER_MISSING"
    assert result.content_digest


def test_coarse_screen_validates_members_turnover_and_zero_amount() -> None:
    with pytest.raises(ValueError, match="member side"):
        coarse_symbol_screen([{"symbol": "BTCUSDT", "opening_notional": "1"}], {"BTCUSDT": Decimal("100")}, permitted_turnover_share=Decimal("0.5"), composition_id="p")
    with pytest.raises(ValueError, match="opening_notional"):
        coarse_symbol_screen([{"symbol": "BTCUSDT", "side": "LONG", "opening_notional": "bad"}], {"BTCUSDT": Decimal("100")}, permitted_turnover_share=Decimal("0.5"), composition_id="p")
    missing = coarse_symbol_screen([{"symbol": "BTCUSDT", "side": "LONG", "opening_notional": "0"}], {}, permitted_turnover_share=Decimal("0.5"), composition_id="p")
    assert missing.status == "UNKNOWN" and missing.reason == "TURNOVER_MISSING"
    assert missing.total_opening_notional == Decimal(0)
    bare_numeric = coarse_symbol_screen(
        [{"symbol": "BTCUSDT", "side": "LONG", "opening_notional": "1"}],
        {"BTCUSDT": Decimal("100")}, permitted_turnover_share=Decimal("0.5"), composition_id="p",
        now_ms=2, max_age_ms=10,
    )
    assert bare_numeric.status == "UNKNOWN" and bare_numeric.reason == "TURNOVER_MISSING"


def test_coarse_snapshot_requires_explicit_freshness_and_exact_identity() -> None:
    snapshot = TickerSnapshot("PASS", "BTCUSDT", "linear", Decimal("1000"), Decimal("10"), 1, 1, "USDT", "fixture", "digest")
    member = [{"symbol": "BTCUSDT", "side": "LONG", "opening_notional": "100"}]
    assert coarse_symbol_screen(member, {"BTCUSDT": snapshot}, permitted_turnover_share=Decimal("0.5"), composition_id="p", now_ms=2, max_age_ms=10).status == "PASS"
    assert coarse_symbol_screen(member, {"BTCUSDT": snapshot}, permitted_turnover_share=Decimal("0.5"), composition_id="p", now_ms=2, max_age_ms=None).reason == "TURNOVER_MISSING"
    stale = TickerSnapshot("PASS", "BTCUSDT", "linear", Decimal("1000"), Decimal("10"), 1, 0, "USDT", "fixture", "digest")
    assert coarse_symbol_screen(member, {"BTCUSDT": stale}, permitted_turnover_share=Decimal("0.5"), composition_id="p", now_ms=2, max_age_ms=1).reason == "TURNOVER_MISSING"


def test_coarse_screen_counts_repeated_symbol_without_long_short_netting() -> None:
    snapshot = TickerSnapshot("PASS", "BTCUSDT", "linear", Decimal("1000"), Decimal("10"), 1, 1, "USDT", "fixture", "digest")
    result = coarse_symbol_screen(
        [
            {"symbol": "BTCUSDT", "side": "LONG", "opening_notional": "400"},
            {"symbol": "BTCUSDT", "side": "SHORT", "opening_notional": "400"},
        ],
        {"BTCUSDT": snapshot},
        permitted_turnover_share=Decimal("0.5"),
        composition_id="portfolio-1",
        now_ms=2,
        max_age_ms=10,
    )
    assert result.status == "FAIL"
    assert result.evidence_class == COARSE_ESTIMATE
    assert result.total_opening_notional == Decimal("800")
    assert result.composition_digest


def test_sizing_envelope_requires_finite_upper_bound() -> None:
    assert sizing_envelope(Decimal("1000"), Decimal("500")) == SizingEnvelopeResult("PASS", None, Decimal("500"))
    assert sizing_envelope(Decimal("1000"), None) == SizingEnvelopeResult("PASS", None, Decimal("1000"))
    unknown = sizing_envelope(None, None)
    assert unknown.status == "UNKNOWN"
    assert unknown.reason == "SIZING_ENVELOPE_UNBOUNDED"

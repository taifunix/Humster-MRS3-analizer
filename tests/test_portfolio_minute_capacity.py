from datetime import date, datetime, timezone
from decimal import Decimal
import gzip
from io import BytesIO
from pathlib import Path

import pytest

from mrs3.portfolio.minute_capacity import (
    MinuteCapacityError,
    backfill_missing_days,
    calculate_minute_capacity,
    fetch_bybit_trade_archive,
    resolve_liquidity_window,
)


HEADER = "timestamp,open,high,low,close,volume,buy_volume,sell_volume,trades\n"


def write_day(root: Path, symbol: str, day: date, rows: list[str]) -> Path:
    folder = root / symbol
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{symbol}{day.isoformat()}_1m.csv"
    path.write_text(HEADER + "".join(rows), encoding="utf-8", newline="")
    return path


def minute(day: date, offset: int, *, close="10", volume="10", buy="4", sell="6", trades=2) -> str:
    stamp = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000) + offset * 60_000
    return f"{stamp},{close},{close},{close},{close},{volume},{buy},{sell},{trades}\n"


def test_existing_tester_csv_with_utf8_bom_is_accepted(tmp_path: Path):
    day = date(2026, 9, 1)
    path = tmp_path / "BTCUSDT" / "BTCUSDT2026-09-01_1m.csv"
    path.parent.mkdir()
    path.write_text(HEADER + minute(day, 0), encoding="utf-8-sig")

    result = calculate_minute_capacity(
        tmp_path, "BTCUSDT", end_date=day, participation_pct=30,
        round_down_usdt=50, weekend_start_utc="SATURDAY 00:00",
        weekend_end_utc="MONDAY 00:00", publication_lag_hours=6,
        now=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
    )

    assert result.status == "PRELIMINARY"


def test_capacity_uses_all_clock_minutes_and_default_weekday_window(tmp_path: Path):
    # 2026-09-07 is Monday. One 100 USDT traded minute per day.
    for offset in range(7):
        day = date(2026, 9, 7 + offset)
        write_day(tmp_path, "BTCUSDT", day, [minute(day, 0)])

    result = calculate_minute_capacity(
        tmp_path,
        "BTCUSDT",
        end_date=date(2026, 9, 13),
        participation_pct=30,
        round_down_usdt="0.001",
        now=datetime(2026, 9, 14, 8, tzinfo=timezone.utc),
        publication_lag_hours=6,
    )

    assert result.status == "READY"
    assert result.calendar_7d.clock_minutes == 10_080
    assert result.weekday_5d.clock_minutes == 7_200
    assert result.weekday_5d.available_days == 5
    assert result.calendar_7d.observed_minutes == 7
    assert result.calendar_7d.zero_trade_minutes == 10_073
    assert result.calendar_7d.mean_minute_turnover == Decimal("700") / Decimal(10_080)
    assert result.weekday_5d.mean_minute_turnover == Decimal("500") / Decimal(7_200)
    assert result.position_cap_usdt == Decimal("0.020")


def test_missing_days_are_preliminary_without_turning_them_into_zero(tmp_path: Path):
    day = date(2026, 9, 13)
    write_day(tmp_path, "ETHUSDT", day, [minute(day, 0, close="20", volume="5", buy="2", sell="3")])

    result = calculate_minute_capacity(
        tmp_path,
        "ETHUSDT",
        end_date=day,
        participation_pct=30,
        round_down_usdt="0.001",
        now=datetime(2026, 9, 14, 8, tzinfo=timezone.utc),
        publication_lag_hours=6,
    )

    assert result.status == "PRELIMINARY"
    assert len(result.missing_days) == 6
    assert result.calendar_7d.available_days == 1
    assert result.calendar_7d.clock_minutes == 1_440
    assert result.calendar_7d.mean_minute_turnover == Decimal("100") / Decimal(1_440)


def test_zero_trade_minutes_are_counted_but_missing_days_are_not(tmp_path: Path):
    day = date(2026, 9, 13)
    write_day(tmp_path, "BTCUSDT", day, [minute(day, 0)])

    result = calculate_minute_capacity(
        tmp_path, "BTCUSDT", end_date=day, participation_pct=30,
        now=datetime(2026, 9, 14, 8, tzinfo=timezone.utc), publication_lag_hours=6,
    )

    assert result.calendar_7d.clock_minutes == 1_440
    assert result.calendar_7d.zero_trade_minutes == 1_439
    assert len(result.missing_days) == 6


def test_no_available_days_fails_closed(tmp_path: Path):
    with pytest.raises(MinuteCapacityError, match="no valid daily CSV"):
        calculate_minute_capacity(
            tmp_path,
            "ETHUSDT",
            end_date=date(2026, 9, 13),
            participation_pct=30,
            now=datetime(2026, 9, 15, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize("participation", [1, 30, 200])
def test_participation_range_is_inclusive(tmp_path: Path, participation: int):
    day = date(2026, 9, 13)
    write_day(tmp_path, "BTCUSDT", day, [minute(day, 0)])
    assert calculate_minute_capacity(tmp_path, "BTCUSDT", end_date=day, participation_pct=participation, now=datetime(2026, 9, 15, tzinfo=timezone.utc)).participation_pct == participation


@pytest.mark.parametrize("participation", [0, 201, True, 1.5])
def test_bad_participation_is_rejected(tmp_path: Path, participation):
    with pytest.raises(ValueError):
        calculate_minute_capacity(tmp_path, "BTCUSDT", end_date=date(2026, 9, 13), participation_pct=participation, now=datetime(2026, 9, 15, tzinfo=timezone.utc))


@pytest.mark.parametrize(
    "rows",
    [
        lambda day: [minute(day, 1), minute(day, 1)],
        lambda day: [minute(day, 2), minute(day, 1)],
        lambda day: [minute(day, 1440)],
        lambda day: [minute(day, 0, volume="11", buy="4", sell="6")],
        lambda day: [minute(day, 0, trades=0)],
    ],
)
def test_invalid_existing_day_fails_closed(tmp_path: Path, rows):
    day = date(2026, 9, 13)
    write_day(tmp_path, "BTCUSDT", day, rows(day))
    with pytest.raises(MinuteCapacityError):
        calculate_minute_capacity(tmp_path, "BTCUSDT", end_date=day, participation_pct=30, now=datetime(2026, 9, 15, tzinfo=timezone.utc))


def test_publication_lag_rejects_not_yet_publishable_end_day(tmp_path: Path):
    day = date(2026, 9, 13)
    write_day(tmp_path, "BTCUSDT", day, [minute(day, 0)])
    with pytest.raises(MinuteCapacityError, match="publication lag"):
        calculate_minute_capacity(tmp_path, "BTCUSDT", end_date=day, participation_pct=30, publication_lag_hours=6, now=datetime(2026, 9, 14, 5, tzinfo=timezone.utc))


def archive_bytes(symbol: str, day: date) -> bytes:
    raw = (
        "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
        f"{day.isoformat()}T00:00:01.000Z,{symbol},Buy,2,10,PlusTick,a,0,0,0\n"
        f"{day.isoformat()}T00:00:20.000Z,{symbol},Sell,3,11,MinusTick,b,0,0,0\n"
        f"{day.isoformat()}T00:01:00.000Z,{symbol},Buy,1,12,PlusTick,c,0,0,0\n"
    ).encode()
    stream = BytesIO()
    with gzip.GzipFile(fileobj=stream, mode="wb") as target:
        target.write(raw)
    return stream.getvalue()


def test_injected_archive_backfill_aggregates_and_never_overwrites(tmp_path: Path):
    day = date(2026, 9, 13)
    calls = []

    def fetch(symbol, requested):
        calls.append((symbol, requested))
        return archive_bytes(symbol, requested)

    first = backfill_missing_days(tmp_path, "BTCUSDT", (day,), fetch_day=fetch, enabled=True)
    target = tmp_path / "BTCUSDT" / "BTCUSDT2026-09-13_1m.csv"
    original = target.read_bytes()
    second = backfill_missing_days(tmp_path, "BTCUSDT", (day,), fetch_day=lambda *_: b"bad", enabled=True)

    assert first.created == (day,)
    assert second.existing == (day,)
    assert target.read_bytes() == original
    rows = target.read_text(encoding="utf-8").splitlines()
    assert rows[1].endswith(",5,2,3,2")
    assert rows[2].endswith(",1,1,0,1")
    assert len(calls) == 1


def test_disabled_backfill_only_reports_missing(tmp_path: Path):
    day = date(2026, 9, 13)
    result = backfill_missing_days(tmp_path, "BTCUSDT", (day,), fetch_day=lambda *_: b"", enabled=False)
    assert result.missing == (day,)
    assert not (tmp_path / "BTCUSDT").exists()


def test_failed_backfill_remains_reported_as_missing(tmp_path: Path):
    day = date(2026, 9, 13)
    result = backfill_missing_days(
        tmp_path,
        "BTCUSDT",
        (day,),
        fetch_day=lambda *_: b"invalid",
        enabled=True,
        attempts=1,
    )
    assert result.missing == (day,)
    assert tuple(result.failed) == (day,)


def test_public_archive_fetch_uses_exact_bybit_daily_path(monkeypatch: pytest.MonkeyPatch):
    calls = []

    class Response:
        content = b"archive"

        def raise_for_status(self):
            return None

    def get(url, *, timeout, follow_redirects):
        calls.append((url, timeout, follow_redirects))
        return Response()

    monkeypatch.setattr("mrs3.portfolio.minute_capacity.httpx.get", get)
    data = fetch_bybit_trade_archive("BTCUSDT", date(2026, 9, 8))
    assert data == b"archive"
    assert calls == [("https://public.bybit.com/trading/BTCUSDT/BTCUSDT2026-09-08.csv.gz", 30.0, True)]


def test_lot_model_freezes_seven_days_and_computes_type7_q25_and_a15(tmp_path: Path):
    anchor = datetime(2026, 9, 8, 8, tzinfo=timezone.utc)
    days = resolve_liquidity_window(anchor, 6)
    assert days == tuple(date(2026, 9, day) for day in range(1, 8))
    for day in days:
        rows = []
        if day == days[0]:
            rows = [
                minute(day, 0, close="1", volume="10", buy="5", sell="5"),
                minute(day, 1, close="1", volume="20", buy="10", sell="10"),
                minute(day, 16, close="1", volume="30", buy="15", sell="15"),
                minute(day, 31, close="1", volume="40", buy="20", sell="20"),
            ]
        write_day(tmp_path, "BTCUSDT", day, rows)

    result = calculate_minute_capacity(
        tmp_path, "BTCUSDT", lot_model=True, anchor_created_at=anchor,
        publication_lag_hours=6,
    )

    assert result.status == "READY"
    assert result.window_start == days[0] and result.window_end == days[-1]
    assert result.position_cap_usdt == 0
    assert result.lot_features.v25 == Decimal("17.5")
    assert result.lot_features.active_15m_bins == 3
    assert result.lot_features.a15 == Decimal(3) / Decimal(672)
    assert result.lot_features.window_dates == days
    assert result.lot_features.publication_lag_hours == 6
    assert result.lot_features.anchor_created_at_utc == anchor.isoformat()
    assert len(result.lot_features.file_hashes) == 7
    assert all(len(digest) == 64 for _name, digest in result.lot_features.file_hashes)
    assert all("tmp" not in name.lower() for name, _digest in result.lot_features.file_hashes)
    assert result.lot_features.content_digest
    assert result.content_digest
    changed_lag = calculate_minute_capacity(
        tmp_path, "BTCUSDT", lot_model=True, anchor_created_at=anchor,
        publication_lag_hours=7,
    )
    assert changed_lag.lot_features.window_dates == days
    assert changed_lag.lot_features.content_digest != result.lot_features.content_digest


def test_lot_model_requires_every_valid_file_and_positive_turnover(tmp_path: Path):
    anchor = datetime(2026, 9, 8, 6, tzinfo=timezone.utc)
    days = resolve_liquidity_window(anchor, 6)
    for day in days[:-1]:
        write_day(tmp_path, "BTCUSDT", day, [minute(day, 0)])
    with pytest.raises(MinuteCapacityError, match="LIQUIDITY_MODEL_WINDOW_UNAVAILABLE"):
        calculate_minute_capacity(tmp_path, "BTCUSDT", lot_model=True, anchor_created_at=anchor)

    for day in days:
        write_day(tmp_path, "ETHUSDT", day, [])
    with pytest.raises(MinuteCapacityError, match="LIQUIDITY_MODEL_WINDOW_UNAVAILABLE"):
        calculate_minute_capacity(tmp_path, "ETHUSDT", lot_model=True, anchor_created_at=anchor)

    for day in days:
        write_day(tmp_path, "XRPUSDT", day, [minute(day, 0)])
    (tmp_path / "XRPUSDT" / f"XRPUSDT{days[3].isoformat()}_1m.csv").write_text("bad header\n", encoding="utf-8")
    with pytest.raises(MinuteCapacityError, match="LIQUIDITY_MODEL_WINDOW_UNAVAILABLE"):
        calculate_minute_capacity(tmp_path, "XRPUSDT", lot_model=True, anchor_created_at=anchor)


def test_lot_model_requires_a_frozen_aware_campaign_anchor(tmp_path: Path):
    with pytest.raises(MinuteCapacityError, match="LIQUIDITY_MODEL_ANCHOR_UNAVAILABLE"):
        calculate_minute_capacity(
            tmp_path, "BTCUSDT", lot_model=True,
            anchor_created_at=datetime(2026, 9, 8, 6),
        )


def test_liquidity_window_uses_publication_lag_at_campaign_creation_boundary():
    assert resolve_liquidity_window(datetime(2026, 9, 8, 5, 59, tzinfo=timezone.utc), 6)[-1] == date(2026, 9, 6)
    assert resolve_liquidity_window(datetime(2026, 9, 8, 6, tzinfo=timezone.utc), 6)[-1] == date(2026, 9, 7)
    anchor = datetime(2026, 9, 8, 6, 30, tzinfo=timezone.utc)
    assert resolve_liquidity_window(anchor, 6)[-1] == date(2026, 9, 7)
    assert resolve_liquidity_window(anchor, 7)[-1] == date(2026, 9, 6)

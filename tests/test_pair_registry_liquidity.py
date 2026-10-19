"""Pair registry stage 2: weekday liquidity, LOT_MODEL_V2 (ADR-0079).

Contract: docs/specs/2026-10-18-pair-registry.md, «Stage 2 contract».
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from mrs3.pair_registry.liquidity import (
    MODEL, day_facts, read_liquidity, refresh_liquidity, select_window, window_row,
)
from mrs3.pair_registry.refresh import refresh_exchange
from mrs3.pair_registry.store import RegistrySettings, connect
from mrs3.portfolio.minute_capacity import _lot_model_capacity
from mrs3.tester_candles import encode_day

MONDAY = date(2026, 10, 5)          # a Monday


def _write_day(root: Path, symbol: str, day: date, minutes: dict[int, tuple[float, float]]) -> Path:
    """minute index -> (close, volume); one trade per listed minute."""
    start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
    columns = {name: [] for name in ("timestamp", "open", "high", "low", "close", "volume", "buy_volume", "sell_volume", "trades")}
    for minute in sorted(minutes):
        close, volume = minutes[minute]
        columns["timestamp"].append(start + minute * 60_000)
        for name in ("open", "high", "low", "close"):
            columns[name].append(close)
        columns["volume"].append(volume)
        columns["buy_volume"].append(volume)
        columns["sell_volume"].append(0.0)
        columns["trades"].append(1)
    path = root / symbol / f"{symbol}{day.isoformat()}_1m.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_day(columns))
    return path


def _minutes(day: date) -> dict[int, tuple[float, float]]:
    """Busy weekdays, thin weekends: deterministic per day."""
    seed = day.toordinal() % 7 + 1
    if day.weekday() >= 5:
        return {minute: (10.0, 0.5) for minute in range(0, 1440, 240)}
    return {minute: (10.0 + seed, float(1 + (minute * seed) % 13)) for minute in range(0, 1440, 7)}


def test_day_facts_count_positive_minutes_and_active_bins(tmp_path: Path) -> None:
    path = _write_day(tmp_path, "XOMUSDT", MONDAY, {0: (10.0, 2.0), 14: (10.0, 1.0), 15: (20.0, 0.5), 600: (5.0, 4.0)})
    facts = day_facts(path, MONDAY)
    assert (facts.weekend, facts.positive_minutes, facts.active_bins) == (False, 4, 3)    # bins 0, 1 and 40
    assert facts.turnover == Decimal("20") + Decimal("10") + Decimal("10") + Decimal("20")
    assert len(facts.file_sha256) == 64
    saturday = _write_day(tmp_path, "XOMUSDT", MONDAY + timedelta(days=5), {0: (10.0, 1.0)})
    assert day_facts(saturday, MONDAY + timedelta(days=5)).weekend is True


def test_window_formulas_equal_lot_model_v1_on_the_same_days(tmp_path: Path) -> None:
    root = tmp_path / "data"
    days = [MONDAY + timedelta(days=offset) for offset in range(7)]
    for day in days:
        _write_day(root, "XOMUSDT", day, _minutes(day))
    anchor = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)     # V1 window: Mon 5 .. Sun 11
    v1 = _lot_model_capacity(root, "XOMUSDT", anchor_created_at=anchor, round_down_usdt="10", publication_lag_hours=6)
    assert v1.window_start == days[0] and v1.window_end == days[-1]
    row = window_row([day_facts(root / "XOMUSDT" / f"XOMUSDT{day.isoformat()}_1m.parquet", day) for day in days],
                     k=Decimal("9"), step=Decimal("10"))
    assert Decimal(row["v25"]) == v1.lot_features.v25 and Decimal(row["a15"]) == v1.lot_features.a15
    assert Decimal(row["l"]) == v1.lot_features.v25 * v1.lot_features.a15
    raw = Decimal(9) * Decimal(row["l"])
    assert Decimal(row["base_lot"]) == (raw / 10).to_integral_value(rounding="ROUND_DOWN") * 10
    assert (row["window_days"], row["total_bins"]) == ([day.isoformat() for day in days], 7 * 96)


def test_the_window_takes_the_last_weekdays_only() -> None:
    files = {MONDAY + timedelta(days=offset): object() for offset in range(14)}          # two full weeks
    chosen, missing = select_window(files, MONDAY + timedelta(days=13), 7)                 # ends Sunday 18
    assert [day.isoformat() for day in chosen] == [
        "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]
    assert missing == []
    del files[date(2026, 10, 13)]
    chosen, missing = select_window(files, date(2026, 10, 16), 7)
    assert missing == [date(2026, 10, 13)] and len(chosen) == 6
    assert all(day.weekday() < 5 for day in chosen)                                       # weekends never enter
    assert [day.isoformat() for day in select_window(files, date(2026, 10, 16), 3)[0]] == [
        "2026-10-14", "2026-10-15", "2026-10-16"]


def _registry_with_pair(tmp_path: Path, symbol: str = "XOMUSDT"):
    con = connect(tmp_path / "registry")
    record = {"symbol": symbol, "status": "TRADING", "api_symbol": symbol, "listing_ms": 1_700_000_000_000}
    refresh_exchange(con, "bybit", [record], [], now_ms=1_760_000_000_000)
    return con


def test_refresh_writes_day_rows_and_a_weekday_window(tmp_path: Path) -> None:
    root = tmp_path / "data"
    for offset in range(14):
        day = MONDAY + timedelta(days=offset)
        _write_day(root, "XOMUSDT", day, _minutes(day))
    con = _registry_with_pair(tmp_path)
    settings = RegistrySettings(liquidity_data_root=root)
    summary = refresh_liquidity(con, "bybit", date(2026, 10, 19), settings)        # Monday 19 -> data until Sunday 18
    assert summary == {"pairs": 1, "ready": 1, "unavailable": 0, "day_rows": 11, "unreadable_days": 0}
    row = read_liquidity(con, "bybit", "XOMUSDT")
    assert (row["model"], row["as_of_day"], row["window_weekdays"]) == (MODEL, "2026-10-16", 7)
    assert row["window_days"][0] == "2026-10-08" and "2026-10-10" not in row["window_days"]
    weekend = con.execute("select count(*) from liquidity_days where weekend").fetchone()[0]
    assert weekend == 4
    again = refresh_liquidity(con, "bybit", date(2026, 10, 19), settings)
    assert again == summary
    assert con.execute("select count(*) from liquidity").fetchone()[0] == 1
    assert con.execute("select count(*) from liquidity_days").fetchone()[0] == 11


def test_a_missing_weekday_makes_the_window_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "data"
    for offset in range(14):
        day = MONDAY + timedelta(days=offset)
        if day != date(2026, 10, 14):
            _write_day(root, "XOMUSDT", day, _minutes(day))
    con = _registry_with_pair(tmp_path)
    summary = refresh_liquidity(con, "bybit", date(2026, 10, 19), RegistrySettings(liquidity_data_root=root))
    assert (summary["ready"], summary["unavailable"]) == (0, 1)
    status, missing, reason = con.execute("select status, missing_days, reason from liquidity").fetchone()
    assert status == "LIQUIDITY_MODEL_WINDOW_UNAVAILABLE" and json.loads(missing) == ["2026-10-14"]
    assert reason == "missing weekday files"
    assert read_liquidity(con, "bybit", "XOMUSDT") is None


def test_window_length_setting_is_provenance_only(tmp_path: Path) -> None:
    root = tmp_path / "data"
    for offset in range(14):
        day = MONDAY + timedelta(days=offset)
        _write_day(root, "XOMUSDT", day, _minutes(day))
    con = _registry_with_pair(tmp_path)
    refresh_liquidity(con, "bybit", date(2026, 10, 19), RegistrySettings(liquidity_data_root=root, liquidity_window_weekdays=5))
    row = read_liquidity(con, "bybit", "XOMUSDT")
    assert row["window_weekdays"] == 5 and len(row["window_days"]) == 5 and row["total_bins"] == 5 * 96
    assert set(row) >= {"l", "k", "step", "base_lot", "model", "as_of_day"}


@pytest.mark.parametrize("liquidity", [{"window_weekdays": 2}, {"window_weekdays": 21}, {"window_weekdays": "7"},
                                       {"window_weekdays": 7.5}, {"window_weekdays": True}, {"k": "0.5"}, {"k": 21},
                                       {"round_down_usdt": "2.5"}])
def test_liquidity_settings_are_validated(tmp_path: Path, liquidity) -> None:
    from mrs3.pair_registry.store import RegistryError, load_registry_settings

    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"liquidity": liquidity}}), encoding="utf-8")
    with pytest.raises(RegistryError):
        load_registry_settings(config)


def test_liquidity_settings_defaults(tmp_path: Path) -> None:
    from mrs3.pair_registry.store import load_registry_settings

    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"tester_runner": {"bot_root": str(tmp_path / "bot")}}), encoding="utf-8")
    settings = load_registry_settings(config)
    assert (settings.liquidity_window_weekdays, settings.liquidity_k, settings.liquidity_round_down) == (7, Decimal(9), Decimal(10))
    assert settings.liquidity_data_root == (tmp_path / "bot" / "tester" / "data" / "bybit").resolve()


def test_cli_liquidity_refreshes_the_registry_pairs(tmp_path: Path, capsys) -> None:
    from mrs3.pair_registry import cli

    root = tmp_path / "data"
    for offset in range(14):
        day = MONDAY + timedelta(days=offset)
        _write_day(root, "XOMUSDT", day, _minutes(day))
    _registry_with_pair(tmp_path).close()
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"root": str(tmp_path / "registry"),
                                                    "liquidity": {"data_root": str(root)}}}), encoding="utf-8")
    assert cli.main(["liquidity", "--config", str(config), "--today", "2026-10-19"]) == 0
    assert json.loads(capsys.readouterr().out) == {"bybit": {"pairs": 1, "ready": 1, "unavailable": 0, "day_rows": 11,
                                                             "unreadable_days": 0}}


def _two_weeks(root: Path, skip: tuple[date, ...] = ()) -> None:
    for offset in range(14):
        day = MONDAY + timedelta(days=offset)
        if day not in skip:
            _write_day(root, "XOMUSDT", day, _minutes(day))


def test_the_reader_fails_closed_on_a_newer_unavailable_window(tmp_path: Path) -> None:
    # review of registry stage 2: a newer UNAVAILABLE window must hide the older READY one (ADR-0079, fail closed)
    root = tmp_path / "data"
    _two_weeks(root)
    con = _registry_with_pair(tmp_path)
    settings = RegistrySettings(liquidity_data_root=root)
    refresh_liquidity(con, "bybit", date(2026, 10, 19), settings)
    assert read_liquidity(con, "bybit", "XOMUSDT")["as_of_day"] == "2026-10-16"
    (root / "XOMUSDT" / "XOMUSDT2026-10-14_1m.parquet").write_bytes(b"broken")
    _write_day(root, "XOMUSDT", date(2026, 10, 19), _minutes(date(2026, 10, 19)))
    summary = refresh_liquidity(con, "bybit", date(2026, 10, 20), settings)
    assert (summary["unavailable"], summary["unreadable_days"]) == (1, 1)
    assert read_liquidity(con, "bybit", "XOMUSDT") is None
    assert con.execute("select count(*) from liquidity_days where day = '2026-10-14'").fetchone()[0] == 0


def test_stale_minute_files_make_the_window_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _two_weeks(root)                                   # files end on Sunday 2026-10-18
    con = _registry_with_pair(tmp_path)
    settings = RegistrySettings(liquidity_data_root=root)
    refresh_liquidity(con, "bybit", date(2026, 10, 19), settings)
    refresh_liquidity(con, "bybit", date(2026, 10, 30), settings)
    rows = con.execute("select as_of_day, status from liquidity order by as_of_day").fetchall()
    assert [(day.isoformat(), status) for day, status in rows] == [
        ("2026-10-16", "READY"), ("2026-10-29", "LIQUIDITY_MODEL_WINDOW_UNAVAILABLE")]     # the old row is kept
    assert read_liquidity(con, "bybit", "XOMUSDT") is None


def test_the_reader_ignores_a_pair_that_left_trading(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _two_weeks(root)
    con = _registry_with_pair(tmp_path)
    refresh_liquidity(con, "bybit", date(2026, 10, 19), RegistrySettings(liquidity_data_root=root))
    con.execute("update pairs set status = 'DELISTED' where symbol = 'XOMUSDT'")
    assert read_liquidity(con, "bybit", "XOMUSDT") is None


def test_liquidity_runs_are_journaled(tmp_path: Path) -> None:
    from mrs3.pair_registry.store import RegistryError

    root = tmp_path / "data"
    _two_weeks(root)
    con = _registry_with_pair(tmp_path)
    refresh_liquidity(con, "bybit", date(2026, 10, 19), RegistrySettings(liquidity_data_root=root))
    with pytest.raises(RegistryError):
        refresh_liquidity(con, "bybit", date(2026, 10, 19), RegistrySettings())
    rows = con.execute("select status, counts, error from journal where step = 'liquidity' order by started_at, status").fetchall()
    assert sorted(row[0] for row in rows) == ["FAILED", "OK"]
    ok = next(row for row in rows if row[0] == "OK")
    assert json.loads(ok[1])["ready"] == 1
    assert "not configured" in next(row for row in rows if row[0] == "FAILED")[2]


def test_window_rows_carry_their_input_files_and_floor_like_v1(tmp_path: Path) -> None:
    from mrs3.portfolio.position_sizing import _floor_step

    root = tmp_path / "data"
    _two_weeks(root)
    days = [day for day in (MONDAY + timedelta(days=offset) for offset in range(14)) if day.weekday() < 5][-7:]
    facts = [day_facts(root / "XOMUSDT" / f"XOMUSDT{day.isoformat()}_1m.parquet", day) for day in days]
    row = window_row(facts, k=Decimal(9), step=Decimal(3))
    assert Decimal(row["base_lot"]) == _floor_step(Decimal(9) * Decimal(row["l"]), Decimal(3))
    assert row["window_files"] == [[item.day.isoformat(), item.file_sha256] for item in facts]
    assert len(row["inputs_sha256"]) == 64
    con = _registry_with_pair(tmp_path)
    refresh_liquidity(con, "bybit", date(2026, 10, 19), RegistrySettings(liquidity_data_root=root))
    stored = read_liquidity(con, "bybit", "XOMUSDT")
    assert len(stored["inputs_sha256"]) == 64 and len(stored["window_files"]) == 7


def test_the_reader_is_exported_by_the_package() -> None:
    import mrs3.pair_registry as registry

    assert registry.read_liquidity is read_liquidity

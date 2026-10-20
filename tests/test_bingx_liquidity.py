"""BingX liquidity, LOT_MODEL_V2 on verified BingX minute days (Panel stage 5d).

Contract: docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 5d contract». A fake BingX client in temporary
folders: nothing leaves the machine.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path

from mrs3.pair_registry.liquidity import bingx_day_facts, ensure_bingx_window, refresh_liquidity
from mrs3.pair_registry.minute_days import day_path
from mrs3.pair_registry.store import RegistrySettings
from tests.test_minute_days import API, NOW, FakeBingX, _full_day, _world

TODAY = date(2026, 10, 19)                                  # Monday: the window ends on Friday 10-16
DAYS = [date(2026, 10, day) for day in range(14, 19)]       # window 10-14..10-16 (3 weekdays), then the weekend


def _settings(tmp_path: Path) -> RegistrySettings:
    return RegistrySettings(root=tmp_path / "registry", bingx_data_root=tmp_path / "bingx",
                            liquidity_window_weekdays=3)


def _verified(tmp_path: Path, quiet=()):
    con, _base = _world(tmp_path)
    settings = _settings(tmp_path)
    client = FakeBingX({day: _full_day(day) for day in DAYS if day not in quiet}, {})
    counts = ensure_bingx_window(con, settings, TODAY, TODAY, client=client, now=NOW)
    return con, settings, counts


def _refresh(con, settings):
    return refresh_liquidity(con, "bingx", TODAY, settings, now=NOW)


def _liquidity(con) -> dict:
    row = con.execute("select status, missing_days, positive_minutes, base_lot, window_days from liquidity "
                      "where exchange = 'bingx' and symbol = 'AMDUSDT'").fetchone()
    return dict(zip(("status", "missing", "positive", "base_lot", "window"), row))


def test_the_bingx_reader_takes_close_times_volume(tmp_path: Path) -> None:
    con, settings, _counts = _verified(tmp_path)
    facts = bingx_day_facts(day_path(settings.bingx_data_root, "AMDUSDT", API, DAYS[0]), DAYS[0])
    assert facts.positive_minutes == 1440 and facts.active_bins == 96
    assert facts.turnover == Decimal("10.1") * 1440                       # close 10.1 x volume 1 per minute


def test_a_window_of_verified_days_is_ready(tmp_path: Path) -> None:
    con, settings, counts = _verified(tmp_path)
    assert counts == {"pairs": 1, "accepted": 5, "refused": 0, "failed_pairs": 0, "failed": {}}
    result = _refresh(con, settings)
    assert result["ready"] == 1 and result["unavailable"] == 0
    row = _liquidity(con)
    assert row["status"] == "READY" and row["positive"] == 3 * 1440
    assert json.loads(row["window"]) == ["2026-10-14", "2026-10-15", "2026-10-16"]
    assert con.execute("select count(*) from liquidity_days where exchange = 'bingx'").fetchone()[0] == 5


def test_a_verified_quiet_weekday_has_zero_turnover_and_is_not_missing(tmp_path: Path) -> None:
    quiet = date(2026, 10, 15)
    con, settings, _counts = _verified(tmp_path, quiet={quiet})
    _refresh(con, settings)
    row = _liquidity(con)
    assert row["status"] == "READY" and row["positive"] == 2 * 1440 and json.loads(row["missing"]) == []
    assert con.execute("select turnover, file_sha256 from liquidity_days where exchange = 'bingx' and day = ?",
                       [quiet]).fetchone() == ("0", "none")


def test_an_unverified_or_changed_day_makes_the_window_unavailable(tmp_path: Path) -> None:
    con, settings, _counts = _verified(tmp_path)
    changed = date(2026, 10, 16)
    path = day_path(settings.bingx_data_root, "AMDUSDT", API, changed)
    path.write_bytes(path.read_bytes() + b"\x00")                          # changed since the verification
    con.execute("delete from minute_days where day = ?", [date(2026, 10, 14)])   # never verified
    _refresh(con, settings)
    row = _liquidity(con)
    assert row["status"] == "LIQUIDITY_MODEL_WINDOW_UNAVAILABLE"
    assert json.loads(row["missing"]) == ["2026-10-14", "2026-10-16"]


def test_the_cli_runs_bingx_liquidity_after_the_window_days(tmp_path: Path, monkeypatch, capsys) -> None:
    from mrs3.pair_registry import cli
    import mrs3.pair_registry.minute_days as minute_days

    con, _base = _world(tmp_path)
    con.close()
    client = FakeBingX({day: _full_day(day) for day in DAYS}, {})
    monkeypatch.setattr(minute_days, "BingXClient", lambda: client)
    monkeypatch.setattr(minute_days, "last_available_day", lambda now: date(2026, 10, 18))
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"root": str(tmp_path / "registry"),
                                                    "liquidity": {"window_weekdays": 3},
                                                    "minute_data": {"bingx_data_root": str(tmp_path / "bingx")}}}),
                      encoding="utf-8")
    assert cli.main(["liquidity", "--config", str(config), "--exchange", "bingx", "--today", "2026-10-19"]) == 0
    result = json.loads(capsys.readouterr().out)["bingx"]
    assert result["ready"] == 1 and result["minute_days"]["accepted"] == 5


def test_a_run_before_the_bingx_lag_keeps_the_last_rows(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    con, settings, _counts = _verified(tmp_path)
    _refresh(con, settings)
    early = refresh_liquidity(con, "bingx", date(2026, 10, 17), settings,          # Saturday 03:00 UTC: Friday is
                              now=datetime(2026, 10, 17, 3, tzinfo=timezone.utc))  # not on BingX yet
    assert early["not_yet_available"] == "2026-10-16" and _liquidity(con)["status"] == "READY"


def test_a_file_present_where_the_verification_found_none_is_missing(tmp_path: Path) -> None:
    quiet = date(2026, 10, 15)
    con, settings, _counts = _verified(tmp_path, quiet={quiet})
    source = day_path(settings.bingx_data_root, "AMDUSDT", API, DAYS[0])
    day_path(settings.bingx_data_root, "AMDUSDT", API, quiet).write_bytes(source.read_bytes())
    _refresh(con, settings)
    assert json.loads(_liquidity(con)["missing"]) == ["2026-10-15"]


def test_a_broken_bingx_row_is_an_unreadable_day(tmp_path: Path) -> None:
    import pytest

    from mrs3.portfolio.minute_capacity import MinuteCapacityError
    from mrs3.tester_candles import encode_day

    stamp = 1_760_400_000_000                                                # 2026-10-14 00:00 UTC
    path = tmp_path / "bad.parquet"
    path.write_bytes(encode_day({"timestamp": [stamp], "open": [10.0], "high": [9.0], "low": [9.5], "close": [10.0],
                                 "volume": [1.0], "buy_volume": [0.0], "sell_volume": [0.0], "trades": [0]}))
    with pytest.raises(MinuteCapacityError):
        bingx_day_facts(path, DAYS[0])
    path.write_bytes(encode_day({"timestamp": [stamp], "open": [float("nan")], "high": [10.0], "low": [9.0],
                                 "close": [10.0], "volume": [1.0], "buy_volume": [0.0], "sell_volume": [0.0],
                                 "trades": [0]}))
    with pytest.raises(MinuteCapacityError):
        bingx_day_facts(path, DAYS[0])


def test_one_pair_failing_never_stops_the_others(tmp_path: Path, monkeypatch) -> None:
    import mrs3.pair_registry.minute_days as minute_days
    from mrs3.pair_registry.refresh import refresh_exchange
    from mrs3.pair_registry.store import RegistryError
    from tests.test_minute_days import _ms

    con, _base = _world(tmp_path)
    refresh_exchange(con, "bingx", [
        {"symbol": "AMDUSDT", "status": "TRADING", "api_symbol": API, "listing_ms": _ms(NOW) - 200 * 86_400_000},
        {"symbol": "BADUSDT", "status": "TRADING", "api_symbol": "NCSKBAD2USD-USDT", "listing_ms": 0}], [],
        now_ms=_ms(NOW))
    original = minute_days.verify_bingx_days          # the pairs run in parallel threads (sync part 2)

    def flaky(data_root, symbol, *args, **kwargs):
        if symbol == "BADUSDT":
            raise RegistryError("broken")
        return original(data_root, symbol, *args, **kwargs)

    monkeypatch.setattr(minute_days, "verify_bingx_days", flaky)
    counts = ensure_bingx_window(con, _settings(tmp_path), TODAY, TODAY,
                                 client=FakeBingX({day: _full_day(day) for day in DAYS}, {}), now=NOW)
    assert counts["failed"] == {"BADUSDT": "RegistryError"} and counts["accepted"] == 5

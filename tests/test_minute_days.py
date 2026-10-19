"""The minute data layer and the BingX minute verification (Panel stage 5a).

Contract: docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 5a contract»; rules in
docs/specs/2026-10-18-pair-registry.md, «BingX minute verification». A fake BingX client: nothing leaves the machine.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from mrs3.pair_registry.minute_days import ACCEPTED, ensure_minute_days, verify_bingx_days
from mrs3.pair_registry.refresh import refresh_exchange
from mrs3.pair_registry.store import RegistryError, RegistrySettings, connect

DAY = date(2026, 10, 14)
NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)                  # last available day: 2026-10-18
LISTED = datetime(2026, 6, 1, tzinfo=timezone.utc)
API = "NCSKAMD2USD-USDT"


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _minute(day: date, index: int, price: float = 10.0, volume: str = "1") -> dict:
    stamp = _ms(datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(minutes=index))
    return {"time": stamp, "open": str(price), "high": str(price + 0.5), "low": str(price - 0.5),
            "close": str(price + 0.1), "volume": volume}


def _hours(minutes: list[dict], day: date, *, broken_hour: int | None = None) -> list[dict]:
    start = _ms(datetime(day.year, day.month, day.day, tzinfo=timezone.utc))
    hours = []
    for hour in range(24):
        inside = [m for m in minutes if start + hour * 3_600_000 <= m["time"] < start + (hour + 1) * 3_600_000]
        if not inside:
            continue
        volume = sum(float(m["volume"]) for m in inside) + (5.0 if hour == broken_hour else 0.0)
        hours.append({"time": start + hour * 3_600_000, "open": inside[0]["open"], "close": inside[-1]["close"],
                      "high": str(max(float(m["high"]) for m in inside)),
                      "low": str(min(float(m["low"]) for m in inside)), "volume": str(volume)})
    return hours


class FakeBingX:
    """Minutes and hours per day; `calls` lists (interval, day) of every request."""

    def __init__(self, minutes: dict[date, list[dict]], hours: dict[date, list[dict]] | None = None):
        self.minutes, self.hours, self.calls = minutes, hours or {}, []

    def klines(self, symbol: str, start_ms: int, end_ms: int, interval: str = "1m") -> list[dict]:
        assert symbol == API
        day = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
        self.calls.append((interval, day))
        source = self.minutes if interval == "1m" else self.hours
        return list(source.get(day, []))


def _world(tmp_path: Path):
    con = connect(tmp_path / "registry")
    refresh_exchange(con, "bingx", [{"symbol": "AMDUSDT", "status": "TRADING", "api_symbol": API,
                                     "listing_ms": _ms(LISTED)}], [], now_ms=_ms(NOW))
    settings = RegistrySettings(root=tmp_path / "registry", bingx_data_root=tmp_path / "bingx")
    return con, settings


def _full_day(day: date = DAY) -> list[dict]:
    return [_minute(day, index) for index in range(1440)]


def _record(con, day: date = DAY) -> dict:
    row = con.execute("select * from minute_days where exchange = 'bingx' and symbol = 'AMDUSDT' and day = ?",
                      [day]).fetchone()
    names = [column[0] for column in con.description]
    return dict(zip(names, row))


def test_a_complete_day_is_ok_and_needs_no_hourly_request(tmp_path: Path) -> None:
    con, settings = _world(tmp_path)
    client = FakeBingX({DAY: _full_day()})
    result = ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=client, now=NOW)
    assert result.accepted == [DAY] and result.refused == {}
    assert client.calls == [("1m", DAY)]
    record = _record(con)
    assert (record["status"], record["candles"], record["expected_minutes"], record["missing_minutes"]) == (
        "OK", 1440, 1440, 0)
    assert (tmp_path / "bingx" / API / "AMDUSDT2026-10-14_1m.parquet").is_file()


def test_gaps_whose_hours_equal_the_minutes_are_verified(tmp_path: Path) -> None:
    con, settings = _world(tmp_path)
    minutes = [m for index, m in enumerate(_full_day()) if not 1372 <= index <= 1382]   # 22:52-23:02 absent
    client = FakeBingX({DAY: minutes}, {DAY: _hours(minutes, DAY)})
    result = ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=client, now=NOW)
    assert result.accepted == [DAY]
    record = _record(con)
    assert (record["status"], record["missing_minutes"]) == ("VERIFIED_GAPS", 11)
    assert json.loads(record["gap_ranges"]) == ["22:52-23:02"]
    assert client.calls == [("1m", DAY), ("1h", DAY)]


def test_a_day_without_candles_and_empty_hours_is_verified_without_a_file(tmp_path: Path) -> None:
    con, settings = _world(tmp_path)
    client = FakeBingX({}, {})
    result = ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=client, now=NOW)
    assert result.accepted == [DAY]
    record = _record(con)
    assert (record["status"], record["candles"], record["file_sha256"]) == ("VERIFIED_GAPS", 0, None)
    assert not (tmp_path / "bingx" / API).exists() or not list((tmp_path / "bingx" / API).iterdir())
    client.calls.clear()                                                   # reused while the file stays absent
    assert ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=client, now=NOW).accepted == [DAY]
    assert client.calls == []


def test_a_differing_hour_is_refetched_then_suspect_and_refused(tmp_path: Path) -> None:
    con, settings = _world(tmp_path)
    minutes = [m for index, m in enumerate(_full_day()) if index != 600]
    client = FakeBingX({DAY: minutes}, {DAY: _hours(minutes, DAY, broken_hour=10)})
    result = ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=client, now=NOW)
    assert result.accepted == [] and "SUSPECT" in result.refused[DAY]
    assert [call[0] for call in client.calls].count("1m") == 4                # the first download + 3 refetches
    record = _record(con)
    assert record["status"] == "SUSPECT" and json.loads(record["differing_hours"]) == [10]
    client.calls.clear()                                                   # a SUSPECT day is checked again
    ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=client, now=NOW)
    assert client.calls


def test_a_verified_day_is_reused_until_its_file_changes_or_overwrite(tmp_path: Path) -> None:
    con, settings = _world(tmp_path)
    client = FakeBingX({DAY: _full_day()})
    ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=client, now=NOW)
    client.calls.clear()
    assert ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=client, now=NOW).accepted == [DAY]
    assert client.calls == []                                               # same hash: no request
    path = tmp_path / "bingx" / API / "AMDUSDT2026-10-14_1m.parquet"
    path.write_bytes(b"broken")                                             # a changed (damaged) file
    assert ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=client, now=NOW).accepted == [DAY]
    assert client.calls == [("1m", DAY)]
    client.calls.clear()
    overwrite = RegistrySettings(root=settings.root, bingx_data_root=settings.bingx_data_root,
                                 bingx_download_mode="overwrite")
    ensure_minute_days(con, overwrite, "bingx", "AMDUSDT", DAY, DAY, client=client, now=NOW)
    assert client.calls == [("1m", DAY)]


def test_a_day_already_on_disk_is_verified_once_without_download(tmp_path: Path) -> None:
    con, settings = _world(tmp_path)
    ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=FakeBingX({DAY: _full_day()}), now=NOW)
    con.execute("delete from minute_days")                                 # as if downloaded before the check existed
    client = FakeBingX({})
    assert ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=client, now=NOW).accepted == [DAY]
    assert client.calls == [] and _record(con)["status"] == "OK"


def test_days_outside_the_plan_are_skipped(tmp_path: Path) -> None:
    con, settings = _world(tmp_path)
    client = FakeBingX({})
    result = ensure_minute_days(con, settings, "bingx", "AMDUSDT", date(2026, 5, 30), date(2026, 5, 31),
                                client=client, now=NOW)                    # before the listing day
    assert (result.accepted, result.refused, client.calls) == ([], {}, [])
    result = ensure_minute_days(con, settings, "bingx", "AMDUSDT", date(2026, 10, 19), date(2026, 10, 20),
                                client=client, now=NOW)                    # not available yet
    assert (result.accepted, result.refused, client.calls) == ([], {}, [])


def test_a_failed_request_is_refused_and_not_recorded(tmp_path: Path) -> None:
    from mrs3.bingx_downloader import BingXDownloadError

    class Down(FakeBingX):
        def klines(self, *args, **kwargs):
            raise BingXDownloadError("request to /klines failed after 4 attempts")

    con, settings = _world(tmp_path)
    result = ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=Down({}), now=NOW)
    assert result.accepted == [] and "failed" in result.refused[DAY]
    assert con.execute("select count(*) from minute_days").fetchone()[0] == 0


def test_verify_without_the_registry_returns_the_records(tmp_path: Path) -> None:
    client = FakeBingX({DAY: _full_day()})
    result = verify_bingx_days(tmp_path / "bingx", "AMDUSDT", API, _ms(LISTED), DAY, DAY, known={},
                               client=client, now=NOW)
    assert result.accepted == [DAY] and result.records[0]["status"] in ACCEPTED


def test_bybit_delegates_to_the_archive_layer(tmp_path: Path, monkeypatch) -> None:
    import mrs3.pair_registry.minute_days as module

    seen = {}

    def fake_ensure_days(data_root, symbol, first, last, **kwargs):
        seen.update(root=data_root, symbol=symbol, first=first, last=last)
        return {"existing": [first], "created": [last], "failed": [date(2026, 10, 13)]}

    monkeypatch.setattr(module, "ensure_days", fake_ensure_days)
    con, _settings = _world(tmp_path)
    settings = RegistrySettings(root=tmp_path / "registry", liquidity_data_root=tmp_path / "bybit")
    result = ensure_minute_days(con, settings, "bybit", "GOODUSDT", date(2026, 10, 12), DAY, now=NOW)
    assert result.accepted == [date(2026, 10, 12), DAY] and list(result.refused) == [date(2026, 10, 13)]
    assert seen["root"] == tmp_path / "bybit" and seen["symbol"] == "GOODUSDT"


def test_an_unknown_bingx_pair_is_refused(tmp_path: Path) -> None:
    con, settings = _world(tmp_path)
    with pytest.raises(RegistryError):
        ensure_minute_days(con, settings, "bingx", "NOPEUSDT", DAY, DAY, client=FakeBingX({}), now=NOW)


class Sequenced(FakeBingX):
    """Each 1m request for a day returns the next answer of its queue (the last one repeats)."""

    def __init__(self, answers: dict[date, list[list[dict]]], hours: dict[date, list[dict]] | None = None):
        super().__init__({}, hours)
        self.answers = answers

    def klines(self, symbol: str, start_ms: int, end_ms: int, interval: str = "1m") -> list[dict]:
        day = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
        if interval == "1m":
            queue = self.answers.get(day, [[]])
            self.calls.append((interval, day))
            return list(queue.pop(0) if len(queue) > 1 else queue[0])
        return super().klines(symbol, start_ms, end_ms, interval)


def _file(tmp_path: Path, day: date = DAY) -> Path:
    return tmp_path / "bingx" / API / f"AMDUSDT{day.isoformat()}_1m.parquet"


def test_an_empty_answer_never_removes_a_valid_file_in_overwrite_mode(tmp_path: Path) -> None:
    con, settings = _world(tmp_path)
    ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=FakeBingX({DAY: _full_day()}), now=NOW)
    before = _file(tmp_path).read_bytes()
    overwrite = RegistrySettings(root=settings.root, bingx_data_root=settings.bingx_data_root,
                                 bingx_download_mode="overwrite")
    result = ensure_minute_days(con, overwrite, "bingx", "AMDUSDT", DAY, DAY, client=FakeBingX({}, {}), now=NOW)
    assert result.accepted == [] and "file is kept" in result.refused[DAY]
    assert _file(tmp_path).read_bytes() == before and _record(con)["status"] == "OK"


def test_a_worse_refetch_never_replaces_the_file_on_disk(tmp_path: Path) -> None:
    con, settings = _world(tmp_path)
    gapped = [m for index, m in enumerate(_full_day()) if index != 600]
    ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY,
                       client=FakeBingX({DAY: gapped}, {DAY: _hours(gapped, DAY)}), now=NOW)
    before = _file(tmp_path).read_bytes()
    con.execute("delete from minute_days")                                 # verify the file on disk again
    client = Sequenced({DAY: [[]]}, {DAY: _hours(gapped, DAY, broken_hour=10)})
    result = ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=client, now=NOW)
    assert "SUSPECT" in result.refused[DAY]
    assert _file(tmp_path).read_bytes() == before                         # empty refetches changed nothing


def test_a_refetch_that_heals_the_day_is_written(tmp_path: Path) -> None:
    con, settings = _world(tmp_path)
    gapped = [m for index, m in enumerate(_full_day()) if index != 600]
    client = Sequenced({DAY: [gapped, _full_day()]}, {DAY: _hours(_full_day(), DAY)})
    result = ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=client, now=NOW)
    assert result.accepted == [DAY] and _record(con)["status"] == "OK" and _record(con)["candles"] == 1440
    assert _record(con)["file_sha256"] is not None and _file(tmp_path).is_file()


def test_the_listing_day_counts_from_the_listing_minute(tmp_path: Path) -> None:
    con = connect(tmp_path / "registry")
    listed = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)
    refresh_exchange(con, "bingx", [{"symbol": "AMDUSDT", "status": "TRADING", "api_symbol": API,
                                     "listing_ms": _ms(listed)}], [], now_ms=_ms(NOW))
    settings = RegistrySettings(root=tmp_path / "registry", bingx_data_root=tmp_path / "bingx")
    minutes = [_minute(DAY, index) for index in range(13 * 60 + 30, 1440)]
    client = FakeBingX({DAY: minutes})
    assert ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, DAY, client=client, now=NOW).accepted == [DAY]
    record = _record(con)
    assert (record["status"], record["expected_minutes"], record["candles"]) == ("OK", 630, 630)


def test_an_hour_without_minutes_must_have_no_traded_candle() -> None:
    from mrs3.pair_registry.minute_days import differing_hours

    rows = [[str(m["time"]), m["open"], m["high"], m["low"], m["close"], m["volume"], "0", "0", "0"]
            for m in _full_day() if m["time"] < _ms(datetime(2026, 10, 14, 23, tzinfo=timezone.utc))]
    hours = _hours([m for m in _full_day()], DAY)                          # hour 23 traded on BingX
    assert differing_hours(rows, hours, DAY, _ms(LISTED)) == [23]
    quiet = [*hours[:23], {**hours[23], "volume": "0"}]
    assert differing_hours(rows, quiet, DAY, _ms(LISTED)) == []


def test_the_tolerance_is_relative() -> None:
    from decimal import Decimal

    from mrs3.pair_registry.minute_days import _near

    assert _near(Decimal("0"), Decimal("0"))
    assert not _near(Decimal("0.0030"), Decimal("0.0030000009"))           # 3e-7 relative
    assert _near(Decimal("1000"), Decimal("1000.0000001"))                 # 1e-10 relative


def test_a_malformed_answer_refuses_its_day_and_the_others_go_on(tmp_path: Path) -> None:
    con, settings = _world(tmp_path)
    other = DAY + timedelta(days=1)
    client = FakeBingX({DAY: [{"time": None, "open": "1"}], other: _full_day(other)})
    result = ensure_minute_days(con, settings, "bingx", "AMDUSDT", DAY, other, client=client, now=NOW)
    assert result.accepted == [other] and "unexpected klines answer" in result.refused[DAY]
    assert _record(con, other)["status"] == "OK"


def test_the_minute_data_settings(tmp_path: Path) -> None:
    from mrs3.pair_registry.store import load_registry_settings

    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"tester_runner": {"bot_root": str(tmp_path / "bot")},
                                  "duckdb_import": {"workers": 3}, "pair_registry": {"root": "registry"}}),
                      encoding="utf-8")
    settings = load_registry_settings(config)
    assert settings.bingx_data_root == (tmp_path / "bot" / "tester" / "data" / "bingx").resolve()
    assert settings.liquidity_data_root == (tmp_path / "bot" / "tester" / "data" / "bybit").resolve()
    assert (settings.bingx_download_mode, settings.workers) == ("incremental", 3)
    config.write_text(json.dumps({"tester_runner": {"bot_root": str(tmp_path / "bot")}, "pair_registry": {
        "root": "registry", "minute_data": {"bingx_data_root": "bx", "bingx_download_mode": "overwrite"}}}),
        encoding="utf-8")
    settings = load_registry_settings(config)
    assert settings.bingx_data_root == (tmp_path / "bx").resolve() and settings.bingx_download_mode == "overwrite"
    config.write_text(json.dumps({"pair_registry": {"root": "registry", "minute_data": {"bingx_data_root": ""}}}),
                      encoding="utf-8")
    assert load_registry_settings(config).bingx_data_root is None          # empty = not set, like data_root
    config.write_text(json.dumps({"pair_registry": {"root": "registry",
                                                    "minute_data": {"bingx_download_mode": "always"}}}),
                      encoding="utf-8")
    with pytest.raises(RegistryError):
        load_registry_settings(config)


def test_the_hourly_volume_may_exceed_its_minutes_only_by_the_measured_noise() -> None:
    # ADR-0082: prices always match; BingX hour volumes drift from their minutes (measured at most +0.048 %)
    from mrs3.pair_registry.minute_days import differing_hours

    minutes = [m for index, m in enumerate(_full_day()) if index != 600]
    rows = [[str(m["time"]), m["open"], m["high"], m["low"], m["close"], m["volume"], "0", "0", "0"] for m in minutes]
    hours = _hours(minutes, DAY)
    sixty = float(hours[10]["volume"])

    def with_hour_10(**fields):
        return [*hours[:10], {**hours[10], **{k: str(v) for k, v in fields.items()}}, *hours[11:]]

    assert differing_hours(rows, with_hour_10(volume=sixty * 1.0005), DAY, _ms(LISTED)) == []   # +0.05 %: noise
    assert differing_hours(rows, with_hour_10(volume=sixty * 0.9), DAY, _ms(LISTED)) == []      # minutes hold more
    assert differing_hours(rows, with_hour_10(volume=sixty * 1.01), DAY, _ms(LISTED)) == [10]   # +1 %: lost trades
    assert differing_hours(rows, with_hour_10(high=99), DAY, _ms(LISTED)) == [10]               # a price never drifts

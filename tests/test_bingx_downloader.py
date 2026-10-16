from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import urllib.error

import pytest

import pyarrow.parquet as pq

from mrs3.bingx_downloader import (
    BingXClient,
    BingXContract,
    BingXDownloadError,
    bingx_data_root,
    day_file_path,
    download_pairs,
    last_available_day,
    load_account_keys,
    load_bingx_settings,
    normalize_ticker,
    planned_days,
    resolve_contracts,
    validate_day_file,
    write_day_file,
)
from mrs3.tester_candles import HEADER, encode_day

PANW_LAUNCH_MS = int(datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

CONTRACTS = [
    {"symbol": "BTC-USDT", "displayName": "BTC-USDT", "launchTime": 1},
    {"symbol": "NCSKPANW2USD-USDT", "displayName": "PANW-USDT", "launchTime": PANW_LAUNCH_MS},
    {
        "symbol": "NCSKBABA2USD-USDT",
        "displayName": "BABA-USDT",
        "launchTime": int(datetime(2026, 8, 4, 9, 30, tzinfo=timezone.utc).timestamp() * 1000),
    },
]


def _day_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


def _candles(start_ms: int, end_ms: int, *, skip: int | None = None) -> list[dict[str, object]]:
    rows = []
    for index, stamp in enumerate(range(start_ms, end_ms + 1, 60_000)):
        if index == skip:
            continue
        rows.append(
            {"open": "419.51", "close": "419.32", "high": "419.60", "low": "419.30", "volume": "2.98295", "time": stamp}
        )
    rows.reverse()  # BingX returns candles newest first.
    return rows


class FakeClient:
    def __init__(self, *, contracts=CONTRACTS, broken_symbols=(), short_days=(), empty_days=()):
        self._contracts = contracts
        self.broken_symbols = set(broken_symbols)
        self.short_days = set(short_days)
        self.empty_days = set(empty_days)  # days without a single trade (e.g. weekends)
        self.kline_calls: list[tuple[str, int, int]] = []

    def contracts(self):
        return self._contracts

    def klines(self, symbol: str, start_ms: int, end_ms: int):
        self.kline_calls.append((symbol, start_ms, end_ms))
        if symbol in self.broken_symbols:
            raise BingXDownloadError(f"boom {symbol}")
        launch = next(int(item["launchTime"]) for item in self._contracts if item["symbol"] == symbol)
        first = max(start_ms, -(-launch // 60_000) * 60_000)
        day = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
        if day in self.empty_days:
            return []
        return _candles(first, end_ms, skip=5 if day in self.short_days else None)


def _contract(ticker: str = "PANW") -> BingXContract:
    return resolve_contracts([ticker], CONTRACTS)[0][ticker]


def _rows(path: Path) -> list[dict[str, object]]:
    return pq.read_table(path).to_pylist()


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    """Write rows as a tester-format day file, bypassing the downloader's row checks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_day({name: [row[name] for row in rows] for name in HEADER}))


def test_normalize_ticker_accepts_plain_and_usdt_forms():
    assert normalize_ticker("panw") == "PANW"
    assert normalize_ticker(" PANWUSDT ") == "PANW"
    with pytest.raises(BingXDownloadError):
        normalize_ticker("PAN-W")
    with pytest.raises(BingXDownloadError):
        normalize_ticker("USDT")


def test_resolve_contracts_maps_display_name_to_api_symbol():
    resolved, errors = resolve_contracts(["PANW", "PANWUSDT", "NOPE"], CONTRACTS)
    panw = resolved["PANW"]
    assert panw.api_symbol == "NCSKPANW2USD-USDT"
    assert panw.file_ticker == "PANWUSDT"
    assert panw.launch_ms == PANW_LAUNCH_MS
    assert set(resolved) == {"PANW"}
    assert list(errors) == ["NOPE"]


def test_last_available_day_waits_nine_hours_after_utc_midnight():
    assert last_available_day(datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)) == date(2026, 10, 9)
    assert last_available_day(datetime(2026, 10, 10, 8, 59, tzinfo=timezone.utc)) == date(2026, 10, 8)


def test_planned_days_skip_pre_listing_days_and_unavailable_days():
    days = planned_days(date(2026, 6, 9), date(2026, 10, 12), PANW_LAUNCH_MS, date(2026, 10, 9))
    assert days[0] == date(2026, 6, 11)
    assert days[-1] == date(2026, 10, 9)
    assert planned_days(date(2026, 10, 11), date(2026, 10, 12), PANW_LAUNCH_MS, date(2026, 10, 9)) == []


def test_full_day_file_matches_tester_converter_format(tmp_path: Path):
    client = FakeClient()
    report = download_pairs(
        bot_root=tmp_path, pairs=["PANW"], start=date(2026, 10, 8), end=date(2026, 10, 8),
        client=client, now=NOW,
    )
    path = day_file_path(bingx_data_root(tmp_path), _contract(), date(2026, 10, 8))
    assert path == tmp_path / "tester" / "data" / "bingx" / "NCSKPANW2USD-USDT" / "PANWUSDT2026-10-08_1m.parquet"
    parquet = pq.ParquetFile(path)
    assert parquet.schema_arrow.names == list(HEADER)
    assert not any(field.nullable for field in parquet.schema_arrow)
    assert parquet.metadata.metadata == {b"hb.schema": b"candles_1m/1"}
    group = parquet.metadata.row_group(0)
    assert {group.column(index).compression for index in range(group.num_columns)} == {"ZSTD"}
    rows = _rows(path)
    assert len(rows) == 1440
    # As the tester's converter turns the former CSV row "...,2.98295,0,0,0" into parquet.
    assert rows[0] == {
        "timestamp": _day_ms(date(2026, 10, 8)), "open": 419.51, "high": 419.60, "low": 419.30, "close": 419.32,
        "volume": 2.98295, "buy_volume": 0.0, "sell_volume": 0.0, "trades": 0,
    }
    assert rows[-1]["timestamp"] == _day_ms(date(2026, 10, 8)) + 1439 * 60_000
    assert client.kline_calls == [("NCSKPANW2USD-USDT", _day_ms(date(2026, 10, 8)), _day_ms(date(2026, 10, 8)) + 86_399_999)]
    assert report.downloaded == 1 and report.errors == []
    assert sorted(item.name for item in path.parent.iterdir()) == ["PANWUSDT2026-10-08_1m.parquet"]  # no .tmp, no CSV


def test_listing_day_is_complete_from_listing_minute(tmp_path: Path):
    download_pairs(
        bot_root=tmp_path, pairs=["PANW"], start=date(2026, 6, 10), end=date(2026, 6, 11),
        client=FakeClient(), now=NOW,
    )
    folder = bingx_data_root(tmp_path) / "NCSKPANW2USD-USDT"
    assert sorted(item.name for item in folder.iterdir()) == ["PANWUSDT2026-06-11_1m.parquet"]
    path = folder / "PANWUSDT2026-06-11_1m.parquet"
    assert len(_rows(path)) == 840
    assert validate_day_file(path, date(2026, 6, 11), PANW_LAUNCH_MS) is None


def test_minutes_without_trades_are_kept_as_gaps_like_bybit(tmp_path: Path):
    # BingX omits a candle for a minute without trades; the day file keeps the gap (no invented rows).
    report = download_pairs(
        bot_root=tmp_path, pairs=["PANW"], start=date(2026, 10, 8), end=date(2026, 10, 8),
        client=FakeClient(short_days={date(2026, 10, 8)}), now=NOW,
    )
    assert report.downloaded == 1 and report.errors == []
    path = day_file_path(bingx_data_root(tmp_path), _contract(), date(2026, 10, 8))
    stamps = [row["timestamp"] for row in _rows(path)]
    assert len(stamps) == 1439
    assert _day_ms(date(2026, 10, 8)) + 5 * 60_000 not in stamps
    assert validate_day_file(path, date(2026, 10, 8), PANW_LAUNCH_MS) is None


def test_zero_volume_candles_are_kept_like_the_tester_converter(tmp_path: Path):
    class ZeroVolume(FakeClient):
        def klines(self, symbol, start_ms, end_ms):
            candles = super().klines(symbol, start_ms, end_ms)
            candles[0] = {**candles[0], "volume": "0"}
            return candles

    report = download_pairs(
        bot_root=tmp_path, pairs=["PANW"], start=date(2026, 10, 8), end=date(2026, 10, 8),
        client=ZeroVolume(), now=NOW,
    )
    assert report.downloaded == 1 and report.errors == []
    rows = _rows(day_file_path(bingx_data_root(tmp_path), _contract(), date(2026, 10, 8)))
    assert len(rows) == 1440 and rows[-1]["volume"] == 0.0


def test_duplicate_candles_leave_no_file(tmp_path: Path):
    class Broken(FakeClient):
        def klines(self, symbol, start_ms, end_ms):
            candles = super().klines(symbol, start_ms, end_ms)
            return candles + [dict(candles[0])]  # duplicate minute

    report = download_pairs(
        bot_root=tmp_path, pairs=["PANW"], start=date(2026, 10, 8), end=date(2026, 10, 8),
        client=Broken(), now=NOW,
    )
    folder = bingx_data_root(tmp_path) / "NCSKPANW2USD-USDT"
    assert not folder.exists() or list(folder.iterdir()) == []
    assert report.downloaded == 0
    assert [(error.pair, error.day) for error in report.errors] == [("PANW", date(2026, 10, 8))]


def test_day_without_trades_creates_no_file_and_is_rechecked_next_run(tmp_path: Path):
    weekend = {date(2026, 10, 3), date(2026, 10, 4)}
    days = dict(start=date(2026, 10, 2), end=date(2026, 10, 5))
    report = download_pairs(bot_root=tmp_path, pairs=["PANW"], client=FakeClient(empty_days=weekend), now=NOW, **days)
    assert (report.downloaded, report.no_trades, report.errors) == (2, 2, [])
    assert any("без сделок" in line and "2" in line for line in report.summary_lines())
    for day in weekend:
        assert not day_file_path(bingx_data_root(tmp_path), _contract(), day).exists()

    client = FakeClient()  # trades appeared on a re-check: the day is downloaded now
    again = download_pairs(bot_root=tmp_path, pairs=["PANW"], client=client, now=NOW, **days)
    assert (again.downloaded, again.skipped_valid, again.no_trades) == (2, 2, 0)
    assert sorted(call[1] for call in client.kline_calls) == sorted(_day_ms(day) for day in weekend)


def test_empty_or_csv_or_foreign_format_file_is_never_valid(tmp_path: Path):
    path = day_file_path(bingx_data_root(tmp_path), _contract(), date(2026, 10, 8))
    _write_rows(path, [])  # zero rows
    assert validate_day_file(path, date(2026, 10, 8), PANW_LAUNCH_MS) is not None
    path.write_text(",".join(HEADER) + "\n", encoding="utf-8-sig")  # a CSV under the parquet name
    assert validate_day_file(path, date(2026, 10, 8), PANW_LAUNCH_MS) is not None
    assert validate_day_file(path.with_name("absent_1m.parquet"), date(2026, 10, 8), PANW_LAUNCH_MS) is not None


def test_validate_rejects_misaligned_or_out_of_day_timestamps(tmp_path: Path):
    download_pairs(bot_root=tmp_path, pairs=["PANW"], start=date(2026, 10, 8), end=date(2026, 10, 8), client=FakeClient(), now=NOW)
    path = day_file_path(bingx_data_root(tmp_path), _contract(), date(2026, 10, 8))
    rows = _rows(path)
    _write_rows(path, [{**rows[0], "timestamp": rows[0]["timestamp"] + 1}] + rows[1:])  # not on a minute
    assert validate_day_file(path, date(2026, 10, 8), PANW_LAUNCH_MS) is not None
    _write_rows(path, [{**rows[0], "timestamp": rows[0]["timestamp"] - 60_000}] + rows[1:])  # previous day
    assert validate_day_file(path, date(2026, 10, 8), PANW_LAUNCH_MS) is not None
    _write_rows(path, rows[:2] + rows[1:])  # repeated minute
    assert validate_day_file(path, date(2026, 10, 8), PANW_LAUNCH_MS) is not None
    _write_rows(path, rows)
    assert validate_day_file(path, date(2026, 10, 8), PANW_LAUNCH_MS) is None


def test_incremental_skips_valid_and_redownloads_damaged_days(tmp_path: Path):
    days = dict(start=date(2026, 10, 6), end=date(2026, 10, 8))
    download_pairs(bot_root=tmp_path, pairs=["PANW"], client=FakeClient(), now=NOW, **days)
    damaged = day_file_path(bingx_data_root(tmp_path), _contract(), date(2026, 10, 7))
    damaged.write_bytes(damaged.read_bytes()[:-100])  # truncated
    assert validate_day_file(damaged, date(2026, 10, 7), PANW_LAUNCH_MS) is not None

    client = FakeClient()
    report = download_pairs(bot_root=tmp_path, pairs=["PANW"], client=client, now=NOW, start=date(2026, 10, 6), end=date(2026, 10, 9))
    assert (report.downloaded, report.skipped_valid, report.redownloaded) == (1, 2, 1)
    assert [call[1] for call in client.kline_calls] == [_day_ms(date(2026, 10, 7)), _day_ms(date(2026, 10, 9))]
    assert validate_day_file(damaged, date(2026, 10, 7), PANW_LAUNCH_MS) is None


def test_overwrite_mode_redownloads_valid_days(tmp_path: Path):
    days = dict(start=date(2026, 10, 7), end=date(2026, 10, 8))
    download_pairs(bot_root=tmp_path, pairs=["PANW"], client=FakeClient(), now=NOW, **days)
    client = FakeClient()
    report = download_pairs(bot_root=tmp_path, pairs=["PANW"], client=client, now=NOW, mode="overwrite", **days)
    assert len(client.kline_calls) == 2
    # Valid files are re-downloaded but are not reported as damaged.
    assert (report.downloaded, report.skipped_valid, report.redownloaded) == (2, 0, 0)


def test_one_pair_error_does_not_stop_other_pairs(tmp_path: Path):
    client = FakeClient(broken_symbols={"NCSKPANW2USD-USDT"})
    report = download_pairs(
        bot_root=tmp_path, pairs=["PANW", "NOPE", "BABA"], start=date(2026, 10, 8), end=date(2026, 10, 8),
        client=client, now=NOW,
    )
    assert report.downloaded == 1
    assert {error.pair for error in report.errors} == {"PANW", "NOPE"}
    assert day_file_path(bingx_data_root(tmp_path), _contract("BABA"), date(2026, 10, 8)).is_file()


def test_validate_rejects_bad_ohlc_and_nonzero_extra_columns(tmp_path: Path):
    download_pairs(bot_root=tmp_path, pairs=["PANW"], start=date(2026, 10, 8), end=date(2026, 10, 8), client=FakeClient(), now=NOW)
    path = day_file_path(bingx_data_root(tmp_path), _contract(), date(2026, 10, 8))
    rows = _rows(path)
    for change in ({"trades": 1}, {"buy_volume": 2.98295}, {"high": 419.0}, {"volume": -1.0}, {"open": float("nan")}):
        _write_rows(path, [{**rows[0], **change}] + rows[1:])
        assert validate_day_file(path, date(2026, 10, 8), PANW_LAUNCH_MS) is not None, change


def test_legacy_csv_day_is_not_used_and_the_parquet_day_is_downloaded(tmp_path: Path):
    parquet = day_file_path(bingx_data_root(tmp_path), _contract(), date(2026, 10, 8))
    legacy = parquet.with_suffix(".csv")
    legacy.parent.mkdir(parents=True)
    legacy.write_text(",".join(HEADER) + "\n", encoding="utf-8-sig")
    report = download_pairs(bot_root=tmp_path, pairs=["PANW"], start=date(2026, 10, 8), end=date(2026, 10, 8),
                            client=FakeClient(), now=NOW)
    assert (report.downloaded, report.skipped_valid) == (1, 0)
    assert validate_day_file(parquet, date(2026, 10, 8), PANW_LAUNCH_MS) is None
    assert legacy.read_text(encoding="utf-8-sig") == ",".join(HEADER) + "\n"  # left alone


def test_new_day_never_overwrites_a_file_that_appeared_meanwhile(tmp_path: Path):
    day = date(2026, 10, 8)
    path = day_file_path(bingx_data_root(tmp_path), _contract(), day)
    rows = [[str(_day_ms(day) + index * 60_000), "1.5", "2", "1", "1.5", "3", "0", "0", "0"] for index in range(3)]
    _write_rows(path, [{"timestamp": _day_ms(day), "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                        "volume": 1.0, "buy_volume": 0.0, "sell_volume": 0.0, "trades": 0}])
    before = path.read_bytes()
    write_day_file(path, rows, day, PANW_LAUNCH_MS)  # a valid day is already there: kept as is
    assert path.read_bytes() == before
    _write_rows(path, [])
    with pytest.raises(BingXDownloadError):
        write_day_file(path, rows, day, PANW_LAUNCH_MS)  # an invalid day that appeared is an error, not overwritten
    write_day_file(path, rows, day, PANW_LAUNCH_MS, replace=True)
    assert [row["timestamp"] for row in _rows(path)] == [int(row[0]) for row in rows]
    assert sorted(item.name for item in path.parent.iterdir()) == [path.name]


def test_client_checks_code_and_retries_network_errors():
    calls = []

    def fetch(url: str, headers: dict[str, str]):
        calls.append(url)
        if len(calls) == 1:
            raise urllib.error.URLError("offline")
        if "contracts" in url:
            return {"code": 0, "msg": "", "data": CONTRACTS}
        return {"code": 109400, "msg": "Invalid parameters", "data": {}}

    client = BingXClient(fetch_json=fetch, sleep=lambda _: None, request_delay_seconds=0)
    assert client.contracts() == CONTRACTS
    assert len(calls) == 2
    with pytest.raises(BingXDownloadError, match="109400"):
        client.klines("NCSKPANW2USD-USDT", 0, 86_399_999)
    assert "limit=1440" in calls[-1] and "interval=1m" in calls[-1]


def test_client_gives_up_after_bounded_retries():
    def fetch(url: str, headers: dict[str, str]):
        raise urllib.error.URLError("offline")

    client = BingXClient(fetch_json=fetch, sleep=lambda _: None, retries=2, request_delay_seconds=0)
    with pytest.raises(BingXDownloadError):
        client.contracts()


def test_load_settings_and_account_keys(tmp_path: Path):
    bot_root = tmp_path / "bot"
    (bot_root / "settings_accounts").mkdir(parents=True)
    (bot_root / "settings_accounts" / "bingx.json").write_text(
        "﻿" + json.dumps({"name": "bingx", "api_key": "k1", "api_secret": "s1"}), encoding="utf-8"
    )
    config = tmp_path / "config.local.json"
    config.write_text(
        json.dumps(
            {
                "tester_runner": {"bot_root": "bot"},
                "bingx": {"start_date": "2026-06-13", "end_date": "2026-10-11", "pairs": ["PANW"], "download_mode": "incremental"},
            }
        ),
        encoding="utf-8",
    )
    settings = load_bingx_settings(config)
    assert settings.bot_root == bot_root.resolve()
    assert settings.start_date == date(2026, 6, 13) and settings.end_date == date(2026, 10, 11)
    assert settings.pairs == ("PANW",) and settings.download_mode == "incremental"
    assert load_account_keys(bot_root) == ("k1", "s1")
    assert load_account_keys(tmp_path / "missing") is None

    config.write_text(json.dumps({"tester_runner": {"bot_root": "bot"}, "bingx": {"download_mode": "fast"}}), encoding="utf-8")
    with pytest.raises(BingXDownloadError):
        load_bingx_settings(config)
    config.write_text(json.dumps({"tester_runner": {"bot_root": "bot"}}), encoding="utf-8")
    assert load_bingx_settings(config).download_mode == "incremental"


def test_manual_script_reads_config_block_and_reports_exit_code(tmp_path: Path, capsys):
    from scripts.download_bingx_data import main

    config = tmp_path / "config.local.json"
    config.write_text(
        json.dumps(
            {
                "tester_runner": {"bot_root": "bot"},
                "bingx": {"start_date": "2026-10-08", "end_date": "2026-10-08", "pairs": ["PANW", "NOPE"]},
            }
        ),
        encoding="utf-8",
    )
    assert main(["--config", str(config)], client=FakeClient()) == 1
    output = capsys.readouterr().out
    assert "скачано новых дней: 1" in output and "NOPE" in output
    assert (tmp_path / "bot" / "tester" / "data" / "bingx" / "NCSKPANW2USD-USDT" / "PANWUSDT2026-10-08_1m.parquet").is_file()

    config.write_text(json.dumps({"tester_runner": {"bot_root": "bot"}, "bingx": {}}), encoding="utf-8")
    assert main(["--config", str(config)], client=FakeClient()) == 2


def test_client_retries_truncated_responses_and_rate_limit_codes():
    import http.client

    responses = [
        http.client.IncompleteRead(b"{"),
        {"code": 100410, "msg": "rate limit", "data": {}},
        {"code": 0, "msg": "", "data": CONTRACTS},
    ]
    delays = []

    def fetch(url: str, headers: dict[str, str]):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = BingXClient(fetch_json=fetch, sleep=delays.append, request_delay_seconds=0, backoff_seconds=1)
    assert client.contracts() == CONTRACTS
    assert delays == [1, 2]


def test_unexpected_day_error_is_recorded_and_other_pairs_continue(tmp_path: Path):
    class Flaky(FakeClient):
        def klines(self, symbol, start_ms, end_ms):
            if symbol == "NCSKPANW2USD-USDT":
                raise RuntimeError("socket reset")
            return super().klines(symbol, start_ms, end_ms)

    report = download_pairs(
        bot_root=tmp_path, pairs=["PANW", "BABA"], start=date(2026, 10, 8), end=date(2026, 10, 8),
        client=Flaky(), now=NOW,
    )
    assert report.downloaded == 1
    assert [(error.pair, error.message) for error in report.errors] == [("PANW", "socket reset")]


TRADFI_COLLISIONS = [
    {"symbol": "ON-USDT", "displayName": "ON-USDT", "launchTime": 1},
    {"symbol": "NCSKON2USD-USDT", "displayName": "ON-USDT", "launchTime": 2},
    {"symbol": "NCSKAMD2USD-USDT", "displayName": "AMDUS-USDT", "launchTime": 3},
    {"symbol": "NCSKNETFLIX2USD-USDT", "displayName": "NFLX-USDT", "launchTime": 4},
    {"symbol": "NCSKGME2USD-USDT", "displayName": "GME-USDT", "launchTime": 5},
    {"symbol": "NCFXEUR2USD-USDT", "displayName": "EURUSD-USDT", "launchTime": 6},
    {"symbol": "RAMSES-USDT", "displayName": "RAM-USDT", "launchTime": 7},
]


def test_resolve_contracts_uses_only_tradfi_contracts_and_bingx_name_variants():
    resolved, errors = resolve_contracts(
        ["ON", "AMDUSDT", "AMDSTOCKUSDT", "NFLX", "GMESTOCKUSDT", "EURUSD", "RAM"], TRADFI_COLLISIONS
    )
    assert resolved["ON"].api_symbol == "NCSKON2USD-USDT"  # not the ON crypto coin
    assert resolved["AMD"].api_symbol == "NCSKAMD2USD-USDT"  # BingX name AMDUS-USDT
    assert resolved["AMDSTOCK"].api_symbol == "NCSKAMD2USD-USDT"  # Bybit STOCK suffix
    assert resolved["AMDSTOCK"].file_ticker == "AMDUSDT"
    assert resolved["NFLX"].api_symbol == "NCSKNETFLIX2USD-USDT"
    assert resolved["GMESTOCK"].file_ticker == "GMEUSDT"
    assert set(errors) == {"EURUSD", "RAM"}  # forex and crypto are not TradFi stocks


def test_resolve_contracts_reports_ambiguous_and_prefers_full_stock_ticker():
    contracts = [
        {"symbol": "NCSKDUP2USD-USDT", "displayName": "DUP-USDT", "launchTime": 1},
        {"symbol": "NCSIDUP2USD-USDT", "displayName": "DUP-USDT", "launchTime": 1},
        {"symbol": "NCSKXSTOCK2USD-USDT", "displayName": "XSTOCK-USDT", "launchTime": 1},
        {"symbol": "NCSKX2USD-USDT", "displayName": "X-USDT", "launchTime": 1},
    ]
    resolved, errors = resolve_contracts(["DUP", "XSTOCK"], contracts)
    assert "ambiguous" in errors["DUP"]
    assert resolved["XSTOCK"].api_symbol == "NCSKXSTOCK2USD-USDT"


def test_check_local_days_reports_missing_days_without_downloading(tmp_path: Path):
    from mrs3.bingx_downloader import check_local_days

    download_pairs(bot_root=tmp_path, pairs=["PANW"], start=date(2026, 10, 6), end=date(2026, 10, 7), client=FakeClient(), now=NOW)
    client = FakeClient()
    resolved, errors, missing = check_local_days(
        bot_root=tmp_path, pairs=["PANW", "BABA", "NOPE"], start=date(2026, 10, 6), end=date(2026, 10, 12),
        client=client, now=NOW,
    )
    # Only days without a local file are re-checked at BingX (to tell "no trades" from "not downloaded").
    checked = {(symbol, start) for symbol, start, _ in client.kline_calls}
    assert ("NCSKPANW2USD-USDT", _day_ms(date(2026, 10, 6))) not in checked
    assert ("NCSKPANW2USD-USDT", _day_ms(date(2026, 10, 8))) in checked
    assert resolved["PANW"].api_symbol == "NCSKPANW2USD-USDT"
    assert list(errors) == ["NOPE"]
    assert missing["PANW"] == [date(2026, 10, 8), date(2026, 10, 9)]  # 10-10.. are not available yet
    assert missing["BABA"][0] == date(2026, 10, 6)


def test_check_local_days_accepts_confirmed_days_without_trades(tmp_path: Path):
    from mrs3.bingx_downloader import check_local_days

    weekend = {date(2026, 10, 3), date(2026, 10, 4)}
    download_pairs(bot_root=tmp_path, pairs=["PANW"], start=date(2026, 10, 2), end=date(2026, 10, 5),
                   client=FakeClient(empty_days=weekend), now=NOW)
    client = FakeClient(empty_days=weekend)
    _, _, missing = check_local_days(bot_root=tmp_path, pairs=["PANW"], start=date(2026, 10, 2), end=date(2026, 10, 5),
                                     client=client, now=NOW)
    assert missing == {}
    assert sorted(call[1] for call in client.kline_calls) == sorted(_day_ms(day) for day in weekend)  # only absent days

    _, _, missing = check_local_days(bot_root=tmp_path, pairs=["PANW"], start=date(2026, 10, 2), end=date(2026, 10, 5),
                                     client=FakeClient(), now=NOW)  # trades exist now -> data is missing locally
    assert missing == {"PANW": sorted(weekend)}


def test_damaged_file_on_a_no_trade_day_is_removed(tmp_path: Path):
    from mrs3.bingx_downloader import check_local_days

    saturday = date(2026, 10, 3)
    path = day_file_path(bingx_data_root(tmp_path), _contract(), saturday)
    _write_rows(path, [])  # broken: no rows
    report = download_pairs(bot_root=tmp_path, pairs=["PANW"], start=saturday, end=saturday,
                            client=FakeClient(empty_days={saturday}), now=NOW)
    assert report.no_trades == 1 and report.errors == [] and not path.exists()
    _, _, missing = check_local_days(bot_root=tmp_path, pairs=["PANW"], start=saturday, end=saturday,
                                     client=FakeClient(empty_days={saturday}), now=NOW)
    assert missing == {}


def test_client_retries_bingx_network_exception_code():
    responses = [{"code": 109701, "msg": "network issue", "data": {}}, {"code": 0, "msg": "", "data": CONTRACTS}]
    client = BingXClient(fetch_json=lambda url, headers: responses.pop(0), sleep=lambda _: None, request_delay_seconds=0)
    assert client.contracts() == CONTRACTS


def test_temporary_file_is_removed_when_the_write_itself_fails(tmp_path: Path, monkeypatch):
    import mrs3.bingx_downloader as downloader

    day = date(2026, 10, 8)
    path = day_file_path(bingx_data_root(tmp_path), _contract(), day)
    rows = [[str(_day_ms(day)), "1.5", "2", "1", "1.5", "3", "0", "0", "0"]]

    def disk_full(_descriptor):
        raise OSError("disk full")

    monkeypatch.setattr(downloader.os, "fsync", disk_full)
    with pytest.raises(OSError):
        write_day_file(path, rows, day, PANW_LAUNCH_MS)
    assert list(path.parent.iterdir()) == []

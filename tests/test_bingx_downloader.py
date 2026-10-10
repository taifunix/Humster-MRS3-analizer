from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import urllib.error

import pytest

from mrs3.bingx_downloader import (
    CSV_HEADER,
    BingXClient,
    BingXContract,
    BingXDownloadError,
    bingx_data_root,
    day_csv_path,
    download_pairs,
    last_available_day,
    load_account_keys,
    load_bingx_settings,
    normalize_ticker,
    planned_days,
    resolve_contracts,
    validate_day_csv,
)

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
    def __init__(self, *, contracts=CONTRACTS, broken_symbols=(), short_days=()):
        self._contracts = contracts
        self.broken_symbols = set(broken_symbols)
        self.short_days = set(short_days)
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
        return _candles(first, end_ms, skip=5 if day in self.short_days else None)


def _contract(ticker: str = "PANW") -> BingXContract:
    return resolve_contracts([ticker], CONTRACTS)[0][ticker]


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


def test_full_day_csv_matches_bybit_format(tmp_path: Path):
    client = FakeClient()
    report = download_pairs(
        bot_root=tmp_path, pairs=["PANW"], start=date(2026, 10, 8), end=date(2026, 10, 8),
        client=client, now=NOW,
    )
    path = day_csv_path(bingx_data_root(tmp_path), _contract(), date(2026, 10, 8))
    assert path == tmp_path / "tester" / "data" / "bingx" / "NCSKPANW2USD-USDT" / "PANWUSDT2026-10-08_1m.csv"
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw
    lines = raw[3:].decode("utf-8").splitlines()
    assert lines[0] == ",".join(CSV_HEADER)
    assert len(lines) == 1441
    first = lines[1].split(",")
    assert first == [str(_day_ms(date(2026, 10, 8))), "419.51", "419.60", "419.30", "419.32", "2.98295", "0", "0", "0"]
    assert int(lines[-1].split(",")[0]) == _day_ms(date(2026, 10, 8)) + 1439 * 60_000
    assert client.kline_calls == [("NCSKPANW2USD-USDT", _day_ms(date(2026, 10, 8)), _day_ms(date(2026, 10, 8)) + 86_399_999)]
    assert report.downloaded == 1 and report.errors == []
    assert not list(path.parent.glob("*.tmp"))


def test_listing_day_is_complete_from_listing_minute(tmp_path: Path):
    download_pairs(
        bot_root=tmp_path, pairs=["PANW"], start=date(2026, 6, 10), end=date(2026, 6, 11),
        client=FakeClient(), now=NOW,
    )
    folder = bingx_data_root(tmp_path) / "NCSKPANW2USD-USDT"
    assert sorted(item.name for item in folder.iterdir()) == ["PANWUSDT2026-06-11_1m.csv"]
    path = folder / "PANWUSDT2026-06-11_1m.csv"
    assert len(path.read_text(encoding="utf-8-sig").splitlines()) == 1 + 840
    assert validate_day_csv(path, date(2026, 6, 11), PANW_LAUNCH_MS) is None


def test_incomplete_download_leaves_no_csv(tmp_path: Path):
    report = download_pairs(
        bot_root=tmp_path, pairs=["PANW"], start=date(2026, 10, 8), end=date(2026, 10, 8),
        client=FakeClient(short_days={date(2026, 10, 8)}), now=NOW,
    )
    folder = bingx_data_root(tmp_path) / "NCSKPANW2USD-USDT"
    assert not folder.exists() or list(folder.iterdir()) == []
    assert report.downloaded == 0
    assert [(error.pair, error.day) for error in report.errors] == [("PANW", date(2026, 10, 8))]


def test_incremental_skips_valid_and_redownloads_damaged_days(tmp_path: Path):
    days = dict(start=date(2026, 10, 6), end=date(2026, 10, 8))
    download_pairs(bot_root=tmp_path, pairs=["PANW"], client=FakeClient(), now=NOW, **days)
    damaged = day_csv_path(bingx_data_root(tmp_path), _contract(), date(2026, 10, 7))
    damaged.write_text(damaged.read_text(encoding="utf-8-sig").rsplit("\n", 3)[0] + "\n", encoding="utf-8-sig")
    assert validate_day_csv(damaged, date(2026, 10, 7), PANW_LAUNCH_MS) is not None

    client = FakeClient()
    report = download_pairs(bot_root=tmp_path, pairs=["PANW"], client=client, now=NOW, start=date(2026, 10, 6), end=date(2026, 10, 9))
    assert (report.downloaded, report.skipped_valid, report.redownloaded) == (1, 2, 1)
    assert [call[1] for call in client.kline_calls] == [_day_ms(date(2026, 10, 7)), _day_ms(date(2026, 10, 9))]
    assert validate_day_csv(damaged, date(2026, 10, 7), PANW_LAUNCH_MS) is None


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
    assert day_csv_path(bingx_data_root(tmp_path), _contract("BABA"), date(2026, 10, 8)).is_file()


def test_validate_rejects_bad_ohlc_and_nonzero_extra_columns(tmp_path: Path):
    download_pairs(bot_root=tmp_path, pairs=["PANW"], start=date(2026, 10, 8), end=date(2026, 10, 8), client=FakeClient(), now=NOW)
    path = day_csv_path(bingx_data_root(tmp_path), _contract(), date(2026, 10, 8))
    text = path.read_text(encoding="utf-8-sig")
    path.write_text(text.replace(",0,0,0\n", ",0,0,1\n", 1), encoding="utf-8-sig")
    assert validate_day_csv(path, date(2026, 10, 8), PANW_LAUNCH_MS) is not None
    path.write_text(text.replace("419.60,419.30", "419.00,419.30", 1), encoding="utf-8-sig")
    assert validate_day_csv(path, date(2026, 10, 8), PANW_LAUNCH_MS) is not None
    path.write_text(text, encoding="utf-8")  # missing BOM
    assert validate_day_csv(path, date(2026, 10, 8), PANW_LAUNCH_MS) is not None


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
    assert (tmp_path / "bot" / "tester" / "data" / "bingx" / "NCSKPANW2USD-USDT" / "PANWUSDT2026-10-08_1m.csv").is_file()

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

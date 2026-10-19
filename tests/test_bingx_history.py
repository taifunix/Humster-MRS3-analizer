"""BingX history in the trade store and READY (Panel stage 5b).

Contract: docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 5b contract». A fake BingX client and temporary
folders: nothing leaves the machine.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from mrs3.period_csv import ms_of
from mrs3.trade_history import BingXMinutes, exchange_run_settings, run_history
from mrs3.trade_store import TradeStore
from tests.test_trade_store import POINTS, ms, walk

REPO = Path(__file__).resolve().parents[1]
API = "NCSKTEST2USD-USDT"
LISTED = date(2026, 6, 1)


class FakeBingX:
    """1m candles of a random walk; `refuse` days fail, `quiet` days have no candles and empty hours."""

    def __init__(self, refuse=(), quiet=()):
        data = walk(LISTED, 50, gaps=False)
        self.days: dict[date, list[dict]] = {}
        for index in range(len(data.timestamps)):
            stamp = int(data.timestamps[index])
            day = datetime.fromtimestamp(stamp / 1000, timezone.utc).date()
            self.days.setdefault(day, []).append({"time": stamp, "open": str(data.opens[index]),
                                                  "high": str(data.highs[index]), "low": str(data.lows[index]),
                                                  "close": str(data.closes[index]), "volume": "1"})
        self.refuse, self.quiet, self.calls = set(refuse), set(quiet), []

    def klines(self, symbol: str, start_ms: int, end_ms: int, interval: str = "1m") -> list[dict]:
        from mrs3.bingx_downloader import BingXDownloadError

        assert symbol == API
        day = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
        self.calls.append((interval, day))
        if day in self.refuse:
            raise BingXDownloadError("request to /klines failed after 4 attempts")
        if day in self.quiet:
            return []
        return [] if interval == "1h" else list(self.days.get(day, []))


def _run(tmp_path: Path, client: FakeBingX, **extra):
    store = TradeStore(tmp_path / "store")
    records: list[dict] = []
    minutes = BingXMinutes(str(tmp_path / "bingx"), {"TESTUSDT": (API, ms("2026-06-01"))}, client=client)
    summary = run_history(store, data_root=tmp_path / "bingx", pairs=[("TESTUSDT", LISTED)], end_day=date(2026, 7, 20),
                          instruments={"TESTUSDT": (0.01, 0.01)}, points=POINTS, workers=1,
                          settings=exchange_run_settings(REPO, "bingx"), exchange="bingx", bingx=minutes,
                          on_minute_records=records.extend, **extra)
    return store, summary, records


def test_a_bingx_history_run_writes_bingx_segments_from_verified_days(tmp_path: Path) -> None:
    store, summary, records = _run(tmp_path, FakeBingX())
    assert summary["written"] == 2 and summary["failed"] == 0
    segments = store.segments("TESTUSDT", "bingx")
    assert [(row["start_ms"], row["end_ms"]) for row in segments] == [
        (ms("2026-06-05"), ms("2026-06-30")), (ms("2026-06-30"), ms("2026-07-20"))]
    assert store.segments("TESTUSDT", "bybit") == []
    assert {record["status"] for record in records} == {"OK"} and len(records) == 49   # 06-01 .. 07-19
    assert (tmp_path / "bingx" / API / "TESTUSDT2026-06-01_1m.parquet").is_file()
    with store.connect(read_only=True) as con:
        settings = json.loads(con.execute("select settings_json from segments limit 1").fetchone()[0])
    assert (settings["maker_fee"], settings["taker_fee"]) == (0.00012, 0.0003)


def test_a_refused_day_skips_its_segment_and_a_quiet_day_does_not(tmp_path: Path) -> None:
    client = FakeBingX(refuse={date(2026, 7, 10)}, quiet={date(2026, 6, 13)})
    store, summary, records = _run(tmp_path, client)
    assert (summary["written"], summary["skipped"]) == (1, 1)
    assert [row["end_ms"] for row in store.segments("TESTUSDT", "bingx")] == [ms("2026-06-30")]
    journal = (tmp_path / "store" / "journal.jsonl").read_text(encoding="utf-8")
    assert '"MISSING_DAYS"' in journal and "2026-07-10" in journal
    quiet = next(record for record in records if record["day"] == date(2026, 6, 13))
    assert (quiet["status"], quiet["candles"], quiet["file_sha256"]) == ("VERIFIED_GAPS", 0, None)
    assert not any(record["day"] == date(2026, 7, 10) for record in records)        # a failed request: no record


def test_without_download_only_days_the_registry_accepts_count(tmp_path: Path) -> None:
    client = FakeBingX()
    _store, _summary, records = _run(tmp_path, client)
    known = {"TESTUSDT": {record["day"]: record for record in records if record["day"] != date(2026, 7, 1)}}
    minutes = BingXMinutes(str(tmp_path / "bingx"), {"TESTUSDT": (API, ms("2026-06-01"))}, known, client=client)
    client.calls.clear()
    store = TradeStore(tmp_path / "store2")
    summary = run_history(store, data_root=tmp_path / "bingx", pairs=[("TESTUSDT", LISTED)], end_day=date(2026, 7, 20),
                          instruments={"TESTUSDT": (0.01, 0.01)}, points=POINTS, workers=1, download=False,
                          settings=exchange_run_settings(REPO, "bingx"), exchange="bingx", bingx=minutes)
    assert client.calls == [] and (summary["written"], summary["skipped"]) == (1, 1)


def test_a_bingx_run_needs_its_minute_description(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_history(TradeStore(tmp_path / "store"), data_root=tmp_path, pairs=[], end_day=date(2026, 7, 20),
                    instruments={}, points=POINTS, workers=1, exchange="bingx")


def test_the_bingx_run_settings_come_from_the_bingx_template() -> None:
    from mrs3.mrs2_simulator import RunSettings

    bingx = exchange_run_settings(REPO, "bingx")
    assert (bingx.maker_fee, bingx.taker_fee, bingx.slippage) == (0.00012, 0.0003, 0.001)
    assert exchange_run_settings(REPO, "bybit") == RunSettings()


def _registry_config(tmp_path: Path, tick: str | None = "0.01") -> Path:
    from mrs3.pair_registry.refresh import refresh_exchange
    from mrs3.pair_registry.store import connect

    con = connect(tmp_path / "registry")
    refresh_exchange(con, "bingx", [{"symbol": "TESTUSDT", "status": "TRADING", "api_symbol": API,
                                     "listing_ms": ms("2026-06-01"), "tick_size": tick, "qty_step": "0.01"}], [],
                     now_ms=ms("2026-10-19"))
    con.close()
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"root": str(tmp_path / "registry"),
                                                    "minute_data": {"bingx_data_root": str(tmp_path / "bingx")}},
                                  "trade_store": {"root": str(tmp_path / "store")}}), encoding="utf-8")
    return config


def test_the_script_refuses_a_run_where_no_bingx_pair_has_tick_size(tmp_path: Path, capsys) -> None:
    import runpy

    script = runpy.run_path(str(REPO / "scripts" / "simulate_history.py"), run_name="not_main")
    config = _registry_config(tmp_path, tick=None)
    with pytest.raises(SystemExit):
        script["main"](["--config", str(config), "--exchange", "bingx", "--pairs", "TESTUSDT", "--no-download"])
    captured = capsys.readouterr()
    assert "tick size" in captured.err and "TESTUSDT" in captured.out


def test_the_script_runs_bingx_history_from_the_registry(tmp_path: Path, monkeypatch, capsys) -> None:
    import runpy

    import mrs3.pair_registry.minute_days as minute_days

    client = FakeBingX()
    monkeypatch.setattr(minute_days, "BingXClient", lambda: client)
    script = runpy.run_path(str(REPO / "scripts" / "simulate_history.py"), run_name="not_main")
    config = _registry_config(tmp_path)
    code = script["main"](["--config", str(config), "--exchange", "bingx", "--pairs", "TESTUSDT", "--workers", "1",
                           "--timeframes", "4h", "--end-day", "2026-07-20"])
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and summary["exchange"] == "bingx" and summary["written"] == 2
    assert summary["minute_days_verified"] == 49
    from mrs3.pair_registry.store import connect

    con = connect(tmp_path / "registry")                                    # the records reached the registry
    assert con.execute("select count(*) from minute_days where status = 'OK'").fetchone()[0] == 49
    con.close()


def test_ready_for_bingx_uses_the_bingx_template_and_exchange(tmp_path: Path, capsys) -> None:
    import runpy

    store, _summary, _records = _run(tmp_path, FakeBingX())
    config = _registry_config(tmp_path)
    script = runpy.run_path(str(REPO / "scripts" / "ready_from_store.py"), run_name="not_main")
    assert script["TEMPLATES"]["bingx"].parent.name == "BingX"
    out = tmp_path / "ready"
    assert script["main"](["--store", str(store.root), "--config", str(config), "--out", str(out),
                           "--exchange", "bingx"]) == 0
    manifest = json.loads((out / "ready_manifest.json").read_text(encoding="utf-8"))
    assert manifest["exchange"] == "bingx" and [pair["pair"] for pair in manifest["pairs"]] == ["TESTUSDT"]


def test_the_process_pool_path_without_download_needs_the_verified_files(tmp_path: Path) -> None:
    # review of Panel stage 5b: workers > 1 pickles BingXMinutes and the accepted days; without download a recorded
    # day counts only while its file is the verified one
    _store, _summary, records = _run(tmp_path, FakeBingX())
    known = {"TESTUSDT": {record["day"]: record for record in records}}
    (tmp_path / "bingx" / API / "TESTUSDT2026-07-10_1m.parquet").unlink()            # a verified file lost since
    minutes = BingXMinutes(str(tmp_path / "bingx"), {"TESTUSDT": (API, ms("2026-06-01"))}, known)
    store = TradeStore(tmp_path / "store2")
    summary = run_history(store, data_root=tmp_path / "bingx", pairs=[("TESTUSDT", LISTED)], end_day=date(2026, 7, 20),
                          instruments={"TESTUSDT": (0.01, 0.01)}, points=POINTS, workers=2, download=False,
                          settings=exchange_run_settings(REPO, "bingx"), exchange="bingx", bingx=minutes)
    assert (summary["written"], summary["skipped"], summary["failed"]) == (1, 1, 0)
    assert '"2026-07-10"' in (tmp_path / "store2" / "journal.jsonl").read_text(encoding="utf-8")


def test_ready_without_pairs_takes_only_the_exchange_of_the_build(tmp_path: Path) -> None:
    import runpy

    from tests.test_trade_store import _write_days

    store, _summary, _records = _run(tmp_path, FakeBingX())
    _write_days(tmp_path / "bybit", "OTHERUSDT", walk(LISTED, 50, gaps=False))       # a Bybit pair in the same store
    run_history(store, data_root=tmp_path / "bybit", pairs=[("OTHERUSDT", LISTED)], end_day=date(2026, 7, 20),
                instruments={"OTHERUSDT": (0.01, 0.01)}, points=POINTS, workers=1, download=False)
    config = _registry_config(tmp_path)
    script = runpy.run_path(str(REPO / "scripts" / "ready_from_store.py"), run_name="not_main")
    out = tmp_path / "ready"
    assert script["main"](["--store", str(store.root), "--config", str(config), "--out", str(out),
                           "--exchange", "bingx"]) == 0
    manifest = json.loads((out / "ready_manifest.json").read_text(encoding="utf-8"))
    assert [pair["pair"] for pair in manifest["pairs"]] == ["TESTUSDT"]


def test_the_script_skips_a_bingx_pair_without_tick_size_and_runs_the_others(tmp_path: Path, monkeypatch,
                                                                              capsys) -> None:
    import runpy

    from mrs3.pair_registry.refresh import refresh_exchange
    from mrs3.pair_registry.store import connect

    import mrs3.pair_registry.minute_days as minute_days

    config = _registry_config(tmp_path)
    con = connect(tmp_path / "registry")
    refresh_exchange(con, "bingx", [
        {"symbol": "TESTUSDT", "status": "TRADING", "api_symbol": API, "listing_ms": ms("2026-06-01"),
         "tick_size": "0.01", "qty_step": "0.01"},
        {"symbol": "BAREUSDT", "status": "TRADING", "api_symbol": "NCSKBARE2USD-USDT", "listing_ms": ms("2026-06-01")}],
        [], now_ms=ms("2026-10-19"))
    con.close()
    client = FakeBingX()
    monkeypatch.setattr(minute_days, "BingXClient", lambda: client)
    script = runpy.run_path(str(REPO / "scripts" / "simulate_history.py"), run_name="not_main")
    code = script["main"](["--config", str(config), "--exchange", "bingx", "--pairs", "TESTUSDT,BAREUSDT",
                           "--workers", "1", "--timeframes", "4h", "--end-day", "2026-07-20"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert code == 0 and any("BAREUSDT" in line and "warning" in line for line in lines)
    assert json.loads(lines[-1])["pairs"] == 1 and json.loads(lines[-1])["written"] == 2

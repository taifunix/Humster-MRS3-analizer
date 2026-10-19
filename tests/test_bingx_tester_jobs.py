"""BingX MRS2 tester sampling (Panel stage 5c).

Contract: docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 5c contract». A fake tester and a fake BingX
client in temporary folders: nothing leaves the machine and the real tester is never started.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pyarrow as pa
import numpy as np

from mrs3.mrs2_simulator import RunSettings
from mrs3.tester_jobs import import_window, plan_jobs, queued_jobs, run_job
from mrs3.trade_history import exchange_run_settings
from mrs3.trade_store import HOURLY_SCHEMA, TRADE_SCHEMA, SegmentKey, SegmentTables, TradeStore, point_params
from tests.test_tester_jobs import IDS, TRADES, _bot, _metrics, _snapshot, _templates, ms, render_report

REPO = Path(__file__).resolve().parents[1]
API = "NCSKAMD2USD-USDT"
START, END = date(2026, 9, 27), date(2026, 10, 17)


def _window(store: TradeStore, exchange: str) -> None:
    import random

    rng = random.Random(9)
    upi = {pid: rng.uniform(-5, 50) for pid in IDS}
    hours = np.arange(ms("2026-09-27"), ms("2026-10-17"), 3_600_000, dtype=np.int64)
    price = {"point_id": np.zeros(len(hours), dtype=np.int32), "hour_ms": hours,
             "close": np.full(len(hours), 100, dtype=np.float32), "low": np.full(len(hours), 99, dtype=np.float32),
             "high": np.full(len(hours), 101, dtype=np.float32), "drawdown": np.zeros(len(hours), dtype=np.float32)}
    tables = SegmentTables(TRADE_SCHEMA.empty_table(), pa.Table.from_pydict(price, schema=HOURLY_SCHEMA),
                           _metrics(lambda pid: upi[pid]))
    record = store.write_files(SegmentKey(exchange, "AMDUSDT", "simulator", START, END, "eng"), tables,
                               settings=exchange_run_settings(REPO, exchange), tick_size=0.01, qty_step=0.01)
    store.register(record)


def _bingx_block() -> dict:
    return json.loads((REPO / "templates/exchanges/BingX/mrs3-strategy.json").read_text(encoding="utf-8"))["exchange"]


def _plan(store: TradeStore) -> dict:
    strategy, config = _templates()
    return plan_jobs(store, "AMDUSDT", strategy_template=strategy, config_template=config, parallel_runs=3,
                     exchange="bingx", settings=exchange_run_settings(REPO, "bingx"), tester_symbol=API,
                     strategy_exchange=_bingx_block())


def test_a_bingx_window_is_planned_run_and_imported_on_the_api_symbol(tmp_path: Path) -> None:
    store = TradeStore(tmp_path / "store")
    _window(store, "bingx")
    planned = _plan(store)
    assert planned["jobs_new"] == planned["blocks"] > 0
    [first, *_] = queued_jobs(store, exchange="bingx")
    folder = store.root / "jobs" / first
    job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    config = json.loads((folder / "config_tester.json").read_text(encoding="utf-8"))
    strategy = json.loads((folder / job["strategy_file"]).read_text(encoding="utf-8"))
    assert (job["symbol"], job["tester_symbol"], job["exchange"]) == ("AMDUSDT", API, "bingx")
    assert strategy["basic"]["symbol"] == API and strategy["exchange"]["name"] == "BingX"
    assert strategy["exchange"]["account"] == "bingx" and strategy["exchange"]["use_upnl"] is False
    template = _templates()[0]["exchange"]                                 # the MRS2 execution options stay
    assert strategy["exchange"]["execute_stop_by_market"] == template["execute_stop_by_market"]
    symbol_entry = next(entry for entry in config["parameter_mining"] if entry["name"].endswith("basic.symbol"))
    assert symbol_entry["values"] == [API] and config["UpdateData"] is False
    assert (config["MakerFee"], config["TakerFee"]) == (0.00012, 0.0003)
    assert "maintenance_margin_rate" not in config
    assert job["run_settings"]["taker_fee"] == 0.0003

    bot = _bot(tmp_path / "bot")
    before = _snapshot(bot)

    def fake_tester(bot_root: Path, timeout: float, log: Path) -> None:
        cfg = json.loads((bot_root / "config_tester.json").read_text(encoding="utf-8"))
        mining = {e["name"].split(".", 1)[1]: e for e in cfg["parameter_mining"]}
        shifts = [round((1 - float(v)) * 10_000) for v in mining["mrs2.ma_long.multiplier"]["values"]]
        folder = bot_root / "tester" / "report" / cfg["name_comment"]
        folder.mkdir(parents=True)
        for index, pid in enumerate(p for p in IDS if point_params(p)[0] in mining["basic.time_frame"]["values"]):
            tf, shift, o, c = point_params(pid)
            if (shift in shifts and mining["mrs2.ma_long.len"]["start"] <= o <= mining["mrs2.ma_long.len"]["end"]
                    and mining["mrs2.ma_close_long.len"]["start"] <= c <= mining["mrs2.ma_close_long.len"]["end"]):
                (folder / f"{cfg['name_comment']}_run_{index:04d}_{API}_{tf}.html").write_bytes(
                    render_report(API, pid, START, END, TRADES))           # the tester reports the API symbol

    results = [run_job(store, job_id, bot, run_tester=fake_tester) for job_id in queued_jobs(store, exchange="bingx")]
    assert {result["state"] for result in results} == {"done"}
    assert _snapshot(bot) == before
    imported = import_window(store, "AMDUSDT", settings=exchange_run_settings(REPO, "bingx"), exchange="bingx")
    assert imported["status"] == "IMPORTED" and imported["points"] == planned["points"]
    assert [row["exchange"] for row in store.segments("AMDUSDT", "bingx", source="tester")] == ["bingx"]
    assert store.segments("AMDUSDT", "bybit", source="tester") == []


def test_the_queue_keeps_the_exchanges_apart(tmp_path: Path) -> None:
    store = TradeStore(tmp_path / "store")
    _window(store, "bybit")
    _window(store, "bingx")
    strategy, config = _templates()
    plan_jobs(store, "AMDUSDT", strategy_template=strategy, config_template=config, parallel_runs=3)
    _plan(store)
    bybit, bingx = queued_jobs(store, exchange="bybit"), queued_jobs(store, exchange="bingx")
    assert bybit and bingx and not set(bybit) & set(bingx)
    assert sorted(queued_jobs(store)) == sorted(bybit + bingx)
    bybit_job = json.loads((store.root / "jobs" / bybit[0] / "job.json").read_text(encoding="utf-8"))
    assert "tester_symbol" not in bybit_job and "run_settings" not in bybit_job       # the Bybit shape is unchanged


def test_a_bingx_plan_needs_the_api_symbol_and_the_exchange_block(tmp_path: Path) -> None:
    import pytest

    from mrs3.tester_jobs import JobQueueError

    store = TradeStore(tmp_path / "store")
    _window(store, "bingx")
    strategy, config = _templates()
    with pytest.raises(JobQueueError):
        plan_jobs(store, "AMDUSDT", strategy_template=strategy, config_template=config, exchange="bingx")


def test_a_bingx_job_with_an_unverified_day_fails_before_the_tester(tmp_path: Path, monkeypatch, capsys) -> None:
    import runpy

    import mrs3.pair_registry.minute_days as minute_days
    from mrs3.bingx_downloader import BingXDownloadError
    from mrs3.pair_registry.refresh import refresh_exchange
    from mrs3.pair_registry.store import connect

    store = TradeStore(tmp_path / "store")
    _window(store, "bingx")
    con = connect(tmp_path / "registry")
    refresh_exchange(con, "bingx", [{"symbol": "AMDUSDT", "status": "TRADING", "api_symbol": API,
                                     "listing_ms": ms("2026-06-01"), "tick_size": "0.01", "qty_step": "0.01"}], [],
                     now_ms=ms("2026-10-19"))
    con.close()
    bot = _bot(tmp_path / "bot")
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"root": str(tmp_path / "registry")},     # the tester's folder
                                  "trade_store": {"root": str(store.root)}, "tester_runner": {"bot_root": str(bot)},
                                  "duckdb_import": {"workers": 2}}), encoding="utf-8")

    class Down:
        def klines(self, *args, **kwargs):
            raise BingXDownloadError("request to /klines failed after 4 attempts")

    monkeypatch.setattr(minute_days, "BingXClient", Down)
    script = runpy.run_path(str(REPO / "scripts" / "tester_jobs.py"), run_name="not_main")
    assert script["main"](["plan", "--config", str(config), "--exchange", "bingx", "--pairs", "AMDUSDT"]) == 0
    before = _snapshot(bot)
    assert script["main"](["run", "--config", str(config), "--exchange", "bingx", "--limit", "1"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    failed = [event for event in events if event.get("state") == "failed"]
    assert failed and failed[0]["error"].startswith("MISSING_DAYS: [")         # the refused days are listed
    assert _snapshot(bot) == before                                         # the tester never ran
    assert json.loads((store.root / "jobs" / failed[0]["job_id"] / "job.json").read_text(encoding="utf-8"))[
        "tester_symbol"] == API
    from mrs3.tester_jobs import retry_failed

    retry_failed(store)
    elsewhere = json.loads(config.read_text(encoding="utf-8"))
    elsewhere["pair_registry"]["minute_data"] = {"bingx_data_root": str(tmp_path / "elsewhere")}
    config.write_text(json.dumps(elsewhere), encoding="utf-8")
    assert script["main"](["run", "--config", str(config), "--exchange", "bingx", "--limit", "1"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    failed = [event for event in events if event.get("state") == "failed"]
    assert "not the tester's BingX folder" in failed[0]["error"] and str(tmp_path) not in failed[0]["error"]
    assert _snapshot(bot) == before


def test_bingx_days_are_ensured_while_another_process_holds_the_registry(tmp_path: Path) -> None:
    import subprocess
    import sys

    import pytest

    from mrs3.pair_registry.minute_days import ensure_bingx_days_shared
    from mrs3.pair_registry.store import RegistryError
    from mrs3.pair_registry.refresh import refresh_exchange
    from mrs3.pair_registry.store import RegistrySettings, connect
    from tests.test_minute_days import API as MINUTE_API
    from tests.test_minute_days import DAY, NOW, FakeBingX, _full_day

    con = connect(tmp_path / "registry")
    refresh_exchange(con, "bingx", [{"symbol": "AMDUSDT", "status": "TRADING", "api_symbol": MINUTE_API,
                                     "listing_ms": ms("2026-06-01")}], [], now_ms=ms("2026-10-19"))
    con.close()
    settings = RegistrySettings(root=tmp_path / "registry", bingx_data_root=tmp_path / "bingx")
    next_day = date(2026, 10, 15)
    client = FakeBingX({DAY: _full_day(DAY), next_day: _full_day(next_day)})
    assert ensure_bingx_days_shared(settings, "AMDUSDT", DAY, DAY, client=client, now=NOW).accepted == [DAY]
    holder = subprocess.Popen([sys.executable, "-c", "import duckdb, sys, time; c = duckdb.connect(sys.argv[1]); "
                               "print('held', flush=True); time.sleep(60)", str(tmp_path / "registry" / "registry.duckdb")],
                              stdout=subprocess.PIPE, text=True)        # e.g. «Синхронизировать» is running
    try:
        assert holder.stdout.readline().strip() == "held"
        client.calls.clear()
        with pytest.raises(RegistryError):                              # a writer blocks even readers
            ensure_bingx_days_shared(settings, "AMDUSDT", DAY, next_day, client=client, now=NOW)
        result = ensure_bingx_days_shared(settings, "AMDUSDT", DAY, next_day, client=client, now=NOW,
                                          api_symbol=MINUTE_API)
        assert result.accepted == [DAY, next_day]
        assert client.calls == [("1m", next_day)]       # the day on disk is verified from its file, the new one fetched
    finally:
        holder.kill()
        holder.wait()
    con = connect(tmp_path / "registry")                                 # the new record was not written meanwhile
    assert con.execute("select count(*) from minute_days").fetchone()[0] == 1
    con.close()


def test_a_bybit_plan_refuses_a_foreign_symbol_or_exchange_block(tmp_path: Path) -> None:
    import pytest

    from mrs3.tester_jobs import JobQueueError

    store = TradeStore(tmp_path / "store")
    _window(store, "bybit")
    strategy, config = _templates()
    with pytest.raises(JobQueueError):
        plan_jobs(store, "AMDUSDT", strategy_template=strategy, config_template=config, tester_symbol=API)
    with pytest.raises(JobQueueError):
        plan_jobs(store, "AMDUSDT", strategy_template=strategy, config_template=config,
                  strategy_exchange=_bingx_block())

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import random

import numpy as np
import pyarrow as pa
import pytest

from mrs3.mrs2_simulator import RunSettings
from mrs3.period_csv import MA_LENGTHS, SHIFTS_BP, grid, multiplier_text
from mrs3.performance import parse_performance_report
from mrs3.tester_jobs import (
    RestoreError, JobQueueError, import_window, plan_jobs, queued_jobs, recover, report_tables, retry_failed,
    run_job, run_queue,
)
import mrs3.tester_jobs as tester_jobs
from mrs3.tester_sampling import Block, block_config, cover_blocks, neighbours, select_points
from mrs3.trade_store import (
    HOURLY_SCHEMA, METRICS_SCHEMA, TRADE_SCHEMA, SegmentKey, SegmentTables, TradeStore, point_id, point_params,
)
from mrs3.trade_windows import window_metrics

REPO = Path(__file__).parents[1]
GRID = grid()
IDS = [point_id(p.timeframe, p.shift_bp, p.open_len, p.close_len) for p in GRID]


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_neighbours_stay_inside_the_tf_cube():
    assert len(neighbours(point_id("1h", 230, 4, 4))) == 26
    corner = neighbours(point_id("15m", 70, 2, 2))
    assert len(corner) == 7 and all(point_params(pid)[0] == "15m" for pid in corner)


def _metrics(upi_of, trades_of=lambda pid: 10) -> pa.Table:
    rows = {name: [] for name in METRICS_SCHEMA.names}
    for pid in IDS:
        upi = upi_of(pid)
        values = dict(point_id=pid, final_balance=10_000 * (1 + upi * 20 / 30 / 100), marked_final=0.0,
                      trades=trades_of(pid), wins=1, gross_profit=1.0, gross_loss=0.0, fees=0.0, max_drawdown=0.0,
                      max_drawdown_percent=1.0, ulcer_index=1.0, position_avg_percent=0.0, position_max_percent=0.0,
                      last_ms=0)
        for name in METRICS_SCHEMA.names:
            rows[name].append(values[name])
    return pa.Table.from_pydict(rows, schema=METRICS_SCHEMA)


def test_selection_takes_the_top_tenth_of_eligible_points_and_their_neighbours():
    rng = random.Random(3)
    upi = {pid: rng.uniform(-5, 50) for pid in IDS}
    quiet = set(rng.sample(IDS, 500))
    metrics = _metrics(lambda pid: upi[pid], lambda pid: 2 if pid in quiet else 10)
    selection = select_points(metrics, 20.0, "abc")
    eligible = [pid for pid in IDS if pid not in quiet and upi[pid] > 0]
    top = sorted(eligible, key=lambda pid: (-upi[pid], pid))[:378]
    expected = set(top) | {n for pid in top for n in neighbours(pid)}
    assert set(selection.point_ids) == expected and list(selection.point_ids) == sorted(expected)
    assert select_points(metrics, 20.0, "abc").sha256 == selection.sha256
    assert select_points(metrics, 20.0, "other").sha256 != selection.sha256


def test_block_cover_is_exact_and_merges_full_cubes():
    rng = random.Random(5)
    for _ in range(20):
        chosen = set(rng.sample(IDS, rng.randint(1, 900)))
        blocks = cover_blocks(chosen)
        covered = [pid for block in blocks for pid in block.point_ids()]
        assert len(covered) == len(set(covered)) == len(chosen) and set(covered) == chosen
        assert sum(block.runs for block in blocks) == len(chosen)
    full = [pid for pid in IDS if point_params(pid)[0] in ("15m", "2h")]
    assert cover_blocks(full) == [Block(("15m", "2h"), SHIFTS_BP, (2, 7), (2, 7))]


def test_block_config_runs_exactly_the_block():
    template = json.loads((REPO / "templates/tester/mrs2/config_tester_long.json").read_text(encoding="utf-8"))
    block = Block(("30m", "1h"), (70, 140), (3, 5), (2, 2))
    config = block_config(template, "XOMUSDT", date(2026, 9, 27), date(2026, 10, 17), block, "job_x", parallel_runs=7)
    mining = {entry["name"].split(".", 1)[1]: entry for entry in config["parameter_mining"]}
    assert mining["mrs2.ma_long.multiplier"]["values"] == [multiplier_text(70), multiplier_text(140)]
    assert (mining["mrs2.ma_long.len"]["start"], mining["mrs2.ma_long.len"]["end"]) == (3.0, 5.0)
    assert (mining["mrs2.ma_close_long.len"]["start"], mining["mrs2.ma_close_long.len"]["end"]) == (2.0, 2.0)
    assert mining["basic.time_frame"]["values"] == ["30m", "1h"] and mining["basic.symbol"]["values"] == ["XOMUSDT"]
    assert config["order_limit_fill_plus_tick"] is True and config["LimitOrderVolumeCheck"] is False
    assert config["UpdateData"] is False and config["max_parallel_runs"] == 7 and config["name_comment"] == "job_x"
    assert config["report"]["enable_html_report"] and config["report"]["include_trades_table"]
    assert config["StartDate"] == "2026-09-27T00:00:00" and config["EndDate"] == "2026-10-17T00:00:00"


# -- reports -------------------------------------------------------------------------------


def render_report(symbol: str, pid: int, start: date, end: date, trades: list[tuple]) -> bytes:
    """A 2026-10 format MRS2 report: trades = (entry ts, exit ts, entry, exit, qty, fee_in, fee_out, funding).
    Without trades it looks like the real tester's zero-trade report: one equity point, Ulcer `n/a`."""
    timeframe, shift, open_len, close_len = point_params(pid)
    settings = {"name": f"mrs2_long_{symbol}", "exchange": {"name": "Bybit", "use_upnl": False},
                "basic": {"symbol": symbol, "time_frame": timeframe, "strategy": "mrs2", "use_long": True,
                          "use_short": False},
                "mrs2": {"ma_long": {"len": open_len, "multiplier": float(multiplier_text(shift))},
                         "ma_close_long": {"len": close_len, "multiplier": 1.003}}}
    balance, rows = 10_000.0, []
    series = [(ms(start.isoformat()), balance)]
    for entry_ts, exit_ts, entry, exit_price, qty, fee_in, fee_out, funding in trades:
        balance -= fee_in
        rows.append((entry_ts, "Buy", "opened", qty, entry, fee_in, 0.0, balance, qty * entry, 99.5))
        series.append((ms(entry_ts), balance))
        pnl = qty * (exit_price - entry)
        balance += pnl - fee_out - funding
        rows.append((exit_ts, "Sell", "closed", qty, exit_price, fee_out, pnl, balance, qty * exit_price, 100.2))
        series.append((ms(exit_ts), balance))
    trade_rows = "\n".join(
        f"<tr><td>{ts}</td><td>{symbol}</td><td>{i}</td><td>mrs2</td><td>{side}</td><td>{action}</td><td>{qty}</td>"
        f"<td>{price}</td><td>{fee}</td><td>{pnl}</td><td>{bal}</td><td>0</td><td></td><td>{cost}</td><td>{pct}%</td></tr>"
        for i, (ts, side, action, qty, price, fee, pnl, bal, cost, pct) in enumerate(rows, 1))
    points = ",".join(f'[{stamp},"{value}"]' for stamp, value in series)
    return f"""<!doctype html>
<html><body>
<div><span>hb/tester/</span><span title="Version of the program that made the report">ver.2026/10/10</span><span>/backtest_results:</span></div>
<pre>{json.dumps(settings)}</pre>
<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>
<tr><td>Report range</td><td>{start.isoformat()} - {end.isoformat()}</td></tr>
<tr><td>Initial balance</td><td>10000</td></tr>
<tr><td>Final balance</td><td>{balance:.2f}</td></tr>
<tr><td>Total transactions (buy/sell)</td><td>{len(rows)}</td></tr>
<tr><td>Max Drawdown, %</td><td>1.25</td></tr>
<tr><td>Max Drawdown, USDT</td><td>125.00</td></tr>
<tr><td>Ulcer Index (hourly), %</td><td>{"0.4321" if trades else "n/a"}</td></tr>
</tbody></table>
<table><thead><tr><th>Timestamp</th><th>Symbol</th><th>Order ID</th><th>Strategy</th><th>Side</th><th>Action</th><th>Size</th><th>Price</th><th>Fee</th><th>PnL</th><th>Balance</th><th>Post Size</th><th>Post Side</th><th>Cost</th><th>Cost, % of margin balance</th></tr></thead><tbody>
{trade_rows}
</tbody></table>
<script>
const walletSeries = [{points}];
const equitySeries = [{points}];
</script>
</body></html>""".encode("utf-8")


PID = point_id("1h", 140, 3, 2)
TRADES = [("2026-09-28 01:00:00", "2026-09-28 05:30:00", 100.0, 101.0, 100.0, 0.1, 0.101, 0.25),
          ("2026-10-16 22:00:00", "2026-10-16 23:59:00", 100.0, 98.902, 100.0, 0.1, 100 * 98.902 * 0.0002, 0.0)]


def test_report_conversion_derives_funding_exit_kind_and_metrics():
    report = parse_performance_report(render_report("XOMUSDT", PID, date(2026, 9, 27), date(2026, 10, 17), TRADES),
                                      report_format_2026_10=True)
    pid, trades, hourly, metrics = report_tables(report, symbol="XOMUSDT", start=date(2026, 9, 27),
                                                 end=date(2026, 10, 17))
    assert pid == PID
    assert trades["exit_kind"] == ["limit", "market_end"]
    assert trades["funding"] == pytest.approx([0.25, 0.0], abs=1e-9)
    assert trades["entry_ms"][0] == ms("2026-09-28 01:00:00") and trades["qty"] == [100.0, 100.0]
    assert metrics["trades"] == [2] and metrics["wins"] == [1]
    assert metrics["max_drawdown_percent"] == [1.25] and metrics["ulcer_index"] == [0.4321]
    final = 10_000 - 0.1 + 100 - 0.101 - 0.25 - 0.1 + 100 * (98.902 - 100) - 100 * 98.902 * 0.0002
    assert metrics["final_balance"][0] == pytest.approx(final)
    assert metrics["last_ms"] == [ms("2026-10-16 23:59:00")]
    assert len(hourly["hour_ms"]) == 20 * 24 and hourly["close"][-1] == np.float32(final)
    with pytest.raises(JobQueueError):
        report_tables(report, symbol="SNOWUSDT", start=date(2026, 9, 27), end=date(2026, 10, 17))
    with pytest.raises(JobQueueError):
        report_tables(report, symbol="XOMUSDT", start=date(2026, 9, 26), end=date(2026, 10, 17))


# -- planning, executor, import ----------------------------------------------------------


def _window_store(root: Path) -> TradeStore:
    store = TradeStore(root)
    rng = random.Random(9)
    upi = {pid: rng.uniform(-5, 50) for pid in IDS}
    hours = np.arange(ms("2026-09-27"), ms("2026-10-17"), 3_600_000, dtype=np.int64)
    price = {"point_id": np.zeros(len(hours), dtype=np.int32), "hour_ms": hours,
             "close": np.full(len(hours), 100, dtype=np.float32), "low": np.full(len(hours), 99, dtype=np.float32),
             "high": np.full(len(hours), 101, dtype=np.float32), "drawdown": np.zeros(len(hours), dtype=np.float32)}
    tables = SegmentTables(TRADE_SCHEMA.empty_table(), pa.Table.from_pydict(price, schema=HOURLY_SCHEMA),
                           _metrics(lambda pid: upi[pid]))
    record = store.write_files(SegmentKey("bybit", "XOMUSDT", "simulator", date(2026, 9, 27), date(2026, 10, 17),
                                          "eng"), tables, settings=RunSettings(), tick_size=0.01, qty_step=0.01)
    store.register(record)
    return store


def _templates() -> tuple[dict, dict]:
    strategy = json.loads((REPO / "templates/strategies/source-v6-mrs2/long.json").read_text(encoding="utf-8"))
    config = json.loads((REPO / "templates/tester/mrs2/config_tester_long.json").read_text(encoding="utf-8"))
    return strategy, config


def _bot(root: Path) -> Path:
    (root / "tester" / "settings_strategy").mkdir(parents=True)
    (root / "config_tester.json").write_text('{"own": "config"}', encoding="utf-8")
    (root / "tester" / "settings_strategy" / "KOUSDT.json").write_text('{"own": "strategy"}', encoding="utf-8")
    return root


def _snapshot(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file() and "report" not in path.parts
            and not path.name.startswith(".mrs3-tester-target.lock")}


def test_jobs_are_planned_once_run_with_restore_and_imported_as_a_tester_segment(tmp_path: Path):
    store = _window_store(tmp_path / "store")
    strategy, config = _templates()
    first = plan_jobs(store, "XOMUSDT", strategy_template=strategy, config_template=config, parallel_runs=3)
    again = plan_jobs(store, "XOMUSDT", strategy_template=strategy, config_template=config, parallel_runs=3)
    assert first["jobs_new"] == first["blocks"] > 0 and again["jobs_new"] == 0
    assert first["runs"] == first["points"] and 378 <= first["points"] < 3780
    bot = _bot(tmp_path / "bot")
    before = _snapshot(bot)

    def fake_tester(bot_root: Path, timeout: float, log: Path) -> None:
        cfg = json.loads((bot_root / "config_tester.json").read_text(encoding="utf-8"))
        files = [path.name for path in (bot_root / "tester" / "settings_strategy").iterdir()]
        assert files == ["mrs2_long_XOMUSDT.json"]                   # only the job's strategy is in place
        mining = {e["name"].split(".", 1)[1]: e for e in cfg["parameter_mining"]}
        job_points = [p for p in IDS if point_params(p)[0] in mining["basic.time_frame"]["values"]]
        shifts = [round((1 - float(v)) * 10_000) for v in mining["mrs2.ma_long.multiplier"]["values"]]
        folder = bot_root / "tester" / "report" / cfg["name_comment"]
        folder.mkdir(parents=True)
        for index, pid in enumerate(job_points):
            tf, shift, o, c = point_params(pid)
            if (shift in shifts and mining["mrs2.ma_long.len"]["start"] <= o <= mining["mrs2.ma_long.len"]["end"]
                    and mining["mrs2.ma_close_long.len"]["start"] <= c <= mining["mrs2.ma_close_long.len"]["end"]):
                (folder / f"{cfg['name_comment']}_run_{index:04d}_XOMUSDT_{tf}.html").write_bytes(
                    render_report("XOMUSDT", pid, date(2026, 9, 27), date(2026, 10, 17), TRADES))

    results = [run_job(store, job_id, bot, run_tester=fake_tester) for job_id in queued_jobs(store)]
    assert {result["state"] for result in results} == {"done"}
    assert _snapshot(bot) == before                                    # the tester's own files are back
    assert not any((bot / "tester" / "report").iterdir())              # HTML deleted after conversion
    imported = import_window(store, "XOMUSDT")
    assert imported["status"] == "IMPORTED" and imported["points"] == first["points"]
    [segment] = store.segments("XOMUSDT", source="tester")
    window = window_metrics(store, [segment])
    assert len(window.point_ids) == first["points"] and set(window.trades) == {2}
    assert import_window(store, "XOMUSDT")["status"] == "EXISTS"
    # a new selection of the same window gets its own segment, which supersedes the older one
    plan_jobs(store, "XOMUSDT", strategy_template=strategy, config_template=config, parallel_runs=3, fraction=0.12)
    for job_id in queued_jobs(store):
        assert run_job(store, job_id, bot, run_tester=fake_tester)["state"] == "done"
    second = import_window(store, "XOMUSDT")
    assert second["status"] == "IMPORTED" and second["segment_id"] != imported["segment_id"]
    assert [row["segment_id"] for row in store.segments("XOMUSDT", source="tester")] == [second["segment_id"]]
    with store.connect(read_only=True) as con:
        assert con.execute("select count(*) from jobs where result_segment = ?",
                           [second["segment_id"]]).fetchone()[0] > 0
    # the window recomputed under a new tick size supersedes the tester segment of the old one; importing the old
    # plan again never brings it back (docs/specs/2026-10-20-panel-history-incremental.md)
    with store.connect() as con:
        con.execute("update segments set tick_size = 0.05 where source = 'simulator'")
        con.execute("update segments set status = 'superseded', superseded_by = 'INSTRUMENT_CHANGED' "
                    "where source = 'tester'")
    assert import_window(store, "XOMUSDT")["status"] == "INSTRUMENT_CHANGED"
    assert store.segments("XOMUSDT", source="tester") == []


def test_a_failing_tester_run_still_restores_the_tester_files(tmp_path: Path):
    store = _window_store(tmp_path / "store")
    strategy, config = _templates()
    plan_jobs(store, "XOMUSDT", strategy_template=strategy, config_template=config)
    bot = _bot(tmp_path / "bot")
    before = _snapshot(bot)
    job_id = queued_jobs(store)[0]

    def broken(bot_root: Path, timeout: float, log: Path) -> None:
        raise RuntimeError("tester crashed")

    result = run_job(store, job_id, bot, run_tester=broken)
    assert result["state"] == "failed" and "crashed" in result["error"]
    assert _snapshot(bot) == before
    assert import_window(store, "XOMUSDT")["status"] == "PENDING"
    assert retry_failed(store) == 1 and job_id in queued_jobs(store)

    def no_reports(bot_root: Path, timeout: float, log: Path) -> None:
        return None

    assert run_job(store, job_id, bot, run_tester=no_reports)["state"] == "failed"   # missing reports
    assert _snapshot(bot) == before

    def vandal(bot_root: Path, timeout: float, log: Path) -> None:
        (tmp_path / "store" / "jobs" / job_id / "backup" / "settings_strategy" / "KOUSDT.json").write_text("x")

    retry_failed(store)
    with pytest.raises(RestoreError):
        run_job(store, job_id, bot, run_tester=vandal)
    assert MA_LENGTHS[0] == 2



def test_a_report_without_trades_is_converted_not_rejected():
    # format checked on the real tester 2026-10-17 (XOM 4h 550 bp MA 7/7, two days): one equity point, Ulcer n/a
    report = parse_performance_report(render_report("XOMUSDT", PID, date(2026, 9, 27), date(2026, 10, 17), []),
                                      allow_zero_activity=True, report_format_2026_10=True)
    pid, trades, hourly, metrics = report_tables(report, symbol="XOMUSDT", start=date(2026, 9, 27),
                                                 end=date(2026, 10, 17))
    assert pid == PID and trades["entry_ms"] == [] and metrics["trades"] == [0]
    assert metrics["final_balance"] == [10_000.0] and metrics["last_ms"] == [ms("2026-09-27")]
    assert set(hourly["close"]) == {np.float32(10_000.0)} and set(hourly["drawdown"]) == {np.float32(0.0)}


def _state(store: TradeStore, job_id: str) -> tuple:
    with store.connect(read_only=True) as con:
        return con.execute("select state, error from jobs where job_id = ?", [job_id]).fetchone()


def test_a_backup_that_fails_midway_puts_the_strategy_files_back(tmp_path: Path, monkeypatch):
    store = _window_store(tmp_path / "store")
    strategy, config = _templates()
    plan_jobs(store, "XOMUSDT", strategy_template=strategy, config_template=config)
    bot = _bot(tmp_path / "bot")
    (bot / "tester" / "settings_strategy" / "ZZZ.json").write_text("{}", encoding="utf-8")
    before = _snapshot(bot)
    job_id = queued_jobs(store)[0]
    real_move = tester_jobs.shutil.move
    calls = []

    def flaky_move(source, target):
        calls.append(source)
        if len(calls) == 2:
            raise PermissionError("file is open in another program")
        return real_move(source, target)

    monkeypatch.setattr(tester_jobs.shutil, "move", flaky_move)
    result = run_job(store, job_id, bot, run_tester=lambda *args: None)
    monkeypatch.setattr(tester_jobs.shutil, "move", real_move)
    assert result["state"] == "failed" and "open in another program" in result["error"]
    assert _snapshot(bot) == before and not (tmp_path / "store" / "jobs" / job_id / "backup").exists()
    assert _state(store, job_id)[0] == "failed"


def test_an_interrupted_executor_is_recovered_before_the_next_run(tmp_path: Path):
    store = _window_store(tmp_path / "store")
    strategy, config = _templates()
    plan_jobs(store, "XOMUSDT", strategy_template=strategy, config_template=config)
    bot = _bot(tmp_path / "bot")
    before = _snapshot(bot)
    job_id = queued_jobs(store)[0]
    folder = tmp_path / "store" / "jobs" / job_id
    # what a killed executor leaves behind: its lock, the backup, the job's files in the bot, state `running`
    (tmp_path / "store" / "jobs" / ".executor.lock").write_text(f"999999999 {job_id}", encoding="ascii")
    tester_jobs._backup(bot, folder / "backup")
    (bot / "config_tester.json").write_text('{"job": "config"}', encoding="utf-8")
    (bot / "tester" / "settings_strategy" / "mrs2_long_XOMUSDT.json").write_text("{}", encoding="utf-8")
    tester_jobs.set_state(store, job_id, "running")
    with pytest.raises(JobQueueError, match="recover"):
        run_job(store, queued_jobs(store)[1], bot, run_tester=lambda *args: None)
    summary = recover(store, bot)
    assert summary == {"restored": [job_id], "interrupted": [job_id]}
    assert _snapshot(bot) == before and not (tmp_path / "store" / "jobs" / ".executor.lock").exists()
    assert _state(store, job_id) == ("failed", "INTERRUPTED: the executor stopped during this job")


def test_the_queue_stops_between_jobs_and_runs_only_the_current_plan(tmp_path: Path):
    store = _window_store(tmp_path / "store")
    strategy, config = _templates()
    first = plan_jobs(store, "XOMUSDT", strategy_template=strategy, config_template=config)
    second = plan_jobs(store, "XOMUSDT", strategy_template=strategy, config_template=config, fraction=0.12)
    assert second["selection_sha256"] != first["selection_sha256"]
    with store.connect(read_only=True) as con:
        current = {row[0] for row in con.execute("select job_id from jobs where selection_sha256 = ?",
                                                 [second["selection_sha256"]]).fetchall()}
    assert set(queued_jobs(store)) == current                          # the older selection is not run
    ran = []

    def fake_run(store_, job_id, bot_root, **kwargs):
        ran.append(job_id)
        (tmp_path / "store" / "jobs" / ".stop").write_text("stop", encoding="ascii")   # the Panel's Stop
        return {"job_id": job_id, "state": "done"}

    summary = run_queue(store, tmp_path / "bot", run=fake_run)
    assert summary == {"stopped": True, "done": 1, "failed": 0} and len(ran) == 1
    assert not (tmp_path / "store" / "jobs" / ".stop").exists()


def test_a_keyboard_interrupt_still_restores_and_marks_the_job_failed(tmp_path: Path):
    store = _window_store(tmp_path / "store")
    strategy, config = _templates()
    plan_jobs(store, "XOMUSDT", strategy_template=strategy, config_template=config)
    bot = _bot(tmp_path / "bot")
    before = _snapshot(bot)
    job_id = queued_jobs(store)[0]

    def interrupted(*args):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_job(store, job_id, bot, run_tester=interrupted)
    assert _snapshot(bot) == before and _state(store, job_id)[0] == "failed"
    assert not (tmp_path / "store" / "jobs" / ".executor.lock").exists()



def test_a_locked_strategy_copied_but_not_deleted_is_not_a_leftover(tmp_path: Path, monkeypatch):
    store = _window_store(tmp_path / "store")
    strategy, config = _templates()
    plan_jobs(store, "XOMUSDT", strategy_template=strategy, config_template=config)
    bot = _bot(tmp_path / "bot")
    (bot / "tester" / "settings_strategy" / "ZZZ.json").write_text('{"z": 1}', encoding="utf-8")
    before = _snapshot(bot)
    job_id = queued_jobs(store)[0]
    real_move = tester_jobs.shutil.move

    def copy_but_cannot_delete(source, target):
        if Path(source).name == "ZZZ.json":
            tester_jobs.shutil.copy2(source, target)        # what shutil.move does when the original is locked
            raise PermissionError("the original is open in another program")
        return real_move(source, target)

    monkeypatch.setattr(tester_jobs.shutil, "move", copy_but_cannot_delete)
    result = run_job(store, job_id, bot, run_tester=lambda *args: None)
    monkeypatch.setattr(tester_jobs.shutil, "move", real_move)
    assert result["state"] == "failed" and _snapshot(bot) == before
    assert not (tmp_path / "store" / "jobs" / job_id / "backup").exists()
    assert recover(store, bot)["restored"] == []


def test_an_unfinished_backup_stops_the_executor(tmp_path: Path):
    store = _window_store(tmp_path / "store")
    strategy, config = _templates()
    plan_jobs(store, "XOMUSDT", strategy_template=strategy, config_template=config)
    bot = _bot(tmp_path / "bot")
    job_id = queued_jobs(store)[0]
    (tmp_path / "store" / "jobs" / job_id / "backup").mkdir()
    with pytest.raises(RestoreError):
        run_job(store, job_id, bot, run_tester=lambda *args: None)
    assert _state(store, job_id)[0] == "failed"
    assert not (tmp_path / "store" / "jobs" / ".executor.lock").exists()


def test_a_lock_names_its_owner_by_pid_and_start_time(tmp_path: Path):
    import subprocess
    import sys
    store = _window_store(tmp_path / "store")
    bot = _bot(tmp_path / "bot")
    lock = tmp_path / "store" / "jobs" / ".executor.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        start = tester_jobs._process_start(child.pid)
        lock.write_text(f"{child.pid} {start} job", encoding="ascii")
        with pytest.raises(JobQueueError, match="another executor"):
            recover(store, bot)
        lock.write_text(f"{child.pid} 12345.000 job", encoding="ascii")          # a reused pid
        assert recover(store, bot) == {"restored": [], "interrupted": []} and not lock.exists()
    finally:
        child.kill()
        child.wait()

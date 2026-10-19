"""Tester jobs for sampled points: planning, queue, local executor (with restore of the tester's files),
HTML report conversion and import into `tester` segments of the trade store.

Contract: docs/specs/2026-10-17-mrs2-history-store-and-tester-sampling.md («Stage 3 contract»).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable, Mapping, Sequence

import numpy as np
import psutil
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .locking import TesterTargetLock
from .mrs2_simulator import DAY_MS, RunSettings
from .performance import parse_performance_report
from .period_csv import strategy_document
from .tester_sampling import COVER_VERSION, SELECTION_VERSION, Block, block_config, cover_blocks, select_points
from .trade_store import (
    HOURLY_SCHEMA, METRICS_SCHEMA, PRICE_POINT, TRADE_SCHEMA, SegmentKey, SegmentTables, TradeStore, TradeStoreError,
    _hourly_aggregate, build_tables, parquet_bytes, point_id, utc_now, write_segment_files,
)

TESTER_SOURCE = "tester"
TESTER_ENGINE = "hb-tester"
STATES = ("queued", "running", "done", "failed")


class JobQueueError(RuntimeError):
    pass


class RestoreError(JobQueueError):
    """The tester's own files could not be restored and verified: the executor must stop."""


def _date(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date()


def window_segment(store: TradeStore, symbol: str, exchange: str = "bybit") -> dict:
    segments = store.segments(symbol, exchange, source="simulator")
    if not segments:
        raise JobQueueError(f"no active simulator segment for {symbol}")
    return segments[-1]


def jobs_root(store: TradeStore) -> Path:
    return store.root / "jobs"


# -- planning ------------------------------------------------------------------------------


def plan_jobs(store: TradeStore, symbol: str, *, strategy_template: Mapping, config_template: Mapping,
              settings: RunSettings = RunSettings(), parallel_runs: int = 4, fraction: float = 0.10,
              exchange: str = "bybit", tester_symbol: str | None = None,
              strategy_exchange: Mapping | None = None) -> dict:
    """Select the window's points, cover them by blocks and queue one job per block (idempotent).

    BingX (Panel stage 5c): the tester and the strategy get `tester_symbol` (the BingX API symbol) and the
    exchange block `strategy_exchange`; the Bybit-only `maintenance_margin_rate` is dropped; the job records its
    tester symbol and run settings, so the reports are converted with them."""
    if exchange != "bybit" and (not tester_symbol or strategy_exchange is None):
        raise JobQueueError(f"{exchange}: the tester symbol and the strategy exchange block are required")
    if exchange == "bybit" and (tester_symbol not in (None, symbol) or strategy_exchange is not None):
        raise JobQueueError("bybit: jobs run on the pair symbol with the template's exchange block")
    run_symbol = tester_symbol or symbol
    window = window_segment(store, symbol, exchange)
    start, end = _date(window["start_ms"]), _date(window["end_ms"])
    selection = select_points(store.read_metrics(window), (window["end_ms"] - window["start_ms"]) / DAY_MS,
                              window["metrics_sha256"], fraction=fraction)
    blocks = cover_blocks(selection.point_ids)
    strategy = strategy_document(strategy_template, run_symbol, settings)
    if strategy_exchange is not None:
        # only the exchange and the account, as the BingX tester mode of `panel_testing`: the MRS2 template's
        # execution options (e.g. execute_stop_by_market) stay, so both exchanges run the same strategy logic
        strategy["exchange"].update(name=strategy_exchange["name"], account=strategy_exchange["account"])
    created = 0
    with store.connect(read_only=True) as con:
        known = {row[0] for row in con.execute("select job_id from jobs where symbol = ?", [symbol]).fetchall()}
    rows = []
    for block in blocks:
        identity = {"exchange": exchange, "symbol": symbol, "start": start.isoformat(), "end": end.isoformat(),
                    "block": block.as_dict(), "selection": selection.version, "cover": COVER_VERSION,
                    "selection_sha256": selection.sha256}
        job_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        if job_id in known:
            continue
        name = f"job_{job_id}"
        folder = jobs_root(store) / job_id
        folder.mkdir(parents=True, exist_ok=True)
        config = block_config(config_template, run_symbol, start, end, block, name, settings, parallel_runs)
        job = {**identity, "job_id": job_id, "name": name, "window_segment": window["segment_id"],
               "strategy_file": f"{strategy['name']}.json", "points": block.point_ids()}
        if exchange != "bybit":                    # Bybit jobs keep their exact pre-BingX shape
            config.pop("maintenance_margin_rate", None)
            job.update(tester_symbol=run_symbol, run_settings=asdict(settings))
        (folder / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (folder / "config_tester.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                                                   encoding="utf-8")
        (folder / job["strategy_file"]).write_text(json.dumps(strategy, ensure_ascii=False, indent=2) + "\n",
                                                   encoding="utf-8")
        rows.append([job_id, exchange, symbol, window["start_ms"], window["end_ms"],
                     json.dumps(block.as_dict(), sort_keys=True), f"{SELECTION_VERSION}+{COVER_VERSION}",
                     selection.sha256, "queued", None, None, utc_now(), utc_now(), None])
        created += 1
    with store.connect() as con:
        con.execute("begin")
        if rows:
            con.executemany("insert into jobs values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        # the latest plan of a window decides which selection is run and imported
        con.execute("insert or replace into plans values (?, ?, ?, ?, ?, ?)",
                    [exchange, symbol, window["start_ms"], window["end_ms"], selection.sha256, utc_now()])
        con.execute("commit")
    return {"pair": symbol, "window": [start.isoformat(), end.isoformat()], "selection_sha256": selection.sha256,
            "points": len(selection.point_ids), "blocks": len(blocks), "runs": sum(b.runs for b in blocks),
            "jobs_new": created}


# -- queue ------------------------------------------------------------------------------------


def set_state(store: TradeStore, job_id: str, state: str, *, executor: str | None = None,
              error: str | None = None) -> None:
    if state not in STATES:
        raise JobQueueError(f"unknown job state: {state}")
    with store.connect() as con:
        con.execute("update jobs set state = ?, executor = coalesce(?, executor), error = ?, updated_utc = ? "
                    "where job_id = ?", [state, executor, error, utc_now(), job_id])


def queued_jobs(store: TradeStore, symbols: Sequence[str] | None = None, exchange: str | None = None) -> list[str]:
    """Queued jobs of the current plan only: the latest planned window of each pair and its latest selection
    (of one exchange when given: a human ticker can exist on both exchanges)."""
    with store.connect(read_only=True) as con:
        rows = con.execute("""
            with latest as (select exchange, symbol, max(start_ms) as start_ms from plans group by exchange, symbol)
            select j.job_id, j.symbol, j.exchange from jobs j
            join plans p on p.exchange = j.exchange and p.symbol = j.symbol and p.start_ms = j.start_ms
                        and p.end_ms = j.end_ms and p.selection_sha256 = j.selection_sha256
            join latest l on l.exchange = j.exchange and l.symbol = j.symbol and l.start_ms = j.start_ms
            where j.state = 'queued' order by j.created_utc, j.job_id""").fetchall()
    return [job_id for job_id, symbol, job_exchange in rows
            if (not symbols or symbol in symbols) and (exchange is None or job_exchange == exchange)]


def retry_failed(store: TradeStore, job_ids: Sequence[str] | None = None) -> int:
    with store.connect() as con:
        failed = [row[0] for row in con.execute("select job_id from jobs where state = 'failed'").fetchall()]
    chosen = [job_id for job_id in failed if not job_ids or job_id in job_ids]
    for job_id in chosen:
        set_state(store, job_id, "queued")
    return len(chosen)


# -- HTML conversion ------------------------------------------------------------------------


def _ms(text: str) -> int:
    moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return default


def report_point(report) -> int:
    basic, mrs2 = report.settings["basic"], report.settings["mrs2"]
    shift = round((1 - float(mrs2["ma_long"]["multiplier"])) * 10_000)
    return point_id(str(basic["time_frame"]), shift, int(mrs2["ma_long"]["len"]), int(mrs2["ma_close_long"]["len"]))


def report_tables(report, *, symbol: str, start: date, end: date, settings: RunSettings = RunSettings()):
    """(point id, trade columns, hourly rows, metrics columns) of one parsed MRS2 tester report."""
    if str(report.settings["basic"]["symbol"]) != symbol:
        raise JobQueueError(f"report symbol {report.settings['basic']['symbol']} is not {symbol}")
    expected_range = f"{start.isoformat()} - {end.isoformat()}"
    if str(report.metrics.get("Report range", "")).strip() != expected_range:
        raise JobQueueError(f"report range {report.metrics.get('Report range')!r} is not {expected_range!r}")
    pid = report_point(report)
    columns: dict[str, list] = {name: [] for name in TRADE_SCHEMA.names}
    opened = None
    for action in report.actions:
        if action["Action"] == "opened":
            if opened is not None:
                raise JobQueueError("an opened action without a close before the next open")
            opened = action
            continue
        if action["Action"] != "closed" or opened is None:
            raise JobQueueError(f"unexpected action {action['Action']!r}")
        qty, entry, exit_price = _number(opened["Size"]), _number(opened["Price"]), _number(action["Price"])
        fee_out = _number(action["Fee"])
        rate = fee_out / (qty * exit_price) if qty * exit_price else 0.0
        funding = _number(opened["Balance"]) + _number(action["PnL"]) - fee_out - _number(action["Balance"])
        values = {"point_id": pid, "entry_ms": _ms(opened["Timestamp"]), "exit_ms": _ms(action["Timestamp"]),
                  "entry_price": entry, "exit_price": exit_price, "qty": qty, "fee_in": _number(opened["Fee"]),
                  "fee_out": fee_out, "funding": funding,
                  "exit_kind": "market_end" if abs(rate - settings.taker_fee) < abs(rate - settings.maker_fee)
                  else "limit"}
        for name, value in values.items():
            columns[name].append(value)
        opened = None
    if opened is not None:
        raise JobQueueError("a position is open at the end of the report")
    start_ms = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp() * 1000)
    stamps = np.array([int(stamp) for stamp, _ in report.equity_series], dtype=np.int64)
    equity = np.array([float(value) for _, value in report.equity_series], dtype=np.float64)
    hourly = _hourly_aggregate(pid, stamps, equity, start_ms, end_ms, settings.lot)
    final = float(report.wallet_series[-1][1]) if report.wallet_series else settings.lot
    marked = final
    if columns["exit_kind"] and columns["exit_kind"][-1] == "market_end":
        before = int(np.searchsorted(stamps, columns["exit_ms"][-1], "left")) - 1
        marked = float(equity[before]) if before >= 0 else final
    gross = [q * (x - e) for q, x, e in zip(columns["qty"], columns["exit_price"], columns["entry_price"])]
    costs = [_number(action.get("Cost, % of margin balance")) for action in report.actions]
    entries = [value for action, value in zip(report.actions, costs) if action["Action"] == "opened"]
    metrics = {"point_id": [pid], "final_balance": [final], "marked_final": [marked], "trades": [len(gross)],
               "wins": [sum(1 for value in gross if value > 0)], "gross_profit": [sum(v for v in gross if v > 0)],
               "gross_loss": [-sum(v for v in gross if v < 0)],
               "fees": [sum(columns["fee_in"]) + sum(columns["fee_out"])],
               "max_drawdown": [_number(report.metrics.get("Max Drawdown, USDT"))],
               "max_drawdown_percent": [_number(report.metrics.get("Max Drawdown, %"))],
               "ulcer_index": [_number(report.metrics.get("Ulcer Index (hourly), %"))],
               "position_avg_percent": [sum(entries) / len(entries) if entries else 0.0],
               "position_max_percent": [max(costs, default=0.0)],
               "last_ms": [int(stamps[-1]) if len(stamps) else start_ms]}
    return pid, columns, hourly, metrics


# -- executor ---------------------------------------------------------------------------------


def run_hb_tester(bot_root: Path, timeout_s: float, log_path: Path) -> None:
    executable = Path(bot_root) / "hb_c.exe"
    if not executable.is_file():
        raise JobQueueError(f"tester executable not found: {executable}")
    with log_path.open("wb") as log:
        completed = subprocess.run([str(executable), "--run-tester"], cwd=str(bot_root), stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, timeout=timeout_s)
    if completed.returncode != 0:
        raise JobQueueError(f"tester exit code {completed.returncode}")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backup(bot_root: Path, backup: Path) -> dict[str, str | None]:
    """Move the strategy files and copy the config into the backup; returns {relative path: sha or None}."""
    strategies = bot_root / "tester" / "settings_strategy"
    if backup.exists():
        raise RestoreError(f"a backup from an earlier run is still present: {backup}")
    (backup / "settings_strategy").mkdir(parents=True)
    manifest: dict[str, str | None] = {}
    config = bot_root / "config_tester.json"
    if config.is_file():
        manifest["config_tester.json"] = _sha(config)
        shutil.copy2(config, backup / "config_tester.json")
    else:
        manifest["config_tester.json"] = None
    strategies.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    try:
        for path in sorted(strategies.iterdir()):
            if path.is_file():
                manifest[f"settings_strategy/{path.name}"] = _sha(path)
                shutil.move(str(path), backup / "settings_strategy" / path.name)
                moved.append(path.name)
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    except BaseException:
        _undo_backup(bot_root, backup)
        raise
    return manifest


def _undo_backup(bot_root: Path, backup: Path) -> None:
    """Put back the strategy files of a backup that never reached its manifest (nothing was replaced yet)."""
    strategies = bot_root / "tester" / "settings_strategy"
    folder = backup / "settings_strategy"
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            target = strategies / path.name
            if not target.exists():
                shutil.move(str(path), target)
            elif _sha(target) == _sha(path):
                # a move that copied the file but could not delete the locked original: the bot still has it
                path.unlink()
    left = sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []
    if left:
        raise RestoreError(f"strategy files left in an unfinished backup: {left}")
    shutil.rmtree(backup, ignore_errors=True)


def _restore(bot_root: Path, backup: Path, manifest: Mapping[str, str | None]) -> None:
    strategies = bot_root / "tester" / "settings_strategy"
    for path in strategies.iterdir():
        if path.is_file() and f"settings_strategy/{path.name}" not in manifest:
            path.unlink()
    for relative, digest in manifest.items():
        if relative == "config_tester.json":
            target = bot_root / "config_tester.json"
            if digest is None:
                target.unlink(missing_ok=True)
            else:
                shutil.copy2(backup / "config_tester.json", target)
        else:
            source = backup / relative
            if source.exists():
                shutil.move(str(source), strategies / Path(relative).name)
    for relative, digest in manifest.items():
        target = bot_root / "config_tester.json" if relative == "config_tester.json" else strategies / Path(relative).name
        actual = _sha(target) if target.is_file() else None
        if actual != digest:
            raise RestoreError(f"restore check failed for {relative}")
    extra = {path.name for path in strategies.iterdir() if path.is_file()} - {
        Path(relative).name for relative in manifest if relative != "config_tester.json"}
    if extra:
        raise RestoreError(f"unexpected strategy files after restore: {sorted(extra)}")
    shutil.rmtree(backup)


LOCK_NAME = ".executor.lock"
STOP_NAME = ".stop"


def _process_start(pid: int) -> str | None:
    try:
        return f"{psutil.Process(pid).create_time():.3f}"
    except (psutil.Error, ValueError, OverflowError):
        return None


def _owner_alive(lock: Path) -> bool:
    """The lock's owner (`<pid> <process start> <job id>`) is the same live process, not a reused pid."""
    try:
        owner = lock.read_text(encoding="ascii", errors="replace").split()
    except OSError:
        return False
    if len(owner) < 2 or not owner[0].isdigit():
        return False
    return int(owner[0]) != os.getpid() and _process_start(int(owner[0])) == owner[1]


def _claim(lock: Path, job_id: str) -> None:
    """The store's executor lock; a lock left by a dead process needs `recover` first."""
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise JobQueueError("another executor is running" if _owner_alive(lock) else
                            "an interrupted executor left its lock: run `tester_jobs.py recover`") from None
    os.write(handle, f"{os.getpid()} {_process_start(os.getpid())} {job_id}".encode("ascii"))
    os.close(handle)


def recover(store: TradeStore, bot_root: Path) -> dict:
    """After an interrupted executor (killed process, power loss): restore the tester's files from every job
    backup, mark interrupted jobs failed and remove a stale lock. Refuses while the lock's process is alive."""
    lock = jobs_root(store) / LOCK_NAME
    if lock.exists() and _owner_alive(lock):
        raise JobQueueError("another executor is running")
    restored, interrupted = [], []
    folders = sorted(path.parent for path in jobs_root(store).glob("*/backup")) if jobs_root(store).is_dir() else []
    if folders:
        with TesterTargetLock(Path(bot_root)):
            for folder in folders:
                backup = folder / "backup"
                manifest_path = backup / "manifest.json"
                if manifest_path.is_file():
                    _restore(Path(bot_root), backup, json.loads(manifest_path.read_text(encoding="utf-8")))
                else:
                    _undo_backup(Path(bot_root), backup)
                restored.append(folder.name)
    with store.connect(read_only=True) as con:
        running = [row[0] for row in con.execute("select job_id from jobs where state = 'running'").fetchall()]
    for job_id in sorted(set(running) | set(restored)):
        set_state(store, job_id, "failed", error="INTERRUPTED: the executor stopped during this job")
        interrupted.append(job_id)
    lock.unlink(missing_ok=True)
    return {"restored": restored, "interrupted": interrupted}


def run_job(store: TradeStore, job_id: str, bot_root: Path, *, timeout_s: float = 6 * 3600, keep_html: bool = False,
            run_tester: Callable[[Path, float, Path], None] = run_hb_tester,
            parse_report: Callable[[bytes], object] | None = None, executor: str = "local") -> dict:
    """Run one queued job on the local tester; the tester's own files are restored whatever happens."""
    parse = parse_report or (lambda data: parse_performance_report(data, allow_zero_activity=True,
                                                                   report_format_2026_10=True))
    bot_root = Path(bot_root)
    folder = jobs_root(store) / job_id
    job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    lock = jobs_root(store) / LOCK_NAME
    _claim(lock, job_id)
    error: BaseException | None = None
    try:
        # the bot's target lock is shared with every Panel tester caller: nobody else touches its files meanwhile
        with TesterTargetLock(bot_root):
            manifest = None
            backup = folder / "backup"
            try:
                set_state(store, job_id, "running", executor=executor)
                manifest = _backup(bot_root, backup)
                shutil.copy2(folder / "config_tester.json", bot_root / "config_tester.json")
                shutil.copy2(folder / job["strategy_file"],
                             bot_root / "tester" / "settings_strategy" / job["strategy_file"])
                run_tester(bot_root, timeout_s, folder / "tester.log")
            except BaseException as caught:  # restore first, report after (KeyboardInterrupt included)
                error = caught
            finally:
                if manifest is not None:
                    try:
                        _restore(bot_root, backup, manifest)
                    except Exception as restore_error:
                        _quiet_state(store, job_id, f"RESTORE: {restore_error}")
                        raise RestoreError(str(restore_error)) from restore_error
        if error is not None:
            _quiet_state(store, job_id, f"{type(error).__name__}: {error}")
            if isinstance(error, RestoreError) or not isinstance(error, Exception):
                raise error                 # an unfinished backup or an interrupt stops the executor
            return {"job_id": job_id, "state": "failed", "error": str(error)}
        try:
            summary = convert_reports(store, job, bot_root, keep_html=keep_html, parse=parse)
            set_state(store, job_id, "done")
        except Exception as caught:
            _quiet_state(store, job_id, f"{type(caught).__name__}: {caught}")
            return {"job_id": job_id, "state": "failed", "error": str(caught)}
        return {"job_id": job_id, "state": "done", **summary}
    finally:
        lock.unlink(missing_ok=True)


def _quiet_state(store: TradeStore, job_id: str, error: str) -> None:
    """Mark a job failed without letting a catalog problem hide the original outcome (`recover` fixes it later)."""
    try:
        set_state(store, job_id, "failed", error=error[:2000])
    except Exception:
        pass


def run_queue(store: TradeStore, bot_root: Path, *, symbols: Sequence[str] | None = None, limit: int = 0,
              timeout_s: float = 6 * 3600, keep_html: bool = False, exchange: str | None = None,
              prepare: Callable[[Mapping], str | None] | None = None,
              run: Callable[..., dict] = run_job, report: Callable[[dict], None] = lambda item: None) -> dict:
    """Run queued jobs one by one. Stops cleanly between jobs when `jobs/.stop` appears (the Panel's Stop) and
    recovers an interrupted executor first. `prepare(job)` returns an error text to skip a job (missing data)."""
    stop = jobs_root(store) / STOP_NAME          # cleared by the starter (Panel) and at the end, not here: a Stop
    report({"recover": recover(store, bot_root)})   # pressed while the process starts must still count
    done = failed = 0
    for job_id in queued_jobs(store, symbols, exchange):
        if stop.exists():
            stop.unlink(missing_ok=True)
            report({"stopped": True, "done": done, "failed": failed})
            return {"stopped": True, "done": done, "failed": failed}
        if limit and done + failed >= limit:
            break
        job = json.loads((jobs_root(store) / job_id / "job.json").read_text(encoding="utf-8"))
        problem = prepare(job) if prepare else None
        if problem:
            set_state(store, job_id, "failed", error=problem)
            report({"job_id": job_id, "state": "failed", "error": problem})
            failed += 1
            continue
        result = run(store, job_id, bot_root, timeout_s=timeout_s, keep_html=keep_html)
        report(result)
        if result["state"] == "done":
            done += 1
        else:
            failed += 1
    stop.unlink(missing_ok=True)
    return {"stopped": False, "done": done, "failed": failed}


def convert_reports(store: TradeStore, job: Mapping, bot_root: Path, *, keep_html: bool,
                    parse: Callable[[bytes], object]) -> dict:
    """Job report folder -> result tables; every block point exactly once; the HTML is deleted afterwards."""
    reports = Path(bot_root) / "tester" / "report" / job["name"]
    paths = sorted(reports.glob("*_run_*.html")) if reports.is_dir() else []
    start, end = date.fromisoformat(job["start"]), date.fromisoformat(job["end"])
    # a BingX job ran on the API symbol with the BingX fees (Panel stage 5c); Bybit jobs keep the defaults
    settings = RunSettings(**job["run_settings"]) if "run_settings" in job else RunSettings()
    found: dict[int, tuple] = {}
    for path in paths:
        pid, trades, hourly, metrics = report_tables(parse(path.read_bytes()), symbol=job.get("tester_symbol",
                                                     job["symbol"]), start=start, end=end, settings=settings)
        if pid in found:
            raise JobQueueError(f"two reports for point {pid}")
        found[pid] = (trades, hourly, metrics)
    expected = sorted(job["points"])
    if sorted(found) != expected:
        missing = sorted(set(expected) - set(found))
        extra = sorted(set(found) - set(expected))
        raise JobQueueError(f"reports do not match the block: missing {missing[:5]} ({len(missing)}), "
                             f"extra {extra[:5]} ({len(extra)})")
    tables = build_tables((found[pid][0] for pid in expected), (found[pid][1] for pid in expected),
                          (found[pid][2] for pid in expected))
    folder = jobs_root(store) / job["job_id"]
    for kind in ("trades", "hourly", "metrics"):
        (folder / f"result_{kind}.parquet").write_bytes(parquet_bytes(getattr(tables, kind)))
    if not keep_html and reports.is_dir():
        shutil.rmtree(reports)
    return {"reports": len(paths), "trades": tables.trades.num_rows}


# -- import ------------------------------------------------------------------------------------


def import_window(store: TradeStore, symbol: str, *, settings: RunSettings = RunSettings(),
                  exchange: str = "bybit") -> dict:
    """Combine the finished jobs of the pair's current selection into one `tester` segment."""
    window = window_segment(store, symbol, exchange)
    with store.connect(read_only=True) as con:
        plan = con.execute("select selection_sha256 from plans where exchange = ? and symbol = ? and start_ms = ? "
                           "and end_ms = ?", [exchange, symbol, window["start_ms"], window["end_ms"]]).fetchone()
        rows = [] if plan is None else con.execute(
            "select job_id, state from jobs where exchange = ? and symbol = ? and start_ms = ? and end_ms = ? and "
            "selection_sha256 = ? order by job_id", [exchange, symbol, window["start_ms"], window["end_ms"], plan[0]]
        ).fetchall()
    if not rows:
        return {"pair": symbol, "status": "NO_JOBS"}
    selection = plan[0]
    jobs = list(rows)
    if any(state != "done" for _, state in jobs):
        return {"pair": symbol, "status": "PENDING", "done": sum(state == "done" for _, state in jobs),
                "jobs": len(jobs)}
    parts = {kind: [pq.read_table(jobs_root(store) / job_id / f"result_{kind}.parquet") for job_id, _ in jobs]
             for kind in ("trades", "hourly", "metrics")}
    trades = pa.concat_tables(parts["trades"]).sort_by([("point_id", "ascending"), ("entry_ms", "ascending")])
    metrics = pa.concat_tables(parts["metrics"]).sort_by("point_id")
    price = store.read_hourly(window)
    price = price.filter(pc.equal(price.column("point_id"), PRICE_POINT))
    hourly = pa.concat_tables([price, *parts["hourly"]]).sort_by([("point_id", "ascending"), ("hour_ms", "ascending")])
    tables = SegmentTables(trades.cast(TRADE_SCHEMA), hourly.cast(HOURLY_SCHEMA), metrics.cast(METRICS_SCHEMA))
    # one segment per selection: a new selection of the same window supersedes the older tester segment
    key = SegmentKey(exchange, symbol, TESTER_SOURCE, _date(window["start_ms"]), _date(window["end_ms"]),
                     f"{TESTER_ENGINE}-{selection[:8]}")
    status = store.statuses(exchange, symbol).get(key.segment_id)
    if status == "active":
        return {"pair": symbol, "status": "EXISTS", "segment_id": key.segment_id}
    if status == "superseded":
        store.reactivate(key.segment_id)
    else:
        record = write_segment_files(store.root, key, tables, settings=settings, tick_size=window["tick_size"],
                                     qty_step=window["qty_step"], replace_unregistered=True)
        store.register(record, replace_superseded=True)
    with store.connect() as con:
        con.executemany("update jobs set result_segment = ?, updated_utc = ? where job_id = ?",
                        [[key.segment_id, utc_now(), job_id] for job_id, _ in jobs])
    return {"pair": symbol, "status": "IMPORTED", "segment_id": key.segment_id, "points": metrics.num_rows,
            "jobs": len(jobs)}


__all__ = ["RestoreError", "TESTER_ENGINE", "TESTER_SOURCE", "JobQueueError", "convert_reports", "import_window",
           "plan_jobs", "queued_jobs", "recover", "report_tables", "retry_failed", "run_hb_tester", "run_job",
           "run_queue", "set_state", "window_segment"]

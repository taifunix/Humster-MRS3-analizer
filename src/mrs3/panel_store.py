"""Panel control of the MRS2 trade store: one background CLI task at a time, its log, and a catalog summary.

Contract: docs/specs/2026-10-17-mrs2-history-store-and-tester-sampling.md («Stage 4 contract»).
The work itself runs in the store CLIs as a child process, so worker pools never start inside the Panel server.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Callable, Mapping, Sequence

import duckdb

from .error_sanitization import redact_local_paths

MODES = ("simulation", "tester")
ACTIONS = ("history", "plan", "run", "import", "ready")
SYMBOL = re.compile(r"^[A-Z0-9]{2,30}USDT$")
LOG_LINES = 300


class StoreTaskError(ValueError):
    """A request the Panel can show as is (no local paths)."""


def _redacted(value: object) -> object:
    """JSON result of a store CLI with every string path-redacted (results reach the Panel UI)."""
    if isinstance(value, str):
        return redact_local_paths(value)[:1000]
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redacted(item) for key, item in value.items()}
    return value


def _symbols(value: object) -> list[str]:
    if value in (None, ""):
        return []
    items = re.split(r"[\s,;]+", value) if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)) or not all(isinstance(item, str) for item in items):
        raise StoreTaskError("пары: список тикеров через запятую")
    symbols = list(dict.fromkeys(item.strip().upper() for item in items if item.strip()))
    bad = [symbol for symbol in symbols if not SYMBOL.match(symbol)]
    if bad:
        raise StoreTaskError(f"неизвестный формат тикера: {', '.join(bad[:5])}")
    return symbols


def store_summary(store_root: Path) -> dict:
    """Pairs with their active segments, tester jobs by state, recent runs and READY rows (read-only)."""
    catalog = Path(store_root) / "catalog.duckdb"
    if not catalog.is_file():
        return {"exists": False, "pairs": [], "jobs": {}, "runs": [], "ready_rows": 0}
    try:
        con = duckdb.connect(str(catalog), read_only=True)
    except duckdb.Error:
        return {"exists": True, "busy": True, "pairs": [], "jobs": {}, "runs": [], "ready_rows": 0}
    try:
        def epoch_day(ms: int | None) -> str | None:
            return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat()

        rows = con.execute("""
            select symbol, count(*) filter (where source = 'simulator'), min(start_ms) filter (where source = 'simulator'),
                   max(end_ms) filter (where source = 'simulator'), count(*) filter (where source = 'tester'),
                   max(end_ms) filter (where source = 'tester')
            from segments where status = 'active' group by symbol order by symbol""").fetchall()
        jobs = dict(con.execute("select state, count(*) from jobs group by state").fetchall())
        pair_jobs = {}
        for symbol, state, count in con.execute("select symbol, state, count(*) from jobs group by symbol, state").fetchall():
            pair_jobs.setdefault(symbol, {})[state] = count
        runs = con.execute("select run_id, started_utc, finished_utc, end_day, written, skipped, failed from runs "
                           "order by started_utc desc limit 5").fetchall()
        ready_rows = con.execute("select count(*) from ready").fetchone()[0]
    finally:
        con.close()
    pairs = [{"pair": symbol, "periods": periods, "first": epoch_day(first), "end": epoch_day(end),
              "tester_segments": tester, "tester_end": epoch_day(tester_end),
              "latest_tested": tester_end is not None and tester_end == end, "jobs": pair_jobs.get(symbol, {})}
             for symbol, periods, first, end, tester, tester_end in rows]
    return {"exists": True, "pairs": pairs, "jobs": jobs, "ready_rows": ready_rows,
            "runs": [dict(zip(("run_id", "started_utc", "finished_utc", "end_day", "written", "skipped", "failed"), row))
                     for row in runs]}


class StoreTaskService:
    """Runs one store CLI at a time in a child process and keeps its state for the Panel."""

    def __init__(self, repo_root: Path, config_path: Path, *, popen: Callable[..., subprocess.Popen] = subprocess.Popen,
                 python: str = sys.executable, today: Callable[[], date] = lambda: datetime.now(timezone.utc).date()):
        self.repo_root = Path(repo_root)
        self.config_path = Path(config_path)
        self._popen = popen
        self._python = python
        self._today = today
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._state: dict = {"state": "idle", "action": None, "log": [], "results": []}
        self._summary: dict | None = None

    def _store_root(self) -> Path:
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise StoreTaskError("конфиг не читается") from None
        root = (config.get("trade_store") or {}).get("root")
        if not root:
            raise StoreTaskError("в config не задан trade_store.root")
        return Path(root)

    def store_root(self) -> Path:
        """The configured trade store root (`trade_store.root`); StoreTaskError when it is not set."""
        return self._store_root()

    def command(self, action: str, payload: Mapping[str, object]) -> list[str]:
        """argv of the store CLI for an action (validated; no shell)."""
        if action not in ACTIONS:
            raise StoreTaskError(f"неизвестное действие: {action}")
        mode = payload.get("mode", "simulation")
        if mode not in MODES:
            raise StoreTaskError("режим: simulation или tester")
        symbols = _symbols(payload.get("pairs"))
        store = str(self._store_root())
        scripts = self.repo_root / "scripts"
        common = ["--store", store, "--config", str(self.config_path)]
        pairs = ["--pairs", ",".join(symbols)] if symbols else []
        if action == "history":
            argv = [str(scripts / "simulate_history.py"), *common, *pairs]
            end_day = payload.get("end_day")
            if end_day:
                try:
                    argv += ["--end-day", date.fromisoformat(str(end_day)).isoformat()]
                except ValueError:
                    raise StoreTaskError("дата конца: ГГГГ-ММ-ДД") from None
            if payload.get("download") is False:
                argv.append("--no-download")
        elif action in ("plan", "run", "import"):
            if mode != "tester":
                raise StoreTaskError("задания тестера доступны в режиме tester")
            argv = [str(scripts / "tester_jobs.py"), action, *common, *pairs]
            limit = payload.get("limit", 0)
            if isinstance(limit, bool) or not isinstance(limit, (int, float, str)):
                raise StoreTaskError("число заданий: целое ≥ 0")
            try:
                limit = int(limit or 0)
            except ValueError:
                raise StoreTaskError("число заданий: целое ≥ 0") from None
            if limit < 0:
                raise StoreTaskError("число заданий: целое ≥ 0")
            if action == "run" and limit:
                argv += ["--limit", str(limit)]
        else:
            out = Path(store) / "ready" / f"{self._today().isoformat()}_{mode}_{datetime.now(timezone.utc).strftime('%H%M%S')}"
            argv = [str(scripts / "ready_from_store.py"), *common, *pairs, "--out", str(out),
                    "--latest-source", "tester" if mode == "tester" else "simulator"]
        return [self._python, *argv]

    def start(self, action: str, payload: Mapping[str, object]) -> dict:
        argv = self.command(action, payload)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise StoreTaskError("задача хранилища уже выполняется")
            if action == "run":
                # a stop request left from an earlier run is cleared here, so a Stop pressed while the new run
                # starts up is not lost (the run itself never clears it at start)
                (self._store_root() / "jobs" / ".stop").unlink(missing_ok=True)
            self._state = {"state": "running", "action": action, "mode": payload.get("mode", "simulation"),
                           "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "log": [],
                           "results": [], "returncode": None}
            try:
                self._process = self._popen(argv, cwd=str(self.repo_root), stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                            encoding="utf-8", errors="replace")
            except OSError:
                self._state.update(state="failed", returncode=None, log=["процесс не запустился"])
                self._process = None
                raise StoreTaskError("не удалось запустить процесс задачи") from None
            self._thread = threading.Thread(target=self._follow, args=(self._process, self._state),
                                            name="store-task", daemon=True)
            self._thread.start()
        return self.status(summary=False)

    def _follow(self, process: subprocess.Popen, state: dict) -> None:
        for line in process.stdout:
            text = redact_local_paths(line.rstrip("\n"))[:1000]
            with self._lock:
                state["log"] = [*state["log"], text][-LOG_LINES:]
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    state["results"] = [*state["results"], _redacted(parsed)][-LOG_LINES:]
        code = process.wait()
        with self._lock:
            state.update(state="done" if code == 0 else "failed", returncode=code,
                         finished_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

    def stop(self) -> dict:
        """A tester run stops cooperatively between jobs (killing it would skip the restore of the tester's
        files); the other actions are terminated (their files are written atomically and rerun safely)."""
        with self._lock:
            process, action = self._process, self._state.get("action")
            running = process is not None and process.poll() is None
            if running and action == "run":
                stop_file = self._store_root() / "jobs" / ".stop"
                stop_file.parent.mkdir(parents=True, exist_ok=True)
                stop_file.write_text("stop", encoding="ascii")
                self._state["stopping"] = True
        if running and action != "run":
            process.terminate()
        return self.status(summary=False)

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def status(self, summary: bool = True) -> dict:
        with self._lock:
            task = json.loads(json.dumps(self._state, default=str))
        out = {"task": task, "modes": list(MODES), "actions": list(ACTIONS)}
        if summary:
            if task.get("state") == "running" and self._summary is not None:
                # no catalog connection while a store CLI writes it: the last summary is shown instead
                out["store"] = {**self._summary, "stale": True}
                return out
            try:
                self._summary = store_summary(self._store_root())
                out["store"] = self._summary
            except StoreTaskError as error:
                out["store"] = {"exists": False, "error": str(error)}
        return out


__all__ = ["ACTIONS", "MODES", "StoreTaskError", "StoreTaskService", "store_summary"]

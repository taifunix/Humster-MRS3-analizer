from __future__ import annotations

from datetime import date
import io
import json
from pathlib import Path
import threading

import pytest

from mrs3.mrs2_simulator import RunSettings
from mrs3.panel_store import StoreTaskError, StoreTaskService, store_summary
from mrs3.trade_store import HOURLY_SCHEMA, METRICS_SCHEMA, TRADE_SCHEMA, SegmentKey, SegmentTables, TradeStore


class FakeProcess:
    def __init__(self, argv, lines, code, gate):
        self.argv = argv
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
        self._code = code
        self._gate = gate
        self.terminated = False

    def wait(self):
        self._gate.wait(5)
        return self._code

    def poll(self):
        return None if not self._gate.is_set() else self._code

    def terminate(self):
        self.terminated = True
        self._gate.set()


def _service(tmp_path: Path, lines=(), code=0):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"trade_store": {"root": str(tmp_path / "store")}}), encoding="utf-8")
    started = []
    gate = threading.Event()

    def popen(argv, **kwargs):
        process = FakeProcess(argv, lines, code, gate)
        started.append(process)
        return process

    service = StoreTaskService(tmp_path / "repo", config, popen=popen, python="py",
                               today=lambda: date(2026, 10, 17))
    return service, started, gate


def test_commands_are_validated_and_follow_the_mode(tmp_path: Path):
    service, _, _ = _service(tmp_path)
    store = str(tmp_path / "store")
    history = service.command("history", {"pairs": "xomusdt, SNOWUSDT XOMUSDT", "end_day": "2026-10-17",
                                          "download": False})
    assert history[0] == "py" and history[1].endswith("simulate_history.py")
    assert history[2:] == ["--store", store, "--config", str(tmp_path / "config.json"), "--pairs", "XOMUSDT,SNOWUSDT",
                           "--end-day", "2026-10-17", "--no-download"]
    run = service.command("run", {"mode": "tester", "pairs": ["XOMUSDT"], "limit": 2})
    assert run[1].endswith("tester_jobs.py") and run[2] == "run" and run[-2:] == ["--limit", "2"]
    ready = service.command("ready", {"mode": "tester"})
    assert ready[1].endswith("ready_from_store.py") and ready[-2:] == ["--latest-source", "tester"]
    assert service.command("ready", {})[-1] == "simulator"
    for action, payload in (("plan", {"mode": "simulation"}), ("history", {"pairs": "XOM; rm -rf"}),
                            ("history", {"mode": "both"}), ("drop", {}), ("history", {"end_day": "17.10.2026"})):
        with pytest.raises(StoreTaskError):
            service.command(action, payload)


def test_one_task_at_a_time_with_log_and_parsed_results(tmp_path: Path):
    lines = ["plain line with D:\\secret\\path", json.dumps({"written": 3, "failed": 0})]
    service, started, gate = _service(tmp_path, lines)
    status = service.start("history", {"pairs": "XOMUSDT"})
    assert status["task"]["state"] == "running" and status["task"]["action"] == "history"
    with pytest.raises(StoreTaskError):
        service.start("ready", {})
    gate.set()
    service.wait(5)
    task = service.status()["task"]
    assert task["state"] == "done" and task["returncode"] == 0
    assert task["results"] == [{"written": 3, "failed": 0}]
    assert "secret" not in task["log"][0]
    assert service.start("ready", {})["task"]["action"] == "ready" and len(started) == 2


def test_a_failing_task_and_stop(tmp_path: Path):
    service, started, gate = _service(tmp_path, ["boom"], code=2)
    service.start("history", {})
    service.stop()
    service.wait(5)
    assert started[0].terminated and service.status()["task"]["state"] == "failed"


def test_store_summary_lists_pairs_jobs_and_runs(tmp_path: Path):
    assert store_summary(tmp_path / "none") == {"exists": False, "pairs": [], "jobs": {}, "runs": [], "ready_rows": 0}
    store = TradeStore(tmp_path / "store")
    empty = SegmentTables(TRADE_SCHEMA.empty_table(), HOURLY_SCHEMA.empty_table(), METRICS_SCHEMA.empty_table())
    for source, start, end in (("simulator", date(2026, 9, 7), date(2026, 9, 27)),
                               ("simulator", date(2026, 9, 27), date(2026, 10, 17)),
                               ("tester", date(2026, 9, 27), date(2026, 10, 17))):
        record = store.write_files(SegmentKey("bybit", "XOMUSDT", source, start, end, "e"), empty,
                                   settings=RunSettings(), tick_size=0.01, qty_step=0.01)
        store.register(record)
    with store.connect() as con:
        con.execute("insert into jobs (job_id, symbol, state) values ('a', 'XOMUSDT', 'done'), ('b', 'XOMUSDT', 'queued')")
    summary = store_summary(tmp_path / "store")
    [pair] = summary["pairs"]
    assert (pair["pair"], pair["periods"], pair["first"], pair["end"]) == ("XOMUSDT", 2, "2026-09-07", "2026-10-17")
    assert pair["latest_tested"] and pair["jobs"] == {"done": 1, "queued": 1}
    assert summary["jobs"] == {"done": 1, "queued": 1}


def test_panel_routes_start_stop_and_report_the_store_task(tmp_path: Path):
    import urllib.error
    import urllib.request
    from types import SimpleNamespace

    from mrs3.panel import _PanelServer

    service, started, gate = _service(tmp_path, [json.dumps({"written": 1})])
    server = _PanelServer(("127.0.0.1", 0), SimpleNamespace(store_tasks=lambda: service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        assert call("/api/v2/store/start", {"action": "plan", "mode": "simulation"}) == (
            400, {"error": "задания тестера доступны в режиме tester"})
        code, result = call("/api/v2/store/start", {"action": "history", "pairs": "XOMUSDT"})
        assert code == 200 and result["task"]["state"] == "running"
        gate.set()
        service.wait(5)
        code, status = call("/api/v2/store/status")
        assert code == 200 and status["task"]["results"] == [{"written": 1}] and status["store"]["exists"] is False
        assert call("/api/v2/store/stop", {})[0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_static_shell_has_the_store_card():
    root = Path(__file__).parents[1] / "src" / "mrs3" / "panel_web"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    testing = html.split('<section id="testing"', 1)[1].split('<section id="source-db"', 1)[0]
    assert testing.index('id="runner-remote"') < testing.index('id="store-local"')
    for name in ("mode", "pairs", "end-date", "run-limit", "download", "history", "plan", "run", "import", "ready",
                 "stop", "status", "log", "pairs-body"):
        assert f'id="store-{name}"' in testing
    assert testing.count("data-tester-only") == 3
    for endpoint in ("/api/v2/store/start", "/api/v2/store/status", "/api/v2/store/stop"):
        assert endpoint in js
    assert "'/api/v2/store/'," in js



def test_stop_of_a_tester_run_is_cooperative_and_inputs_are_checked(tmp_path: Path):
    service, started, gate = _service(tmp_path, [json.dumps({"out": "D:\\Users\\me\\store\\ready"})])
    service.start("run", {"mode": "tester"})
    service.stop()
    assert not started[0].terminated and (tmp_path / "store" / "jobs" / ".stop").is_file()
    assert service.status(summary=False)["task"]["stopping"] is True
    gate.set()
    service.wait(5)
    assert "Users" not in json.dumps(service.status()["task"]["results"])
    for limit in (-1, "two", True, [1]):
        with pytest.raises(StoreTaskError):
            service.command("run", {"mode": "tester", "limit": limit})


def test_a_process_that_cannot_start_leaves_a_failed_task(tmp_path: Path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"trade_store": {"root": str(tmp_path / "store")}}), encoding="utf-8")

    def broken(argv, **kwargs):
        raise OSError("no python")

    service = StoreTaskService(tmp_path, config, popen=broken)
    with pytest.raises(StoreTaskError):
        service.start("history", {})
    assert service.status(summary=False)["task"]["state"] == "failed"


def test_the_summary_is_not_read_while_a_task_runs(tmp_path: Path, monkeypatch):
    import mrs3.panel_store as panel_store
    service, _, gate = _service(tmp_path)
    reads = []
    monkeypatch.setattr(panel_store, "store_summary", lambda root: reads.append(root) or {"exists": False, "pairs": []})
    service.status()
    service.start("history", {})
    assert service.status()["store"]["stale"] is True and len(reads) == 1
    gate.set()
    service.wait(5)
    service.status()
    assert len(reads) == 2


def test_catalog_connections_wait_for_a_lock_held_elsewhere(tmp_path: Path, monkeypatch):
    import duckdb
    import mrs3.trade_store as trade_store
    store = TradeStore(tmp_path / "store")
    real = duckdb.connect
    attempts = []

    def busy(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise duckdb.IOException("Could not set lock on file")
        return real(*args, **kwargs)

    monkeypatch.setattr(trade_store.duckdb, "connect", busy)
    with store.connect() as con:
        assert con.execute("select 1").fetchone() == (1,)
    assert len(attempts) == 3
    attempts.clear()
    with pytest.raises(duckdb.IOException):
        store.connect(wait_s=0)



def test_a_stale_stop_request_is_cleared_when_a_run_starts(tmp_path: Path):
    service, _, gate = _service(tmp_path)
    stop = tmp_path / "store" / "jobs" / ".stop"
    stop.parent.mkdir(parents=True)
    stop.write_text("stop", encoding="ascii")
    service.start("run", {"mode": "tester"})
    assert not stop.exists()
    gate.set()
    service.wait(5)

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Callable, Mapping
from urllib.parse import parse_qs, urlparse
import uuid
import webbrowser


PANEL_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MRS3 Control Panel</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b1020;
      --panel: #141b2d;
      --panel-2: #1a2338;
      --line: #2b3854;
      --text: #eef3ff;
      --muted: #9aabc7;
      --blue: #5c8dff;
      --blue-2: #355dcc;
      --green: #43d19e;
      --red: #ff6b78;
      --amber: #f5bd54;
      --radius: 14px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: radial-gradient(circle at top right, #17244a 0, var(--bg) 34rem);
      color: var(--text);
      font: 14px/1.45 Inter, Segoe UI, Arial, sans-serif;
    }
    main { width: min(1180px, calc(100% - 28px)); margin: 26px auto 50px; }
    header { display: flex; justify-content: space-between; gap: 20px; align-items: end; margin-bottom: 18px; }
    h1 { margin: 0; font-size: clamp(24px, 4vw, 36px); letter-spacing: -0.04em; }
    h2, h3 { margin: 0 0 12px; }
    .subtitle, .muted { color: var(--muted); }
    .badge { padding: 7px 11px; border: 1px solid var(--line); border-radius: 999px; background: #10172a; }
    .grid { display: grid; grid-template-columns: 1.12fr .88fr; gap: 16px; }
    .card { background: rgba(20, 27, 45, .94); border: 1px solid var(--line); border-radius: var(--radius); padding: 18px; box-shadow: 0 18px 50px rgba(0,0,0,.18); }
    .stack { display: grid; gap: 13px; }
    label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; font-weight: 600; }
    input, select {
      width: 100%; border: 1px solid var(--line); border-radius: 9px; padding: 10px 11px;
      background: #0e1528; color: var(--text); outline: none;
    }
    input:focus, select:focus { border-color: var(--blue); box-shadow: 0 0 0 3px rgba(92,141,255,.13); }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .buttons { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 3px; }
    button {
      border: 0; border-radius: 9px; padding: 10px 14px; color: white; background: var(--blue-2);
      font-weight: 700; cursor: pointer;
    }
    button.primary { background: var(--blue); color: #081126; }
    button:hover { filter: brightness(1.08); }
    button:disabled { opacity: .45; cursor: not-allowed; }
    details { border-top: 1px solid var(--line); padding-top: 13px; }
    summary { cursor: pointer; font-weight: 700; margin-bottom: 13px; }
    .status-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
    .status-name { font-size: 20px; font-weight: 800; }
    .state { font-weight: 800; }
    .state.good { color: var(--green); } .state.bad { color: var(--red); } .state.work { color: var(--amber); }
    .bar { height: 13px; overflow: hidden; background: #0a1121; border: 1px solid var(--line); border-radius: 999px; margin: 16px 0 8px; }
    .bar > div { height: 100%; width: 0; background: linear-gradient(90deg, var(--blue), var(--green)); transition: width .35s ease; }
    .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 13px 0; }
    .stat { background: var(--panel-2); border-radius: 10px; padding: 10px; }
    .stat b { display: block; font-size: 19px; } .stat span { color: var(--muted); font-size: 11px; }
    .notice { min-height: 20px; margin: 8px 0; color: var(--amber); }
    pre { max-height: 285px; overflow: auto; white-space: pre-wrap; word-break: break-word; background: #080e1b; border: 1px solid var(--line); border-radius: 10px; padding: 12px; color: #cbd8ef; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { border-bottom: 1px solid var(--line); text-align: left; padding: 7px 5px; }
    .artifacts { display: flex; flex-wrap: wrap; gap: 8px; }
    .artifacts a { color: #bcd0ff; background: #111b34; border: 1px solid var(--line); border-radius: 8px; padding: 7px 9px; text-decoration: none; }
    @media (max-width: 850px) { .grid { grid-template-columns: 1fr; } .stats { grid-template-columns: 1fr 1fr; } }
  </style>
</head>
<body>
<main>
  <header>
    <div><h1>MRS3 Control Panel</h1><div class="subtitle">Локальное управление селектором и Hamster Bot Tester</div></div>
    <div class="badge">127.0.0.1 · отдельный процесс</div>
  </header>
  <div class="grid">
    <section class="card stack">
      <h2>Управление</h2>
      <label>Файл конфигурации<input id="config" type="text"></label>
      <h3>Пакетный тестер</h3>
      <label>Каталог JSON-стратегий<input id="strategies" value="output_long\strategies" type="text"></label>
      <label>Итоговый CSV<input id="output_csv" value="results\mrs3_long_results.csv" type="text"></label>
      <div class="buttons">
        <button id="planButton" onclick="startAction('tester-plan')">Проверить план</button>
        <button id="runButton" class="primary" onclick="startAction('tester-run')">Запустить тесты</button>
      </div>
      <details>
        <summary>Создать стратегии</summary>
        <div class="stack">
          <label>Исходный CSV<input id="input_csv" value="reports_history_bybit_long_day2.csv" type="text"></label>
          <div class="row">
            <label>Даты листинга<input id="dates" value="dates.xlsx" type="text"></label>
            <label>Шаблон JSON<input id="template" value="ADM_3_LONG_SHORT.json" type="text"></label>
          </div>
          <div class="row">
            <label>Сторона<select id="side"><option>LONG</option><option>SHORT</option></select></label>
            <label>Каталог результата<input id="select_output_dir" value="output_long" type="text"></label>
          </div>
          <button onclick="startAction('select')">Запустить селектор</button>
        </div>
      </details>
      <details>
        <summary>DD5 после теста</summary>
        <div class="stack">
          <label>CSV результатов<input id="results_csv" value="results\mrs3_long_results.csv" type="text"></label>
          <label>Audit XLSX<input id="audit_xlsx" value="output_long\audit.xlsx" type="text"></label>
          <label>Каталог исходных стратегий<input id="posttest_strategies" value="output_long\strategies" type="text"></label>
          <label>Каталог DD5<input id="posttest_output_dir" value="posttest_long" type="text"></label>
          <button onclick="startAction('posttest')">Собрать DD5</button>
        </div>
      </details>
      <div id="notice" class="notice"></div>
    </section>
    <section class="card">
      <div class="status-head">
        <div><div class="muted">Текущая операция</div><div id="operation" class="status-name">Нет задачи</div></div>
        <div id="state" class="state">IDLE</div>
      </div>
      <div class="bar"><div id="barFill"></div></div>
      <div id="progressText" class="muted">Ожидание запуска</div>
      <div class="stats">
        <div class="stat"><b id="submitted">0</b><span>отправлено</span></div>
        <div class="stat"><b id="running">0</b><span>в работе</span></div>
        <div class="stat"><b id="result">0</b><span>результат</span></div>
        <div class="stat"><b id="completed">0</b><span>проверено</span></div>
      </div>
      <h3>Активные стратегии</h3>
      <div style="max-height:220px;overflow:auto"><table><thead><tr><th>Имя</th><th>Статус</th><th>%</th></tr></thead><tbody id="activeRows"></tbody></table></div>
      <h3 style="margin-top:15px">Готовые файлы</h3><div id="artifacts" class="artifacts muted">Пока нет</div>
      <h3 style="margin-top:15px">Журнал</h3><pre id="logs">Панель готова.</pre>
    </section>
  </div>
</main>
<script>
const labels = {
  'tester-plan':'Проверка плана', 'tester-run':'Пакетное тестирование', 'select':'Создание стратегий', 'posttest':'DD5-анализ',
  'PRECHECK':'Предварительная проверка', 'STOPPED':'Бот остановлен', 'CLEAN':'Отчёты очищены', 'INSTALLED':'Стратегии установлены',
  'STARTED':'Бот запущен', 'VISIBLE':'Стратегии появились', 'SUBMITTED':'Все тесты отправлены', 'MONITORING':'Идёт тестирование',
  'RECONCILED':'Результаты сверены', 'CSV_COMMITTED':'CSV сохранён', 'STOPPED_FOR_CLEANUP':'Бот остановлен для очистки',
  'RAW_ARTIFACTS_REMOVED':'Временные отчёты удалены', 'COMPLETED':'Завершено', 'FAILED':'Ошибка'
};
let defaultsLoaded = false;
const value = id => document.getElementById(id).value.trim();
function payload(action) {
  const base = {action, config:value('config')};
  if (action === 'tester-plan') return {...base, strategies:value('strategies')};
  if (action === 'tester-run') return {...base, strategies:value('strategies'), output_csv:value('output_csv')};
  if (action === 'select') return {...base, input_csv:value('input_csv'), dates:value('dates'), template:value('template'), side:value('side'), output_dir:value('select_output_dir')};
  return {...base, results_csv:value('results_csv'), audit_xlsx:value('audit_xlsx'), strategies:value('posttest_strategies'), output_dir:value('posttest_output_dir')};
}
async function startAction(action) {
  const notice = document.getElementById('notice'); notice.textContent = 'Запуск…';
  try {
    const response = await fetch('/api/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload(action))});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || 'Ошибка запуска');
    notice.textContent = 'Задача запущена.'; render(body);
  } catch (error) { notice.textContent = error.message; }
}
function render(data) {
  if (!defaultsLoaded && data.defaults) { document.getElementById('config').value = data.defaults.config; defaultsLoaded = true; }
  const job = data.job;
  const buttons = document.querySelectorAll('button'); buttons.forEach(button => button.disabled = Boolean(job && job.running));
  if (!job) return;
  const workflow = job.workflow || {}; const progress = job.progress || {};
  const phase = workflow.state || progress.workflow_state || job.status;
  document.getElementById('operation').textContent = labels[job.action] || job.action;
  const state = document.getElementById('state'); state.textContent = labels[phase] || phase;
  state.className = 'state ' + (job.status === 'FAILED' || phase === 'FAILED' ? 'bad' : (job.running ? 'work' : 'good'));
  const expected = Number(progress.expected_count || job.expected_count || 0);
  const complete = Number(progress.completed_count || 0); const submitted = Number(progress.submitted_count || 0);
  const monitoring = Number(progress.polls || 0) > 0;
  const shown = monitoring ? complete : submitted; const percent = expected ? Math.min(100, shown * 100 / expected) : (job.running ? 3 : 100);
  document.getElementById('barFill').style.width = percent + '%';
  document.getElementById('progressText').textContent = expected ? (monitoring ? `${complete} завершено из ${expected} · ${percent.toFixed(1)}%` : `${submitted} отправлено из ${expected} · ${percent.toFixed(1)}%`) : (job.error || labels[phase] || phase);
  document.getElementById('submitted').textContent = submitted;
  document.getElementById('running').textContent = progress.running_count || 0;
  document.getElementById('result').textContent = progress.result_count || 0;
  document.getElementById('completed').textContent = complete;
  const tbody = document.getElementById('activeRows'); tbody.replaceChildren();
  for (const item of (progress.active || [])) {
    const row = document.createElement('tr');
    for (const text of [item.name, item.state, item.percent == null ? '—' : item.percent]) { const cell=document.createElement('td'); cell.textContent=text; row.appendChild(cell); }
    tbody.appendChild(row);
  }
  const artifactBox = document.getElementById('artifacts'); artifactBox.replaceChildren();
  const artifacts = job.artifacts || {};
  if (!Object.keys(artifacts).length) artifactBox.textContent = 'Пока нет';
  for (const [name, filename] of Object.entries(artifacts)) { const link=document.createElement('a'); link.href='/api/artifact?name='+encodeURIComponent(name); link.textContent=filename; artifactBox.appendChild(link); }
  document.getElementById('logs').textContent = (job.logs || []).join('\n') || 'Ожидание вывода…';
}
async function refresh() { try { const response=await fetch('/api/status', {cache:'no-store'}); render(await response.json()); } catch (_) {} }
refresh(); setInterval(refresh, 1200);
</script>
</body>
</html>
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class _Job:
    job_id: str
    action: str
    command: tuple[str, ...]
    artifacts: dict[str, Path]
    artifact_baseline: dict[str, tuple[int, int] | None]
    expected_count: int = 0
    status: str = "STARTING"
    started_at: str = field(default_factory=_utc_now)
    finished_at: str | None = None
    exit_code: int | None = None
    pid: int | None = None
    error: str | None = None
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=500))

    @property
    def running(self) -> bool:
        return self.status in {"STARTING", "RUNNING"}


class PanelController:
    def __init__(
        self,
        root: Path,
        default_config: Path,
        process_factory: Callable[..., object] = subprocess.Popen,
    ) -> None:
        self.root = root.resolve()
        self.default_config = self._path(default_config)
        self._process_factory = process_factory
        self._lock = threading.RLock()
        self._job: _Job | None = None

    def _path(self, value: str | Path) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return candidate.resolve()

    @staticmethod
    def _signature(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_size, stat.st_mtime_ns) if path.is_file() else None

    @staticmethod
    def _required(payload: Mapping[str, object], name: str) -> str:
        value = str(payload.get(name, "")).strip()
        if not value:
            raise ValueError(f"required field is empty: {name}")
        return value

    def _build_command(
        self, action: str, payload: Mapping[str, object]
    ) -> tuple[tuple[str, ...], dict[str, Path]]:
        config = self._path(self._required(payload, "config"))
        command = [sys.executable, "-m", "mrs3.cli", action, "--config", str(config)]
        artifacts: dict[str, Path] = {}
        if action in {"tester-plan", "tester-run"}:
            strategies = self._path(self._required(payload, "strategies"))
            command.extend(["--strategies", str(strategies)])
            if action == "tester-run":
                output = self._path(self._required(payload, "output_csv"))
                command.extend(["--output-csv", str(output)])
                artifacts = {
                    "output_csv": output,
                    "state": output.with_name(f"{output.stem}.state.json"),
                    "progress": output.with_name(f"{output.stem}.progress.json"),
                }
        elif action == "select":
            input_csv = self._path(self._required(payload, "input_csv"))
            dates = self._path(self._required(payload, "dates"))
            template = self._path(self._required(payload, "template"))
            side = self._required(payload, "side").upper()
            if side not in {"LONG", "SHORT"}:
                raise ValueError("side must be LONG or SHORT")
            output_dir = self._path(self._required(payload, "output_dir"))
            command.extend(
                [
                    "--input-csv",
                    str(input_csv),
                    "--dates",
                    str(dates),
                    "--template",
                    str(template),
                    "--side",
                    side,
                    "--output-dir",
                    str(output_dir),
                ]
            )
            artifacts = {
                "manifest": output_dir / "run_manifest.json",
                "audit": output_dir / "audit.xlsx",
            }
        elif action == "posttest":
            results_csv = self._path(self._required(payload, "results_csv"))
            audit_xlsx = self._path(self._required(payload, "audit_xlsx"))
            strategies = self._path(self._required(payload, "strategies"))
            output_dir = self._path(self._required(payload, "output_dir"))
            command.extend(
                [
                    "--results-csv",
                    str(results_csv),
                    "--audit-xlsx",
                    str(audit_xlsx),
                    "--strategies-dir",
                    str(strategies),
                    "--output-dir",
                    str(output_dir),
                ]
            )
            artifacts = {
                "workbook": output_dir / "posttest.xlsx",
                "manifest": output_dir / "posttest_manifest.json",
            }
        else:
            raise ValueError(f"unsupported action: {action}")
        return tuple(command), artifacts

    def start(self, action: str, payload: Mapping[str, object]) -> dict[str, object]:
        command, artifacts = self._build_command(action, payload)
        with self._lock:
            if self._job is not None and self._job.running:
                raise RuntimeError("another panel job is already running")
            job = _Job(
                job_id=uuid.uuid4().hex,
                action=action,
                command=command,
                artifacts=artifacts,
                artifact_baseline={
                    name: self._signature(path) for name, path in artifacts.items()
                },
            )
            self._job = job
        thread = threading.Thread(
            target=self._run_job,
            args=(job.job_id,),
            name=f"mrs3-panel-{action}",
            daemon=True,
        )
        thread.start()
        return self.snapshot()

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            if self._job is None or self._job.job_id != job_id:
                return
            job = self._job
            job.status = "RUNNING"
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        try:
            process = self._process_factory(
                list(job.command),
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
            )
            with self._lock:
                job.pid = int(getattr(process, "pid", 0)) or None
            output = getattr(process, "stdout", None)
            if output is not None:
                for raw_line in output:
                    line = str(raw_line).rstrip("\r\n")
                    if line:
                        with self._lock:
                            job.logs.append(line[:4000])
            exit_code = int(process.wait())
            with self._lock:
                job.exit_code = exit_code
                job.status = "SUCCEEDED" if exit_code == 0 else "FAILED"
                if exit_code != 0:
                    job.error = f"command exited with code {exit_code}"
        except BaseException as error:
            with self._lock:
                job.status = "FAILED"
                job.error = f"{type(error).__name__}: {error}"
                job.logs.append(job.error)
        finally:
            with self._lock:
                job.finished_at = _utc_now()

    @staticmethod
    def _read_json(path: Path | None) -> dict[str, object] | None:
        if path is None:
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            job = self._job
            if job is None:
                job_document = None
            else:
                current_artifacts = {
                    name: path
                    for name, path in job.artifacts.items()
                    if self._signature(path) != job.artifact_baseline.get(name)
                    and path.is_file()
                }
                state_path = current_artifacts.get("state")
                progress_path = current_artifacts.get("progress")
                job_document = {
                    "id": job.job_id,
                    "action": job.action,
                    "command": list(job.command),
                    "status": job.status,
                    "running": job.running,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                    "exit_code": job.exit_code,
                    "pid": job.pid,
                    "error": job.error,
                    "logs": list(job.logs),
                    "expected_count": job.expected_count,
                    "workflow": self._read_json(state_path),
                    "progress": self._read_json(progress_path),
                    "artifacts": {
                        name: path.name
                        for name, path in current_artifacts.items()
                    },
                }
        return {
            "defaults": {
                "root": str(self.root),
                "config": str(self.default_config),
            },
            "job": job_document,
        }

    def artifact(self, name: str) -> Path | None:
        with self._lock:
            if self._job is None:
                return None
            path = self._job.artifacts.get(name)
            baseline = self._job.artifact_baseline.get(name)
        if path is None or not path.is_file() or self._signature(path) == baseline:
            return None
        return path


class _PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], controller: PanelController
    ) -> None:
        self.controller = controller
        super().__init__(address, _PanelHandler)


class _PanelHandler(BaseHTTPRequestHandler):
    server: _PanelServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
        )
        self.end_headers()

    def _json(self, status: int, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(payload))
        self.wfile.write(payload)

    def _has_local_host(self) -> bool:
        host = self.headers.get("Host", "").casefold()
        port = self.server.server_port
        return host in {
            "127.0.0.1",
            f"127.0.0.1:{port}",
            "localhost",
            f"localhost:{port}",
            "[::1]",
            f"[::1]:{port}",
        }

    def do_GET(self) -> None:
        if not self._has_local_host():
            self._json(403, {"error": "local Host header required"})
            return
        parsed = urlparse(self.path)
        if parsed.path == "/":
            payload = PANEL_HTML.encode("utf-8")
            self._headers(200, "text/html; charset=utf-8", len(payload))
            self.wfile.write(payload)
            return
        if parsed.path == "/api/status":
            self._json(200, self.server.controller.snapshot())
            return
        if parsed.path == "/api/artifact":
            name = parse_qs(parsed.query).get("name", [""])[0]
            artifact = self.server.controller.artifact(name)
            if artifact is None:
                self._json(404, {"error": "artifact is not available"})
                return
            content_type = mimetypes.guess_type(artifact.name)[0] or "application/octet-stream"
            size = artifact.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{artifact.name.replace(chr(34), "")}"',
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with artifact.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    self.wfile.write(chunk)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._has_local_host():
            self._json(403, {"error": "local Host header required"})
            return
        if urlparse(self.path).path != "/api/start":
            self._json(404, {"error": "not found"})
            return
        content_type = self.headers.get("Content-Type", "").partition(";")[0]
        if content_type.strip().casefold() != "application/json":
            self._json(415, {"error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "invalid Content-Length"})
            return
        if length <= 0 or length > 65536:
            self._json(400, {"error": "JSON body must be between 1 and 65536 bytes"})
            return
        try:
            document = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(document, dict):
                raise ValueError("JSON body must be an object")
            action = str(document.get("action", ""))
            result = self.server.controller.start(action, document)
        except RuntimeError as error:
            self._json(409, {"error": str(error)})
            return
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self._json(400, {"error": str(error)})
            return
        self._json(202, result)


def create_panel_server(
    host: str, port: int, controller: PanelController
) -> ThreadingHTTPServer:
    if host.casefold() not in {"127.0.0.1", "localhost"}:
        raise ValueError("control panel must bind to a loopback host")
    if not 0 <= port <= 65535:
        raise ValueError("panel port must be between 0 and 65535")
    return _PanelServer((host, port), controller)


def serve_panel(
    host: str = "127.0.0.1",
    port: int = 8765,
    default_config: Path = Path("config.example.json"),
    *,
    open_browser: bool = True,
) -> None:
    root = Path.cwd().resolve()
    controller = PanelController(root, default_config)
    server = create_panel_server(host, port, controller)
    shown_host = "127.0.0.1" if host == "localhost" else host
    url = f"http://{shown_host}:{server.server_port}/"
    print(f"MRS3 Control Panel: {url}", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

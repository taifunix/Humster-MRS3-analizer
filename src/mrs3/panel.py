from __future__ import annotations

from collections import deque
import csv
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from hashlib import sha256
import inspect
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Callable, Mapping, Sequence
from urllib.parse import parse_qs, urlparse
import uuid
import webbrowser

_PANEL_WEB = Path(__file__).with_name("panel_web")

import duckdb

from .analysis_exports import export_analysis_run
from .analysis_strategies import (
    generate_analysis_strategies,
    generate_v6_analysis_strategies,
    load_validated_plateau_facts,
)
from .analysis_shortlist import CRITERIA, filter_analysis_candidates
from .analysis_filter_export import export_filter_audit
from .analysis_storage import (
    compare_analysis_runs,
    ensure_analysis_schema,
    list_surface_library,
    publish_analysis_run,
    require_canonical_operational_surface,
)
from .config import AlgorithmConfig
from .config import (
    DirectMaterializationSettings,
    DuckDBImportSettings,
    load_direct_materialization_settings,
    load_duckdb_import_settings,
    load_source_v6_import_settings,
    save_duckdb_import_settings,
)
from .duckdb_import import ImportJobResult, ImportPreflight, ImportProgress, ImportRequest, SnapshotProgress, import_html_tree, preflight_html_import
from .duckdb_source_schema import migrate_source_database
from .loader import load_listing_dates
from .duckdb_direct import (
    CANONICAL_GRID_VERSION,
    CANONICAL_MATERIALIZER_VERSION,
    POINT_MATERIALIZATION_SEMANTICS_VERSION,
    V2_AUDIT_SCHEMA_VERSION,
    DirectBuildRequest,
    DirectCommonInterval,
    DirectMaterializationError,
    DirectPreflight,
    DirectScope,
    READINESS_CONTRACT_VERSION,
    READINESS_MAX_SHIFT_BP,
    V2_GRID_CONTRACT_KIND,
    _CoverageScan,
    common_intervals_for_scopes,
    coverage_scan_direct,
    list_duckdb_direct_coverage,
    preflight_duckdb_direct,
    prepare_direct_surfaces,
    freeze_direct_preflights,
    replay_direct_preflights,
    canonical_point_materialization_config_hash,
    publish_direct_surfaces,
    run_panel_direct_build,
)
from .models import Side
from .pipeline import run_published_pipeline
from .performance_import import _canonical, _canonical_contract, _sha256
from .published_surface import load_published_surface
from .source_v6_importer import SourceV6WorkerFailure, import_source_v6, preflight_source_v6, source_v6_import_lock
from .source_v6_merge import (
    merge_source_v6,
    preflight_source_v6_merge,
)
from .source_v6_surface import (
    SOURCE_V6_EVENT_MODE,
    SOURCE_V6_EVENT_SCHEMA_VERSION,
    SOURCE_V6_FROZEN_DIGEST_ALGORITHM,
    SOURCE_V6_METRIC_SCHEMA_VERSION,
    SOURCE_V6_READINESS_SCHEMA_VERSION,
    SOURCE_V6_SURFACE_SCHEMA_VERSION,
    load_source_v6_pipeline_input,
    list_source_v6_analysis_runs,
    publish_surface_db,
    read_surface_db,
    run_source_v6_analysis,
    scan_surface_diagnostics,
    _canonical_json as _source_v6_canonical_json,
    _v6_listing_snapshot,
)
from .source_v6_analysis import export_plateau_report
from .source_v6_coverage import canonical_ready_intervals, coverage_csv, coverage_json, missing_cells, select_ready_interval
from .source_v6_materializer import materialize_source_v6
from .source_v6_surface_fresh import publish_multiscope_surface, read_multiscope_surface
from .source_v6_analysis_fresh import run_multiscope_analysis
from .panel_settings import (
    PanelSettingsError,
    bootstrap as panel_bootstrap,
    reload_settings as reload_panel_settings,
    save_settings as save_panel_settings,
    validate_settings as validate_panel_settings,
)
from .panel_jobs import PanelJobError, PanelJobRegistry
from .panel_remote_testing import RemoteTestingService, remote_testing_status
from .panel_remote_source_db import RemoteSourceDbExecutor, RemoteSourceDbError
from .panel_source_db import LocalSourceDbService
from .panel_source_jobs import LocalSourceDbJobRunner
from .panel_surfaces import LocalSurfacesService
from .panel_testing import LocalTestingService, PanelTestingError
from .fresh_analysis_strategies import generate_fresh_analysis_strategies, list_fresh_analysis_shortlist
from .panel_strategy_batch import LocalStrategyBatchService
from .panel_performance_dd5 import LocalPerformanceDd5Jobs, PerformanceDd5Request
from .runner.config import RunnerConfig


_DIRECT_MATERIALIZER_VERSION = CANONICAL_MATERIALIZER_VERSION
_DIRECT_POINT_CONFIG_HASH = canonical_point_materialization_config_hash(tuple(AlgorithmConfig().canonical_shifts_bp))
_TESTER_START_PATTERN = re.compile(r"^\[RUN (\d+)/(\d+)\] start\.")
_TESTER_PROGRESS_PATTERN = re.compile(
    r"^RUN (\d+)/(\d+) time=([^ ]+ [^ ]+) \(([0-9]+(?:\.[0-9]+)?)%\)"
)
_DIRECT_GENERIC_ERROR = "direct build failed"
_DIRECT_WINDOWS_PATH_PATTERN = re.compile(r"[A-Za-z]:[\\/]")
_DIRECT_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<!\w)[\\/][^\s\"')]+")


def _safe_direct_error(message: str | None) -> str | None:
    """Return a client-safe direct error, suppressing local path details."""
    if message is None:
        return None
    if _DIRECT_WINDOWS_PATH_PATTERN.search(message) or _DIRECT_ABSOLUTE_PATH_PATTERN.search(message):
        return _DIRECT_GENERIC_ERROR
    return message


def _direct_error_message(error: BaseException) -> str:
    if isinstance(error, DirectMaterializationError):
        message = _safe_direct_error(str(error))
        if message and message.strip():
            return message
    return _DIRECT_GENERIC_ERROR


def _normalise_tester_log_line(line: str) -> str | None:
    """Return concise UI-safe tester progress while retaining raw output on disk."""
    if "\ufffd" in line or re.search(r"[\u0400-\u04ff]", line):
        return None
    if match := _TESTER_START_PATTERN.match(line):
        return f"TESTER RUN: {match.group(1)}/{match.group(2)} started"
    if match := _TESTER_PROGRESS_PATTERN.match(line):
        return (
            f"TESTER PROGRESS: run {match.group(1)}/{match.group(2)} "
            f"at {match.group(4)}% ({match.group(3)})"
        )
    if match := re.fullmatch(r"loaded (\d+) API keys", line, flags=re.IGNORECASE):
        return f"TESTER CONFIG: {match.group(1)} API keys loaded"
    if match := re.fullmatch(r"loaded (\d+) settings files", line, flags=re.IGNORECASE):
        return f"TESTER CONFIG: {match.group(1)} settings files loaded"
    if line.startswith("Interactive chart generated:"):
        return f"REPORT READY: {Path(line.removeprefix('Interactive chart generated:').strip()).name}"
    return line


def _tester_plan_summary(output: str) -> dict[str, object] | None:
    try:
        document = json.loads(output)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, dict):
        return None
    total = document.get("expected_count")
    reusable = document.get("resume_completed_count", 0)
    remaining = document.get("resume_remaining_names")
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or isinstance(reusable, bool)
        or not isinstance(reusable, int)
        or not isinstance(remaining, list)
        or not all(isinstance(name, str) for name in remaining)
    ):
        return None
    prepared = len(remaining)
    if total < 0 or reusable < 0 or reusable + prepared != total:
        return None
    return {
        "total": total,
        "reusable": reusable,
        "prepared": prepared,
        "mode": "RESUME" if reusable else "CLEAN",
    }


_BROWSE_FILE_TYPES: dict[str, tuple[tuple[str, str], ...]] = {
    "csv": (("CSV files", "*.csv"),),
    "duckdb": (("DuckDB files", "*.duckdb;*.db"),),
    "dates": (("Listing dates", "*.csv;*.xlsx"),),
    "template": (("Strategy template", "*.json"),),
    "config": (("Configuration", "*.json"),),
    "results_csv": (("Result CSV", "*.csv"),),
    "audit_xlsx": (("Audit workbook", "*.xlsx"),),
}


def _native_browse(kind: str, multiple: bool) -> tuple[Path, ...]:
    """Show a native chooser only after an explicit loopback UI request."""
    if kind == "directory":
        multiple = False
    elif kind not in _BROWSE_FILE_TYPES:
        raise ValueError(f"unsupported browse kind: {kind}")
    try:
        import tkinter as tk
        from tkinter import filedialog

        window = tk.Tk()
        window.withdraw()
        window.attributes("-topmost", True)
        try:
            if kind == "directory":
                selected = filedialog.askdirectory(parent=window, mustexist=True)
                values = (selected,) if selected else ()
            elif multiple:
                values = tuple(
                    filedialog.askopenfilenames(
                        parent=window, filetypes=_BROWSE_FILE_TYPES[kind]
                    )
                )
            else:
                selected = filedialog.askopenfilename(
                    parent=window, filetypes=_BROWSE_FILE_TYPES[kind]
                )
                values = (selected,) if selected else ()
        finally:
            window.destroy()
    except Exception as error:
        raise ValueError(f"native file chooser is unavailable: {error}") from error
    return tuple(Path(value).resolve() for value in values)


def _stream_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


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
      font: 14px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-optical-sizing: auto;
    }
    main { width: min(1180px, calc(100% - 28px)); margin: 26px auto 50px; }
    header { display: flex; justify-content: space-between; gap: 20px; align-items: end; margin-bottom: 18px; }
    h1 { margin: 0; font-size: clamp(24px, 4vw, 36px); letter-spacing: -0.04em; }
    h2, h3 { margin: 0 0 12px; }
    .subtitle, .muted { color: var(--muted); }
    .badge { padding: 7px 11px; border: 1px solid var(--line); border-radius: 999px; background: #10172a; }
    .path-control { display: flex; gap: .45rem; align-items: center; }
    .path-control input { min-width: 0; flex: 1; }
    .secondary { background: #243452; color: var(--text); }
    .tablist { display: flex; gap: .35rem; overflow-x: auto; padding: .3rem; margin-bottom: 1rem; border-radius: 12px; background: rgba(10, 17, 33, .72); }
    [role="tab"] { flex: 1 0 max-content; background: transparent; color: var(--text); }
    [role="tab"][aria-selected="true"] { background: var(--blue); color: #081126; }
    button:focus-visible, input:focus-visible, select:focus-visible { outline: 3px solid #bcd0ff; outline-offset: 2px; }
    [role="tabpanel"] { animation: panel-in .16s ease-out; }
    .source-note { margin: 0; color: var(--muted); }
    .workflow-card + .workflow-card { margin-top: 1rem; }
    .queued { color: var(--amber); font-weight: 800; }
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
    .direct-unavailable { color: var(--red); font-weight: 600; }
    .bar { height: 13px; overflow: hidden; background: #0a1121; border: 1px solid var(--line); border-radius: 999px; margin: 16px 0 8px; }
    .bar > div { height: 100%; width: 0; background: linear-gradient(90deg, var(--blue), var(--green)); transition: width .35s ease; }
    .stats { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; margin: 13px 0; }
    [hidden] { display: none !important; }
    .stat { background: var(--panel-2); border-radius: 10px; padding: 10px; }
    .stat b { display: block; font-size: 19px; } .stat span { color: var(--muted); font-size: 11px; }
    .notice { min-height: 20px; margin: 8px 0; color: var(--amber); }
    pre { max-height: 285px; overflow: auto; white-space: pre-wrap; word-break: break-word; background: #080e1b; border: 1px solid var(--line); border-radius: 10px; padding: 12px; color: #cbd8ef; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { border-bottom: 1px solid var(--line); text-align: left; padding: 7px 5px; }
    .artifacts { display: flex; flex-wrap: wrap; gap: 8px; }
    .artifacts a { color: #bcd0ff; background: #111b34; border: 1px solid var(--line); border-radius: 8px; padding: 7px 9px; text-decoration: none; }
    .decision-dashboard { margin-top: 1rem; border-top: 1px solid var(--line); padding-top: 1rem; }
    .decision-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .7rem; }
    .decision-card { background: #111a2e; border: 1px solid var(--line); border-radius: 11px; padding: .8rem; }
    .decision-card h4 { margin: 0; font-size: .92rem; }
    .decision-state { margin: .3rem 0 .55rem; color: var(--amber); font-size: .76rem; font-weight: 800; letter-spacing: .04em; }
    .decision-state.good { color: var(--green); } .decision-state.bad { color: var(--red); }
    .decision-metrics { display: grid; grid-template-columns: 1fr 1fr; gap: .35rem; }
    .decision-metric { color: var(--muted); font-size: .72rem; }
    .decision-metric b { display: block; color: var(--text); font-size: 1rem; }
    .decision-details { margin: .55rem 0 0; padding-left: 1rem; color: var(--muted); font-size: .76rem; }
    .shortlist-counters { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 12px 0; }
    .shortlist-counter { background: var(--panel-2); border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px; }
    .shortlist-counter b { display: block; color: var(--text); font-size: 1.15rem; }
    .shortlist-counter span { color: var(--muted); font-size: .68rem; font-weight: 700; letter-spacing: .06em; }
    .shortlist-table-wrap { overflow: auto; max-height: 470px; border: 1px solid var(--line); border-radius: 10px; background: #0d1425; }
    #shortlist_table { min-width: 930px; margin: 0; }
    #shortlist_table th { position: sticky; top: 0; z-index: 1; background: #17223a; color: #cbd8ef; font-size: .7rem; letter-spacing: .04em; text-transform: uppercase; }
    #shortlist_table td { vertical-align: top; font-size: .76rem; }
    #shortlist_table tbody tr:hover { background: rgba(92,141,255,.08); }
    .shortlist-status { font-weight: 800; white-space: nowrap; }
    .shortlist-status.ready { color: var(--green); }
    .shortlist-status.deferred { color: var(--amber); }
    .shortlist-structure { color: var(--text); font-weight: 700; }
    .shortlist-group { display: block; color: var(--muted); font-size: .68rem; font-weight: 400; }
    .shortlist-orders { min-width: 420px; color: #cbd8ef; line-height: 1.55; }
    .shortlist-order { display: block; white-space: nowrap; }
    .shortlist-empty { padding: 18px; color: var(--muted); text-align: center; }
    .coverage-review { display: grid; gap: 12px; margin: 14px 0 10px; }
    .coverage-review-head { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
    .coverage-review-title { font-size: 15px; font-weight: 700; letter-spacing: -.02em; }
    .coverage-review-note { color: var(--muted); font-size: 12px; }
    .coverage-group { border: 1px solid rgba(188, 208, 255, .12); border-radius: 12px; background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.015)); overflow: hidden; }
    .coverage-group-head { display: flex; justify-content: space-between; gap: 10px; padding: 10px 12px; background: rgba(255,255,255,.03); border-bottom: 1px solid rgba(188, 208, 255, .08); }
    .coverage-group-head b { font-size: 13px; letter-spacing: -.01em; }
    .coverage-group-head span { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
    .coverage-table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    .coverage-table th { color: var(--muted); font-size: 11px; letter-spacing: .06em; text-transform: uppercase; }
    .coverage-table th, .coverage-table td { padding: 9px 12px; border-bottom: 1px solid rgba(188, 208, 255, .06); vertical-align: top; }
    .coverage-table tr:last-child td { border-bottom: 0; }
    .coverage-check { width: 42px; text-align: center; }
    .coverage-tf { width: 80px; white-space: nowrap; font-weight: 700; }
    .coverage-interval { width: 240px; color: #dce7ff; }
    .coverage-gap { color: var(--muted); }
    .coverage-gap.bad { color: #ffb1ba; }
    .coverage-empty { padding: 14px 12px; color: var(--muted); }
    @media (max-width: 650px) { .shortlist-counters { grid-template-columns: 1fr 1fr; } }
    @keyframes panel-in { from { opacity: .55; } to { opacity: 1; } }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: .01ms !important; transition-duration: .01ms !important; } }
    @media (prefers-reduced-transparency: reduce) { .card, .tablist { background: var(--panel); backdrop-filter: none; } }
    @media (prefers-contrast: more) { .card, .tablist, input, select { border-color: #fff; } }
    @media (max-width: 850px) { .grid { grid-template-columns: 1fr; } .stats, .decision-grid { grid-template-columns: 1fr 1fr; } }
    #panel-candidates .workflow-card:first-of-type { display: none; }
  </style>
</head>
<body>
<main>
  <header>
    <div><h1>MRS3 Control Panel</h1><div class="subtitle">Локальное управление селектором и Hamster Bot Tester</div></div>
    <div class="badge">127.0.0.1 · отдельный процесс</div>
  </header>
  <div class="tablist" role="tablist" aria-label="Рабочие разделы MRS3">
    <button role="tab" id="tab-csv-source" aria-selected="false" aria-controls="panel-csv-source" tabindex="-1" hidden>Legacy CSV source</button>
    <button role="tab" id="tab-duckdb-source" aria-selected="false" aria-controls="panel-duckdb-source" tabindex="-1">1–5. Import → surface → analysis → JSON</button>
    <button role="tab" id="tab-candidates" aria-selected="false" aria-controls="panel-candidates" tabindex="-1">6–8. Test plan → tests → DD5</button>
    <button role="tab" id="tab-portfolio" aria-selected="false" aria-controls="panel-portfolio" tabindex="-1">Анализатор портфелей</button>
    <button role="tab" id="tab-settings" aria-selected="false" aria-controls="panel-settings" tabindex="-1">Настройки</button>
  </div>
  <div class="grid">
    <section class="card stack">
      <section role="tabpanel" id="panel-csv-source" aria-labelledby="tab-csv-source" hidden>
        <h2>MRS2 · CSV</h2><p class="source-note">Точный UTC-интервал; PointEventCount = TotalTrades. Этот пакет нельзя смешивать с DuckDB-пакетом.</p>
        <div class="stack workflow-card">
          <label>CSV-файлы (через ;)<div class="path-control"><input id="source_csv_files" value="reports_history_bybit_long_day2.csv" type="text"><button type="button" class="secondary" onclick="browse('source_csv_files','csv',true)">Выбрать…</button></div></label>
          <fieldset class="row"><legend>Окно UTC</legend><label>Начало<input id="csv_start" value="2026-07-15T00:00:00Z" type="text"></label><label>Конец<input id="csv_end" value="2026-08-06T00:00:00Z" type="text"></label></fieldset>
          <div class="buttons"><button data-runnable="true" class="primary" onclick="startAction('source-csv')">Собрать CSV-пакет</button><span class="badge">legacy_trades_proxy</span></div>
        </div>
      </section>
      <section role="tabpanel" id="panel-duckdb-source" aria-labelledby="tab-duckdb-source">
        <h2>MRS2 · DuckDB</h2><p class="source-note">Данные read-only: учитываются только полностью закрытые циклы в [start, end), audit фиксирует исключения.</p>
        <div class="stack workflow-card">
          <label>DuckDB v4<div class="path-control"><input id="source_duckdb" value="mrs3_parallel_compact_v4.duckdb" type="text"><button type="button" class="secondary" onclick="browse('source_duckdb','duckdb',false)">Выбрать…</button></div></label>
          <fieldset class="row"><legend>Окно UTC</legend><label>Начало<input id="duckdb_start" value="2026-07-15T00:00:00Z" type="text"></label><label>Конец<input id="duckdb_end" value="2026-08-06T00:00:00Z" type="text"></label></fieldset>
          <label>HTML-каталог для верификации (необязательно)<div class="path-control"><input id="verify_html_root" value="" type="text"><button type="button" class="secondary" onclick="browse('verify_html_root','directory',false)">Выбрать…</button></div></label>
          <label>HTML-выборка (3–5)<input id="verification_sample_count" value="3" type="number" min="3" max="5" step="1"></label>
          <div class="buttons"><button data-runnable="true" class="primary" onclick="startAction('source-duckdb')">Собрать DuckDB-пакет</button><span class="badge">real_independent_events</span></div>
          <details><summary>HTML → source DuckDB import</summary><div class="stack"><label>HTML root<div class="path-control"><input id="import_html_root" type="text"><button type="button" class="secondary" onclick="browse('import_html_root','directory',false)">Browse…</button></div></label><div class="buttons"><button type="button" onclick="duckdbPreflight()">Preflight</button><button type="button" class="primary" onclick="duckdbImport()">Start import</button><button type="button" onclick="duckdbCancel()">Cancel</button></div><div id="duckdbImportStatus" aria-live="polite" class="muted">No import job.</div><div class="stats"><div class="stat"><b id="import_parsed">0</b><span>parsed</span></div><div class="stat"><b id="import_inserted">0</b><span>inserted</span></div><div class="stat"><b id="import_replaced">0</b><span>replaced</span></div><div class="stat"><b id="import_identical">0</b><span>identical</span></div><div class="stat"><b id="import_ambiguous">0</b><span>ambiguous</span></div><div class="stat"><b id="import_quarantined">0</b><span>quarantined</span></div></div></div></details>
          <details open><summary>Immutable DUCKDB_DIRECT surface</summary><div class="stack">
            <fieldset class="row"><legend>UTC window</legend><label>Start<input id="direct_start" type="datetime-local"></label><label>End<input id="direct_end" type="datetime-local"></label></fieldset>
            <div class="row"><label>Side<select id="direct_side" onchange="applyWorkflowDefaults(true)"><option>LONG</option><option>SHORT</option></select></label><label>Symbols (; separated)<input id="direct_symbols" type="text" placeholder="BTCUSDT;ETHUSDT"></label></div>
            <div class="source-note">Shifts: <output id="direct_shifts">Auto-detected after coverage check.</output><br>All observed shifts that cover the selected UTC window are frozen into the surface contract automatically.</div>
            <div class="buttons"><button type="button" onclick="directPreflight()">Check coverage</button><button type="button" class="primary" onclick="directBuild()">Build surface</button><button type="button" onclick="directCancel()">Cancel</button></div>
            <div class="source-note">Coverage derives UTC intervals and side from checked Pair + Side + TF rows; manual UTC and Side fields do not constrain the coverage-token workflow.</div>
            <div id="directCoverage" role="note" class="muted">Coverage review appears in the right panel after preflight.</div><div id="directStatus" class="muted" aria-live="polite">No direct build.</div><div id="directArtifacts" class="artifacts muted">Coverage artifacts appear after a successful check.</div>
          </div></details>
          <details open><summary>Source v6 stitched surfaces</summary><div class="stack" id="sourceV6Workflow">
            <label>HTML reports<div class="path-control"><input id="source_v6_root" type="text"><button type="button" class="secondary" onclick="browse('source_v6_root','directory',false)">Browse…</button></div></label>
            <label>Fresh v6 database<div class="path-control"><input id="source_v6_database" value="Output/source-v6.duckdb" type="text"><button type="button" class="secondary" onclick="browse('source_v6_database','duckdb',false)">Browse…</button></div></label>
            <fieldset class="row"><legend>READY scopes</legend><label>Pair|Side|TF<select id="source_v6_scope" multiple size="5"></select></label><label>Start<input id="source_v6_start_date" type="datetime-local"></label><label>End<input id="source_v6_end_date" type="datetime-local"></label></fieldset>
            <div class="buttons"><button type="button" onclick="sourceV6Preflight()">Preflight</button><button type="button" class="primary" onclick="sourceV6Start()">Start Import → Surface</button><button type="button" onclick="sourceV6Cancel()">Cancel</button><button type="button" onclick="sourceV6Library()">Сведения</button><button type="button" onclick="sourceV6Gaps()">Download gaps</button><button type="button" onclick="sourceV6Export()">Plateau report</button></div>
            <details><summary>Compact Source v6 merge-only</summary><div class="stack"><label>Input databases (semicolon-separated)<input id="source_v6_merge_inputs" type="text"></label><label>Fresh merge target<input id="source_v6_merge_target" type="text"></label><div class="buttons"><button type="button" onclick="sourceV6MergePreflight()">Merge preflight</button><button type="button" class="primary" onclick="sourceV6MergeStart()">Start merge</button><button type="button" onclick="sourceV6MergeCancel()">Cancel merge</button></div></div></details>
            <label>Published surface<input id="source_v6_surface_path" type="text" readonly></label><label>Plateau output directory<input id="source_v6_export_dir" value="Output/source-v6-plateau" type="text"></label>
            <label>Published surface to analyze<select id="source_v6_surface_select"></select></label>
            <div class="row"><label>Listing dates snapshot<input id="source_v6_analysis_dates" type="text"></label><label>Analysis config<input id="source_v6_analysis_config" type="text"></label></div>
            <label>Algorithm version<input id="source_v6_algorithm_version" value="0.7-canonical-phase1" type="text"></label>
            <div class="buttons"><button type="button" class="primary" onclick="sourceV6Analyze()">Analyze v6 surface</button><button type="button" onclick="sourceV6AnalysisCancel()">Cancel analysis</button></div>
            <progress id="source_v6_progress" max="1" value="0"></progress><div id="source_v6_status" class="muted" aria-live="polite">No Source v6 operation.</div><div id="source_v6_library" class="artifacts muted"></div>
          </div></details>
          <details><summary>Analysis Library</summary><div class="stack">
            <div class="row"><label>Side<select id="analysis_side"><option value="">Any</option><option>LONG</option><option>SHORT</option></select></label><label>Build mode<select id="analysis_build_mode"><option value="">Any</option><option>DUCKDB_DIRECT</option></select></label></div><label>Symbol<input id="analysis_symbol" type="text"></label>
            <div class="row"><label>Period start<input id="analysis_period_start" type="datetime-local"></label><label>Period end<input id="analysis_period_end" type="datetime-local"></label></div>
            <div class="row"><label>Parent surface<input id="analysis_parent" type="text"></label><label>Source hash<input id="analysis_source_hash" type="text"></label></div>
            <div class="buttons"><button id="analysis_initialize" type="button" onclick="analysisInitialize()">Initialize / migrate v4</button><button type="button" onclick="analysisRefresh()">Refresh library</button></div>
            <div id="analysis_schema_status" class="muted" aria-live="assertive">Analysis schema status is unknown.</div>
            <label>Surface ID<input id="analysis_surface_id" type="text"></label><label>Run ID<input id="analysis_run_id" type="text"></label>
            <div class="stats"><div class="stat"><b id="analysis_unique">—</b><span>unique points</span></div><div class="stat"><b id="analysis_economic">—</b><span>economic eligible</span></div><div class="stat"><b id="analysis_event">—</b><span>event eligible</span></div><div class="stat"><b id="analysis_plateaus">—</b><span>plateaus</span></div><div class="stat"><b id="analysis_ready">—</b><span>READY</span></div></div>
            <label>Listing dates<div class="path-control"><input id="analysis_dates" type="text"><button type="button" class="secondary" onclick="browse('analysis_dates','dates',false)">Browse…</button></div></label>
            <label>Algorithm config<div class="path-control"><input id="analysis_config" type="text"><button type="button" class="secondary" onclick="browse('analysis_config','config',false)">Browse…</button></div></label>
            <label>Export directory<div class="path-control"><input id="analysis_output" type="text"><button type="button" class="secondary" onclick="browse('analysis_output','directory',false)">Browse…</button></div></label>
            <div class="row"><label>Left run<input id="analysis_left_run" type="text"></label><label>Right run<input id="analysis_right_run" type="text"></label></div>
            <div class="buttons"><button type="button" onclick="analysisRefine()">Refine</button><button type="button" class="primary" onclick="analysisRerun()">Re-run analysis</button><button type="button" onclick="analysisCompare()">Compare periods</button><button type="button" onclick="analysisExport()">Export</button></div>
            <div id="analysisLibrary"></div><div id="analysisStatus" class="muted" aria-live="polite">No analysis selected.</div>
            <hr><h3>5. JSON strategies from this analysis run</h3><p class="source-note">Only READY candidates from the selected immutable run. The bot is not started automatically.</p>
            <label>Strategy template<div class="path-control"><input id="analysis_template" value="ADM_3_LONG_SHORT.json" type="text"><button type="button" class="secondary" onclick="browse('analysis_template','template',false)">Browse…</button></div></label>
            <label>Strategy output directory<div class="path-control"><input id="analysis_strategy_output" value="Output\\strategies" type="text"><button type="button" class="secondary" onclick="browse('analysis_strategy_output','directory',false)">Browse…</button></div></label>
            <div class="shortlist-counters" aria-live="polite" aria-label="Shortlist counts"><div class="shortlist-counter"><b id="shortlist_all">0</b><span>ALL CANDIDATES</span></div><div class="shortlist-counter"><b id="shortlist_ready">0</b><span>READY</span></div><div class="shortlist-counter"><b id="shortlist_deferred">0</b><span>DEFERRED</span></div><div class="shortlist-counter"><b id="shortlist_comparable">0</b><span>COMPARABLE</span></div></div>
            <div class="row"><label>Pair scope<select id="shortlist_symbol_mode" data-shortlist-control="true" onchange="analysisShortlist()"><option value="all">All pairs</option><option value="include">Only selected</option><option value="exclude">All except selected</option></select></label><label>Timeframe scope<select id="shortlist_timeframe_mode" data-shortlist-control="true" onchange="analysisShortlist()"><option value="all">All timeframes</option><option value="include">Only selected</option><option value="exclude">All except selected</option></select></label></div><div class="row"><fieldset><legend>Pairs</legend><div id="shortlist_symbols"></div></fieldset><fieldset><legend>Timeframes</legend><div id="shortlist_timeframes"></div></fieldset></div>
            <fieldset><legend>Phase 2 structural comparison filters</legend><div class="row"><label><input id="filter_source_pnl" data-shortlist-filter="true" type="checkbox" onchange="analysisShortlist()"> Source PnL per order</label><label><input id="filter_efficiency" data-shortlist-filter="true" type="checkbox" onchange="analysisShortlist()"> PnL/DD per order</label><label><input id="filter_close_support" data-shortlist-filter="true" type="checkbox" onchange="analysisShortlist()"> CloseSupport per order</label><label><input id="filter_point_event_count" data-shortlist-filter="true" type="checkbox" onchange="analysisShortlist()"> PointEventCount per order</label></div><p class="source-note">Compared only within Pair + Side + TF + Orders + CloseMA. Source PnL is never summed.</p></fieldset>
            <label>Filter audit output<div class="path-control"><input id="analysis_filter_output" value="Output\phase2_filter_audit.xlsx" type="text"><button type="button" class="secondary" onclick="browse('analysis_filter_output','audit_xlsx',false)">Browse…</button></div></label>
            <div class="buttons"><button id="shortlist_refresh" data-shortlist-control="true" type="button" onclick="analysisShortlist()">Refresh scope summary</button><button id="shortlist_export" data-shortlist-control="true" type="button" onclick="analysisFilterExport()">Export filter audit XLS</button><button id="shortlist_generate" data-shortlist-control="true" type="button" class="primary" onclick="analysisStrategies()">Generate READY JSON</button></div><div id="analysisStrategiesStatus" class="muted" aria-live="polite">Refresh the Pair/TF scope summary.</div><div id="shortlist_audit_note" class="source-note">Candidate-level details are available in the XLS audit.</div><div id="shortlist_table_container" class="shortlist-table-wrap"><table id="shortlist_table"><thead id="shortlist_table_header"><tr><th scope="col">Pair</th><th scope="col">TF</th><th scope="col">1ORD</th><th scope="col">2 orders</th><th scope="col">3 orders</th><th scope="col">4 orders</th><th scope="col">READY</th><th scope="col">DEFERRED</th><th scope="col">ALL</th></tr></thead><tbody id="shortlist_table_body"></tbody></table><div id="shortlist_empty" class="shortlist-empty" hidden>No strategies match the current scope.</div></div>
          </div></details>
        </div>
      </section>
      <section role="tabpanel" id="panel-candidates" aria-labelledby="tab-candidates" hidden>
        <h2>6–8. Test plan, tests, DD5</h2><p class="source-note">The generated JSON directory is pre-filled here after step 5. Source metrics remain diagnostic.</p>
        <div class="stack workflow-card"><h3>6. Manual legacy candidate generation</h3><p class="source-note">Use only for CSV/source-pack research, not after an immutable analysis run.</p><label>Источник точек<select id="select_source_mode" onchange="syncCandidateSource()"><option value="csv">Совместимый CSV-вход</option><option value="package">Проверенный source-pack</option></select></label><label id="raw_csv_source">Совместимый CSV-вход (текущий путь)<div class="path-control"><input id="input_csv" value="reports_history_bybit_long_day2.csv" type="text"><button type="button" class="secondary" onclick="browse('input_csv','csv',false)">Выбрать…</button></div></label><label id="package_source" hidden>Каталог проверенного source-pack<div class="path-control"><input id="source_package" value="source_package" type="text"><button type="button" class="secondary" onclick="browse('source_package','directory',false)">Выбрать…</button></div></label><div class="row"><label>Даты листинга<div class="path-control"><input id="dates" value="dates.xlsx" type="text"><button type="button" class="secondary" onclick="browse('dates','dates',false)">Выбрать…</button></div></label><label>Шаблон JSON<div class="path-control"><input id="template" value="ADM_3_LONG_SHORT.json" type="text"><button type="button" class="secondary" onclick="browse('template','template',false)">Выбрать…</button></div></label></div><label>Сторона<select id="side"><option>LONG</option><option>SHORT</option></select></label><button data-runnable="true" onclick="startAction('select')">Запустить селектор</button></div>
        <div class="stack workflow-card"><h3>7. Test plan and tester run</h3><label>Каталог JSON-стратегий<div class="path-control"><input id="strategies" value="output_long\strategies" type="text"><button type="button" class="secondary" onclick="browse('strategies','directory',false)">Выбрать…</button></div></label><div class="buttons"><button data-runnable="true" id="planButton" onclick="startAction('tester-plan')">Проверить план</button><button data-runnable="true" id="runButton" class="primary" onclick="startAction('tester-run')">Запустить тесты</button></div><div id="testerPlanSummary" class="muted">План ещё не проверен.</div></div>
        <div class="stack workflow-card"><h3>8. DD5 calculated comparison</h3><p class="source-note">DD5 — расчётная нормализация для ранжирования по projected PnL/DD. Настройки стратегии и сделки берутся из проверенного CSV; повторный tick-test DD5 JSON не входит в workflow.</p><label>CSV результатов<div class="path-control"><input id="results_csv" value="results\mrs3_long_results.csv" type="text"><button type="button" class="secondary" onclick="browse('results_csv','results_csv',false)">Выбрать…</button></div></label><label>Новый каталог результата DD5<div class="path-control"><input id="posttest_output_dir" type="text" readonly><button type="button" class="secondary" onclick="browse('posttest_output_dir','directory',false)">Выбрать…</button></div></label><button data-runnable="true" onclick="startAction('posttest')">Рассчитать DD5</button></div>
        <div class="stack workflow-card"><h3>Performance DuckDB DD5</h3><p class="source-note">CALCULATION_ONLY: import, calculate, export, then cleanup.</p><label>Performance DuckDB<div class="path-control"><input id="performance_database" value="data/databases/strategy_performance.duckdb" type="text"><button type="button" class="secondary" onclick="browse('performance_database','duckdb',false)">Select...</button></div></label><label>Completed performance inbox<div class="path-control"><input id="performance_inbox" value="data/tester_inbox" type="text"><button type="button" class="secondary" onclick="browse('performance_inbox','directory',false)">Select...</button></div></label><button data-runnable="true" onclick="startAction('performance-dd5')">Run DuckDB DD5</button></div>
      </section>
      <section role="tabpanel" id="panel-portfolio" aria-labelledby="tab-portfolio" hidden>
        <h2>Анализатор портфелей</h2><p id="portfolio-prerequisites" class="source-note"><span class="queued">Queued — Layer A only after input-contract check.</span> Нужны: формат individual results, timestamps входа/выхода, limiter contract (positions vs orders; LONG/SHORT; hedge/one-way), L2 и margin data/rules. Анализатор не превращает source-метрики или individual ranking в портфельный результат.</p>
        <div class="buttons"><button disabled aria-describedby="portfolio-prerequisites">Симулятор сетов недоступен</button><button disabled aria-describedby="portfolio-prerequisites">Рекомендации недоступны</button></div><p class="source-note">Layer A возможен после проверки входного контракта.</p>
      </section>
      <section role="tabpanel" id="panel-settings" aria-labelledby="tab-settings" hidden>
        <h2>Настройки</h2><p class="source-note">Постоянные локальные пути. Выбор открывает системный диалог только по вашему действию; ручной ввод сохраняется.</p>
        <div class="stack workflow-card">
          <label>Конфигурация runner<div class="path-control"><input id="config" type="text"><button type="button" class="secondary" onclick="browse('config','config',false)">Выбрать…</button></div></label>
          <details open><summary>DuckDB import</summary><div class="stack">
            <label>Source DuckDB<div class="path-control"><input id="import_source_duckdb" type="text"><button type="button" class="secondary" onclick="browse('import_source_duckdb','duckdb',false)">Выбрать…</button></div></label>
            <label>Analysis DuckDB<div class="path-control"><input id="import_analysis_duckdb" type="text"><button type="button" class="secondary" onclick="browse('import_analysis_duckdb','duckdb',false)">Выбрать…</button></div></label>
            <label>HTML root<div class="path-control"><input id="import_default_html_root" type="text"><button type="button" class="secondary" onclick="browse('import_default_html_root','directory',false)">Выбрать…</button></div></label>
            <label>Audit root<div class="path-control"><input id="import_audit_root" type="text"><button type="button" class="secondary" onclick="browse('import_audit_root','directory',false)">Выбрать…</button></div></label>
            <div class="row"><label>Workers<input id="import_workers" type="number" min="1" step="1"></label><label>Transaction batch size<input id="import_batch_size" type="number" min="1" step="1"></label></div>
            <button type="button" onclick="saveDuckdbSettings()">Сохранить настройки импорта</button>
            <label>Migration target<div class="path-control"><input id="migration_target" type="text"><button type="button" class="secondary" onclick="browse('migration_target','duckdb',false)">Выбрать…</button></div></label>
            <button type="button" onclick="migrateDuckdb()">Мигрировать и активировать</button>
          </div></details>
          <div class="row"><label>Каталог CSV source-pack<div class="path-control"><input id="csv_output_dir" value="source_package" type="text"><button type="button" class="secondary" onclick="browse('csv_output_dir','directory',false)">Выбрать…</button></div></label><label>Каталог DuckDB source-pack<div class="path-control"><input id="duckdb_output_dir" value="source_package" type="text"><button type="button" class="secondary" onclick="browse('duckdb_output_dir','directory',false)">Выбрать…</button></div></label></div>
          <div class="row"><label>Каталог результата selection<div class="path-control"><input id="select_output_dir" value="output_long" type="text"><button type="button" class="secondary" onclick="browse('select_output_dir','directory',false)">Выбрать…</button></div></label><label>Итоговый CSV тестера<div class="path-control"><input id="output_csv" value="results\mrs3_long_results.csv" type="text"><button type="button" class="secondary" onclick="browse('output_csv','results_csv',false)">Выбрать…</button></div></label></div>
        </div>
      </section>
      <div id="notice" class="notice" aria-live="polite"></div>
    </section>
    <section class="card">
      <div class="status-head">
        <div><div class="muted">Текущая операция</div><div id="operation" class="status-name">Нет задачи</div></div>
        <div id="state" class="state">IDLE</div>
      </div>
      <div id="progressBar" class="bar" role="progressbar" aria-label="Прогресс операции" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="barFill"></div></div>
      <div id="progressText" class="muted">Ожидание запуска</div>
      <section id="coverageReview" class="coverage-review" aria-live="polite" aria-label="DuckDB coverage review" hidden>
        <div class="coverage-review-head">
          <div class="coverage-review-title">DuckDB coverage review</div>
          <div class="coverage-review-note">Review gap-free scopes before direct materialization.</div>
        </div>
        <div id="coverageReviewBody"></div>
      </section>
      <div id="operationStats" class="stats">
        <div class="stat"><b id="submitted">0</b><span>отправлено</span></div>
        <div class="stat"><b id="running">0</b><span>в работе</span></div>
        <div class="stat"><b id="result">0</b><span>результат</span></div>
        <div class="stat"><b id="completed">0</b><span>проверено</span></div>
        <div class="stat"><b id="retries">0</b><span>повторы</span></div>
      </div>
      <div id="activeStrategies"><h3>Активные стратегии</h3>
      <div style="max-height:220px;overflow:auto"><table><thead><tr><th>Имя</th><th>Статус</th><th>%</th></tr></thead><tbody id="activeRows"></tbody></table></div></div>
      <h3 style="margin-top:15px">Готовые файлы</h3><div id="artifacts" class="artifacts muted">Пока нет</div>
      <h3 style="margin-top:15px">Журнал</h3><pre id="logs">Панель готова.</pre>
      <section class="decision-dashboard" aria-live="polite" aria-label="Статистика для решений">
        <h3>Статистика для решений</h3><p class="source-note">Только артефакты, созданные текущим процессом панели.</p>
        <div id="decisionDashboard" class="decision-grid"></div>
      </section>
    </section>
  </div>
</main>
<script>
const labels = {
  'tester-plan':'Проверка плана', 'tester-run':'Пакетное тестирование', 'select':'Создание стратегий', 'posttest':'DD5-анализ', 'performance-dd5':'Performance DuckDB DD5', 'source-csv':'CSV source-pack', 'source-duckdb':'DuckDB source-pack',
  'PRECHECK':'Предварительная проверка', 'STOPPED':'Бот остановлен', 'CLEAN':'Отчёты очищены', 'INSTALLED':'Стратегии установлены',
  'STARTED':'Бот запущен', 'VISIBLE':'Стратегии появились', 'SUBMITTED':'Все тесты отправлены', 'MONITORING':'Идёт тестирование',
  'RECONCILED':'Результаты сверены', 'CSV_COMMITTED':'CSV сохранён', 'STOPPED_FOR_CLEANUP':'Бот остановлен для очистки',
  'RAW_ARTIFACTS_REMOVED':'Временные отчёты удалены', 'COMPLETED':'Завершено', 'FAILED':'Ошибка'
};
let defaultsLoaded = false;
let workflowDefaults = {listing_dates_path:'', strategy_templates:{}};
const value = id => document.getElementById(id).value.trim();
function nextPosttestOutput(csvPath) {
  const stem=(csvPath.split(/[\\/]/).pop() || 'results').replace(/\.csv$/i,'').replace(/[^a-z0-9_-]+/gi,'_');
  const now=new Date(); const pad=n=>String(n).padStart(2,'0');
  return `Output\\posttest_${stem}_${now.getFullYear()}${pad(now.getMonth()+1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
}
function payload(action) {
  const base = {action, config:value('config')};
  if (action === 'tester-plan') return {...base, strategies:value('strategies'), output_csv:value('output_csv')};
  if (action === 'tester-run') return {...base, strategies:value('strategies'), output_csv:value('output_csv'), v6_confirmation:window.v6TesterConfirmation};
  if (action === 'source-csv') return {...base, input_csv:value('source_csv_files'), start:value('csv_start'), end:value('csv_end'), output_dir:value('csv_output_dir')};
  if (action === 'source-duckdb') return {...base, database:value('source_duckdb'), start:value('duckdb_start'), end:value('duckdb_end'), output_dir:value('duckdb_output_dir'), verify_html_root:value('verify_html_root'), verification_sample_count:value('verification_sample_count')};
  if (action === 'select') {
    const source = value('select_source_mode') === 'package'
      ? {source_package:value('source_package')}
      : {input_csv:value('input_csv')};
    return {...base, ...source, dates:value('dates'), template:value('template'), side:value('side'), output_dir:value('select_output_dir')};
  }
  if (action === 'performance-dd5') return {...base, database:value('performance_database'), inbox:value('performance_inbox'), output_dir:value('posttest_output_dir')};
  return {...base, results_csv:value('results_csv'), output_dir:value('posttest_output_dir')};
}
async function startAction(action) {
  if (action === 'tester-run' && window.v6TesterConfirmation) {
    const c = window.v6TesterConfirmation;
    const prompt = `Run v6 tester batch?\nSurface: ${c.source_surface_id}\nSurface manifest: ${c.source_manifest_sha256}\nAnalysis run/config: ${c.analysis_run_id} / ${c.analysis_config_sha256}\nREADY JSON count: ${c.strategy_count}\nGeneration manifest: ${c.generation_manifest_sha256}`;
    if (!window.confirm(prompt)) { document.getElementById('notice').textContent = 'Tester run cancelled: v6 provenance was not confirmed.'; return; }
    window.v6TesterConfirmation = {...c, confirmed:true};
  }
  const notice = document.getElementById('notice'); notice.textContent = 'Запуск…';
  try {
    const response = await fetch('/api/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload(action))});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || 'Ошибка запуска');
    notice.textContent = 'Задача запущена.'; render(body);
  } catch (error) { notice.textContent = error.message; }
}
async function browse(id, kind, multiple) {
  const notice = document.getElementById('notice'); notice.textContent = 'Открываю системный выбор…';
  try {
    const response = await fetch('/api/browse', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({kind, multiple})});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || 'Выбор недоступен');
    if (body.paths.length) { document.getElementById(id).value = body.paths.join(';'); notice.textContent = 'Путь выбран.'; }
    else { notice.textContent = 'Выбор отменён.'; }
  } catch (error) { notice.textContent = error.message; }
}
let duckdbPreflightToken = '';
async function duckdbRequest(endpoint, body={}) { const response = await fetch(endpoint, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}); const document = await response.json(); if (!response.ok) throw new Error(document.error || 'Import request failed'); return document; }
function showDuckdbSettings(settings) { const fields={source_duckdb_path:'import_source_duckdb', analysis_duckdb_path:'import_analysis_duckdb', default_html_root:'import_default_html_root', audit_root:'import_audit_root', workers:'import_workers', transaction_batch_size:'import_batch_size'}; for (const [name,id] of Object.entries(fields)) document.getElementById(id).value=settings[name] ?? ''; if (!value('import_html_root')) document.getElementById('import_html_root').value=settings.default_html_root ?? ''; }
function applyWorkflowDefaults(force=false) { const side=value('direct_side') || 'LONG'; const dates=workflowDefaults.listing_dates_path || ''; const template=workflowDefaults.strategy_templates?.[side] || ''; for(const id of ['analysis_dates','dates']) { if(dates && (force || !value(id))) document.getElementById(id).value=dates; } for(const id of ['analysis_template','template']) { if(template && (force || !value(id) || value(id)==='ADM_3_LONG_SHORT.json')) document.getElementById(id).value=template; } }
async function loadDuckdbSettings() { try { const response=await fetch('/api/duckdb-import/settings', {cache:'no-store'}); const settings=await response.json(); if (!response.ok) throw new Error(settings.error || 'Settings load failed'); showDuckdbSettings(settings); } catch (error) { document.getElementById('notice').textContent=error.message; } }
async function saveDuckdbSettings() { try { const settings=await duckdbRequest('/api/duckdb-import/settings', {source_duckdb_path:value('import_source_duckdb') || null, analysis_duckdb_path:value('import_analysis_duckdb') || null, default_html_root:value('import_default_html_root') || null, audit_root:value('import_audit_root') || null, workers:value('import_workers'), transaction_batch_size:value('import_batch_size')}); showDuckdbSettings(settings); document.getElementById('notice').textContent='Настройки импорта сохранены.'; } catch (error) { document.getElementById('notice').textContent=error.message; } }
async function migrateDuckdb() { try { const settings=await duckdbRequest('/api/duckdb-import/migrate', {target_path:value('migration_target')}); showDuckdbSettings(settings); document.getElementById('notice').textContent='Миграция проверена и активирована.'; } catch (error) { document.getElementById('notice').textContent=error.message; } }
async function duckdbPreflight() { try { render(await duckdbRequest('/api/duckdb-import/preflight', {root_path:value('import_html_root')})); document.getElementById('notice').textContent='Preflight started.'; } catch (error) { document.getElementById('notice').textContent=error.message; } }
async function duckdbImport() { try { const result = await duckdbRequest('/api/duckdb-import/start', {root_path:value('import_html_root'), preflight_token:duckdbPreflightToken}); render(result); } catch (error) { document.getElementById('notice').textContent=error.message; } }
async function duckdbCancel() { try { render(await duckdbRequest('/api/duckdb-import/cancel')); } catch (error) { document.getElementById('notice').textContent=error.message; } }
let sourceV6Token='';
async function sourceV6Preflight() { try { const result=await duckdbRequest('/api/source-v6/preflight',{root_path:value('source_v6_root'),database_path:value('source_v6_database')}); sourceV6Token=result.token||''; const scope=document.getElementById('source_v6_scope'); scope.innerHTML=(result.scopes||[]).map(item=>`<option value="${item}">${item}</option>`).join(''); const ready=(result.ready_intervals||[]); const selected=ready.find(item=>item.scope_key===scope.value)||ready[0]; if(selected){ const start=document.getElementById('source_v6_start_date'); const end=document.getElementById('source_v6_end_date'); start.min=selected.start+'T00:00'; start.max=selected.end+'T23:59'; end.min=selected.start+'T00:00'; end.max=selected.end+'T23:59'; if(!start.value) start.value=selected.start+'T00:00'; if(!end.value) end.value=selected.end+'T23:59'; } document.getElementById('source_v6_status').textContent=`Preflight ready · ${result.snapshotted ?? result.parsed ?? 0}/${result.total} reports`; document.getElementById('source_v6_progress').value=0; } catch(error) { document.getElementById('source_v6_status').textContent=error.message; } }
async function sourceV6Start() { try { const scopes=[...document.getElementById('source_v6_scope').selectedOptions].map(item=>item.value); const result=await duckdbRequest('/api/source-v6/fresh/multiscope/start',{preflight_token:sourceV6Token,scope_keys:scopes}); document.getElementById('source_v6_progress').value=Number(result.progress||0); if(result.surface_path) document.getElementById('source_v6_surface_path').value=result.surface_path; document.getElementById('source_v6_status').textContent=`${result.phase} · ${result.current||0}/${result.total||0}${result.surface_id?' · '+result.surface_id:''}`; } catch(error) { document.getElementById('source_v6_status').textContent=error.message; } }
let sourceV6MergeToken='';
function sourceV6MergePayload() { return {input_paths:value('source_v6_merge_inputs').split(';').map(item=>item.trim()).filter(Boolean),target_path:value('source_v6_merge_target')}; }
async function sourceV6MergePreflight() { try { const result=await duckdbRequest('/api/source-v6/merge/preflight',sourceV6MergePayload()); sourceV6MergeToken=result.token||''; document.getElementById('source_v6_status').textContent=`Merge preflight ready · ${result.total||0} inputs`; } catch(error) { document.getElementById('source_v6_status').textContent=error.message; } }
async function sourceV6MergeStart() { try { if(!sourceV6MergeToken) await sourceV6MergePreflight(); const result=await duckdbRequest('/api/source-v6/merge/start',{preflight_token:sourceV6MergeToken}); document.getElementById('source_v6_status').textContent=`${result.phase} · ${result.accepted_count||0} fragments`; } catch(error) { document.getElementById('source_v6_status').textContent=error.message; } }
async function sourceV6MergeCancel() { try { const result=await duckdbRequest('/api/source-v6/merge/cancel',{}); document.getElementById('source_v6_status').textContent=result.phase||'CANCELLED'; } catch(error) { document.getElementById('source_v6_status').textContent=error.message; } }
async function sourceV6Cancel() { try { const result=await duckdbRequest('/api/source-v6/cancel',{}); document.getElementById('source_v6_status').textContent=result.phase||'CANCELLED'; } catch(error) { document.getElementById('source_v6_status').textContent=error.message; } }
async function sourceV6Library() { try { const rows=await duckdbRequest('/api/source-v6/fresh/library',{}); const select=document.getElementById('source_v6_surface_select'); select.replaceChildren(); for(const item of rows.filter(item=>item.status==='VALID')) select.add(new Option(`${item.surface_id} · ${item.scope_count} READY scopes`,item.path)); if(select.options.length) document.getElementById('source_v6_surface_path').value=select.value; document.getElementById('source_v6_library').textContent=rows.map(item=>`${item.status} · ${item.surface_id||item.path}`).join('\n')||'No surfaces.'; } catch(error) { document.getElementById('source_v6_library').textContent=error.message; } }
document.getElementById('source_v6_surface_select').addEventListener('change', event=>{ document.getElementById('source_v6_surface_path').value=event.target.value; });
async function sourceV6Analyze() { try { const select=document.getElementById('source_v6_surface_select'), option=select.options[select.selectedIndex]; if(!option) throw new Error('Select a VALID fresh v6 surface from the library.'); const result=await duckdbRequest('/api/source-v6/fresh/multiscope/analysis/start',{surface_path:option.value,listing_dates_path:value('source_v6_analysis_dates')||value('analysis_dates'),config_path:value('source_v6_analysis_config')||value('analysis_config'),algorithm_version:value('source_v6_algorithm_version')}); if(result.analysis_path) document.getElementById('source_v6_status').textContent=`COMMITTED · ${result.analysis_path}`; else document.getElementById('source_v6_status').textContent=`${result.phase||'STARTING'} · analysis lifecycle`; } catch(error) { document.getElementById('source_v6_status').textContent=error.message; } }
async function sourceV6AnalysisCancel() { try { const result=await duckdbRequest('/api/source-v6/cancel',{}); document.getElementById('source_v6_status').textContent=result.phase||'CANCELLED'; } catch(error) { document.getElementById('source_v6_status').textContent=error.message; } }
async function sourceV6Gaps() { try { const start=document.getElementById('source_v6_start_date').value; const end=document.getElementById('source_v6_end_date').value; const result=await duckdbRequest('/api/source-v6/gaps',{start:start+'Z',end:end+'Z'}); document.getElementById('source_v6_status').textContent=`Gaps exported · ${result.cells.length} cells`; } catch(error) { document.getElementById('source_v6_status').textContent=error.message; } }
async function sourceV6Export() { try { const result=await duckdbRequest('/api/source-v6/export',{surface_path:document.getElementById('source_v6_surface_path').value,output_dir:document.getElementById('source_v6_export_dir').value}); document.getElementById('source_v6_status').textContent=`Plateau report exported · ${result.report||'manifest'}`; } catch(error) { document.getElementById('source_v6_status').textContent=error.message; } }
let directPreflightToken = '';
let directStartEligible=false;
let directCoverageChecking=false;
function directUtc(id) { const raw=value(id); if (!raw) return ''; return raw.endsWith('Z') ? raw : new Date(raw+'Z').toISOString(); }
function directPayload() { return {start_utc:directUtc('direct_start'), end_utc:directUtc('direct_end'), side:value('direct_side'), symbols:value('direct_symbols').split(';')}; }
function setDirectStartEligible(enabled) {
  directStartEligible=enabled;
  const button=document.querySelector('button[onclick="directBuild()"]');
  if (button) button.disabled=!enabled;
}
function setDirectCoverageChecking(enabled) {
  directCoverageChecking=enabled;
  const button=document.querySelector('button[onclick="directPreflight()"]');
  if (button) { button.disabled=enabled; button.textContent=enabled?'Checking coverage...':'Check coverage'; }
  const target=document.getElementById('directCoverage');
  if (target) target.setAttribute('aria-busy', String(enabled));
}
function renderDirectArtifactLinks(artifacts={}) {
  const target=document.getElementById('directArtifacts');
  if (!target) return;
  target.replaceChildren();
  const entries=Object.entries(artifacts || {});
  if (!entries.length) { target.textContent='Coverage artifacts appear after a successful check.'; return; }
  for (const [name, filename] of entries) { const link=document.createElement('a'); link.href='/api/artifact?name='+encodeURIComponent(name); link.textContent=filename; target.appendChild(link); }
}
function clearDirectCoverageState() {
  directPreflightToken='';
  setDirectStartEligible(false);
  const target=document.getElementById('directCoverage');
  if (target) target.textContent='Coverage review appears in the right panel after preflight.';
  document.getElementById('coverageReview').hidden=true;
  const body=document.getElementById('coverageReviewBody');
  if (body) body.replaceChildren();
  document.getElementById('direct_shifts').textContent='Auto-detected after coverage check.';
  renderDirectArtifactLinks({});
  const artifactBox=document.getElementById('artifacts');
  if (artifactBox) artifactBox.textContent='';
}
function clearDirectExecutionState() {
  document.getElementById('directStatus').textContent='No direct build.';
  const artifactBox=document.getElementById('artifacts');
  if (artifactBox) artifactBox.textContent='';
}
function renderDirectCoverage(result) {
  const target=document.getElementById('directCoverage'); target.replaceChildren();
  document.getElementById('direct_shifts').textContent=(result.required_shifts_bp || []).join('; ') || 'No shifts cover this window';
  for (const [symbol,timeframes] of Object.entries(result.usable_timeframes || {})) { const label=document.createElement('label'); const box=document.createElement('input'); box.type='checkbox'; box.name='direct_selected_symbol'; box.value=symbol; box.checked=true; label.append(box, document.createTextNode(` ${symbol} · ${timeframes.join(', ')}`)); target.appendChild(label); }
  const excluded=new Map(); for(const issue of (result.coverage_issues || [])){ if(!['GRID_NOT_COVERED','GRID_NO_COMMON_PAIRS','CONFLICTING_CANONICAL_POINT'].includes(issue.code)) continue; const key=issue.symbol+' · '+issue.timeframe; excluded.set(key,issue.code); } for(const [scope,code] of excluded){ const row=document.createElement('div'); row.className='direct-unavailable'; row.textContent='! '+scope+' excluded: '+code; target.appendChild(row); }
  for (const symbol of Object.keys(result.unavailable_symbols || {})) { const row=document.createElement('div'); row.className='direct-unavailable'; const reasons=(result.coverage_issues || []).filter(item=>item.symbol===symbol).map(item=>`${item.code}: ${item.detail}`).join('; '); row.textContent=`⚠ ${symbol} · ${reasons || 'unavailable'}`; target.appendChild(row); }
}
async function directPreflight() {
  if (directCoverageChecking) return;
  clearDirectCoverageState();
  setDirectCoverageChecking(true);
  document.getElementById('directStatus').textContent='Checking coverage...';
  try {
    const result=await duckdbRequest('/api/duckdb-direct/coverage', {symbols:value('direct_symbols').split(';')});
    directPreflightToken=result.token||'';
    renderDirectCoverageReview(result);
    setDirectStartEligible(true);
    document.getElementById('directStatus').textContent='Coverage checked.';
  } catch(error) {
    document.getElementById('directStatus').textContent=error.message;
  } finally {
    setDirectCoverageChecking(false);
  }
}
async function directBuild(parentSurfaceId='') { clearDirectExecutionState(); try { const scopes=selectedDirectScopes(); if(!scopes.length) throw new Error('Select at least one gap-free Pair + TF row.'); const selectedSymbols=[...new Set(scopes.map(item=>item.symbol))]; const base={...directPayload(),symbols:selectedSymbols}; const flight=await duckdbRequest('/api/duckdb-direct/preflight',{...base,coverage_token:directPreflightToken,selected_scopes:scopes}); if(flight.preflight_token) directPreflightToken=flight.preflight_token; renderDirectCommonIntervals(flight.selected_intervals); const payload={...base,preflight_token:directPreflightToken,selected_scopes:scopes}; if(parentSurfaceId) payload.parent_surface_id=parentSurfaceId; document.getElementById('coverageReview').hidden=true; render(await duckdbRequest('/api/duckdb-direct/start', payload)); } catch(error) { document.getElementById('directStatus').textContent=error.message; } }
async function directCancel() { try { render(await duckdbRequest('/api/duckdb-direct/cancel')); } catch(error) { document.getElementById('directStatus').textContent=error.message; } }
function directDateOnly(utc) { return (utc || '').slice(0, 10); }
function directIntervalLabel(row) { return `${directDateOnly(row.interval_start_utc)} .. ${directDateOnly(row.interval_end_utc)}`; }
function renderDirectCommonIntervals(intervals={}) {
  const parts=Object.entries(intervals || {}).map(([side,item])=>`${side.toUpperCase()}: ${item.display}`);
  if(parts.length) document.getElementById('direct_shifts').textContent=parts.join('; ');
}
function selectedDirectScopes() {
  return [...document.querySelectorAll('input[name="direct_scope"]:checked')].map((item)=>({symbol:item.dataset.symbol,side:item.dataset.side,timeframe:item.dataset.timeframe}));
}
function renderDirectCoverageReview(result) {
  const target=document.getElementById('directCoverage');
  const section=document.getElementById('coverageReview');
  const body=document.getElementById('coverageReviewBody');
  renderDirectArtifactLinks(result.artifacts);
  target.textContent='Coverage review is shown in the right panel.';
  body.replaceChildren();
  document.getElementById('direct_shifts').textContent=(result.required_shifts_bp || []).join('; ') || 'No shifts cover this window';
  const rows=[...(result.coverage_rows || [])];
  if(!rows.length){
    const empty=document.createElement('div');
    empty.className='coverage-empty';
    empty.textContent='No Pair + Side + TF scopes were discovered for the current selection.';
    body.appendChild(empty);
    section.hidden=false;
    return;
  }
  const groups=new Map();
  for(const row of rows){
    const key=`${row.pair}|${row.side}`;
    if(!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  for(const [key, items] of groups){
    const [pair, side]=key.split('|');
    const card=document.createElement('section');
    card.className='coverage-group';
    const head=document.createElement('div');
    head.className='coverage-group-head';
    const title=document.createElement('b');
    title.textContent=pair;
    const meta=document.createElement('span');
    meta.textContent=side;
    head.append(title, meta);
    card.appendChild(head);
    const table=document.createElement('table');
    table.className='coverage-table';
    table.innerHTML='<thead><tr><th class="coverage-check">Select</th><th class="coverage-tf">TF</th><th class="coverage-interval">Available interval</th><th class="coverage-gap">Gap</th></tr></thead>';
    const tbody=document.createElement('tbody');
    for(const item of items){
      const row=document.createElement('tr');
      const check=document.createElement('td');
      check.className='coverage-check';
      const box=document.createElement('input');
      box.type='checkbox';
      box.name='direct_scope';
      box.dataset.symbol=item.pair;
      box.dataset.side=item.side;
      box.dataset.timeframe=item.timeframe;
      box.checked=Boolean(item.selectable);
      box.disabled=!item.selectable;
      check.appendChild(box);
      const tf=document.createElement('td');
      tf.className='coverage-tf';
      tf.textContent=item.timeframe;
      const interval=document.createElement('td');
      interval.className='coverage-interval';
      interval.textContent=directIntervalLabel(item);
      const gap=document.createElement('td');
      gap.className='coverage-gap' + (item.gap_details?.length ? ' bad' : '');
      gap.textContent=item.gap_details?.length ? item.gap_details.join('; ') : 'none';
      row.append(check, tf, interval, gap);
      tbody.appendChild(row);
    }
    table.appendChild(tbody);
    card.appendChild(table);
    body.appendChild(card);
  }
  section.hidden=false;
}
function showAnalysisFacts(facts={}) { for (const [id,key] of [['analysis_unique','unique_point_count'],['analysis_economic','economic_eligible_point_count'],['analysis_event','event_eligible_point_count'],['analysis_plateaus','plateau_count'],['analysis_ready','ready_candidate_count']]) document.getElementById(id).textContent=facts[key] ?? '—'; }
function renderAnalysisLibrary(rows) {
  const target=document.getElementById('analysisLibrary'); target.replaceChildren();
  for(const surface of rows) { const button=document.createElement('button'); button.type='button'; button.className='secondary'; button.textContent=`${surface.period_start_utc} → ${surface.period_end_utc} · ${surface.side} · ${surface.unique_point_count} points`; button.onclick=()=>{ document.getElementById('analysis_surface_id').value=surface.surface_id; const run=(surface.runs||[])[0]; document.getElementById('analysis_run_id').value=run?.run_id||''; showAnalysisFacts(run?.facts||{unique_point_count:surface.unique_point_count}); document.getElementById('analysisStatus').textContent=`parent=${surface.parent_surface_id||'none'} · sources=${(surface.source_hashes||[]).length} · coverage=${(surface.coverage_reasons||[]).join(', ')||'OK'} · final=${run?.facts?.final_state||run?.facts?.facts_state||'surface only'}`; }; target.appendChild(button); }
}
async function analysisRefresh() { try { const rows=await duckdbRequest('/api/analysis/library',{side:value('analysis_side'),build_mode:value('analysis_build_mode'),symbol:value('analysis_symbol'),period_start_utc:value('analysis_period_start'),period_end_utc:value('analysis_period_end'),parent_surface_id:value('analysis_parent'),source_hash:value('analysis_source_hash')}); renderAnalysisLibrary(rows); document.getElementById('analysisStatus').textContent=`${rows.length} surfaces`; } catch(error){ document.getElementById('analysisStatus').textContent=error.message; } }
async function analysisInitialize(){ const button=document.getElementById('analysis_initialize'), status=document.getElementById('analysis_schema_status'); button.disabled=true; status.textContent='Initializing / migrating analysis schema...'; try { const result=await duckdbRequest('/api/analysis/initialize',{}); status.textContent=`Analysis schema v${result.schema_version} ready.`; document.getElementById('analysisStatus').textContent=status.textContent; } catch(error){ status.textContent=`Analysis schema failed: ${error.message}`; document.getElementById('analysisStatus').textContent=status.textContent; } finally { button.disabled=false; } }
function analysisRefine(){ const parent=value('analysis_surface_id'); if(!parent){ document.getElementById('analysisStatus').textContent='Select a parent surface.'; return; } directBuild(parent); }
async function analysisRerun(){ try { render(await duckdbRequest('/api/analysis/rerun',{surface_id:value('analysis_surface_id'),dates_path:value('analysis_dates'),config_path:value('analysis_config'),comparison_run_id:value('analysis_left_run')})); } catch(error){ document.getElementById('analysisStatus').textContent=error.message; } }
async function analysisCompare(){ try { const result=await duckdbRequest('/api/analysis/compare',{left_run_id:value('analysis_left_run'),right_run_id:value('analysis_right_run')}); document.getElementById('analysisStatus').textContent=JSON.stringify(result); } catch(error){ document.getElementById('analysisStatus').textContent=error.message; } }
async function analysisExport(){ try { const result=await duckdbRequest('/api/analysis/export',{run_id:value('analysis_run_id'),output_path:value('analysis_output')}); document.getElementById('analysisStatus').textContent=`Exported ${result.output} · ${result.manifest}`; } catch(error){ document.getElementById('analysisStatus').textContent=error.message; } }
let shortlistScopes=[];
let shortlistMeta={input_count:0,ready_count:0,deferred_count:0,comparable_count:0,comparison_group_count:0};
let shortlistBusy=false;
function analysisFilterCriteria(){ return [['filter_source_pnl','source_pnl'],['filter_efficiency','efficiency'],['filter_close_support','close_support'],['filter_point_event_count','point_event_count']].filter(([id])=>document.getElementById(id).checked).map(([,name])=>name); }
function setShortlistBusy(busy){ shortlistBusy=busy; document.querySelectorAll('[data-shortlist-control],[data-shortlist-filter]').forEach(item=>item.disabled=busy); if(busy) document.getElementById('analysisStrategiesStatus').textContent='Loading shortlist…'; }
function renderShortlist(){
  const body=document.getElementById('shortlist_table_body'); body.replaceChildren();
  document.getElementById('shortlist_all').textContent=shortlistMeta.input_count;
  document.getElementById('shortlist_ready').textContent=shortlistMeta.ready_count;
  document.getElementById('shortlist_deferred').textContent=shortlistMeta.deferred_count;
  document.getElementById('shortlist_comparable').textContent=shortlistMeta.comparable_count;
  document.getElementById('shortlist_empty').hidden=shortlistScopes.length !== 0;
  for(const item of shortlistScopes){
    const row=document.createElement('tr');
    for(const value of [item.symbol,item.timeframe,item.base_1ord,item.order_2,item.order_3,item.order_4,item.ready,item.deferred,item.total]){ const cell=document.createElement('td'); cell.textContent=value; row.appendChild(cell); }
    body.appendChild(row);
  }
}
async function analysisShortlist(){ if(shortlistBusy) return; const selectedSymbol=value('shortlist_symbol'), selectedTf=value('shortlist_timeframe'); setShortlistBusy(true); try { const result=await duckdbRequest('/api/analysis/shortlist',{run_id:value('analysis_run_id'),criteria:analysisFilterCriteria(),symbol:selectedSymbol,timeframe:selectedTf}); shortlistScopes=result.scopes||[]; shortlistMeta={input_count:result.input_count??0,ready_count:result.ready_count??0,deferred_count:result.deferred_count??0,comparable_count:result.comparable_count??0,comparison_group_count:result.comparison_group_count??0}; const symbol=document.getElementById('shortlist_symbol'), tf=document.getElementById('shortlist_timeframe'); symbol.replaceChildren(new Option('All','')); tf.replaceChildren(new Option('All','')); for(const item of (result.facets?.symbols||[])) symbol.add(new Option(item,item)); for(const item of (result.facets?.timeframes||[])) tf.add(new Option(item,item)); symbol.value=selectedSymbol; tf.value=selectedTf; renderShortlist(); document.getElementById('analysisStrategiesStatus').textContent=`${shortlistMeta.ready_count} READY · ${shortlistMeta.deferred_count} DEFERRED · ${shortlistMeta.comparable_count} COMPARABLE · ${shortlistMeta.comparison_group_count} comparison groups`; } catch(error){ document.getElementById('analysisStrategiesStatus').textContent=error.message; } finally { setShortlistBusy(false); } }
async function analysisFilterExport(){ if(shortlistBusy) return; setShortlistBusy(true); try { const result=await duckdbRequest('/api/analysis/filter-export',{run_id:value('analysis_run_id'),criteria:analysisFilterCriteria(),output_path:value('analysis_filter_output')}); document.getElementById('analysisStrategiesStatus').textContent=`Filter audit: ${result.output}`; } catch(error){ document.getElementById('analysisStrategiesStatus').textContent=error.message; } finally { setShortlistBusy(false); } }
async function analysisStrategies(){ try { render(await duckdbRequest('/api/analysis/strategies',{run_id:value('analysis_run_id'),criteria:analysisFilterCriteria(),symbol:value('shortlist_symbol'),timeframe:value('shortlist_timeframe'),template_path:value('analysis_template'),output_dir:value('analysis_strategy_output'),config_path:value('analysis_config')})); } catch(error){ document.getElementById('analysisStrategiesStatus').textContent=error.message; } }
function selectedScope(name){ return [...document.querySelectorAll(`input[name="${name}"]:checked`)].map(item=>item.value); }
function shortlistScopePayload(){ return {symbol_mode:value('shortlist_symbol_mode'),symbols:selectedScope('shortlist_symbol'),timeframe_mode:value('shortlist_timeframe_mode'),timeframes:selectedScope('shortlist_timeframe')}; }
function renderScopeOptions(targetId,name,items,selected){ const target=document.getElementById(targetId); target.replaceChildren(); for(const item of items){ const label=document.createElement('label'), box=document.createElement('input'); box.type='checkbox'; box.name=name; box.value=item; box.checked=selected.includes(item); box.dataset.shortlistControl='true'; box.onchange=analysisShortlist; label.append(box,document.createTextNode(` ${item}`)); target.appendChild(label); } }
async function analysisShortlist(){ if(shortlistBusy) return; const payload=shortlistScopePayload(); setShortlistBusy(true); try { const result=await duckdbRequest('/api/analysis/shortlist',{run_id:value('analysis_run_id'),source_v6_surface_path:value('source_v6_surface_path'),criteria:analysisFilterCriteria(),...payload}); shortlistScopes=result.scopes||[]; shortlistMeta={input_count:result.input_count??0,ready_count:result.ready_count??0,deferred_count:result.deferred_count??0,comparable_count:result.comparable_count??0,comparison_group_count:result.comparison_group_count??0}; renderScopeOptions('shortlist_symbols','shortlist_symbol',result.facets?.symbols||[],payload.symbols); renderScopeOptions('shortlist_timeframes','shortlist_timeframe',result.facets?.timeframes||[],payload.timeframes); renderShortlist(); document.getElementById('analysisStrategiesStatus').textContent=`${shortlistMeta.ready_count} READY; ${shortlistMeta.deferred_count} DEFERRED; ${shortlistMeta.comparable_count} COMPARABLE; ${shortlistMeta.comparison_group_count} comparison groups`; } catch(error){ document.getElementById('analysisStrategiesStatus').textContent=error.message; } finally { setShortlistBusy(false); } }
async function analysisStrategies(){ if(!shortlistScopes.length){ document.getElementById('analysisStrategiesStatus').textContent='No shortlist scopes available. Refresh the Pair/TF scope summary before generating READY JSON.'; return; } try { render(await duckdbRequest('/api/analysis/strategies',{run_id:value('analysis_run_id'),source_v6_surface_path:value('source_v6_surface_path'),criteria:analysisFilterCriteria(),...shortlistScopePayload(),selected_scopes:shortlistScopes.map(item=>({symbol:item.symbol,side:item.side,timeframe:item.timeframe})),template_path:value('analysis_template'),output_dir:value('analysis_strategy_output'),config_path:value('analysis_config')})); } catch(error){ document.getElementById('analysisStrategiesStatus').textContent=error.message; } }
function renderDashboard(dashboard) {
  const target = document.getElementById('decisionDashboard'); target.replaceChildren();
  const order = ['csv', 'duckdb', 'candidates', 'tester', 'posttest'];
  for (const key of order) {
    const item = dashboard?.[key]; if (!item) continue;
    const card = document.createElement('section'); card.className = 'decision-card';
    const heading = document.createElement('h4'); heading.textContent = item.title; card.appendChild(heading);
    const state = document.createElement('div'); const positive=['SELECTABLE','PACKAGE_COMPLETE','READY_FOR_TEST','CALCULATED','COMPLETED']; const failed=['FAILED']; state.className = 'decision-state ' + (positive.includes(item.state) ? 'good' : (failed.includes(item.state) ? 'bad' : '')); state.textContent = item.state; card.appendChild(state);
    const metrics = document.createElement('div'); metrics.className = 'decision-metrics';
    for (const metric of (item.metrics || [])) { const block=document.createElement('div'); block.className='decision-metric'; const number=document.createElement('b'); number.textContent=metric.value; block.append(number, document.createTextNode(metric.label)); metrics.appendChild(block); }
    card.appendChild(metrics);
    const details = item.details || [];
    if (details.length) { const list=document.createElement('ul'); list.className='decision-details'; for (const detail of details) { const row=document.createElement('li'); row.textContent=detail; list.appendChild(row); } card.appendChild(list); }
    target.appendChild(card);
  }
}
function renderPerformanceProgress(active, progress, fallbackStage) {
  let block = document.getElementById('performanceProgress');
  if (!active) { if (block) block.hidden = true; return; }
  if (!block) {
    block = document.createElement('section');
    block.id = 'performanceProgress';
    block.className = 'stack';
    block.setAttribute('aria-live', 'polite');
    block.setAttribute('aria-label', 'Performance DD5 progress');
    block.innerHTML = '<h3>Performance DD5 progress</h3><div class="stats"><div class="stat"><b id="performanceStage">—</b><span>stage</span></div><div class="stat"><b id="performanceCompleted">0</b><span>completed</span></div><div class="stat"><b id="performanceTotal">0</b><span>total</span></div><div class="stat"><b id="performanceScheduled">0</b><span>scheduled</span></div><div class="stat"><b id="performancePrepared">0</b><span>prepared</span></div><div class="stat"><b id="performanceImported">0</b><span>imported</span></div><div class="stat"><b id="performanceSkipped">0</b><span>skipped</span></div><div class="stat"><b id="performanceQuarantined">0</b><span>quarantined</span></div></div><div id="performancePhases" class="muted"></div><div id="performanceTerminalError" class="muted" hidden></div>';
 document.getElementById('progressText').after(block);
  }
  block.hidden = false;
  document.getElementById('performanceStage').textContent = progress.stage || fallbackStage;
 document.getElementById('performanceCompleted').textContent = progress.completed || 0;
 document.getElementById('performanceTotal').textContent = progress.total || 0;
  document.getElementById('performanceScheduled').textContent = progress.scheduled || 0;
  document.getElementById('performancePrepared').textContent = progress.prepared || 0;
  document.getElementById('performanceImported').textContent = progress.imported || 0;
  document.getElementById('performanceSkipped').textContent = progress.skipped || 0;
 document.getElementById('performanceQuarantined').textContent = progress.quarantined || 0;
  const phases = progress.phase_seconds || {};
  document.getElementById('performancePhases').textContent = Object.entries(phases).map(([key, value]) => `${key}: ${Number(value).toFixed(2)}s`).join(' | ');
 const terminalError = document.getElementById('performanceTerminalError');
  terminalError.hidden = !progress.terminal_error;
  terminalError.textContent = progress.terminal_error ? `terminal error: ${progress.terminal_error}` : '';
}
function autofillPerformanceInbox(workflow) {
  if (!workflow || workflow.state !== 'COMPLETED' || !workflow.inbox_path) return;
  const input = document.getElementById('performance_inbox');
  const current = input && input.value;
  if (input && (!current || current === 'data/tester_inbox')) input.value = workflow.inbox_path;
}
function render(data) {
  if (!defaultsLoaded && data.defaults) { document.getElementById('config').value = data.defaults.config; document.getElementById('analysis_config').value = data.defaults.config; workflowDefaults=data.defaults.workflow || workflowDefaults; applyWorkflowDefaults(); const tester=data.defaults.tester || {}; if(tester.strategies){ document.getElementById('strategies').value=tester.strategies; } if(tester.output_csv){ document.getElementById('output_csv').value=tester.output_csv; document.getElementById('results_csv').value=tester.output_csv; } document.getElementById('posttest_output_dir').value=nextPosttestOutput(value('results_csv')); defaultsLoaded = true; }
  renderDashboard(data.dashboard);
  const imported = data.duckdb_import;
  if (imported) { document.getElementById('duckdbImportStatus').textContent = `${imported.running ? imported.phase : imported.final_state} · parsed=${imported.counts?.parsed || 0}/${imported.discovered || 0} · inserted=${imported.counts?.inserted || 0} · replaced=${imported.counts?.replaced || 0} · quarantined=${imported.counts?.quarantined || 0} · safe_to_delete=${imported.safe_to_delete}`; for (const [name, count] of Object.entries(imported.counts || {})) { const item=document.getElementById('import_'+name); if (item) item.textContent=count; } }
  const preflight = data.duckdb_import_preflight;
  if (preflight) { const bytes=v=>`${(Number(v||0)/1073741824).toFixed(2)} GB`; document.getElementById('duckdbImportStatus').textContent = preflight.running ? `Preflight · ${preflight.snapshotted}/${preflight.discovered} files · ${bytes(preflight.processed_bytes)}/${bytes(preflight.total_bytes)}` : (preflight.error || `Preflight ready · ${preflight.discovered} reports`); if(preflight.token) duckdbPreflightToken=preflight.token; }
  const preflightBusy = Boolean(preflight?.running);
  const importBusy = Boolean(imported?.running);
  if (imported && !preflight && !preflightBusy) document.getElementById('duckdbImportStatus').textContent = `${imported.running ? imported.phase : imported.final_state} · parsed=${imported.counts?.parsed || 0}/${imported.discovered || 0} · inserted=${imported.counts?.inserted || 0} · replaced=${imported.counts?.replaced || 0} · quarantined=${imported.counts?.quarantined || 0} · safe_to_delete=${imported.safe_to_delete}`;
  document.querySelector('button[onclick="duckdbPreflight()"]')?.toggleAttribute('disabled', preflightBusy || importBusy);
  document.querySelector('button[onclick="duckdbImport()"]')?.toggleAttribute('disabled', preflightBusy || importBusy || !preflight?.token);
  const analysis = data.analysis;
  if (analysis) { document.getElementById('analysis_surface_id').value=analysis.surface_id||''; if(analysis.run_id) { document.getElementById('analysis_run_id').value=analysis.run_id; const prefix=analysis.run_id.slice(0,12); if(!value('analysis_output')) document.getElementById('analysis_output').value=`Output\\analysis_${prefix}`; if(value('analysis_strategy_output')==='Output\\strategies') document.getElementById('analysis_strategy_output').value=`Output\\strategies_${prefix}`; } showAnalysisFacts(analysis.statistics||{}); document.getElementById('analysisStatus').textContent=`${analysis.phase}${analysis.run_id?' · '+analysis.run_id:''}${analysis.error?' · '+analysis.error:''}`; }
  const sourceV6Analysis = data.source_v6_analysis;
  if (sourceV6Analysis) { document.getElementById('source_v6_surface_path').value=sourceV6Analysis.surface_path||''; }
  if (sourceV6Analysis) { const total=Number(sourceV6Analysis.work_units_total||0), complete=Number(sourceV6Analysis.work_units_completed||0); document.getElementById('source_v6_progress').value=total ? complete/total : 0; document.getElementById('source_v6_status').textContent=`${sourceV6Analysis.phase} · ${complete}/${total} work units${sourceV6Analysis.analysis_run_id?' · '+sourceV6Analysis.analysis_run_id:''}${sourceV6Analysis.error?' · '+sourceV6Analysis.error:''}`; }
  const strategy=data.analysis_strategies;
  window.v6TesterConfirmation = strategy?.v6_confirmation || null;
  if(strategy){ document.getElementById('analysisStrategiesStatus').textContent=`${strategy.phase} · ${strategy.strategy_count||0} JSON${strategy.error?' · '+strategy.error:''}`; if(strategy.strategies_path){ document.getElementById('strategies').value=strategy.strategies_path; } }
  const direct = data.duckdb_direct;
  // directStatus is rendered below with publication/error/coordinate details
  if (direct && !directCoverageChecking) {
    const coordinate = direct.side && direct.ordinal && direct.total ? `· ${direct.side} ${direct.ordinal}/${direct.total}` : '';
    const publication = direct.publication_state ? `· ${direct.publication_state}` : '';
    const error = direct.error ? `· ${direct.error}` : '';
    document.getElementById('directStatus').textContent = `${direct.phase}· points=${direct.point_count || 0}${coordinate}${publication}${error}${direct.surface_id ? `· `+direct.surface_id : ''}`;
  }
  if (direct && Object.keys(direct.artifacts || {}).length) renderDirectArtifactLinks(direct.artifacts);
  const job = data.job;
  const buttons = document.querySelectorAll('[data-runnable]'); buttons.forEach(button => button.disabled = Boolean(job && job.running));
  if (!job) return;
  const workflow = job.workflow || {}; const progress = job.progress || {}; const performance = job.performance_progress || {};
  autofillPerformanceInbox(workflow);
  const plan = job.plan_summary;
  if (plan) { const summary=document.getElementById('testerPlanSummary'); if(summary) summary.textContent=`${plan.mode === 'RESUME' ? 'Возобновление' : 'Новый запуск'}: всего ${plan.total}; готово ${plan.reusable}; подготовлено к тесту ${plan.prepared}.`; const runButton=document.getElementById('runButton'); if(runButton) runButton.textContent=`Запустить тесты (${plan.prepared})`; }
  const performanceAction = job.action === 'performance-dd5';
  renderPerformanceProgress(performanceAction, performance, job.status);
  const phase = job.status === 'FAILED' ? 'FAILED' : (performanceAction ? (performance.stage || job.status) : (workflow.state || progress.workflow_state || job.status));
  const testerAction = job.action === 'tester-run';
  document.getElementById('operationStats').hidden = !testerAction;
  document.getElementById('activeStrategies').hidden = !testerAction;
  document.getElementById('operation').textContent = labels[job.action] || job.action;
  const state = document.getElementById('state'); state.textContent = labels[phase] || phase;
  state.className = 'state ' + (job.status === 'FAILED' || phase === 'FAILED' ? 'bad' : (job.running ? 'work' : 'good'));
  const expected = Number(performanceAction ? (performance.total || 0) : (progress.expected_count || job.expected_count || 0));
  const complete = Number(performanceAction ? (performance.completed || 0) : (progress.completed_count || 0)); const submitted = Number(progress.submitted_count || 0);
  const monitoring = performanceAction || Number(progress.polls || 0) > 0;
  const shown = monitoring ? complete : submitted; const percent = expected ? Math.min(100, shown * 100 / expected) : (job.running ? 3 : 100);
  document.getElementById('barFill').style.width = percent + '%';
  document.getElementById('progressBar').setAttribute('aria-valuenow', String(Math.round(percent)));
  document.getElementById('progressText').textContent = expected ? (monitoring ? `${complete} завершено из ${expected} · ${percent.toFixed(1)}%` : `${submitted} отправлено из ${expected} · ${percent.toFixed(1)}%`) : (job.error || labels[phase] || phase);
  if (performanceAction) document.getElementById('progressText').textContent = `${performance.stage || phase}${expected ? ` · ${complete}/${expected}` : ''}${performance.quarantined ? ` · quarantined=${performance.quarantined}` : ''}${performance.terminal_error ? ` · ${performance.terminal_error}` : ''}`;
  document.getElementById('submitted').textContent = submitted;
  document.getElementById('running').textContent = progress.running_count || 0;
  document.getElementById('result').textContent = progress.result_count || 0;
  document.getElementById('completed').textContent = complete;
  document.getElementById('retries').textContent = performanceAction ? (performance.quarantined || 0) : (progress.retry_count || 0);
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
setDirectStartEligible(false);
const tabs = [...document.querySelectorAll('[role="tab"]')];
function syncCandidateSource() {
  const packageSelected = value('select_source_mode') === 'package';
  document.getElementById('raw_csv_source').hidden = packageSelected;
  document.getElementById('package_source').hidden = !packageSelected;
}
function activateTab(tab) {
  for (const item of tabs) {
    const selected = item === tab;
    item.setAttribute('aria-selected', String(selected));
    item.tabIndex = selected ? 0 : -1;
    document.getElementById(item.getAttribute('aria-controls')).hidden = !selected;
  }
  tab.focus();
}
for (const tab of tabs) {
  tab.addEventListener('click', () => activateTab(tab));
  tab.addEventListener('keydown', event => {
    const index = tabs.indexOf(tab);
    let next = null;
    if (event.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length];
    if (event.key === 'ArrowLeft') next = tabs[(index + tabs.length - 1) % tabs.length];
    if (event.key === 'Home') next = tabs[0];
    if (event.key === 'End') next = tabs[tabs.length - 1];
    if (next) { event.preventDefault(); activateTab(next); }
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activateTab(tab); }
  });
}
syncCandidateSource();
loadDuckdbSettings();
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
    plan_summary: dict[str, object] | None = None
    performance_progress: dict[str, object] | None = None

    @property
    def running(self) -> bool:
        return self.status in {"STARTING", "RUNNING"}


@dataclass(slots=True)
class _ImportJob:
    token: str
    root: Path
    preflight: ImportPreflight
    cancel: threading.Event = field(default_factory=threading.Event)
    running: bool = True
    phase: str = "STARTING"
    counts: dict[str, int] = field(default_factory=lambda: {name: 0 for name in ("parsed", "inserted", "replaced", "identical", "ambiguous", "quarantined")})
    result: ImportJobResult | None = None
    evidence_valid: bool = False
    error: str | None = None


@dataclass(slots=True)
class _ImportPreflightJob:
    root: Path
    running: bool = True
    phase: str = "SNAPSHOTTING"
    discovered: int = 0
    snapshotted: int = 0
    total_bytes: int = 0
    processed_bytes: int = 0
    token: str | None = None
    error: str | None = None


@dataclass(slots=True)
class _DirectJob:
    request: DirectBuildRequest | None = None
    preflight_request: DirectBuildRequest | None = None
    preflight: DirectPreflight | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    running: bool = True
    phase: str = "STARTING"
    surface_id: str | None = None
    point_count: int = 0
    workers: int = 0
    elapsed_seconds: float = 0.0
    points_per_second: float = 0.0
    total_points: int = 0
    side: str | None = None
    ordinal: int = 0
    total: int = 0
    publication_state: str = "PENDING"
    error: str | None = None
    parent_surface_id: str | None = None
    requests: tuple[DirectBuildRequest, ...] | None = None
    coverage_scan: _CoverageScan | None = None
    audit_root: Path | None = None
    artifacts: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    frozen_preflights: tuple[DirectPreflight, ...] | None = None
    materialization_settings: DirectMaterializationSettings = field(default_factory=DirectMaterializationSettings)


@dataclass(frozen=True, slots=True)
class _DirectPreflightState:
    coverage_scan: _CoverageScan
    requests: tuple[DirectBuildRequest, ...]
    preflights: tuple[DirectPreflight, ...]
    token: str
    audit_root: Path


@dataclass(slots=True)
class _AnalysisJob:
    surface_id: str
    dates_path: Path
    config_path: Path
    comparison_run_id: str | None = None
    running: bool = True
    phase: str = "STARTING"
    run_id: str | None = None
    statistics: dict[str, int] = field(default_factory=dict)
    error: str | None = None


@dataclass(slots=True)
class _SourceV6AnalysisJob:
    surface_path: Path
    surface_id: str
    manifest_sha256: str
    frozen_facts_sha256: str
    scope: str
    start_ms: int
    end_ms: int
    dates_path: Path
    config_path: Path
    algorithm_version: str
    cancel: threading.Event = field(default_factory=threading.Event)
    running: bool = True
    phase: str = "STARTING"
    completed_units: int = 0
    total_units: int = 3
    analysis_run_id: str | None = None
    listing_dates_sha256: str | None = None
    config_sha256: str | None = None
    provenance: dict[str, object] = field(default_factory=dict)
    error: str | None = None


@dataclass(slots=True)
class _StrategyJob:
    run_id: str
    template_path: Path
    output_dir: Path
    config_path: Path
    candidate_ids: tuple[str, ...] = ()
    selected_scopes: tuple[tuple[str, str, str], ...] = ()
    criteria: tuple[str, ...] = ()
    source_v6_surface_path: Path | None = None
    running: bool = True
    phase: str = "STARTING"
    strategies_path: Path | None = None
    manifest_path: Path | None = None
    v6_confirmation: dict[str, object] = field(default_factory=dict)
    strategy_count: int = 0
    error: str | None = None

class PanelController:
    def __init__(
        self,
        root: Path,
        default_config: Path,
        process_factory: Callable[..., object] = subprocess.Popen,
        browse_factory: Callable[[str, bool], tuple[Path, ...]] = _native_browse,
        preflight_func: Callable[[ImportRequest], ImportPreflight] = preflight_html_import,
        import_func: Callable[[ImportRequest, Callable[[ImportProgress], object] | None], ImportJobResult] = import_html_tree,
        migration_func: Callable[[Path, Path], object] = migrate_source_database,
        direct_connection_factory: Callable[..., duckdb.DuckDBPyConnection] = duckdb.connect,
        direct_coverage_func: Callable[..., object] = list_duckdb_direct_coverage,
        direct_coverage_scan_func: Callable[..., object] = coverage_scan_direct,
        direct_preflight_func: Callable[[duckdb.DuckDBPyConnection, DirectBuildRequest], DirectPreflight] = preflight_duckdb_direct,
        direct_prepare_func: Callable[..., object] = prepare_direct_surfaces,
        direct_publish_func: Callable[..., object] = publish_direct_surfaces,
        direct_build_func: Callable[..., object] = run_panel_direct_build,
        analysis_library_func: Callable[..., object] = list_surface_library,
        analysis_compare_func: Callable[..., object] = compare_analysis_runs,
        analysis_load_func: Callable[..., object] = load_published_surface,
        analysis_run_func: Callable[..., object] = run_published_pipeline,
        analysis_publish_func: Callable[..., object] = publish_analysis_run,
        analysis_export_func: Callable[..., object] = export_analysis_run,
        analysis_strategy_func: Callable[..., object] = generate_analysis_strategies,
        analysis_shortlist_func: Callable[..., object] = filter_analysis_candidates,
        analysis_filter_export_func: Callable[..., object] = export_filter_audit,
        analysis_config_loader: Callable[[Path], object] = AlgorithmConfig.from_json,
        source_v6_adapter_func: Callable[..., object] = load_source_v6_pipeline_input,
        source_v6_analysis_func: Callable[..., object] = run_source_v6_analysis,
        source_v6_listing_dates_loader: Callable[[Path], Mapping[str, object]] = load_listing_dates,
    ) -> None:
        self.root = root.resolve()
        self.default_config = self._path(default_config)
        self._process_factory = process_factory
        self._browse_factory = browse_factory
        self._lock = threading.RLock()
        self._panel_jobs = PanelJobRegistry(self.root / ".panel-jobs.json")
        self._local_testing_filled = False
        self._remote_testing_filled = False
        self._panel_source_service: LocalSourceDbService | None = None
        self._panel_source_jobs: LocalSourceDbJobRunner | None = None
        self._remote_source_executor: RemoteSourceDbExecutor | None = None
        self._remote_source_targets: dict[str, Path] = {}
        self._panel_surfaces: LocalSurfacesService | None = None
        self._fresh_analysis_paths: dict[str, Path] = {}
        self._fresh_analysis_surfaces: dict[str, Path] = {}
        self._fresh_strategy_manifests: dict[str, Path] = {}
        self._strategy_batch_service: LocalStrategyBatchService | None = None
        self._strategy_batch_inboxes: dict[str, Path] = {}
        self._performance_dd5_jobs: LocalPerformanceDd5Jobs | None = None
        self._reconcile_interrupted_remote_source_jobs()
        self._job: _Job | None = None
        # Keep only artifact paths created by this controller instance.  The
        # dashboard must never discover data by scanning user directories.
        self._section_jobs: dict[str, _Job] = {}
        self._preflight: ImportPreflight | None = None
        self._preflight_root: Path | None = None
        self._import_job: _ImportJob | None = None
        self._import_preflight_job: _ImportPreflightJob | None = None
        self._preflight_func = preflight_func
        self._import_func = import_func
        self._migration_func = migration_func
        self._direct_connection_factory = direct_connection_factory
        self._direct_coverage_func = direct_coverage_func
        self._direct_coverage_scan_func = direct_coverage_scan_func
        self._direct_preflight_func = direct_preflight_func
        self._direct_prepare_func = direct_prepare_func
        self._direct_publish_func = direct_publish_func
        self._direct_build_func = direct_build_func
        self._direct_coverage_scan: _CoverageScan | None = None
        self._direct_artifacts: dict[str, tuple[str, bytes]] = {}
        self._direct_preflight: tuple[DirectBuildRequest, DirectPreflight, str] | None = None
        self._direct_selected_preflight: _DirectPreflightState | None = None
        self._direct_job: _DirectJob | None = None
        self._analysis_job: _AnalysisJob | None = None
        self._strategy_job: _StrategyJob | None = None
        self._analysis_library_func = analysis_library_func
        self._analysis_compare_func = analysis_compare_func
        self._analysis_load_func = analysis_load_func
        self._analysis_run_func = analysis_run_func
        self._analysis_publish_func = analysis_publish_func
        self._analysis_export_func = analysis_export_func
        self._analysis_strategy_func = analysis_strategy_func
        self._analysis_shortlist_func = analysis_shortlist_func
        self._analysis_filter_export_func = analysis_filter_export_func
        self._analysis_config_loader = analysis_config_loader
        self._source_v6_adapter_func = source_v6_adapter_func
        self._source_v6_analysis_func = source_v6_analysis_func
        self._source_v6_listing_dates_loader = source_v6_listing_dates_loader
        self._source_v6_lock = threading.RLock()
        self._source_v6_preflight: dict[str, object] | None = None
        self._source_v6_merge_preflight: object | None = None
        self._source_v6_job: dict[str, object] | None = None
        self._source_v6_analysis_job: _SourceV6AnalysisJob | None = None

    @staticmethod
    def _section(action: str) -> str:
        return {
            "source-csv": "csv",
            "source-duckdb": "duckdb",
            "select": "candidates",
            "tester-plan": "tester",
            "tester-run": "tester",
            "posttest": "posttest",
            "performance-dd5": "posttest",
        }[action]

    def _path(self, value: str | Path) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return candidate.resolve()

    def _workflow_defaults(self) -> dict[str, object]:
        try:
            document = json.loads(self.default_config.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"listing_dates_path": "", "strategy_templates": {}}
        raw = document.get("panel_workflow", {})
        if not isinstance(raw, dict):
            return {"listing_dates_path": "", "strategy_templates": {}}
        dates = raw.get("listing_dates_path", "")
        templates = raw.get("strategy_templates", {})
        resolved_templates = {
            side: str(self._path(path))
            for side, path in templates.items()
            if side in {"LONG", "SHORT"} and isinstance(path, str) and path.strip()
        } if isinstance(templates, dict) else {}
        return {
            "listing_dates_path": str(self._path(dates)) if isinstance(dates, str) and dates.strip() else "",
            "strategy_templates": resolved_templates,
        }

    def _tester_defaults(self) -> dict[str, str]:
        """Restore only the newest valid tester context saved by this project."""
        candidates: list[tuple[int, Path, Path]] = []
        results_dir = self.root / "results"
        try:
            state_paths = results_dir.glob("*.state.json")
        except OSError:
            return {}
        for state_path in state_paths:
            document = self._read_json(state_path)
            strategy_source = document.get("strategy_source")
            output_csv = document.get("output_csv")
            if not isinstance(strategy_source, str) or not isinstance(output_csv, str):
                continue
            source = Path(strategy_source)
            output = Path(output_csv)
            if not source.is_dir():
                continue
            try:
                candidates.append((state_path.stat().st_mtime_ns, source, output))
            except OSError:
                continue
        if not candidates:
            return {}
        _, source, output = max(candidates, key=lambda item: item[0])
        return {"strategies": str(source), "output_csv": str(output)}

    def _completed_tester_strategy_source(self, results_csv: Path) -> Path | None:
        state_path = results_csv.with_name(f"{results_csv.stem}.state.json")
        document = self._read_json(state_path)
        if document.get("state") != "COMPLETED":
            return None
        output_csv = document.get("output_csv")
        strategy_source = document.get("strategy_source")
        if not isinstance(output_csv, str) or not isinstance(strategy_source, str):
            return None
        if self._path(output_csv) != results_csv.resolve():
            return None
        source = Path(strategy_source)
        return source if source.is_dir() else None

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

    @staticmethod
    def _is_hash(value: object) -> bool:
        if not isinstance(value, str) or len(value) != 64:
            return False
        try:
            int(value, 16)
        except ValueError:
            return False
        return True

    @staticmethod
    def _validate_performance_inbox(inbox: Path) -> None:
        manifest_path = inbox / "inbox_manifest.json"
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("inbox is incomplete: invalid manifest") from error
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise ValueError("inbox is incomplete: schema_version must be 1")
        for field in ("batch_id", "tester_config_sha256"):
            if not isinstance(document.get(field), str) or not document[field].strip():
                raise ValueError(f"inbox is incomplete: {field} is missing")
        if not PanelController._is_hash(document["tester_config_sha256"]):
            raise ValueError("inbox is incomplete: tester_config_sha256 is invalid")
        expected_names = document.get("expected_strategy_names")
        entries = document.get("entries")
        if not isinstance(expected_names, list) or not all(isinstance(name, str) and name for name in expected_names):
            raise ValueError("inbox is incomplete: expected_strategy_names is invalid")
        if not isinstance(entries, list) or not entries:
            raise ValueError("inbox is incomplete: manifest has no entries")
        try:
            _canonical_contract(document)
        except Exception as error:
            raise ValueError(f"inbox is incomplete: commission contract: {error}") from error
        names: list[str] = []
        required = ("manifest_entry_id", "strategy_name", "strategy_version_id", "strategy_path", "report_path", "wizard_run_id", "exchange_name", "source_strategy_sha256", "source_report_sha256")
        for entry in entries:
            if not isinstance(entry, dict) or any(not isinstance(entry.get(field), str) or not entry[field].strip() for field in required):
                raise ValueError("inbox is incomplete: mandatory entry field is missing")
            names.append(entry["strategy_name"])
            for field in ("strategy_version_id", "source_strategy_sha256", "source_report_sha256"):
                if not PanelController._is_hash(entry[field]):
                    raise ValueError(f"inbox is incomplete: {field} is invalid")
            paths: dict[str, Path] = {}
            for field in ("strategy_path", "report_path"):
                relative = Path(entry[field])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("inbox is incomplete: path is outside inbox")
                candidate = (inbox / relative).resolve()
                try:
                    candidate.relative_to(inbox.resolve())
                except ValueError as error:
                    raise ValueError("inbox is incomplete: path is outside inbox") from error
                if not candidate.is_file():
                    raise ValueError(f"inbox is incomplete: missing {field}")
                paths[field] = candidate
            if _sha256(paths["report_path"].read_bytes()) != entry["source_report_sha256"]:
                raise ValueError("inbox is incomplete: report hash mismatch")
            strategy_bytes = paths["strategy_path"].read_bytes()
            if _sha256(strategy_bytes) != entry["source_strategy_sha256"]:
                raise ValueError("inbox is incomplete: strategy hash mismatch")
            try:
                strategy = json.loads(strategy_bytes.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as error:
                raise ValueError("inbox is incomplete: invalid strategy JSON") from error
            if not isinstance(strategy, dict) or _sha256(_canonical(strategy)) != entry["strategy_version_id"]:
                raise ValueError("inbox is incomplete: strategy version hash mismatch")
        if sorted(names) != sorted(expected_names):
            raise ValueError("inbox is incomplete: strategy names do not match")

    @staticmethod
    def _optional_string(payload: Mapping[str, object], name: str) -> str:
        value = payload.get(name)
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _scope_values(payload: Mapping[str, object], name: str) -> tuple[str, ...]:
        value = payload.get(name)
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{name} must be a list")
        return tuple(sorted({item.strip() for item in value if isinstance(item, str) and item.strip()}))

    @classmethod
    def _scope_matches(
        cls, payload: Mapping[str, object], singular: str, plural: str, value: object
    ) -> bool:
        selected = cls._scope_values(payload, plural)
        if not selected:
            return not cls._optional_string(payload, singular) or value == cls._optional_string(payload, singular)
        mode = cls._optional_string(payload, f"{singular}_mode") or "all"
        if mode not in {"all", "include", "exclude"}:
            raise ValueError(f"{singular}_mode must be all, include, or exclude")
        return mode == "all" or (str(value) in selected) == (mode == "include")

    @staticmethod
    def _verification_sample_count(payload: Mapping[str, object]) -> int:
        value = payload.get("verification_sample_count", 3)
        if isinstance(value, bool):
            raise ValueError("verification_sample_count must be an integer from 3 to 5")
        try:
            count = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError("verification_sample_count must be an integer from 3 to 5") from None
        if str(value).strip() != str(count) or not 3 <= count <= 5:
            raise ValueError("verification_sample_count must be an integer from 3 to 5")
        return count

    def _build_command(
        self, action: str, payload: Mapping[str, object]
    ) -> tuple[tuple[str, ...], dict[str, Path]]:
        command = [sys.executable, "-m", "mrs3.cli", action]
        artifacts: dict[str, Path] = {}
        if action in {"source-csv", "source-duckdb"}:
            start = self._required(payload, "start")
            end = self._required(payload, "end")
            output_dir = self._path(self._required(payload, "output_dir"))
            if action == "source-csv":
                source_paths = [item.strip() for item in self._required(payload, "input_csv").split(";") if item.strip()]
                if not source_paths:
                    raise ValueError("input_csv must contain at least one path")
                for source in source_paths:
                    command.extend(["--input-csv", str(self._path(source))])
            else:
                command.extend(["--database", str(self._path(self._required(payload, "database")))])
            config = self._path(self._required(payload, "config"))
            command.extend(["--start", start, "--end", end, "--output-dir", str(output_dir), "--config", str(config)])
            if action == "source-duckdb":
                verification_root = self._optional_string(payload, "verify_html_root")
                sample_count = self._verification_sample_count(payload)
                if verification_root:
                    command.extend(["--verify-html-root", str(self._path(verification_root))])
                    command.extend(["--verification-sample-count", str(sample_count)])
            artifacts = {"manifest": output_dir / "package_manifest.json", "points": output_dir / "points.csv", "audit": output_dir / "source_audit.csv"}
            return tuple(command), artifacts
        else:
            config = self._path(self._required(payload, "config"))
            command.extend(["--config", str(config)])
        if action in {"tester-plan", "tester-run"}:
            strategies = self._path(self._required(payload, "strategies"))
            v6_manifest = strategies / "strategy_manifest.json"
            if v6_manifest.is_file():
                manifest = self._read_json(v6_manifest)
                if manifest is None:
                    raise ValueError("v6 strategy manifest is invalid")
                if manifest.get("generator_schema_version") == "mrs3-ready-json-v6-v1":
                    confirmation = payload.get("v6_confirmation")
                    if not isinstance(confirmation, Mapping) or confirmation.get("confirmed") is not True:
                        raise ValueError("user confirmation is required for v6 tester execution")
                    expected_confirmation = {
                        key: manifest.get(key)
                        for key in (
                            "source_surface_id", "source_manifest_sha256", "analysis_run_id",
                            "analysis_config_sha256", "strategy_count", "generation_manifest_sha256",
                        )
                    }
                    if any(confirmation.get(key) != value for key, value in expected_confirmation.items()):
                        raise ValueError("v6 tester confirmation does not match the generated manifest")
            command.extend(["--strategies", str(strategies)])
            if action in {"tester-plan", "tester-run"} and payload.get("output_csv"):
                output = self._path(self._required(payload, "output_csv"))
                command.extend(["--output-csv", str(output)])
            if action == "tester-run":
                output = self._path(self._required(payload, "output_csv"))
                artifacts = {
                    "output_csv": output,
                    "state": output.with_name(f"{output.stem}.state.json"),
                    "progress": output.with_name(f"{output.stem}.progress.json"),
                    "raw_log": output.with_name(f"{output.stem}.raw.log"),
                }
        elif action == "select":
            input_csv_value = self._optional_string(payload, "input_csv")
            source_package_value = self._optional_string(payload, "source_package")
            if bool(input_csv_value) == bool(source_package_value):
                raise ValueError(
                    "select requires exactly one input_csv or source_package"
                )
            dates = self._path(self._required(payload, "dates"))
            template = self._path(self._required(payload, "template"))
            side = self._required(payload, "side").upper()
            if side not in {"LONG", "SHORT"}:
                raise ValueError("side must be LONG or SHORT")
            output_dir = self._path(self._required(payload, "output_dir"))
            if input_csv_value:
                command.extend(["--input-csv", str(self._path(input_csv_value))])
            else:
                command.extend(
                    ["--source-package", str(self._path(source_package_value))]
                )
            command.extend(
                [
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
            audit_xlsx = self.root / "__embedded_tester_settings__.xlsx"
            strategies = self._completed_tester_strategy_source(results_csv) or (
                self.root / "__embedded_tester_settings__"
            )
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
        elif action == "performance-dd5":
            database = self._path(self._required(payload, "database"))
            inbox = self._path(self._required(payload, "inbox"))
            output_dir = self._path(self._required(payload, "output_dir"))
            if not (inbox / "inbox_manifest.json").is_file():
                raise ValueError("inbox is incomplete: inbox_manifest.json is missing")
            self._validate_performance_inbox(inbox)
            command[3] = "performance-dd5"
            command.extend(["--database", str(database), "--inbox", str(inbox), "--output-dir", str(output_dir)])
            artifacts = {"workbook": output_dir / "posttest.xlsx", "manifest": output_dir / "posttest_manifest.json"}
        else:
            raise ValueError(f"unsupported action: {action}")
        return tuple(command), artifacts

    def browse(self, kind: str, multiple: bool) -> tuple[str, ...]:
        if not isinstance(multiple, bool):
            raise ValueError("multiple must be a boolean")
        if kind != "directory" and kind not in _BROWSE_FILE_TYPES:
            raise ValueError(f"unsupported browse kind: {kind}")
        paths = self._browse_factory(kind, multiple)
        return tuple(str(path.resolve()) for path in paths)

    def panel_default_root(self) -> str:
        """Read only the local panel root switch; invalid config stays legacy."""
        if os.environ.get("MRS3_PANEL_ROOT") == "static":
            return "static"
        try:
            document = json.loads(self.default_config.read_text(encoding="utf-8"))
            panel = document.get("panel") if isinstance(document, dict) else None
            value = panel.get("default_root") if isinstance(panel, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return "legacy"
        return value if isinstance(value, str) and value in {"static", "legacy"} else "legacy"

    def panel_bootstrap(self) -> dict[str, object]:
        """Return only validated, non-sensitive v2 panel capabilities."""
        with self._lock:
            return panel_bootstrap(self.default_config, self.root)

    def panel_settings_reload(self) -> dict[str, object]:
        with self._lock:
            return reload_panel_settings(self.default_config, self.root)

    def panel_settings_validate(self, payload: Mapping[str, object]) -> dict[str, object]:
        with self._lock:
            return validate_panel_settings(self.default_config, self.root, payload)

    def panel_settings_save(self, payload: Mapping[str, object]) -> dict[str, object]:
        with self._lock:
            return save_panel_settings(self.default_config, self.root, payload)

    def panel_jobs(self) -> list[dict]:
        return self._panel_jobs.list()

    def panel_job_submit(self, payload: Mapping[str, object]) -> dict:
        kind = payload.get("kind")
        request = payload.get("request")
        if kind == "strategies.tester.start" and isinstance(request, Mapping):
            return self.strategies_tester_start(request)
        if kind == "strategies.tester.cancel" and isinstance(request, Mapping):
            return self.strategies_tester_cancel(self._required(request, "job_id"))
        if kind == "strategies.performance-dd5" and isinstance(request, Mapping):
            return self.strategies_performance_dd5(request)
        idempotency_key = payload.get("idempotency_key")
        resource_keys = payload.get("resource_keys", [])
        if not isinstance(kind, str) or not isinstance(request, dict) or not isinstance(idempotency_key, str) or not isinstance(resource_keys, list):
            raise PanelJobError("INVALID_REQUEST")
        return self._panel_jobs.submit(kind, request, idempotency_key, tuple(resource_keys))

    @staticmethod
    def _panel_resource(value: str) -> str:
        return sha256(value.encode("utf-8")).hexdigest()

    def _start_tracked_panel_job(
        self,
        kind: str,
        request: dict[str, object],
        resource_keys: tuple[str, ...],
        start: Callable[[str], dict[str, object]],
        runtime: dict[str, object] | None = None,
    ) -> dict[str, object]:
        job_id = uuid.uuid4().hex
        self._panel_jobs.submit(kind, request, f"panel:{job_id}", resource_keys, job_id=job_id)
        self._panel_jobs.transition(job_id, "RUNNING")
        if runtime:
            self._panel_jobs.sync(job_id, {"state": "RUNNING", "phase": "RUNNING"}, runtime=runtime)
        try:
            return self._sync_tracked_panel_job(start(job_id))
        except BaseException:
            self._panel_jobs.transition(job_id, "FAILED")
            raise

    def _record_special_job(self, document: dict[str, object]) -> None:
        """Persist worker completion without exposing controller-only artifact paths."""
        job_id = document.get("job_id")
        if not isinstance(job_id, str):
            return
        runtime = {}
        inbox = document.get("inbox_path")
        if isinstance(inbox, str) and inbox:
            runtime["inbox_path"] = inbox
        public = {key: value for key, value in document.items() if key in {"state", "phase", "progress", "error"}}
        try:
            self._panel_jobs.sync(job_id, public, runtime=runtime or None)
        except PanelJobError:
            pass

    def _sync_tracked_panel_job(self, document: dict[str, object]) -> dict[str, object]:
        job_id, state = document.get("job_id"), document.get("state")
        if not isinstance(job_id, str) or not isinstance(state, str):
            return document
        try:
            tracked = self._panel_jobs.get(job_id)
        except PanelJobError:
            return document
        target = state if state in {"COMMITTED", "CANCELLED", "FAILED", "CANCELLING"} else "RUNNING"
        if tracked["state"] != target:
            try:
                self._panel_jobs.transition(job_id, target, phase=str(document.get("phase") or target))
            except PanelJobError:
                pass
        return document

    def _tracked_job_or_interrupted(
        self, job_id: str, status: Callable[[str], dict[str, object]]
    ) -> dict[str, object]:
        try:
            document = status(job_id)
            self._record_special_job(document)
            return self._sync_tracked_panel_job(document)
        except (KeyError, RemoteSourceDbError):
            return self._panel_jobs.get(job_id)

    def local_testing_status(self) -> dict[str, object]:
        try:
            config = RunnerConfig.from_json(self.default_config)
        except Exception:
            return {"preflight_ok": False, "bot": {"exists": False, "executable": False}, "report": {"exists": False}, "strategy": {"exists": False}, "disk_free_bytes": 0}
        return LocalTestingService(config, self.root).status()

    def _local_testing_service(self) -> LocalTestingService:
        try:
            config = RunnerConfig.from_json(self.default_config)
        except Exception:
            raise PanelTestingError("invalid testing request") from None
        return LocalTestingService(config, Path(__file__).resolve().parents[2])

    @staticmethod
    def _local_testing_request(payload: Mapping[str, object]) -> dict[str, object]:
        symbols = payload.get("symbols")
        if isinstance(symbols, str):
            symbols = tuple(item.strip() for item in symbols.split(",") if item.strip())
        if not isinstance(symbols, (tuple, list)) or not all(isinstance(item, str) for item in symbols):
            raise PanelTestingError("invalid testing request")
        try:
            return {
                "side": payload.get("side"), "symbols": symbols,
                "start": payload.get("start"), "end": payload.get("end"),
            }
        except Exception:  # pragma: no cover - protects the HTTP trust boundary.
            raise PanelTestingError("invalid testing request") from None

    def local_testing_fill(self, payload: Mapping[str, object]) -> dict[str, object]:
        try:
            result = self._local_testing_service().fill(**self._local_testing_request(payload))
            self._local_testing_filled = True
            return result
        except Exception:
            raise PanelTestingError("invalid testing request") from None

    def local_testing_start(self) -> dict[str, str]:
        if not self._local_testing_filled:
            raise PanelTestingError("invalid testing request")
        try:
            return self._local_testing_service().start()
        except Exception:
            raise PanelTestingError("invalid testing request") from None

    def local_testing_stop(self) -> dict[str, str]:
        try:
            return self._local_testing_service().stop()
        except Exception:
            raise PanelTestingError("invalid testing request") from None

    def _remote_testing_service(self) -> RemoteTestingService:
        try:
            document = json.loads(self.default_config.read_text(encoding="utf-8"))
            return RemoteTestingService(document)
        except Exception:
            raise PanelTestingError("invalid testing request") from None

    def remote_testing_status(self) -> dict[str, object]:
        try:
            document = json.loads(self.default_config.read_text(encoding="utf-8"))
        except Exception:
            return remote_testing_status({})
        return remote_testing_status(document)

    def remote_testing_prepare(self, payload: Mapping[str, object]) -> dict[str, object]:
        try:
            return self._remote_testing_service().prepare_request(**self._local_testing_request(payload))
        except Exception:
            raise PanelTestingError("invalid testing request") from None

    def remote_testing_check_paths(self) -> dict[str, object]:
        try:
            return self._remote_testing_service().check_paths()
        except Exception:
            raise PanelTestingError("invalid testing request") from None

    def remote_testing_fill(self, payload: Mapping[str, object]) -> dict[str, object]:
        request = self._local_testing_request(payload)
        side = request["side"]
        if not isinstance(side, str):
            raise PanelTestingError("invalid testing request")
        templates = {
            "LONG": ("config_tester_long_standart.json", "Bybit_long.json"),
            "SHORT": ("config_tester_short_standart.json", "Bybit_short.json"),
        }
        selected = templates.get(side.strip().upper())
        if selected is None:
            raise PanelTestingError("invalid testing request")
        try:
            result = self._remote_testing_service().fill(
                request,
                tester_template=(self.root / "Input" / selected[0]).read_text(encoding="utf-8"),
                strategy_template=(self.root / "Input" / selected[1]).read_text(encoding="utf-8"),
            )
            self._remote_testing_filled = True
            return result
        except Exception:
            raise PanelTestingError("invalid testing request") from None

    def remote_testing_start(self) -> dict[str, object]:
        if not self._remote_testing_filled:
            raise PanelTestingError("invalid testing request")
        try:
            return self._remote_testing_service().start()
        except Exception:
            raise PanelTestingError("invalid testing request") from None

    def remote_testing_stop(self) -> dict[str, object]:
        try:
            return self._remote_testing_service().stop()
        except Exception:
            raise PanelTestingError("invalid testing request") from None

    def remote_testing_progress(self) -> dict[str, object]:
        try:
            return self._remote_testing_service().read_progress()
        except Exception:
            raise PanelTestingError("invalid testing request") from None

    def _local_source_jobs(self) -> tuple[LocalSourceDbService, LocalSourceDbJobRunner]:
        if self._panel_source_service is None or self._panel_source_jobs is None:
            try:
                workers = max(1, int(self._import_settings().workers))
            except Exception:
                workers = 1
            self._panel_source_service = LocalSourceDbService(workers=workers)
            self._panel_source_jobs = LocalSourceDbJobRunner(self._panel_source_service, on_update=self._record_special_job)
        return self._panel_source_service, self._panel_source_jobs

    def source_db_local_import_preflight(self, payload: Mapping[str, object]) -> dict[str, object]:
        service, _ = self._local_source_jobs()
        return service.preflight_import(
            self._path(self._required(payload, "html_root")),
            self._path(self._required(payload, "target_path")),
        )

    def source_db_local_merge_preflight(self, payload: Mapping[str, object]) -> dict[str, object]:
        inputs = payload.get("input_paths")
        if not isinstance(inputs, list) or not inputs or not all(isinstance(item, str) for item in inputs):
            raise ValueError("input_paths must be a non-empty list")
        service, _ = self._local_source_jobs()
        return service.preflight_merge(
            tuple(self._path(item) for item in inputs),
            self._path(self._required(payload, "target_path")),
        )

    def source_db_local_start(self, payload: Mapping[str, object], *, merge: bool) -> dict[str, object]:
        token = self._required(payload, "preflight_token")
        target = self._path(self._required(payload, "target_path"))
        service, jobs = self._local_source_jobs()
        if service.target_for(token, merge=merge) != target.resolve():
            raise ValueError("Source DB target does not match preflight")
        resource = self._panel_resource(str(target).casefold())
        return self._start_tracked_panel_job(
            "source.local-merge" if merge else "source.local-import",
            {"operation": "merge" if merge else "import"},
            (f"source:{resource}",),
            lambda job_id: (
                jobs.start_merge(token, str(target).casefold(), job_id=job_id)
                if merge else jobs.start_import(token, str(target).casefold(), job_id=job_id)
            ),
        )

    def source_db_local_jobs(self) -> list[dict[str, object]]:
        _, jobs = self._local_source_jobs()
        live = [self._sync_tracked_panel_job(job) for job in jobs.list()]
        live_ids = {str(job["job_id"]) for job in live}
        restarted = [
            job for job in self._panel_jobs.list()
            if str(job.get("kind", "")).startswith("source.local-") and str(job.get("job_id")) not in live_ids
        ]
        return live + restarted

    def source_db_local_cancel(self, job_id: str) -> dict[str, object]:
        _, jobs = self._local_source_jobs()
        try:
            result = jobs.cancel(job_id)
            self._panel_jobs.cancel(job_id)
            return self._sync_tracked_panel_job(result)
        except KeyError:
            try:
                return self._panel_jobs.get(job_id)
            except PanelJobError:
                raise ValueError("Source DB job not found") from None

    def _remote_source_db(self) -> RemoteSourceDbExecutor:
        if self._remote_source_executor is None:
            try:
                self._remote_source_executor = RemoteSourceDbExecutor(
                    self._remote_testing_service().config
                )
            except Exception:
                raise RemoteSourceDbError("invalid remote source db request") from None
        return self._remote_source_executor

    def _reconcile_interrupted_remote_source_jobs(self) -> None:
        """A restart never leaves a remote importer running without a stop attempt."""
        for job in self._panel_jobs.list():
            if job.get("kind") != "source.remote-import" or job.get("error") != {"code": "INTERRUPTED"}:
                continue
            try:
                runtime = self._panel_jobs.runtime(str(job["job_id"]))
                html, target = runtime.get("remote_html_path"), runtime.get("remote_db_target")
                if not isinstance(html, str) or not isinstance(target, str):
                    continue
                executor = self._remote_source_db()
                executor.resume_import(str(job["job_id"]), html, target)
                executor.cancel(str(job["job_id"]))
            except Exception:
                continue

    def source_db_remote_start(self, payload: Mapping[str, object]) -> dict[str, object]:
        executor = self._remote_source_db()
        target = self._path(self._required(payload, "local_target_path"))
        if target.exists() or target.is_symlink():
            raise ValueError("local source db target already exists")
        remote_html = self._required(payload, "remote_html_path")
        remote_target = self._required(payload, "remote_db_target")
        job = self._start_tracked_panel_job(
            "source.remote-import", {"operation": "remote-import"},
            (f"source:{self._panel_resource(str(target).casefold())}",),
            lambda job_id: executor.start_import(
                remote_html, remote_target, job_id=job_id,
            ),
            runtime={"remote_html_path": remote_html, "remote_db_target": remote_target, "local_target_path": str(target)},
        )
        self._remote_source_targets[str(job["job_id"])] = target
        return job

    def source_db_local_catalog(self) -> dict[str, object]:
        """List only local Source DB candidates from the configured directory."""
        try:
            source = self._import_settings().source_duckdb_path
            root = self._path(source).parent if source is not None else None
        except (OSError, ValueError):
            root = None
        if root is None:
            return {"databases": []}
        try:
            candidates = sorted(
                (
                    path for path in root.iterdir()
                    if path.suffix.casefold() == ".duckdb" and path.is_file() and not path.is_symlink()
                ),
                key=lambda path: path.name.casefold(),
            )
        except OSError:
            candidates = []
        return {"databases": [{"name": path.name, "path": str(path)} for path in candidates]}

    def surface_catalog(self) -> dict[str, object]:
        """List manifest-validated fresh-surface candidates without blocking the UI."""
        try:
            configured = self._import_settings().source_v6_surface_dir
            directory = self._path(configured) if configured else (self.root / "data" / "surfaces")
            candidates = sorted(
                (path for path in directory.rglob("*.surface-v6.duckdb") if path.is_file() and not path.is_symlink()),
                key=lambda path: str(path).casefold(),
            )
        except (OSError, ValueError):
            candidates = []
        surfaces = []
        for path in candidates:
            try:
                read_multiscope_surface(path, decode=False)
            except (OSError, ValueError, duckdb.Error):
                continue
            surfaces.append({"name": path.name, "path": str(path)})
        return {"surfaces": surfaces}

    def source_db_remote_status(self, job_id: str) -> dict[str, object]:
        executor = self._remote_source_db()
        status = self._tracked_job_or_interrupted(job_id, executor.status)
        if status.get("state") == "REMOTE_IMPORTED":
            tracked = self._panel_jobs.get(job_id)
            if tracked.get("state") in {"CANCELLING", "CANCELLED"}:
                status = executor.cancel(job_id)
                return self._sync_tracked_panel_job(status)
            target = self._remote_source_targets.get(job_id)
            if target is None:
                raise RemoteSourceDbError("remote source db job not found")
            status = executor.start_delivery(job_id, target)
        if status.get("state") in {"COMMITTED", "FAILED", "CANCELLED"}:
            self._remote_source_targets.pop(job_id, None)
        return self._sync_tracked_panel_job(status)

    def source_db_remote_cancel(self, job_id: str) -> dict[str, object]:
        try:
            result = self._remote_source_db().cancel(job_id)
        except RemoteSourceDbError:
            return self._panel_jobs.get(job_id)
        self._panel_jobs.cancel(job_id)
        return self._sync_tracked_panel_job(result)

    def _surfaces(self) -> LocalSurfacesService:
        if self._panel_surfaces is None:
            try:
                workers = min(16, max(1, int(self._import_settings().workers)))
            except Exception:
                workers = 1
            self._panel_surfaces = LocalSurfacesService(workers=workers)
        return self._panel_surfaces

    def surface_preflight(self, payload: Mapping[str, object]) -> dict[str, object]:
        try:
            return self._surfaces().preflight(self._path(self._required(payload, "source_db")))
        except Exception:
            raise ValueError("invalid surface request") from None

    def surface_gaps(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self._surfaces().gaps(
            self._required(payload, "preflight_token"), self._required(payload, "scope_key")
        )

    def surface_select(self, payload: Mapping[str, object]) -> dict[str, object]:
        scopes = payload.get("scope_keys")
        if not isinstance(scopes, list):
            raise ValueError("scope_keys must be a list")
        return self._surfaces().select(self._required(payload, "preflight_token"), scopes)

    def surface_publish(self, payload: Mapping[str, object]) -> dict[str, object]:
        scopes = payload.get("scope_keys")
        if not isinstance(scopes, list):
            raise ValueError("scope_keys must be a list")
        try:
            return self._surfaces().publish(
                self._required(payload, "preflight_token"), scopes,
                self._path(self._required(payload, "target_path")),
                self._optional_string(payload, "filename") or None,
            )
        except Exception:
            raise ValueError("invalid surface request") from None

    def surface_publish_start(self, payload: Mapping[str, object]) -> dict[str, object]:
        scopes = payload.get("scope_keys")
        if not isinstance(scopes, list):
            raise ValueError("scope_keys must be a list")
        try:
            return self._surfaces().start_publish(
                self._required(payload, "preflight_token"), scopes,
                self._path(self._required(payload, "target_path")),
                self._optional_string(payload, "filename") or None,
            )
        except Exception:
            raise ValueError("invalid surface request") from None

    def surface_publish_status(self) -> dict[str, object]:
        return self._surfaces().publish_status()

    def _workflow_default(self, name: str, *, side: str | None = None) -> Path:
        try:
            document = json.loads(self.default_config.read_text(encoding="utf-8"))
            workflow = document.get("panel_workflow", {})
            value = workflow.get(name)
            if name == "strategy_templates" and isinstance(value, Mapping) and side is not None:
                value = value.get(side.upper())
            if not isinstance(value, str) or not value.strip():
                raise ValueError
            return self._path(value)
        except Exception:
            raise ValueError("panel workflow default is unavailable") from None

    def _workflow_algorithm_version(self, value: object) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()
        try:
            workflow = json.loads(self.default_config.read_text(encoding="utf-8")).get("panel_workflow", {})
            configured = workflow.get("algorithm_version") if isinstance(workflow, Mapping) else None
            if isinstance(configured, str) and configured.strip():
                return configured.strip()
        except Exception:
            pass
        return "0.7-canonical-phase1"

    def strategies_fresh_analyze(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Run the only supported fresh multi-scope analysis contour."""
        surface = self._path(self._required(payload, "surface_path"))
        result = self.source_v6_start_fresh_analysis({
            "surface_path": str(surface),
            "listing_dates_path": str(self._workflow_default("listing_dates_path")),
            "config_path": str(self.default_config),
            "algorithm_version": self._workflow_algorithm_version(payload.get("algorithm_version")),
            "target_path": self._optional_string(payload, "target_path"),
        })
        if result.get("phase") != "COMMITTED":
            return {"phase": str(result.get("phase", "FAILED")), "error": "Analysis failed. Check panel logs."}
        artifact = Path(str(result.get("analysis_path", "")))
        try:
            connection = duckdb.connect(str(artifact), read_only=True)
            try:
                analysis_id = str(connection.execute("select value from manifest where key='analysis_id'").fetchone()[0])
            finally:
                connection.close()
            if len(analysis_id) != 64:
                raise ValueError
        except Exception:
            raise ValueError("fresh analysis artifact is invalid") from None
        self._fresh_analysis_paths[analysis_id] = artifact
        self._fresh_analysis_surfaces[analysis_id] = surface
        return {"phase": "COMMITTED", "analysis_run_id": analysis_id}

    def strategies_fresh_generate(self, payload: Mapping[str, object]) -> dict[str, object]:
        candidates = payload.get("candidate_ids")
        scopes = payload.get("selected_scopes")
        if not isinstance(candidates, list) or not all(isinstance(item, str) for item in candidates):
            raise ValueError("candidate_ids must be a list")
        if not isinstance(scopes, list) or not all(isinstance(item, list) and len(item) == 3 and all(isinstance(value, str) for value in item) for item in scopes):
            raise ValueError("selected_scopes must be a list")
        scope_sides = {str(item[1]).upper() for item in scopes}
        if len(scope_sides) != 1:
            raise ValueError("fresh strategy generation requires one side per batch")
        config = self._analysis_config_loader(self.default_config)
        analysis_id = self._required(payload, "analysis_run_id")
        analysis_path = self._fresh_analysis_paths.get(analysis_id)
        if analysis_path is None:
            raise ValueError("fresh analysis is not available in this panel session")
        # The operator chose where the JSON batch lands; the previous fixed
        # location was invisible in the panel, so the batch could not be found.
        requested = payload.get("output_dir")
        output_dir = (
            self._path(str(requested))
            if isinstance(requested, str) and requested.strip()
            else self.root / "Output" / "strategies" / analysis_id
        )
        result = generate_fresh_analysis_strategies(
            analysis_path,
            analysis_id,
            candidates,
            [tuple(item) for item in scopes],
            self._workflow_default("strategy_templates", side=next(iter(scope_sides))),
            output_dir,
            config,
            surface_path=self._fresh_analysis_surfaces.get(analysis_id),
        )
        self._fresh_strategy_manifests[analysis_id] = result.manifest_path
        return {
            "phase": "COMMITTED",
            "analysis_run_id": result.analysis_run_id,
            "surface_id": result.surface_id,
            "strategy_count": result.strategy_count,
            "manifest": result.manifest_path.name,
            "output_dir": str(result.manifest_path.parent),
        }

    def strategies_fresh_shortlist(self, payload: Mapping[str, object]) -> dict[str, object]:
        analysis_id = self._required(payload, "analysis_run_id")
        path = self._fresh_analysis_paths.get(analysis_id)
        if path is None:
            raise ValueError("fresh analysis is not available in this panel session")
        return list_fresh_analysis_shortlist(path, analysis_id)

    def _strategy_batch(self) -> LocalStrategyBatchService:
        if self._strategy_batch_service is None:
            self._strategy_batch_service = LocalStrategyBatchService(RunnerConfig.from_json(self.default_config), on_update=self._record_special_job)
        return self._strategy_batch_service

    def strategies_tester_start(self, payload: Mapping[str, object]) -> dict[str, object]:
        analysis_id = self._required(payload, "analysis_run_id")
        manifest = self._fresh_strategy_manifests.get(analysis_id)
        if manifest is None:
            raise ValueError("fresh strategy batch is not available in this panel session")
        return self._start_tracked_panel_job(
            "strategies.tester", {"analysis_run_id": analysis_id},
            ("strategies.tester",),
            lambda job_id: self._strategy_batch().start(manifest, job_id=job_id),
        )

    def strategies_tester_status(self, job_id: str) -> dict[str, object]:
        result = self._tracked_job_or_interrupted(job_id, self._strategy_batch().status)
        if result.get("state") == "COMMITTED" and isinstance(result.get("inbox_path"), str):
            self._strategy_batch_inboxes[job_id] = Path(result["inbox_path"])
        return {key: value for key, value in result.items() if key != "inbox_path"}

    def strategies_tester_cancel(self, job_id: str) -> dict[str, object]:
        try:
            result = self._strategy_batch().cancel(job_id)
        except KeyError:
            return self._panel_jobs.get(job_id)
        self._panel_jobs.cancel(job_id)
        return self._sync_tracked_panel_job(result)

    def strategies_performance_dd5(self, payload: Mapping[str, object]) -> dict[str, object]:
        job_id = self._required(payload, "tester_job_id")
        delete_html = payload.get("delete_html", False)
        if not isinstance(delete_html, bool):
            raise ValueError("delete_html must be a boolean")
        inbox = self._strategy_batch_inboxes.get(job_id)
        if inbox is None:
            try:
                saved = self._panel_jobs.runtime(job_id).get("inbox_path")
                inbox = Path(saved) if isinstance(saved, str) else None
            except PanelJobError:
                inbox = None
        if inbox is None:
            raise ValueError("committed tester inbox is not available")
        output = self.root / "Output" / "dd5" / job_id
        database = self.root / "Output" / "performance" / f"{job_id}.duckdb"
        if self._performance_dd5_jobs is None:
            self._performance_dd5_jobs = LocalPerformanceDd5Jobs(on_update=self._record_special_job)
        request = PerformanceDd5Request(
            inbox=inbox, database=database, output_dir=output,
            config=self._analysis_config_loader(self.default_config), delete_html=delete_html,
        )
        return self._start_tracked_panel_job(
            "strategies.performance-dd5", {"tester_job_id": job_id},
            ("strategies.performance-dd5",),
            lambda tracked_id: self._performance_dd5_jobs.start(request, job_id=tracked_id),
        )

    def strategies_performance_dd5_status(self, job_id: str) -> dict[str, object]:
        if self._performance_dd5_jobs is None:
            return self._panel_jobs.get(job_id)
        return self._tracked_job_or_interrupted(job_id, self._performance_dd5_jobs.status)


    def _import_settings(self, payload: Mapping[str, object] | None = None) -> DuckDBImportSettings:
        if payload is None:
            return load_duckdb_import_settings(self.default_config)
        previous = load_duckdb_import_settings(self.default_config)
        paths = {name: payload.get(name, getattr(previous, name)) for name in ("source_duckdb_path", "analysis_duckdb_path", "source_v6_surface_dir", "default_html_root", "audit_root")}
        def path(name: str) -> Path | None:
            value = paths[name]
            if value is None or value == "": return None
            if isinstance(value, Path): return value
            if not isinstance(value, str): raise ValueError(f"{name} must be a string or null")
            return self._path(value)
        def number(name: str) -> int:
            value = payload.get(name, getattr(previous, name))
            if isinstance(value, bool): raise ValueError(f"{name} must be a positive integer")
            try: result = int(value)
            except (TypeError, ValueError): raise ValueError(f"{name} must be a positive integer") from None
            if result < 1: raise ValueError(f"{name} must be a positive integer")
            return result
        return DuckDBImportSettings(**{name: path(name) for name in paths}, workers=number("workers"), transaction_batch_size=number("transaction_batch_size"))

    @staticmethod
    def _settings_document(settings: DuckDBImportSettings) -> dict[str, object]:
        # Paths are returned only by the settings endpoint, never by job status.
        return {name: (str(value) if isinstance(value, Path) else value) for name, value in ((name, getattr(settings, name)) for name in ("source_duckdb_path", "analysis_duckdb_path", "source_v6_surface_dir", "default_html_root", "audit_root", "workers", "transaction_batch_size"))}

    def duckdb_import_settings(self, payload: Mapping[str, object] | None = None) -> dict[str, object]:
        with self._lock:
            settings = self._import_settings(payload)
            if payload is not None: save_duckdb_import_settings(self.default_config, settings)
        return self._settings_document(settings)

    @staticmethod
    def _source_v6_merge_inputs(payload: Mapping[str, object]) -> tuple[str, ...]:
        raw = payload.get("input_paths", payload.get("database_paths", payload.get("inputs", payload.get("source_paths"))))
        if isinstance(raw, str):
            values = raw.replace("\n", ";").split(";")
        elif isinstance(raw, (list, tuple)):
            values = list(raw)
        else:
            raise ValueError("merge input_paths must be a list of database paths")
        paths = tuple(dict.fromkeys(str(value).strip() for value in values if isinstance(value, str) and value.strip()))
        if not paths:
            raise ValueError("merge input_paths must not be empty")
        return paths

    def _source_v6_import_options(self) -> tuple[int, object, int]:
        workers = max(1, self._import_settings().workers)
        settings = load_source_v6_import_settings(self.default_config)
        config_document = json.loads(self.default_config.read_text(encoding="utf-8")) if self.default_config.exists() else {}
        source_section = config_document.get("source_v6_import") if isinstance(config_document, dict) else None
        if isinstance(source_section, dict) and "segment_writer_limit" in source_section:
            settings.validate_for_workers(workers)
            limit = settings.segment_writer_limit
        else:
            limit = min(settings.segment_writer_limit, workers)
        return workers, settings, limit

    @staticmethod
    def _source_v6_worker_failure(error: BaseException, preflight: object) -> dict[str, object] | None:
        if not isinstance(error, SourceV6WorkerFailure):
            return None
        snapshots = getattr(preflight, "snapshots", ())
        snapshot = next((item for item in snapshots if item.input_ordinal == error.input_ordinal), None)
        return {
            "ordinal": error.input_ordinal,
            "path": str(snapshot.path) if snapshot is not None else None,
            "relative_path": error.relative_path,
            "preflight_size": snapshot.source_size if snapshot is not None else None,
            "preflight_mtime_ns": snapshot.source_mtime_ns if snapshot is not None else None,
            "reason": error.reason,
        }

    def source_v6_merge_preflight(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Validate read-only compact inputs and bind a fresh merge target."""
        with self._source_v6_lock:
            current = self._source_v6_job
            if current and current.get("phase") in {"RUNNING", "MERGE_RUNNING"}:
                raise RuntimeError("another Source v6 write is already running")
        paths = tuple(self._path(item) for item in self._source_v6_merge_inputs(payload))
        target = self._path(self._required(payload, "target_path" if payload.get("target_path") else "database_path"))
        preflight = preflight_source_v6_merge(paths, target)
        with self._source_v6_lock:
            self._source_v6_merge_preflight = preflight
            self._source_v6_preflight = None
            self._source_v6_job = {
                "mode": "MERGE",
                "phase": "MERGE_PREFLIGHT_READY",
                "current": 0,
                "total": len(paths),
                "progress": 0.0,
                "token": preflight.token,
                "target_path": str(target),
                "input_paths": tuple(str(path) for path in paths),
                "error": None,
                "cancel_requested": False,
            }
        return {
            "mode": "MERGE",
            "phase": "MERGE_PREFLIGHT_READY",
            "token": preflight.token,
            "target_path": str(target),
            "input_paths": [str(path) for path in paths],
            "total": len(paths),
        }

    def source_v6_merge_start(self, payload: Mapping[str, object]) -> dict[str, object]:
        token = self._required(payload, "preflight_token")
        with self._source_v6_lock:
            preflight = self._source_v6_merge_preflight
            job = self._source_v6_job
            if preflight is None or job is None or job.get("mode") != "MERGE" or token != getattr(preflight, "token", None):
                raise ValueError("latest Source v6 merge preflight token is required")
            if job.get("phase") in {"RUNNING", "MERGE_RUNNING"}:
                raise RuntimeError("Source v6 merge is already running")
            job = dict(job)
            job.update({"phase": "MERGE_RUNNING", "cancel_requested": False})
            self._source_v6_job = job
        try:
            result = merge_source_v6(
                preflight.input_paths,
                preflight.target_path,
                preflight=preflight,
                cancellation_requested=lambda: bool(job["cancel_requested"]),
                # The same setting the import uses; it reaches only the staging
                # readback, so it cannot change what the merge publishes.
                workers=max(1, self._import_settings().workers),
            )
            with self._source_v6_lock:
                job.update({
                    "phase": "MERGED",
                    "current": result.input_count,
                    "total": result.input_count,
                    "progress": 1.0,
                    "accepted_count": result.accepted_count,
                    "duplicate_count": result.duplicate_count,
                    "source_content_digest": result.source_content_digest,
                    "target_path": str(result.target_path),
                })
        except BaseException as error:
            with self._source_v6_lock:
                job.update({
                    "phase": "CANCELLED" if "cancelled" in str(error).lower() else "FAILED",
                    "error": str(error),
                })
        return dict(job)

    def source_v6_merge(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Run a merge-only panel operation, optionally using a preflight token."""
        if payload.get("preflight_token"):
            return self.source_v6_merge_start(payload)
        preflight = self.source_v6_merge_preflight(payload)
        return self.source_v6_merge_start({"preflight_token": preflight["token"]})

    def source_v6_merge_cancel(self) -> dict[str, object]:
        with self._source_v6_lock:
            if self._source_v6_job is None or self._source_v6_job.get("mode") != "MERGE":
                return {"phase": "IDLE", "cancel_requested": False}
            self._source_v6_job["cancel_requested"] = True
            if self._source_v6_job.get("phase") == "MERGE_PREFLIGHT_READY":
                self._source_v6_job["phase"] = "CANCELLED"
            return dict(self._source_v6_job)

    def source_v6_preflight(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Snapshot report metadata without reading HTML and issue a batch token."""
        with self._source_v6_lock:
            current = self._source_v6_job
            if current and current.get("phase") in {"RUNNING", "MERGE_RUNNING"}:
                raise RuntimeError("another Source v6 write is already running")
        root = self._path(self._required(payload, "root_path"))
        database = self._path(self._required(payload, "database_path"))
        with source_v6_import_lock(database):
            import_preflight = preflight_source_v6(root, database)
        token = import_preflight.token
        snapshots = import_preflight.snapshots
        metadata = [
            {
                "ordinal": snapshot.input_ordinal,
                "path": str(snapshot.path),
                "relative_path": snapshot.relative_path,
                "size": snapshot.source_size,
                "mtime_ns": snapshot.source_mtime_ns,
            }
            for snapshot in snapshots
        ]
        with self._source_v6_lock:
            self._source_v6_preflight = {"root": root, "database": database, "snapshots": snapshots, "fragments": (), "import_preflight": import_preflight, "token": token}
            self._source_v6_job = {"phase": "PREFLIGHT_READY", "current": 0, "total": len(snapshots), "progress": 0.0, "token": token, "error": None, "surface_id": None, "cancel_requested": False}
        return {"phase": "PREFLIGHT_READY", "token": token, "total": len(snapshots), "parsed": len(snapshots), "snapshotted": len(snapshots), "failures": [], "scopes": [], "ready_intervals": [], "snapshots": metadata}

    def source_v6_start(self, payload: Mapping[str, object]) -> dict[str, object]:
        token = self._required(payload, "preflight_token")
        with self._source_v6_lock:
            state = self._source_v6_preflight
            if state is None or token != state["token"]:
                raise ValueError("latest Source v6 preflight token is required")
            job = self._source_v6_job
            if job and job.get("phase") in {"RUNNING", "MERGE_RUNNING"}:
                raise RuntimeError("Source v6 import is already running")
            job = {"phase": "RUNNING", "current": 0, "total": len(state["snapshots"]), "progress": 0.0, "token": token, "error": None, "surface_id": None, "cancel_requested": False}
            self._source_v6_job = job
        try:
            database = state["database"]
            import_preflight = state["import_preflight"]
            workers, source_settings, segment_writer_limit = self._source_v6_import_options()
            imported = import_source_v6(
                state["root"],
                database,
                preflight=import_preflight,
                workers=workers,
                batch_size=source_settings.write_batch_size,
                worker_chunk_size=source_settings.worker_chunk_size,
                max_in_flight_chunks=source_settings.max_in_flight_chunks,
                segment_writer_limit=segment_writer_limit,
                cancellation_requested=lambda: bool(job["cancel_requested"]),
            )
            fragments = imported.accepted_fragments
            active_fragments: list[object] = list(imported.active_fragments)
            overlap_tail_decisions: list[dict[str, object]] = []
            with self._source_v6_lock:
                state["fragments"] = tuple(fragments)
                job.update({"current": len(fragments), "progress": 1.0})
            if not active_fragments:
                raise RuntimeError("no stitchable Source v6 fragments available for surface")
            surface_dir = self._import_settings().source_v6_surface_dir or (self.root / "Output" / "source-v6-surfaces")
            intervals = None
            if payload.get("start_ms") is not None and payload.get("end_ms") is not None:
                start_ms, end_ms = int(payload["start_ms"]), int(payload["end_ms"])
                if end_ms <= start_ms:
                    raise ValueError("selected Source v6 interval must be positive")
                scope = str(payload.get("scope_key") or "|".join((active_fragments[0].point.symbol, active_fragments[0].point.side, active_fragments[0].point.timeframe)))
                ready = canonical_ready_intervals(tuple(active_fragments))
                scope_ready = tuple(item for item in ready if item.scope_key == scope)
                if not scope_ready:
                    raise ValueError("selected Source v6 interval is outside canonical READY coverage")
                selected = select_ready_interval(scope_ready, scope_key=scope_ready[0].scope_key, start=datetime.fromtimestamp(start_ms / 1000, timezone.utc).date(), end=datetime.fromtimestamp(end_ms / 1000, timezone.utc).date())
                start_ms = int(datetime.combine(selected.start, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
                end_ms = int(datetime.combine(selected.end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
                intervals = {fragment.point.canonical_key: (start_ms, end_ms) for fragment in active_fragments if "|".join((fragment.point.symbol, fragment.point.side, fragment.point.timeframe)) == scope}
            surface = publish_surface_db(surface_dir, tuple(active_fragments), intervals=intervals, overlap_tail_decisions=overlap_tail_decisions)
            with self._source_v6_lock:
                job.update({"phase": "PUBLISHED", "progress": 1.0, "surface_id": surface.stem, "surface_path": str(surface)})
        except BaseException as error:
            with self._source_v6_lock:
                job.update({"phase": "CANCELLED" if "cancelled" in str(error).lower() else "FAILED", "error": str(error)})
                failure = self._source_v6_worker_failure(error, state["import_preflight"])
                if failure is not None:
                    job["worker_failure"] = failure
        return dict(job)

    def source_v6_start_fresh(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Import once, then publish every selected READY scope into one fresh surface."""
        token = self._required(payload, "preflight_token")
        scope_keys = tuple(sorted({str(item) for item in payload.get("scope_keys", ()) if isinstance(item, str) and item.strip()}))
        with self._source_v6_lock:
            state = self._source_v6_preflight
            if state is None or token != state["token"]:
                raise ValueError("latest Source v6 preflight token is required")
            if self._source_v6_job and self._source_v6_job.get("phase") == "RUNNING":
                raise RuntimeError("Source v6 import is already running")
            job = {"phase": "RUNNING", "current": 0, "total": len(state["snapshots"]), "progress": 0.0, "token": token, "error": None, "surface_id": None, "cancel_requested": False}
            self._source_v6_job = job
        try:
            workers, source_settings, segment_writer_limit = self._source_v6_import_options()
            imported = import_source_v6(
                state["root"],
                state["database"],
                preflight=state["import_preflight"],
                workers=workers,
                batch_size=source_settings.write_batch_size,
                worker_chunk_size=source_settings.worker_chunk_size,
                max_in_flight_chunks=source_settings.max_in_flight_chunks,
                segment_writer_limit=segment_writer_limit,
                cancellation_requested=lambda: bool(job["cancel_requested"]),
            )
            active = tuple(imported.active_fragments)
            with self._source_v6_lock:
                state["fragments"] = tuple(imported.accepted_fragments)
            if not active:
                raise RuntimeError("no stitchable Source v6 fragments available for surface")
            available = {f"{item.point.symbol}|{item.point.side}|{item.point.timeframe}" for item in active}
            selected = scope_keys or tuple(sorted(available))
            if not set(selected).issubset(available):
                raise ValueError("selected Source v6 scope is not present in imported facts")
            surface_dir = self.root / "Output" / "surfaces-v6-compact"
            surface = publish_multiscope_surface(surface_dir, materialize_source_v6(active, selected))
            with self._source_v6_lock:
                job.update({"phase": "PUBLISHED", "current": len(active), "progress": 1.0, "surface_id": surface.stem, "surface_path": str(surface), "scope_keys": selected})
        except BaseException as error:
            with self._source_v6_lock:
                job.update({"phase": "CANCELLED" if "cancel" in str(error).casefold() else "FAILED", "error": str(error)})
                failure = self._source_v6_worker_failure(error, state["import_preflight"])
                if failure is not None:
                    job["worker_failure"] = failure
        return dict(job)

    def source_v6_fresh_library(self) -> tuple[dict[str, object], ...]:
        directory = self.root / "Output" / "surfaces-v6-compact"
        rows = []
        for path in sorted(directory.glob("*.surface-v6.duckdb")) if directory.is_dir() else ():
            try:
                payload = read_multiscope_surface(path)
                rows.append({"path": str(path), "status": "VALID", **payload})
            except (OSError, ValueError, duckdb.Error) as error:
                rows.append({"path": str(path), "status": "MALFORMED", "error": str(error)})
        return tuple(rows)

    def source_v6_start_fresh_analysis(self, payload: Mapping[str, object]) -> dict[str, object]:
        surface = self._path(self._required(payload, "surface_path"))
        if not surface.name.endswith(".surface-v6.duckdb"):
            raise ValueError("fresh Source v6 surface is required")
        read_multiscope_surface(surface)
        dates_path, config_path = self._path(self._required(payload, "listing_dates_path")), self._path(self._required(payload, "config_path"))
        if not dates_path.is_file() or not config_path.is_file():
            raise ValueError("listing-date snapshot and analysis config files are required")
        with self._source_v6_lock:
            if self._source_v6_job and self._source_v6_job.get("phase") == "RUNNING":
                raise RuntimeError("another Source v6 write is already running")
            job = {"phase": "RUNNING", "current": 0, "total": 1, "progress": 0.0, "error": None, "cancel_requested": False}
            self._source_v6_job = job
        try:
            target_value = self._optional_string(payload, "target_path")
            target = self._path(target_value) if target_value else None
            artifact = run_multiscope_analysis(surface, target.parent if target else self.root / "Output" / "analysis-v6-compact", self._analysis_config_loader(config_path), listing_dates=self._source_v6_listing_dates_loader(dates_path), algorithm_version=str(payload.get("algorithm_version") or "0.7-canonical-phase1"), workers=max(1, self._import_settings().workers), cancel_check=lambda: bool(job["cancel_requested"]), filename=target.name if target else None)
            with self._source_v6_lock:
                job.update({"phase": "COMMITTED", "current": 1, "progress": 1.0, "analysis_path": str(artifact), "analysis_id": artifact.stem})
        except BaseException as error:
            with self._source_v6_lock:
                job.update({"phase": "CANCELLED" if job["cancel_requested"] or "cancel" in str(error).casefold() else "FAILED", "error": str(error)})
        return dict(job)

    def source_v6_cancel(self) -> dict[str, object]:
        with self._source_v6_lock:
            if self._source_v6_job is None:
                return {"phase": "IDLE", "cancel_requested": False}
            if self._source_v6_job.get("mode") == "MERGE":
                self._source_v6_job["cancel_requested"] = True
                if self._source_v6_job.get("phase") == "MERGE_PREFLIGHT_READY":
                    self._source_v6_job["phase"] = "CANCELLED"
                return dict(self._source_v6_job)
            self._source_v6_job["cancel_requested"] = True
            if self._source_v6_job.get("phase") == "PREFLIGHT_READY":
                self._source_v6_job["phase"] = "CANCELLED"
            return dict(self._source_v6_job)

    def source_v6_library(self, payload: Mapping[str, object] | None = None) -> tuple[dict[str, object], ...]:
        settings = self._import_settings()
        directory = self._path(payload.get("directory")) if payload and payload.get("directory") else (settings.source_v6_surface_dir or (self.root / "Output" / "source-v6-surfaces"))
        rows: list[dict[str, object]] = []
        for item in scan_surface_diagnostics(directory):
            row: dict[str, object] = {"path": item.path, "status": item.status, "surface_id": item.surface_id, "error": item.error}
            if item.status == "VALID" and item.path.casefold().endswith(".duckdb"):
                try:
                    manifest = read_surface_db(item.path)
                    row.update({
                        "manifest_sha256": manifest.get("manifest_sha256"),
                        "frozen_facts_sha256": manifest.get("frozen_facts_sha256"),
                        "event_mode": manifest.get("event_mode"),
                        "ready_intervals": tuple(dict(interval) for interval in manifest.get("ready_intervals", ())),
                        "compatibility_versions": {
                            key: manifest.get(key)
                            for key in (
                                "surface_schema_version", "metric_schema_version",
                                "event_schema_version", "readiness_schema_version",
                                "frozen_facts_digest_algorithm",
                            )
                        },
                        "analysis_runs": tuple(
                            {
                                "analysis_run_id": item.get("analysis_run_id"),
                                "state": item.get("state"),
                                "event_mode": item.get("event_mode"),
                                "selected_scope": item.get("metadata", {}).get("selected_scope") if isinstance(item.get("metadata"), Mapping) else None,
                                "selected_interval": item.get("selected_interval"),
                                "source_surface_id": item.get("metadata", {}).get("source_surface_id") if isinstance(item.get("metadata"), Mapping) else None,
                            }
                            for item in list_source_v6_analysis_runs(item.path)
                        ),
                    })
                except (OSError, ValueError, duckdb.Error) as error:
                    row.update({"status": "MALFORMED", "error": str(error), "surface_id": None})
            rows.append(row)
        return tuple(rows)

    def _source_v6_surface_selection(self, payload: Mapping[str, object]) -> tuple[Path, dict[str, object]]:
        path = self._path(self._required(payload, "surface_path"))
        rows = self.source_v6_library()
        selected = next((row for row in rows if Path(str(row["path"])).resolve() == path), None)
        if selected is None or selected.get("status") != "VALID":
            raise ValueError("selected Source v6 surface is not a VALID published library entry")
        if path.suffix.casefold() != ".duckdb":
            raise ValueError("v6 analysis requires a published DuckDB surface")
        if selected.get("event_mode") != SOURCE_V6_EVENT_MODE:
            raise ValueError("selected Source v6 surface has unsupported event mode")
        expected_versions = {
            "surface_schema_version": SOURCE_V6_SURFACE_SCHEMA_VERSION,
            "metric_schema_version": SOURCE_V6_METRIC_SCHEMA_VERSION,
            "event_schema_version": SOURCE_V6_EVENT_SCHEMA_VERSION,
            "readiness_schema_version": SOURCE_V6_READINESS_SCHEMA_VERSION,
            "frozen_facts_digest_algorithm": SOURCE_V6_FROZEN_DIGEST_ALGORITHM,
        }
        versions = selected.get("compatibility_versions")
        if not isinstance(versions, Mapping) or any(versions.get(key) != value for key, value in expected_versions.items()):
            raise ValueError("selected Source v6 surface has unsupported compatibility mapping")
        for name in ("surface_id", "manifest_sha256", "frozen_facts_sha256"):
            if not isinstance(selected.get(name), str) or not str(selected[name]).strip():
                raise ValueError(f"selected Source v6 surface has no {name}")
        try:
            current = read_surface_db(path)
        except (OSError, ValueError, duckdb.Error) as error:
            raise ValueError(f"selected Source v6 surface cannot be revalidated: {error}") from error
        for name in ("surface_id", "manifest_sha256", "frozen_facts_sha256"):
            if str(current.get(name)) != str(selected[name]):
                raise ValueError(f"selected Source v6 surface {name} changed since library refresh")
        if current.get("event_mode") != SOURCE_V6_EVENT_MODE:
            raise ValueError("selected Source v6 surface has unsupported event mode")
        if any(current.get(key) != value for key, value in expected_versions.items()):
            raise ValueError("selected Source v6 surface has unsupported compatibility mapping")
        return path, selected

    @staticmethod
    def _source_v6_interval(payload: Mapping[str, object]) -> tuple[str, int, int]:
        scope = payload.get("selected_scope", payload.get("scope"))
        if not isinstance(scope, str) or len(scope.split("|")) != 3 or any(not part.strip() for part in scope.split("|")):
            raise ValueError("selected READY scope is required as symbol|side|timeframe")

        def millis(name: str, aliases: tuple[str, ...]) -> int:
            raw = next((payload.get(alias) for alias in aliases if payload.get(alias) is not None), None)
            if raw is None or isinstance(raw, bool):
                raise ValueError(f"UTC {name} is required")
            try:
                if isinstance(raw, (int, float)):
                    value = int(raw)
                else:
                    value = int(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp() * 1000)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"UTC {name} is invalid") from None
            return value

        start_ms = millis("start", ("start_ms", "selected_start", "start"))
        end_ms = millis("end", ("end_ms", "selected_end", "end"))
        if end_ms <= start_ms:
            raise ValueError("selected UTC interval must be non-empty")
        return scope.strip(), start_ms, end_ms

    def start_source_v6_analysis(self, payload: Mapping[str, object]) -> dict[str, object]:
        surface_path, selected = self._source_v6_surface_selection(payload)
        for field in ("surface_id", "manifest_sha256", "frozen_facts_sha256"):
            requested = payload.get(field)
            if requested not in (None, "") and str(requested) != str(selected[field]):
                raise ValueError(f"selected surface {field} does not match the published library")
        scope, start_ms, end_ms = self._source_v6_interval(payload)
        dates_path = self._path(payload.get("listing_dates_path", payload.get("dates_path", "")))
        config_path = self._path(payload.get("config_path", ""))
        if not dates_path.is_file():
            raise ValueError("listing-date snapshot file is required")
        if not config_path.is_file():
            raise ValueError("analysis config file is required")
        algorithm_version = payload.get("algorithm_version")
        if not isinstance(algorithm_version, str) or not algorithm_version.strip():
            raise ValueError("algorithm_version is required")
        # Load both inputs before creating a job so invalid files remain an
        # actionable request error and cannot leave a misleading RUNNING state.
        try:
            listing_dates = self._source_v6_listing_dates_loader(dates_path)
            config = self._analysis_config_loader(config_path)
            snapshot = _v6_listing_snapshot(listing_dates)
        except Exception as error:
            raise ValueError(f"invalid analysis inputs: {error}") from error
        dates_hash = sha256(_source_v6_canonical_json(snapshot).encode("utf-8")).hexdigest()
        with self._lock:
            job = self._source_v6_analysis_job
            if job is not None and job.running:
                raise RuntimeError("another Source v6 analysis is already running")
            job = _SourceV6AnalysisJob(
                surface_path=surface_path,
                surface_id=str(selected["surface_id"]),
                manifest_sha256=str(selected["manifest_sha256"]),
                frozen_facts_sha256=str(selected["frozen_facts_sha256"]),
                scope=scope,
                start_ms=start_ms,
                end_ms=end_ms,
                dates_path=dates_path,
                config_path=config_path,
                algorithm_version=algorithm_version.strip(),
                listing_dates_sha256=dates_hash,
            )
            self._source_v6_analysis_job = job
        threading.Thread(
            target=self._run_source_v6_analysis,
            args=(job, listing_dates, config),
            name="mrs3-panel-source-v6-analysis",
            daemon=True,
        ).start()
        return self.snapshot()

    def _run_source_v6_analysis(
        self,
        job: _SourceV6AnalysisJob,
        listing_dates: Mapping[str, object],
        config: object,
    ) -> None:
        try:
            with self._lock:
                job.phase = "ADAPTING"
            if job.cancel.is_set():
                raise RuntimeError("Source v6 analysis cancelled")
            adapter_kwargs = {
                "selected_scope": job.scope,
                "selected_start": job.start_ms,
                "selected_end": job.end_ms,
                "expected_surface_id": job.surface_id,
                "expected_manifest_sha256": job.manifest_sha256,
                "expected_frozen_facts_sha256": job.frozen_facts_sha256,
            }
            try:
                adapter_signature = inspect.signature(self._source_v6_adapter_func)
                accepts_cancel = (
                    "cancel_check" in adapter_signature.parameters
                    or any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in adapter_signature.parameters.values())
                )
            except (TypeError, ValueError):
                accepts_cancel = False
            if accepts_cancel:
                adapter_kwargs["cancel_check"] = job.cancel.is_set
            pipeline_input = self._source_v6_adapter_func(job.surface_path, **adapter_kwargs)
            if job.cancel.is_set():
                raise RuntimeError("Source v6 analysis cancelled")
            with self._lock:
                job.completed_units = 1
                job.phase = "ANALYZING"
            if job.cancel.is_set():
                raise RuntimeError("Source v6 analysis cancelled")
            result = self._source_v6_analysis_func(
                job.surface_path,
                pipeline_input,
                config,
                algorithm_version=job.algorithm_version,
                listing_dates=listing_dates,
                listing_dates_sha256=job.listing_dates_sha256,
                cancel_check=job.cancel.is_set,
            )
            if job.cancel.is_set():
                raise RuntimeError("Source v6 analysis cancelled")
            if not isinstance(result, Mapping) or result.get("state") != "COMMITTED":
                raise ValueError("Source v6 analysis did not commit a run")
            analysis_run_id = result.get("analysis_run_id") if isinstance(result, Mapping) else None
            if not isinstance(analysis_run_id, str) or not analysis_run_id.strip():
                raise ValueError("Source v6 analysis committed without a valid analysis_run_id")
            with self._lock:
                job.completed_units = 2
                job.phase = "COMMITTED"
                job.analysis_run_id = analysis_run_id
                metadata = result.get("metadata")
                job.provenance = dict(metadata) if isinstance(metadata, Mapping) else {}
                job.config_sha256 = str(job.provenance.get("algorithm_config_sha256")) if job.provenance.get("algorithm_config_sha256") else None
                job.completed_units = 3
        except BaseException as error:
            with self._lock:
                job.phase = "CANCELLED" if job.cancel.is_set() or "cancel" in str(error).casefold() else "FAILED"
                job.error = str(error)
        finally:
            with self._lock:
                job.running = False

    def cancel_source_v6_analysis(self) -> dict[str, object]:
        with self._lock:
            job = self._source_v6_analysis_job
            if job is None:
                return {"phase": "IDLE", "running": False, "cancel_requested": False}
            job.cancel.set()
            if job.phase == "STARTING":
                job.phase = "CANCELLED"
                job.running = False
            return self._source_v6_analysis_document(job)

    @staticmethod
    def _source_v6_analysis_document(job: _SourceV6AnalysisJob) -> dict[str, object]:
        return {
            "running": job.running,
            "phase": job.phase,
            "status": job.phase,
            "surface_id": job.surface_id,
            "surface_path": str(job.surface_path),
            "manifest_sha256": job.manifest_sha256,
            "frozen_facts_sha256": job.frozen_facts_sha256,
            "scope": job.scope,
            "start_ms": job.start_ms,
            "end_ms": job.end_ms,
            "analysis_run_id": job.analysis_run_id,
            "listing_dates_sha256": job.listing_dates_sha256,
            "config_sha256": job.config_sha256,
            "work_units_completed": job.completed_units,
            "work_units_total": job.total_units,
            "cancel_requested": job.cancel.is_set(),
            "provenance": dict(job.provenance),
            "error": job.error,
        }

    def source_v6_export(self, payload: Mapping[str, object]) -> dict[str, object]:
        surface = self._path(self._required(payload, "surface_path"))
        output = self._path(self._required(payload, "output_dir"))
        return export_plateau_report(surface, output)

    def source_v6_gaps(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Return deterministic missing-cell evidence for the latest preflight."""
        with self._source_v6_lock:
            state = self._source_v6_preflight
        if state is None:
            raise ValueError("Source v6 preflight is required before gap export")
        start = datetime.fromisoformat(str(self._required(payload, "start")).replace("Z", "+00:00")).date()
        end = datetime.fromisoformat(str(self._required(payload, "end")).replace("Z", "+00:00")).date()
        cells = missing_cells(state["fragments"], start=start, end=end, point_keys=payload.get("point_keys"))
        return {"cells": [asdict(cell) for cell in cells], "csv": coverage_csv(cells).decode("utf-8"), "json": coverage_json(cells).decode("utf-8")}

    def _request(self, root: Path, settings: DuckDBImportSettings, *, token: str | None = None, cancellation_requested: Callable[[], bool] | None = None, preflight: ImportPreflight | None = None) -> ImportRequest:
        if settings.source_duckdb_path is None or settings.audit_root is None:
            raise ValueError("source_duckdb_path and audit_root must be configured")
        return ImportRequest(root, settings.source_duckdb_path, settings.audit_root, settings.workers, settings.transaction_batch_size, cancellation_requested=cancellation_requested, expected_preflight_token=token, preflight=preflight)

    def duckdb_import_preflight(self, payload: Mapping[str, object]) -> dict[str, object]:
        settings = self._import_settings()
        root = self._path(self._required(payload, "root_path"))
        # Injected test/dry-run implementations stay synchronous; production
        # preflight runs in the background so the browser can poll progress.
        if self._preflight_func is not preflight_html_import:
            with self._lock:
                if self._import_job and self._import_job.running: raise RuntimeError("HTML import is already running")
                if self._import_preflight_job and self._import_preflight_job.running: raise RuntimeError("HTML preflight is already running")
                job = _ImportPreflightJob(root); self._import_preflight_job = job
            try:
                preflight = self._preflight_func(self._request(root, settings))
            except BaseException:
                with self._lock: job.running, job.phase, job.error = False, "FAILED", "HTML preflight failed"
                raise
            with self._lock:
                self._preflight, self._preflight_root = preflight, root
                job.running, job.phase, job.discovered, job.snapshotted, job.token = False, "READY", preflight.discovered, preflight.discovered, preflight.token
            return self.snapshot()["duckdb_import_preflight"]
        with self._lock:
            if self._import_preflight_job and self._import_preflight_job.running: raise RuntimeError("HTML preflight is already running")
            if self._import_job and self._import_job.running: raise RuntimeError("HTML import is already running")
            self._preflight = None
            self._preflight_root = None
            job = _ImportPreflightJob(root); self._import_preflight_job = job
        threading.Thread(target=self._run_duckdb_import_preflight, args=(job, settings), name="mrs3-panel-duckdb-preflight", daemon=True).start()
        return self.snapshot()["duckdb_import_preflight"]

    def _run_duckdb_import_preflight(self, job: _ImportPreflightJob, settings: DuckDBImportSettings) -> None:
        def progress(item: SnapshotProgress) -> None:
            with self._lock: job.discovered, job.snapshotted, job.total_bytes, job.processed_bytes = item.discovered, item.snapshotted, item.total_bytes, item.processed_bytes
        try:
            if len(inspect.signature(self._preflight_func).parameters) > 1:
                preflight = self._preflight_func(self._request(job.root, settings), progress)
            else: preflight = self._preflight_func(self._request(job.root, settings))
            with self._lock: self._preflight, self._preflight_root, job.token, job.phase = preflight, job.root, preflight.token, "READY"
        except BaseException:
            with self._lock: job.phase, job.error = "FAILED", "HTML preflight failed"
        finally:
            with self._lock: job.running = False

    def start_duckdb_import(self, payload: Mapping[str, object]) -> dict[str, object]:
        root, token = self._path(self._required(payload, "root_path")), self._required(payload, "preflight_token")
        with self._lock:
            if self._import_job is not None and self._import_job.running: raise RuntimeError("another import is already running")
            if self._import_preflight_job is not None and self._import_preflight_job.running: raise RuntimeError("HTML preflight is still running")
            if self._preflight is None or self._preflight_root != root or self._preflight.token != token: raise ValueError("latest preflight token is required")
            job = _ImportJob(token, root, self._preflight); self._import_job = job
        threading.Thread(target=self._run_duckdb_import, args=(job,), name="mrs3-panel-duckdb-import", daemon=True).start()
        return self.snapshot()

    def _run_duckdb_import(self, job: _ImportJob) -> None:
        try:
            settings = self._import_settings()
            def progress(item: ImportProgress) -> None:
                with self._lock: job.phase, job.counts = item.final_state, item.counts
            result = self._import_func(self._request(job.root, settings, token=job.token, cancellation_requested=job.cancel.is_set, preflight=job.preflight), progress)
            with self._lock:
                job.result, job.phase = result, result.final_state
                job.counts = {name: getattr(result, name) for name in job.counts}
                job.evidence_valid = self._valid_import_evidence(result)
        except BaseException as error:
            with self._lock: job.phase, job.error = "FAILED", f"{type(error).__name__}: import failed"
        finally:
            with self._lock: job.running = False

    @staticmethod
    def _import_evidence(result: ImportJobResult) -> tuple[bytes, bytes] | None:
        try:
            manifest_bytes, checklist_bytes = result.manifest_path.read_bytes(), result.checklist_path.read_bytes()
            if sha256(manifest_bytes).hexdigest() != result.manifest_sha256 or sha256(checklist_bytes).hexdigest() != result.checklist_sha256: return None
            manifest, checklist = json.loads(manifest_bytes), json.loads(checklist_bytes)
            valid = isinstance(manifest, dict) and isinstance(checklist, dict) and manifest.get("job_id") == result.job_id == checklist.get("job_id") and manifest.get("final_state") == result.final_state and manifest.get("safe_to_delete") == checklist.get("safe_to_delete") == result.safe_to_delete and manifest.get("artifacts", {}).get("checklist", {}).get("sha256") == result.checklist_sha256
            return (manifest_bytes, checklist_bytes) if valid else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError): return None

    @staticmethod
    def _valid_import_evidence(result: ImportJobResult) -> bool:
        return PanelController._import_evidence(result) is not None

    def cancel_duckdb_import(self) -> dict[str, object]:
        with self._lock:
            if self._import_job is None: raise ValueError("no import job")
            self._import_job.cancel.set()
        return self.snapshot()["duckdb_import"]

    def migrate_duckdb_import(self, payload: Mapping[str, object]) -> dict[str, object]:
        with self._lock:
            settings = self._import_settings()
        if settings.source_duckdb_path is None: raise ValueError("source_duckdb_path must be configured")
        target = self._path(self._required(payload, "target_path"))
        if target == settings.source_duckdb_path: raise ValueError("source and target paths must be different")
        arguments = (settings.source_duckdb_path, target)
        options = {"workers": settings.workers, "transaction_batch_size": settings.transaction_batch_size}
        try:
            inspect.signature(self._migration_func).bind(*arguments, **options)
        except TypeError:
            result = self._migration_func(*arguments)
        else:
            result = self._migration_func(*arguments, **options)
        if not getattr(getattr(result, "validation", None), "valid", False) or not target.is_file() or _stream_sha256(target) != getattr(result, "target_database_sha256", None): raise ValueError("migration target validation failed")
        with self._lock:
            current = self._import_settings()
            if current.source_duckdb_path != settings.source_duckdb_path:
                raise ValueError("source_duckdb_path changed during migration")
            updated = DuckDBImportSettings(target, current.analysis_duckdb_path, current.default_html_root, current.audit_root, current.workers, current.transaction_batch_size)
            save_duckdb_import_settings(self.default_config, updated)
        return self._settings_document(updated)

    @staticmethod
    def _direct_request(payload: Mapping[str, object]) -> DirectBuildRequest:
        def symbols(value: object) -> tuple[str, ...]:
            items = value.split(";") if isinstance(value, str) else value
            if not isinstance(items, (list, tuple)): raise ValueError("symbols must be a list or semicolon-separated string")
            result = tuple(sorted({str(item).strip() for item in items if str(item).strip()}))
            if not result: raise ValueError("at least one symbol is required")
            return result
        def selected_scopes(value: object) -> tuple[str, ...]:
            if value is None:
                return ()
            if not isinstance(value, (list, tuple)):
                raise ValueError("selected_scopes must be a list")
            scopes: set[str] = set()
            for item in value:
                if not isinstance(item, Mapping):
                    raise ValueError("selected_scopes must contain objects")
                symbol = str(item.get("symbol", "")).strip()
                timeframe = str(item.get("timeframe", "")).strip()
                if not symbol or not timeframe:
                    raise ValueError("selected_scopes entries must include symbol and timeframe")
                scopes.add(f"{symbol}|{timeframe}")
            return tuple(sorted(scopes))
        def shifts() -> tuple[int, ...]:
            raw = payload.get("required_shifts_bp")
            if raw is not None:
                values = raw.split(";") if isinstance(raw, str) else raw
                try: return tuple(sorted({int(value) for value in values if str(value).strip()}))
                except (TypeError, ValueError): raise ValueError("required_shifts_bp must be integers") from None
            range_values = tuple(payload.get(name) for name in ("shift_start_bp", "shift_end_bp", "shift_step_bp"))
            if all(value is None or str(value).strip() == "" for value in range_values):
                return ()
            try:
                start, end, step = (int(payload[name]) for name in ("shift_start_bp", "shift_end_bp", "shift_step_bp"))
            except (KeyError, TypeError, ValueError): raise ValueError("shift range must be integers") from None
            if step < 1 or end < start or (end - start) % step: raise ValueError("shift range is invalid")
            return tuple(range(start, end + 1, step))
        side = PanelController._required(payload, "side").upper()
        if side not in {"LONG", "SHORT"}: raise ValueError("side must be LONG or SHORT")
        return DirectBuildRequest(
            PanelController._required(payload, "start_utc"), PanelController._required(payload, "end_utc"),
            side, symbols(payload.get("symbols", ())), shifts(),
            _DIRECT_MATERIALIZER_VERSION, _DIRECT_POINT_CONFIG_HASH, selected_scopes(payload.get("selected_scopes")),
        )

    @staticmethod
    def _direct_coverage_payload(payload: Mapping[str, object]) -> tuple[str | None, tuple[str, ...]]:
        raw_side = payload.get("side")
        if raw_side is None or str(raw_side).strip() == "":
            side = None
        else:
            side = str(raw_side).upper()
            if side not in {"LONG", "SHORT"}:
                raise ValueError("side must be LONG or SHORT")
        items = payload.get("symbols", ())
        values = items.split(";") if isinstance(items, str) else items
        if not isinstance(values, (list, tuple)):
            raise ValueError("symbols must be a list or semicolon-separated string")
        symbols = tuple(sorted({str(item).strip() for item in values if str(item).strip()}))
        return side, symbols

    @staticmethod
    def _direct_window_ms(request: DirectBuildRequest) -> tuple[int, int]:
        try:
            start = datetime.fromisoformat(request.start_utc.replace("Z", "+00:00"))
            end = datetime.fromisoformat(request.end_utc.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("UTC window is invalid") from error
        start = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start.astimezone(timezone.utc)
        end = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end.astimezone(timezone.utc)
        if end <= start:
            raise ValueError("UTC end must be later than start")
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    def _resolve_direct_request(self, payload: Mapping[str, object]) -> DirectBuildRequest:
        request = self._direct_request(payload)
        if request.required_shifts_bp:
            return request
        start_ms, end_ms = self._direct_window_ms(request)

        def available_shifts(connection: duckdb.DuckDBPyConnection) -> tuple[int, ...]:
            rows = connection.execute(
                """select distinct p.shift_bp
                     from active_reports r join point_configs p using(canonical_point_key)
                     join time_grids g using(grid_hash)
                    where p.side=? and p.symbol in (select * from unnest(?))
                      and g.sample_count > 0 and g.start_timestamp_ms <= ? and g.end_timestamp_ms >= ?
                    order by p.shift_bp""",
                [request.side, list(request.symbols), start_ms, end_ms],
            ).fetchall()
            return tuple(int(row[0]) for row in rows)

        shifts = self._with_source(available_shifts)
        if not shifts:
            raise ValueError("no shifts fully cover the selected UTC window")
        return replace(request, required_shifts_bp=shifts)

    @staticmethod
    def _direct_token(
        request: DirectBuildRequest,
        preflight: DirectPreflight,
        *,
        coverage_scan_token: str | None = None,
        requests: Sequence[DirectBuildRequest] = (),
        preflights: Sequence[DirectPreflight] = (),
    ) -> str:
        def one(current_request: DirectBuildRequest, current: DirectPreflight) -> dict[str, object]:
            request_document = {}
            for name in current_request.__dataclass_fields__:
                value = getattr(current_request, name)
                if isinstance(value, bytes):
                    value = hashlib.sha256(value).hexdigest()
                request_document[name] = list(value) if isinstance(value, tuple) else value
            return {
                "request": request_document,
                "usable": {key: list(value) for key, value in current.usable_timeframes.items()},
                "unavailable": {key: list(value) for key, value in current.unavailable_symbols.items()},
                "issues": [(item.symbol, item.timeframe, item.code, item.detail) for item in current.coverage_issues],
                "grid": PanelController._jsonable(dict(current.grid_contract)),
                "hashes": list(current.source_hashes),
                "manifest": list(current.manifest),
                "points": list(current.accepted_point_keys),
                "witnesses": PanelController._jsonable(dict(current.witnesses)),
                "point_evidence_sha256": current.point_evidence_sha256,
                "audit": {
                    "artifact_name": current.audit_artifact_name,
                    "schema_version": current.audit_schema_version,
                    "size_bytes": current.audit_size_bytes,
                    "row_count": current.audit_row_count,
                    "sha256": current.audit_sha256,
                },
            }
        request_list = tuple(requests) or (request,)
        preflight_list = tuple(preflights) or (preflight,)
        document = {
            "coverage_scan_token": coverage_scan_token,
            "contracts": {
                "grid_kind": V2_GRID_CONTRACT_KIND,
                "grid_version": CANONICAL_GRID_VERSION,
                "readiness_version": READINESS_CONTRACT_VERSION,
                "materializer_version": CANONICAL_MATERIALIZER_VERSION,
                "semantic_version": POINT_MATERIALIZATION_SEMANTICS_VERSION,
            },
            "sides": [one(current_request, current) for current_request, current in zip(request_list, preflight_list, strict=True)],
        }
        return sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _direct_paths(self) -> tuple[Path, Path]:
        settings = self._import_settings()
        source, analysis = settings.source_duckdb_path, settings.analysis_duckdb_path
        if source is None or analysis is None: raise ValueError("source_duckdb_path and analysis_duckdb_path must be configured")
        if source == analysis: raise ValueError("source and analysis DuckDB paths must differ")
        return source, analysis

    def _with_source(self, callback: Callable[[duckdb.DuckDBPyConnection], object]) -> object:
        source, _ = self._direct_paths()
        try:
            connection = self._direct_connection_factory(str(source), read_only=True)
            try: return callback(connection)
            finally: connection.close()
        except (duckdb.Error, OSError) as error:
            raise ValueError("direct preflight failed") from error

    def _analysis_path(self) -> Path:
        analysis = self._import_settings().analysis_duckdb_path
        if analysis is None:
            raise ValueError("analysis_duckdb_path must be configured")
        return analysis

    def _with_analysis(self, read_only: bool, callback: Callable[[duckdb.DuckDBPyConnection], object]) -> object:
        try:
            connection = self._direct_connection_factory(str(self._analysis_path()), read_only=read_only)
        except (duckdb.Error, OSError) as error:
            raise ValueError("analysis database is unavailable") from error
        try:
            try:
                return callback(connection)
            except (duckdb.Error, OSError) as error:
                raise ValueError("analysis operation failed") from error
        finally:
            connection.close()

    @staticmethod
    def _jsonable(value: object) -> object:
        if is_dataclass(value):
            return PanelController._jsonable(asdict(value))
        if isinstance(value, Mapping):
            return {str(key): PanelController._jsonable(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [PanelController._jsonable(item) for item in value]
        if isinstance(value, Decimal):
            return str(value)
        return value

    @staticmethod
    def _direct_preflight_document(request: DirectBuildRequest, preflight: DirectPreflight, token: str) -> dict[str, object]:
        return {
            "token": token,
            "preflight_token": token,
            "required_shifts_bp": list(request.required_shifts_bp),
            "selected_symbols": list(preflight.usable_timeframes),
            "usable_timeframes": {key: list(value) for key, value in preflight.usable_timeframes.items()},
            "unavailable_symbols": {key: list(value) for key, value in preflight.unavailable_symbols.items()},
            "coverage_issues": [{"symbol": item.symbol, "timeframe": item.timeframe, "code": item.code, "detail": item.detail} for item in preflight.coverage_issues],
            "coverage_rows": [
                {
                    "pair": item.symbol,
                    "side": item.side,
                    "timeframe": item.timeframe,
                    "selectable": item.selectable,
                    "interval_start_utc": item.interval_start_utc,
                    "interval_end_utc": item.interval_end_utc,
                    "gap_details": list(item.gap_details),
                }
                for item in preflight.coverage_rows
            ],
            "selected_scopes": [
                {"symbol": item.symbol, "timeframe": item.timeframe}
                for item in preflight.coverage_rows
                if item.selectable
            ],
            "audit_artifact_name": request.audit_artifact_name,
            "audit_schema_version": request.audit_schema_version,
            "audit_size_bytes": request.audit_size_bytes,
            "audit_row_count": request.audit_row_count,
            "audit_sha256": request.audit_sha256,
        }

    @staticmethod
    def _direct_scan_document(scan: _CoverageScan) -> dict[str, object]:
        usable: dict[str, list[str]] = {}
        coverage_rows: list[dict[str, object]] = []
        selected_scopes: list[dict[str, str]] = []
        for item in scan.coverage.rows:
            coverage_rows.append(
                {
                    "pair": item.symbol,
                    "side": item.side,
                    "timeframe": item.timeframe,
                    "selectable": item.selectable,
                    "interval_start_utc": item.interval_start_utc,
                    "interval_end_utc": item.interval_end_utc,
                    "gap_details": list(item.gap_details),
                }
            )
            if item.selectable:
                usable.setdefault(item.symbol, []).append(item.timeframe)
                selected_scopes.append({"symbol": item.symbol, "timeframe": item.timeframe})
        return {
            "token": scan.token,
            "required_shifts_bp": [],
            "selected_symbols": list(usable),
            "usable_timeframes": {
                symbol: sorted(set(timeframes))
                for symbol, timeframes in usable.items()
            },
            "unavailable_symbols": {},
            "coverage_issues": [],
            "coverage_rows": coverage_rows,
            "selected_scopes": selected_scopes,
        }

    @staticmethod
    def _verified_direct_bytes(path: Path, expected_sha256: str) -> bytes:
        try:
            data = path.read_bytes()
        except OSError as error:
            raise ValueError("direct artifact is unavailable") from error
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ValueError("direct artifact verification failed")
        return data

    @staticmethod
    def _direct_selected_scopes(
        payload: Mapping[str, object],
    ) -> tuple[DirectScope, ...]:
        raw = payload.get("selected_scopes")
        if not isinstance(raw, (list, tuple)):
            raise ValueError("selected_scopes must be a list")
        scopes: list[DirectScope] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValueError("selected_scopes must contain objects")
            symbol = str(item.get("symbol", "")).strip()
            timeframe = str(item.get("timeframe", "")).strip()
            raw_side = item.get("side")
            side = str(raw_side).strip().upper() if raw_side not in (None, "") else ""
            if not symbol or not timeframe or side not in {"LONG", "SHORT"}:
                raise ValueError("selected scope must include symbol, side, and timeframe")
            scopes.append(DirectScope(symbol, side, timeframe))
        if not scopes:
            raise ValueError("at least one selected scope is required")
        return tuple(scopes)

    @staticmethod
    def _direct_common_interval_document(interval: DirectCommonInterval) -> dict[str, object]:
        return {
            "side": interval.side,
            "start_utc": interval.start_utc,
            "end_utc": interval.end_utc,
            "start_date": interval.start_utc[:10],
            "end_date": interval.end_utc[:10],
            "display": f"{interval.start_utc[:10]} .. {interval.end_utc[:10]}",
            "scopes": [
                {
                    "symbol": scope.symbol,
                    "side": scope.side,
                    "timeframe": scope.timeframe,
                }
                for scope in interval.scopes
            ],
        }

    @staticmethod
    def _direct_common_intervals_document(
        intervals: Sequence[DirectCommonInterval],
    ) -> dict[str, dict[str, object]]:
        return {
            interval.side.lower(): PanelController._direct_common_interval_document(interval)
            for interval in intervals
        }

    @staticmethod
    def _direct_coverage_request(interval: DirectCommonInterval) -> DirectBuildRequest:
        symbols = tuple(sorted({scope.symbol for scope in interval.scopes}))
        selected_scopes = tuple(
            f"{scope.symbol}|{scope.timeframe}"
            for scope in sorted(interval.scopes, key=lambda scope: (scope.symbol, scope.timeframe))
        )
        return DirectBuildRequest(
            interval.start_utc,
            interval.end_utc,
            interval.side,
            symbols,
            tuple(AlgorithmConfig().canonical_shifts_bp),
            _DIRECT_MATERIALIZER_VERSION,
            _DIRECT_POINT_CONFIG_HASH,
            selected_scopes=selected_scopes,
            grid_contract_kind=V2_GRID_CONTRACT_KIND,
            readiness_contract_version=READINESS_CONTRACT_VERSION,
            readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
        )

    def duckdb_direct_preflight(self, payload: Mapping[str, object]) -> dict[str, object]:
        coverage_token = self._optional_string(payload, "coverage_token") or None
        selected_scopes_supplied = payload.get("selected_scopes") is not None
        if selected_scopes_supplied and coverage_token is None:
            raise ValueError("selected scopes require a valid coverage token")
        if selected_scopes_supplied and coverage_token is not None:
            with self._lock:
                scan = self._direct_coverage_scan
            if scan is None or coverage_token != scan.token:
                raise ValueError("stale coverage token is required")
            scopes = self._direct_selected_scopes(payload)
            intervals = common_intervals_for_scopes(scan.coverage, scopes)
            audit_root = self._import_settings().audit_root
            if audit_root is None:
                raise ValueError("audit_root must be configured for direct builds")
            requests = tuple(self._direct_coverage_request(interval) for interval in intervals)
            try:
                frozen_requests, frozen_preflights = self._with_source(
                    lambda source: freeze_direct_preflights(
                        source,
                        requests,
                        audit_root=audit_root,
                        coverage_scan=scan,
                        preflight_func=self._direct_preflight_func,
                    )
                )
            except BaseException:
                with self._lock:
                    self._direct_selected_preflight = None
                    self._direct_preflight = None
                raise
            token = self._direct_token(
                frozen_requests[0],
                frozen_preflights[0],
                coverage_scan_token=scan.token,
                requests=frozen_requests,
                preflights=frozen_preflights,
            )
            state = _DirectPreflightState(
                scan,
                frozen_requests,
                frozen_preflights,
                token,
                Path(audit_root),
            )
            with self._lock:
                current_scan = self._direct_coverage_scan
                if current_scan is not scan or current_scan is None or current_scan.token != scan.token:
                    self._direct_selected_preflight = None
                    self._direct_preflight = None
                    raise ValueError("stale coverage token is required")
                self._direct_selected_preflight = state
                self._direct_preflight = None
            document = self._direct_preflight_document(
                frozen_requests[0], frozen_preflights[0], token
            )
            document["selected_intervals"] = self._direct_common_intervals_document(intervals)
            document["coverage_token"] = scan.token
            document["selected_sides"] = [request.side for request in frozen_requests]
            return document
        request = self._resolve_direct_request(payload)
        preflight = self._with_source(lambda source: self._direct_preflight_func(source, request))
        assert isinstance(preflight, DirectPreflight)
        token = self._direct_token(request, preflight)
        with self._lock: self._direct_preflight = (request, preflight, token)
        document = self._direct_preflight_document(request, preflight, token)
        return document

    def duckdb_direct_coverage(self, payload: Mapping[str, object]) -> dict[str, object]:
        with self._lock:
            if self._direct_job is not None:
                if self._direct_job.running:
                    raise RuntimeError("another direct build is already running")
            self._direct_job = None
            self._direct_coverage_scan = None
            self._direct_selected_preflight = None
            self._direct_preflight = None
            self._direct_artifacts.clear()
        side, symbols = self._direct_coverage_payload(payload)
        if self._direct_coverage_func is not list_duckdb_direct_coverage:
            rows = self._with_source(
                lambda source: self._direct_coverage_func(
                    source, side=side, symbols=symbols
                )
            )
            return {
                "coverage_rows": [
                    {
                        "pair": item.symbol,
                        "side": item.side,
                        "timeframe": item.timeframe,
                        "selectable": item.selectable,
                        "interval_start_utc": item.interval_start_utc,
                        "interval_end_utc": item.interval_end_utc,
                        "gap_details": list(item.gap_details),
                    }
                    for item in rows
                ]
            }
        audit_root = self._import_settings().audit_root
        if audit_root is None:
            raise ValueError("audit_root must be configured for coverage scans")
        scan = self._with_source(
            lambda source: self._direct_coverage_scan_func(
                source,
                audit_root=audit_root,
                symbols=symbols,
            )
        )
        if not isinstance(scan, _CoverageScan):
            raise ValueError("coverage scan returned an invalid result")
        inventory_bytes = self._verified_direct_bytes(scan.inventory_path, scan.inventory_sha256)
        with self._lock:
            self._direct_coverage_scan = scan
            self._direct_artifacts["coverage_inventory"] = (scan.inventory_path.name, inventory_bytes)
        return {
            "coverage_rows": [
                {
                    "pair": item.symbol,
                    "side": item.side,
                    "timeframe": item.timeframe,
                    "selectable": item.selectable,
                    "interval_start_utc": item.interval_start_utc,
                    "interval_end_utc": item.interval_end_utc,
                    "gap_details": list(item.gap_details),
                }
                for item in scan.coverage.rows
            ],
            "token": scan.token,
            "artifacts": {"coverage_inventory": scan.inventory_path.name},
        }

    def start_duckdb_direct(self, payload: Mapping[str, object]) -> dict[str, object]:
        with self._lock:
            if self._direct_job is not None and self._direct_job.running:
                raise RuntimeError("another direct build is already running")
            self._direct_job = None
        coverage_token = self._optional_string(payload, 'coverage_token') or None
        parent_surface_id = self._optional_string(payload, "parent_surface_id") or None
        if coverage_token is not None:
            raise ValueError("stale coverage token cannot start materialization; preflight_token is required")

        supplied_token = self._required(payload, "preflight_token")
        with self._lock:
            selected_state = self._direct_selected_preflight
        if selected_state is None:
            raise ValueError("latest preflight token is required")
        if selected_state is not None:
            if supplied_token != selected_state.token:
                raise ValueError("latest preflight token is required")
            raw_scopes = payload.get("selected_scopes")
            if raw_scopes is not None:
                selected_scopes = tuple(
                    f"{scope.symbol}|{scope.timeframe}"
                    for scope in self._direct_selected_scopes(payload)
                )
                expected_scopes = tuple(
                    scope
                    for request in selected_state.requests
                    for scope in request.selected_scopes
                )
                if selected_scopes != expected_scopes:
                    raise ValueError("latest preflight token is required")
            requests = selected_state.requests
            if parent_surface_id is not None and len(requests) != 1:
                raise ValueError("parent_surface_id requires a single side")
            if parent_surface_id is not None:
                self._with_analysis(
                    True,
                    lambda connection: require_canonical_operational_surface(
                        connection, parent_surface_id
                    ),
                )
            with self._lock:
                if self._direct_job and self._direct_job.running:
                    raise RuntimeError("another direct build is already running")
                if self._direct_selected_preflight is not selected_state:
                    raise ValueError("latest preflight token is required")
                job = _DirectJob(
                    requests=requests,
                    coverage_scan=selected_state.coverage_scan,
                    audit_root=selected_state.audit_root,
                    frozen_preflights=selected_state.preflights,
                    parent_surface_id=parent_surface_id,
                    materialization_settings=load_direct_materialization_settings(self.default_config),
                )
                job.artifacts.update(self._direct_artifacts)
                self._direct_job = job
            threading.Thread(target=self._run_duckdb_direct, args=(job,), name="mrs3-panel-duckdb-direct", daemon=True).start()
            return self.snapshot()

        request, token = self._resolve_direct_request(payload), supplied_token
        with self._lock:
            if self._direct_preflight is None: raise ValueError("latest preflight token is required")
            original, preflight, expected = self._direct_preflight
            raw_scopes = payload.get("selected_scopes")
            if raw_scopes is not None:
                if token != expected or request != original:
                    raise ValueError("latest preflight token is required")
                selected = request
                allowed_scopes = {
                    f"{item.symbol}|{item.timeframe}"
                    for item in preflight.coverage_rows
                    if item.selectable
                }
                usable_scopes = {
                    f"{symbol}|{timeframe}"
                    for symbol, timeframes in preflight.usable_timeframes.items()
                    for timeframe in timeframes
                }
                if not selected.selected_scopes:
                    raise ValueError("at least one scope is required")
                if not set(selected.selected_scopes).issubset(allowed_scopes & usable_scopes):
                    raise ValueError("selected scope is unavailable")
            else:
                if token != expected or request != original:
                    raise ValueError("latest preflight token is required")
                chosen = payload.get("selected_symbols", tuple(preflight.usable_timeframes))
                selected = self._direct_request({**payload, "symbols": chosen, "required_shifts_bp": request.required_shifts_bp})
                if replace(selected, symbols=request.symbols, selected_scopes=()) != request:
                    raise ValueError("latest preflight token is required")
                if not set(selected.symbols).issubset(preflight.usable_timeframes):
                    raise ValueError("selected symbol is unavailable")
        if parent_surface_id is not None:
            self._with_analysis(
                True,
                lambda connection: require_canonical_operational_surface(
                    connection, parent_surface_id
                ),
            )
        with self._lock:
            if self._direct_job and self._direct_job.running:
                raise RuntimeError("another direct build is already running")
            if self._direct_preflight != (original, preflight, expected):
                raise ValueError("latest preflight token is required")
            job = _DirectJob(
                selected,
                original,
                preflight,
                parent_surface_id=parent_surface_id,
                materialization_settings=load_direct_materialization_settings(self.default_config),
            )
            self._direct_job = job
        threading.Thread(target=self._run_duckdb_direct, args=(job,), name="mrs3-panel-duckdb-direct", daemon=True).start()
        return self.snapshot()

    def _start_direct_coverage_job(
        self,
        payload: Mapping[str, object],
        coverage_token: str,
        parent_surface_id: str | None,
    ) -> dict[str, object]:
        with self._lock:
            scan = self._direct_coverage_scan
            if scan is None or coverage_token != scan.token:
                raise ValueError("stale coverage token is required")
            scopes = self._direct_selected_scopes(payload)
            intervals = common_intervals_for_scopes(scan.coverage, scopes)
            requests = tuple(
                self._direct_coverage_request(interval)
                for interval in intervals
            )
            if parent_surface_id is not None and len(requests) != 1:
                raise ValueError("parent_surface_id requires a single side")
            audit_root = self._import_settings().audit_root
            if audit_root is None:
                raise ValueError("audit_root must be configured for direct builds")
            if self._direct_job is not None and self._direct_job.running:
                raise RuntimeError("another direct build is already running")
            job = _DirectJob(
                requests=requests,
                coverage_scan=scan,
                audit_root=audit_root,
                parent_surface_id=parent_surface_id,
                materialization_settings=load_direct_materialization_settings(self.default_config),
            )
            job.artifacts.update(self._direct_artifacts)
            self._direct_job = job
        threading.Thread(target=self._run_duckdb_direct, args=(job,), name="mrs3-panel-duckdb-direct", daemon=True).start()
        return self.snapshot()

    def _run_duckdb_direct(self, job: _DirectJob) -> None:
        source = analysis = None
        try:
            source_path, analysis_path = self._direct_paths()
            source = self._direct_connection_factory(str(source_path), read_only=True)
            if job.cancel.is_set(): raise DirectMaterializationError("direct build cancelled")
            frozen_manifest_total: int | None = None
            if job.frozen_preflights is not None:
                frozen_manifest_total = sum(len(preflight.manifest) for preflight in job.frozen_preflights)
            side_base_points = 0
            def progress(phase: str, **facts: object) -> None:
                nonlocal side_base_points
                with self._lock:
                    job.phase = phase
                    incoming_side = facts.get("side")
                    if incoming_side is not None and incoming_side != job.side:
                        side_base_points = job.point_count
                        job.side = str(incoming_side)
                    if "materialized_points" in facts:
                        job.point_count = side_base_points + int(facts["materialized_points"])
                    else:
                        job.point_count = int(facts.get("materialized_points", job.point_count))
                    if frozen_manifest_total is not None:
                        job.total_points = frozen_manifest_total
                    elif "total_points" in facts:
                        job.total_points = int(facts["total_points"])
                    if "workers" in facts:
                        job.workers = int(facts["workers"])
                    if "elapsed_seconds" in facts:
                        job.elapsed_seconds = float(facts["elapsed_seconds"])
                    if "points_per_second" in facts:
                        job.points_per_second = float(facts["points_per_second"])
                    if "ordinal" in facts:
                        job.ordinal = int(facts["ordinal"])
                    if "total" in facts:
                        job.total = int(facts["total"])
            if job.requests is not None:
                frozen_v2 = any(
                    request.grid_contract_kind == V2_GRID_CONTRACT_KIND
                    or (
                        index < len(job.frozen_preflights or ())
                        and job.frozen_preflights[index].grid_contract.get("kind") == V2_GRID_CONTRACT_KIND
                    )
                    for index, request in enumerate(job.requests)
                )
                if frozen_v2:
                    if (
                        job.frozen_preflights is None
                        or len(job.frozen_preflights) != len(job.requests)
                        or job.coverage_scan is None
                        or job.audit_root is None
                    ):
                        raise DirectMaterializationError("STALE_PREFLIGHT")
                    try:
                        prepared = replay_direct_preflights(
                            source,
                            job.requests,
                            job.frozen_preflights,
                            audit_root=job.audit_root,
                            coverage_scan=job.coverage_scan,
                            cancellation=job.cancel.is_set,
                            materialization_settings=job.materialization_settings,
                            progress_callback=progress,
                        )
                    except TypeError as error:
                        if "progress_callback" not in str(error) or "unexpected keyword argument" not in str(error):
                            raise
                        prepared = replay_direct_preflights(
                            source,
                            job.requests,
                            job.frozen_preflights,
                            audit_root=job.audit_root,
                            coverage_scan=job.coverage_scan,
                            cancellation=job.cancel.is_set,
                            materialization_settings=job.materialization_settings,
                        )
                else:
                    try:
                        prepared = self._direct_prepare_func(
                            source,
                            job.requests,
                            audit_root=job.audit_root,
                            coverage_scan=job.coverage_scan,
                            cancellation=job.cancel.is_set,
                            progress_callback=progress,
                            materialization_settings=job.materialization_settings,
                        )
                    except TypeError as error:
                        if 'materialization_settings' not in str(error) or 'unexpected keyword argument' not in str(error):
                            raise
                        prepared = self._direct_prepare_func(
                            source,
                            job.requests,
                            audit_root=job.audit_root,
                            coverage_scan=job.coverage_scan,
                            cancellation=job.cancel.is_set,
                            progress_callback=progress,
                        )
                source.close()
                source = None
                analysis = self._direct_connection_factory(str(analysis_path), read_only=False)
                try:
                    result = self._direct_publish_func(
                        analysis,
                        prepared,
                        audit_root=job.audit_root,
                        cancellation=job.cancel.is_set,
                        progress_callback=progress,
                        parent_surface_id=job.parent_surface_id,
                    )
                except TypeError as error:
                    if "audit_root" not in str(error):
                        raise
                    result = self._direct_publish_func(
                        analysis,
                        prepared,
                        cancellation=job.cancel.is_set,
                        progress_callback=progress,
                        parent_surface_id=job.parent_surface_id,
                    )
                with self._lock:
                    job.publication_state = str(result.publication_state)
                    job.phase = str(result.phase or result.publication_state)
                    job.error = _safe_direct_error(result.error)
                    job.surface_id = (
                        str(result.surfaces[0].surface_id)
                        if result.surfaces
                        else None
                    )
                    job.point_count = sum(
                        len(getattr(surface, "points", ()))
                        for surface in result.surfaces
                    )
                    if job.audit_root is not None:
                        for surface in prepared:
                            side = surface.request.side.lower()
                            audit_sha = (
                                getattr(surface.preflight, "audit_sha256", "")
                                if hasattr(surface, "preflight")
                                else ""
                            )
                            if audit_sha:
                                filename = (
                                    getattr(surface.preflight, "audit_artifact_name", "")
                                    or f"surface_coverage_audit_{surface.request.side}.csv"
                                )
                                audit_bytes = getattr(surface.preflight, "audit_bytes", None)
                                if audit_bytes is None:
                                    audit_path = (
                                        Path(job.audit_root)
                                        / "surface_coverage"
                                        / str(audit_sha)
                                        / filename
                                    )
                                    audit_bytes = self._verified_direct_bytes(audit_path, audit_sha)
                                elif hashlib.sha256(audit_bytes).hexdigest() != audit_sha:
                                    raise DirectMaterializationError("publication audit hash verification failed")
                                job.artifacts[f"surface_coverage_audit_{side}"] = (filename, bytes(audit_bytes))
            else:
                active = self._direct_preflight_func(source, job.preflight_request)
                if active != job.preflight: raise DirectMaterializationError("active source changed after preflight")
                analysis = self._direct_connection_factory(str(analysis_path), read_only=False)
                if job.parent_surface_id is None:
                    published = self._direct_build_func(source, analysis, job.request, job.cancel.is_set, progress)
                else:
                    published = self._direct_build_func(
                        source, analysis, job.request, job.cancel.is_set, progress,
                        parent_surface_id=job.parent_surface_id,
                    )
                with self._lock:
                    job.surface_id, job.point_count, job.phase, job.publication_state = str(published.surface_id), len(published.points), "PUBLISHED", "PUBLISHED"
        except BaseException as error:
            with self._lock:
                job.phase = "CANCELLED" if job.cancel.is_set() else "FAILED"
                job.publication_state = job.phase
                job.error = "direct build cancelled" if job.cancel.is_set() else _direct_error_message(error)
        finally:
            if analysis is not None: analysis.close()
            if source is not None: source.close()
            with self._lock: job.running = False

    def cancel_duckdb_direct(self) -> dict[str, object]:
        with self._lock:
            if self._direct_job is None: raise ValueError("no direct build")
            self._direct_job.cancel.set()
        return self.snapshot()["duckdb_direct"]

    def analysis_library(self, payload: Mapping[str, object]) -> object:
        allowed = ("side", "period_start_utc", "period_end_utc", "symbol", "build_mode", "parent_surface_id", "source_hash")
        filters = {name: self._optional_string(payload, name) for name in allowed}
        filters = {name: value for name, value in filters.items() if value}
        if "side" in filters:
            filters["side"] = filters["side"].upper()
            if filters["side"] not in {"LONG", "SHORT"}:
                raise ValueError("side must be LONG or SHORT")
        rows = self._with_analysis(True, lambda connection: self._analysis_library_func(connection, **filters))
        return self._jsonable(rows)

    def initialize_analysis(self) -> dict[str, int]:
        version = self._with_analysis(False, ensure_analysis_schema)
        return {"schema_version": int(version)}

    def compare_analysis(self, payload: Mapping[str, object]) -> object:
        left = self._required(payload, "left_run_id")
        right = self._required(payload, "right_run_id")
        result = self._with_analysis(True, lambda connection: self._analysis_compare_func(connection, left, right))
        return self._jsonable(result)

    def export_analysis(self, payload: Mapping[str, object]) -> dict[str, object]:
        run_id = self._required(payload, "run_id")
        output = self._path(self._required(payload, "output_path"))
        result = self._with_analysis(True, lambda connection: self._analysis_export_func(connection, run_id, output))
        return {
            "run_id": str(result.run_id), "surface_id": str(result.surface_id),
            "output": Path(result.output_path).name, "manifest": Path(result.manifest_path).name,
            "row_counts": dict(result.row_counts),
        }

    def start_analysis_strategies(self, payload: Mapping[str, object]) -> dict[str, object]:
        run_id = self._required(payload, "run_id")
        selected_scopes = self._analysis_selected_scopes(payload)
        source_v6_surface_raw = payload.get("source_v6_surface_path")
        source_v6_surface_path = (
            self._path(source_v6_surface_raw)
            if source_v6_surface_raw not in (None, "")
            else None
        )
        if source_v6_surface_path is not None:
            if not source_v6_surface_path.is_file() or source_v6_surface_path.suffix.casefold() != ".duckdb":
                raise ValueError("v6 strategy generation requires a published DuckDB surface")
            from .source_v6_surface import read_source_v6_analysis_run

            result = read_source_v6_analysis_run(source_v6_surface_path, run_id)
            metadata = result.get("metadata")
            facts = result.get("facts")
            if not isinstance(metadata, Mapping) or not isinstance(facts, Mapping):
                raise ValueError("v6 analysis run is missing immutable READY facts")
            raw_structures = facts.get("structures")
            if not isinstance(raw_structures, list):
                raise ValueError("v6 analysis run has no READY structure list")
            ready_ids = {
                str(item.get("candidate_id", item.get("structure_id")))
                for item in raw_structures
                if isinstance(item, Mapping)
                and item.get("status") == "READY_MRS3_STRUCTURE"
                and (
                    str(item.get("symbol")),
                    str(item.get("side")).upper(),
                    str(item.get("timeframe")),
                ) in selected_scopes
            }
            if not ready_ids:
                raise ValueError("v6 analysis run has no READY candidate in the selected scopes")
            requested_ids = payload.get("candidate_ids")
            if requested_ids is None:
                candidate_ids = tuple(sorted(ready_ids))
            elif isinstance(requested_ids, (list, tuple)) and all(isinstance(item, str) for item in requested_ids):
                requested = {item.strip() for item in requested_ids if item.strip()}
                missing = sorted(requested.difference(ready_ids))
                if missing:
                    raise ValueError(f"selected v6 candidates are absent or not READY: {missing}")
                candidate_ids = tuple(sorted(requested))
                if not candidate_ids:
                    raise ValueError("selected v6 candidates are absent or not READY")
            else:
                raise ValueError("candidate_ids must be a list of strings")
            criteria = ()
        else:
            criteria = self._analysis_criteria(payload)
        shortlist = self._jsonable(self._with_analysis(
            True, lambda connection: self._analysis_shortlist_func(connection, run_id, criteria)
        )) if source_v6_surface_path is None else None
        if source_v6_surface_path is None:
            if not isinstance(shortlist, Mapping):
                raise ValueError("analysis shortlist returned an invalid result")
            candidate_ids = tuple(
                str(row["candidate_id"])
                for row in shortlist.get("rows", ())
                if row.get("filter_status") == "READY_AFTER_FILTERS"
                and self._scope_matches(payload, "symbol", "symbols", row.get("symbol"))
                and self._scope_matches(payload, "timeframe", "timeframes", row.get("timeframe"))
                and (str(row.get("symbol", "")), str(row.get("side", "")).upper(), str(row.get("timeframe", ""))) in selected_scopes
            )
        job = _StrategyJob(
            run_id,
            self._path(self._required(payload, "template_path")),
            self._path(self._required(payload, "output_dir")),
            self._path(self._required(payload, "config_path")),
            selected_scopes=selected_scopes,
            source_v6_surface_path=source_v6_surface_path,
        )
        job.candidate_ids = candidate_ids
        job.criteria = criteria
        with self._lock:
            if self._strategy_job and self._strategy_job.running:
                raise RuntimeError("strategy generation is already running")
            self._strategy_job = job
        threading.Thread(target=self._run_analysis_strategies, args=(job,), name="mrs3-panel-strategies", daemon=True).start()
        return self.snapshot()

    def _run_analysis_strategies(self, job: _StrategyJob) -> None:
        connection = None
        try:
            with self._lock:
                job.phase = "GENERATING"
            config = self._analysis_config_loader(job.config_path)
            if job.source_v6_surface_path is not None:
                result = generate_v6_analysis_strategies(
                    job.source_v6_surface_path,
                    job.run_id,
                    job.candidate_ids,
                    job.selected_scopes,
                    job.template_path,
                    job.output_dir,
                    config,
                )
            else:
                connection = self._direct_connection_factory(str(self._analysis_path()), read_only=True)
                result = self._analysis_strategy_func(
                    connection, job.run_id, job.candidate_ids, job.selected_scopes, job.template_path, job.output_dir,
                    config, job.criteria,
                )
            with self._lock:
                job.strategies_path = Path(result.strategies_path)
                manifest_path = getattr(result, "manifest_path", None)
                job.manifest_path = Path(manifest_path) if manifest_path is not None else None
                if job.source_v6_surface_path is not None and job.manifest_path is not None:
                    manifest = json.loads(job.manifest_path.read_text(encoding="utf-8"))
                    job.v6_confirmation = {
                        "source_surface_id": manifest.get("source_surface_id"),
                        "source_manifest_sha256": manifest.get("source_manifest_sha256"),
                        "analysis_run_id": manifest.get("analysis_run_id"),
                        "analysis_config_sha256": manifest.get("analysis_config_sha256"),
                        "strategy_count": manifest.get("strategy_count"),
                        "generation_manifest_sha256": manifest.get("generation_manifest_sha256"),
                    }
                job.strategy_count = int(result.strategy_count)
                job.phase = "COMMITTED"
        except BaseException as error:
            with self._lock:
                job.phase, job.error = "FAILED", f"{type(error).__name__}: {error}"
        finally:
            if connection is not None:
                connection.close()
            with self._lock:
                job.running = False

    def analysis_shortlist(self, payload: Mapping[str, object]) -> object:
        run_id = self._required(payload, "run_id")
        criteria = self._analysis_criteria(payload)
        source_v6_surface_raw = payload.get("source_v6_surface_path")
        if source_v6_surface_raw not in (None, ""):
            surface_path = self._path(source_v6_surface_raw)
            if not surface_path.is_file() or surface_path.suffix.casefold() != ".duckdb":
                raise ValueError("v6 shortlist requires a published DuckDB surface")
            from .source_v6_surface import read_source_v6_analysis_run

            result = read_source_v6_analysis_run(surface_path, run_id)
            facts = result.get("facts")
            if not isinstance(facts, Mapping) or not isinstance(facts.get("structures"), list):
                raise ValueError("v6 analysis run has no READY structure list")
            structures = [
                item for item in facts["structures"]
                if isinstance(item, Mapping) and item.get("status") == "READY_MRS3_STRUCTURE"
            ]
            scopes: dict[tuple[str, str, str], dict[str, object]] = {}
            for item in structures:
                key = (str(item.get("symbol")), str(item.get("side")).upper(), str(item.get("timeframe")))
                if not all(key) or not self._scope_matches(payload, "symbol", "symbols", key[0]) or not self._scope_matches(payload, "timeframe", "timeframes", key[2]):
                    continue
                orders = int(item.get("order_count", len(item.get("orders", ()))))
                row = scopes.setdefault(key, {
                    "symbol": key[0], "side": key[1], "timeframe": key[2],
                    "base_1ord": 0, "order_2": 0, "order_3": 0, "order_4": 0,
                    "ready": 0, "deferred": 0, "total": 0,
                })
                row["ready"] = int(row["ready"]) + 1
                row["total"] = int(row["total"]) + 1
                row[f"order_{orders}" if orders > 1 else "base_1ord"] = int(row[f"order_{orders}" if orders > 1 else "base_1ord"]) + 1
            scope_rows = tuple(scopes[key] for key in sorted(scopes))
            return {
                "run_id": run_id,
                "criteria": list(criteria),
                "input_count": sum(int(row["total"]) for row in scope_rows),
                "ready_count": sum(int(row["ready"]) for row in scope_rows),
                "deferred_count": 0,
                "comparable_count": sum(int(row["ready"]) for row in scope_rows),
                "comparison_group_count": len(scope_rows),
                "rows": [],
                "scopes": list(scope_rows),
                "facets": {
                    "symbols": sorted({str(row["symbol"]) for row in scope_rows}),
                    "timeframes": sorted({str(row["timeframe"]) for row in scope_rows}),
                },
            }

        def read(connection: duckdb.DuckDBPyConnection) -> object:
            return (
                self._analysis_shortlist_func(connection, run_id, criteria),
                self._published_analysis_scopes(connection, run_id),
            )

        result, (published_scopes, base_counts) = self._with_analysis(True, read)
        result = self._jsonable(result)
        if not isinstance(result, Mapping):
            return result
        public_fields = (
            "run_id", "surface_id", "criteria", "input_count", "ready_count",
            "deferred_count", "comparison_group_count", "comparable_count",
        )
        document = {name: result[name] for name in public_fields if name in result}
        rows = list(result.get("rows", ()))
        scopes: dict[tuple[str, str, str], dict[str, object]] = {}
        for symbol, side, timeframe in published_scopes:
            key = (symbol, side, timeframe)
            scopes.setdefault(key, {
                "symbol": symbol, "side": side, "timeframe": timeframe,
                "base_1ord": base_counts.get(key, 0),
                "order_2": 0, "order_3": 0, "order_4": 0,
                "ready": 0, "deferred": 0, "total": 0,
            })
        for row in rows:
            symbol = str(row.get("symbol", ""))
            side = str(row.get("side", "")).upper()
            timeframe = str(row.get("timeframe", ""))
            if not symbol or not side or not timeframe:
                continue
            key = (symbol, side, timeframe)
            scope = scopes.setdefault(key, {
                "symbol": symbol, "side": side, "timeframe": timeframe,
                "base_1ord": base_counts.get(key, 0),
                "order_2": 0, "order_3": 0, "order_4": 0,
                "ready": 0, "deferred": 0, "total": 0,
            })
            order_count = int(row.get("order_count", 0))
            if order_count in (2, 3, 4):
                scope[f"order_{order_count}"] += 1
            scope["total"] += 1
            status = "deferred" if row.get("filter_status") == "DEFERRED_REDUNDANT" else "ready"
            scope[status] += 1
        visible = [
            key for key in sorted(scopes)
            if self._scope_matches(payload, "symbol", "symbols", scopes[key].get("symbol"))
            and self._scope_matches(payload, "timeframe", "timeframes", scopes[key].get("timeframe"))
        ]
        document["facets"] = {
            "symbols": sorted({key[0] for key in scopes}),
            "timeframes": sorted({key[2] for key in scopes}, key=self._timeframe_sort_key),
        }
        document["scopes"] = [scopes[key] for key in visible]
        return document

    @staticmethod
    def _timeframe_sort_key(value: str) -> tuple[int, str]:
        digits = ""
        for char in value:
            if char.isdigit():
                digits += char
            else:
                break
        return (int(digits) if digits else -1, value[len(digits):])

    @staticmethod
    def _published_analysis_scopes(
        connection: duckdb.DuckDBPyConnection, run_id: str
    ) -> tuple[tuple[tuple[str, str, str], ...], dict[tuple[str, str, str], int]]:
        found = connection.execute(
            "select surface_id from analysis_runs where run_id=?", [run_id]
        ).fetchone()
        if found is None:
            raise ValueError("unknown analysis run")
        surface_id = str(found[0])
        scopes: set[tuple[str, str, str]] = set()
        for (key,) in connection.execute(
            "select canonical_point_key from surface_points where surface_id=?",
            [surface_id],
        ).fetchall():
            parts = str(key).split("|")
            if len(parts) == 6:
                scopes.add((parts[0], parts[1], parts[2]))
        counts: dict[tuple[str, str, str], int] = {}
        for _plateau_id, metrics in load_validated_plateau_facts(connection, run_id):
            if not bool(metrics.get("ready")):
                continue
            base_id = metrics.get("base_1ord_point_id")
            if not isinstance(base_id, str) or not base_id:
                continue
            key = (
                str(metrics.get("symbol")),
                str(metrics.get("side")),
                str(metrics.get("timeframe")),
            )
            if all(key):
                counts[key] = counts.get(key, 0) + 1
        return tuple(sorted(scopes)), counts
    @staticmethod
    def _analysis_selected_scopes(payload: Mapping[str, object]) -> tuple[tuple[str, str, str], ...]:
        raw = payload.get("selected_scopes")
        if not isinstance(raw, (list, tuple)):
            raise ValueError("selected_scopes must be a list")
        if not raw:
            raise ValueError("selected_scopes must not be empty")
        normalized: set[tuple[str, str, str]] = set()
        for scope in raw:
            if not isinstance(scope, Mapping):
                raise ValueError("selected_scopes must contain objects")
            symbol = scope.get("symbol")
            side = scope.get("side")
            timeframe = scope.get("timeframe")
            if not all(isinstance(field, str) for field in (symbol, side, timeframe)):
                raise ValueError("selected scope requires symbol, side, and timeframe strings")
            symbol = symbol.strip()
            side = side.strip().upper()
            timeframe = timeframe.strip()
            if not symbol or not side or not timeframe:
                raise ValueError("selected scope requires non-empty symbol, side, and timeframe")
            if side not in {"LONG", "SHORT"}:
                raise ValueError(f"selected scope has invalid side: {side!r}")
            normalized.add((symbol, side, timeframe))
        return tuple(sorted(normalized))

    @staticmethod
    def _analysis_criteria(payload: Mapping[str, object]) -> tuple[str, ...]:
        raw = payload.get("criteria", ())
        if not isinstance(raw, (list, tuple)) or not all(isinstance(item, str) for item in raw):
            raise ValueError("criteria must be a list of filter names")
        unknown = sorted(set(raw).difference(CRITERIA))
        if unknown:
            raise ValueError(f"unknown criteria: {unknown}")
        return tuple(name for name in CRITERIA if name in raw)

    def export_analysis_filter(self, payload: Mapping[str, object]) -> dict[str, str]:
        run_id = self._required(payload, "run_id")
        output = self._path(self._required(payload, "output_path"))
        result = self._with_analysis(
            True,
            lambda connection: self._analysis_filter_export_func(
                connection, run_id, self._analysis_criteria(payload), output
            ),
        )
        return {"run_id": run_id, "output": str(Path(result))}

    def start_analysis_rerun(self, payload: Mapping[str, object]) -> dict[str, object]:
        job = _AnalysisJob(
            self._required(payload, "surface_id"),
            self._path(self._required(payload, "dates_path")),
            self._path(self._required(payload, "config_path")),
            self._optional_string(payload, "comparison_run_id") or None,
        )
        with self._lock:
            if self._analysis_job and self._analysis_job.running:
                raise RuntimeError("another analysis is already running")
            self._analysis_job = job
        threading.Thread(target=self._run_analysis, args=(job,), name="mrs3-panel-analysis", daemon=True).start()
        return self.snapshot()

    def _run_analysis(self, job: _AnalysisJob) -> None:
        connection = None
        try:
            connection = self._direct_connection_factory(str(self._analysis_path()), read_only=False)
            with self._lock: job.phase = "LOADING"
            loaded = self._analysis_load_func(connection, job.surface_id)
            side = Side(str(loaded.points["side"].iloc[0]))
            config = self._analysis_config_loader(job.config_path)
            with self._lock: job.phase = "ANALYZING"
            result = self._analysis_run_func(
                loaded, job.dates_path, side, config,
                comparison_run_id=job.comparison_run_id,
            )
            if str(result.surface_id) != job.surface_id:
                raise ValueError("analysis changed immutable surface")
            with self._lock: job.phase = "PUBLISHING"
            published = self._analysis_publish_func(connection, result)
            with self._lock:
                job.run_id = str(published.run_id)
                job.statistics = {str(key): int(value) for key, value in (result.statistics or {}).items()}
                job.phase = "COMMITTED"
        except BaseException as error:
            with self._lock:
                job.phase, job.error = "FAILED", f"{type(error).__name__}: analysis failed"
        finally:
            if connection is not None: connection.close()
            with self._lock: job.running = False

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
        raw_log = job.artifacts.get("raw_log") if job.action == "tester-run" else None
        raw_handle = None
        try:
            if raw_log is not None:
                raw_log.parent.mkdir(parents=True, exist_ok=True)
                raw_handle = raw_log.open("w", encoding="utf-8", newline="")
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
            captured: list[str] = []
            if output is not None:
                for raw_line in output:
                    captured.append(str(raw_line))
                    line = str(raw_line).rstrip("\r\n")
                    if raw_handle is not None:
                        raw_handle.write(str(raw_line))
                        raw_handle.flush()
                    if line:
                        if job.action == "performance-dd5":
                            try:
                                event = json.loads(line).get("performance_progress")
                            except (TypeError, ValueError):
                                event = None
                            if isinstance(event, dict):
                                with self._lock:
                                    job.performance_progress = {
                                        key: event[key]
                                        for key in ("stage", "completed", "total", "quarantined", "scheduled", "prepared", "imported", "skipped", "phase_seconds", "terminal_error")
                                        if key in event
                                    }
                                continue
                        display_line = (
                            _normalise_tester_log_line(line)
                            if job.action == "tester-run"
                            else line
                        )
                        if display_line is None:
                            continue
                        with self._lock:
                            job.logs.append(display_line[:4000])
            exit_code = int(process.wait())
            with self._lock:
                job.exit_code = exit_code
                job.status = "SUCCEEDED" if exit_code == 0 else "FAILED"
                if exit_code == 0 and job.action == "tester-plan":
                    job.plan_summary = _tester_plan_summary("".join(captured))
                if exit_code != 0:
                    job.error = f"command exited with code {exit_code}"
        except BaseException as error:
            with self._lock:
                job.status = "FAILED"
                job.error = f"{type(error).__name__}: {error}"
                job.logs.append(job.error)
        finally:
            if raw_handle is not None:
                raw_handle.close()
            with self._lock:
                job.finished_at = _utc_now()
                if self._artifact_paths(job):
                    self._section_jobs[self._section(job.action)] = job

    @staticmethod
    def _read_json(path: Path | None) -> dict[str, object] | None:
        if path is None:
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _artifact_paths(job: _Job) -> dict[str, Path]:
        return {
            name: path
            for name, path in job.artifacts.items()
            if PanelController._signature(path) != job.artifact_baseline.get(name)
            and path.is_file()
        }

    @staticmethod
    def _integer(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    @staticmethod
    def _number_text(value: object) -> str | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if not number == number or number in {float("inf"), float("-inf")}:
            return None
        return f"{number:.6g}"

    @staticmethod
    def _empty_dashboard(title: str) -> dict[str, object]:
        return {
            "title": title,
            "available": False,
            "state": "NOT_AVAILABLE",
            "metrics": [],
            "details": ["Артефакты этого раздела пока недоступны."],
        }

    def _source_dashboard(self, job: _Job, artifacts: Mapping[str, Path]) -> dict[str, object]:
        title = "CSV · MRS2" if job.action == "source-csv" else "DuckDB · MRS2"
        manifest = self._read_json(artifacts.get("manifest"))
        if manifest is None:
            result = self._empty_dashboard(title)
            result["state"] = job.status
            return result
        is_duckdb = job.action == "source-duckdb"
        report_count = self._integer(manifest.get("report_count" if is_duckdb else "source_rows"))
        point_count = self._integer(manifest.get("point_count" if is_duckdb else "accepted_rows"))
        accepted = self._integer(manifest.get("coverage_accepted_reports" if is_duckdb else "accepted_rows"))
        rejected = self._integer(manifest.get("coverage_rejected_reports" if is_duckdb else "rejected_rows"))
        exclusions = manifest.get("exclusions")
        cycle_exclusions: int | None = None
        if is_duckdb and isinstance(exclusions, dict):
            values = [self._integer(value) for value in exclusions.values()]
            cycle_exclusions = sum(value for value in values if value is not None)
        included = self._integer(manifest.get("included_cycles")) if is_duckdb else None
        if is_duckdb:
            metrics = [
                {"label": "Отчёты", "value": report_count if report_count is not None else "—"},
                {"label": "Точки", "value": point_count if point_count is not None else "—"},
                {"label": "Покрытие: принято", "value": accepted if accepted is not None else "—"},
                {"label": "Покрытие: отклонено", "value": rejected if rejected is not None else "—"},
                {"label": "Включено (циклы)", "value": included if included is not None else "—"},
                {"label": "Исключено (циклы)", "value": cycle_exclusions if cycle_exclusions is not None else "—"},
            ]
        else:
            metrics = [
                {"label": "Строки источника", "value": report_count if report_count is not None else "—"},
                {"label": "Точки", "value": point_count if point_count is not None else "—"},
                {"label": "Строки приняты", "value": accepted if accepted is not None else "—"},
                {"label": "Строки отклонены", "value": rejected if rejected is not None else "—"},
            ]
        mode = str(manifest.get("event_mode", "не указан"))
        version = self._integer(manifest.get("package_version"))
        details = [f"{mode} · пакет v{version}" if version is not None else mode]
        start, end = manifest.get("window_start"), manifest.get("window_end")
        if isinstance(start, str) and isinstance(end, str):
            details.append(f"Окно UTC: {start} — {end}")
        if is_duckdb:
            source_status = str(manifest.get("source_summary_status", "NOT_AVAILABLE"))
            window_status = str(manifest.get("window_metrics_status", "NOT_AVAILABLE"))
            details.extend((f"Source summary: {source_status}", f"Window metrics: {window_status}"))
            state = (
                "VERIFICATION_STATUSES_PRESENT"
                if source_status == "VERIFIED"
                and window_status == "DERIVED_FROM_VERIFIED_SOURCE"
                else "AUDIT_ONLY"
            )
        else:
            state = "PACKAGE_COMPLETE" if job.status == "SUCCEEDED" else job.status
        return {"title": title, "available": True, "state": state, "metrics": metrics, "details": details}

    def _candidate_dashboard(self, job: _Job, artifacts: Mapping[str, Path]) -> dict[str, object]:
        manifest = self._read_json(artifacts.get("manifest"))
        if manifest is None:
            result = self._empty_dashboard("Кандидаты стратегий")
            result["state"] = job.status
            return result
        values = (
            ("Event-eligible", "event_eligible_point_count"),
            ("Плато", "geometric_plateau_count"),
            ("Готовые плато", "ready_plateau_count"),
            ("Готовые структуры", "ready_structure_count"),
            ("JSON для теста", "ready_json_count"),
        )
        metrics = [
            {"label": label, "value": self._integer(manifest.get(key)) if self._integer(manifest.get(key)) is not None else "—"}
            for label, key in values
        ]
        ready = self._integer(manifest.get("ready_json_count"))
        state = "READY_FOR_TEST" if ready and ready > 0 else "NO_READY_CANDIDATES"
        return {
            "title": "Кандидаты стратегий",
            "available": True,
            "state": state,
            "metrics": metrics,
            "details": [f"Режим событий: {manifest.get('event_mode', 'не указан')}"],
        }

    def _tester_dashboard(self, job: _Job, artifacts: Mapping[str, Path]) -> dict[str, object]:
        progress = self._read_json(artifacts.get("progress")) or {}
        output = artifacts.get("output_csv")
        rows: list[dict[str, str]] = []
        if output is not None:
            try:
                with output.open("r", encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle))
            except (OSError, UnicodeDecodeError, csv.Error):
                rows = []
        if not progress and not rows:
            result = self._empty_dashboard("Тестер")
            result["state"] = job.status
            return result
        expected = self._integer(progress.get("expected_count"))
        completed = self._integer(progress.get("completed_count"))
        error_count = 0
        best: dict[str, str] | None = None
        for row in rows:
            pnl = self._number_text(row.get("total_pnl_pct"))
            if pnl is None:
                error_count += 1
                continue
            if best is None or float(pnl) > float(best["total_pnl_pct"]):
                best = {"total_pnl_pct": pnl, "max_drawdown_pct": row.get("max_drawdown_pct", "")}
        metrics = [
            {"label": "Результаты", "value": len(rows) if rows else (completed if completed is not None else "—")},
            {"label": "Лучший PnL, %", "value": best["total_pnl_pct"] if best else "—"},
            {"label": "DD лучшего, %", "value": self._number_text(best["max_drawdown_pct"]) if best else "—"},
            {"label": "Ошибки", "value": error_count},
        ]
        state = str((self._read_json(artifacts.get("state")) or {}).get("state", job.status))
        if state == "SUCCEEDED" and rows:
            state = "COMPLETED"
        details = []
        if expected is not None:
            details.append(f"Прогресс: {completed or 0} из {expected}")
        if best is not None:
            details.append("Финальные метрики — результат реального tick-test.")
        return {"title": "Тестер", "available": True, "state": state, "metrics": metrics, "details": details}

    def _posttest_dashboard(self, job: _Job, artifacts: Mapping[str, Path]) -> dict[str, object]:
        manifest = self._read_json(artifacts.get("manifest"))
        if manifest is None:
            result = self._empty_dashboard("DD5 после теста")
            result["state"] = job.status
            return result
        values = (
            ("Реальные результаты", "raw_result_count"),
            ("Pareto", "pareto_count"),
            ("Целевой DD, %", "target_dd_pct"),
        )
        metrics = [
            {"label": label, "value": self._integer(manifest.get(key)) if key != "target_dd_pct" else (self._number_text(manifest.get(key)) or "—")}
            for label, key in values
        ]
        return {
            "title": "DD5 после теста",
            "available": True,
            "state": "CALCULATED" if manifest.get("dd5_mode") == "CALCULATION_ONLY" else "COMPLETED",
            "metrics": metrics,
            "details": ["DD5 расчётно нормализует результаты для сравнения стратегий; повторный tick-test не входит в workflow."],
        }

    def _dashboard(self, jobs: Mapping[str, _Job]) -> dict[str, object]:
        titles = {
            "csv": "CSV · MRS2",
            "duckdb": "DuckDB · MRS2",
            "candidates": "Кандидаты стратегий",
            "tester": "Тестер",
            "posttest": "DD5 после теста",
        }
        dashboard: dict[str, object] = {key: self._empty_dashboard(title) for key, title in titles.items()}
        for section, job in jobs.items():
            artifacts = self._artifact_paths(job)
            if section in {"csv", "duckdb"}:
                dashboard[section] = self._source_dashboard(job, artifacts)
            elif section == "candidates":
                dashboard[section] = self._candidate_dashboard(job, artifacts)
            elif section == "tester":
                dashboard[section] = self._tester_dashboard(job, artifacts)
            elif section == "posttest":
                dashboard[section] = self._posttest_dashboard(job, artifacts)
        return dashboard

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            job = self._job
            dashboard_jobs = dict(self._section_jobs)
            if job is not None and (job.running or self._artifact_paths(job)):
                dashboard_jobs[self._section(job.action)] = job
            dashboard = self._dashboard(dashboard_jobs)
            if job is None:
                job_document = None
            else:
                current_artifacts = self._artifact_paths(job)
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
                    "plan_summary": job.plan_summary,
                    "performance_progress": job.performance_progress,
                    "expected_count": job.expected_count,
                    "workflow": self._read_json(state_path),
                    "progress": self._read_json(progress_path),
                    "artifacts": {
                        name: path.name
                        for name, path in current_artifacts.items()
                    },
                }
            import_job = self._import_job
            preflight_job = self._import_preflight_job
            preflight_document = None if preflight_job is None else {"running": preflight_job.running, "phase": preflight_job.phase, "discovered": preflight_job.discovered, "snapshotted": preflight_job.snapshotted, "total_bytes": preflight_job.total_bytes, "processed_bytes": preflight_job.processed_bytes, "token": preflight_job.token, "error": preflight_job.error}
            if import_job is None:
                import_document = None
            else:
                result = import_job.result
                evidence_valid = bool(result and self._valid_import_evidence(result))
                safe = "YES" if evidence_valid and result and result.final_state == "COMMITTED" and result.safe_to_delete == "YES" else "NO"
                import_document = {
                    "running": import_job.running,
                    "cancel_requested": import_job.cancel.is_set(),
                    "phase": import_job.phase,
                    "discovered": result.discovered if result else import_job.preflight.discovered,
                    "final_state": (result.final_state if evidence_valid else "EVIDENCE_INVALID") if result else import_job.phase,
                    "counts": dict(import_job.counts),
                    "safe_to_delete": safe,
                    "error": import_job.error or ("import failed" if result and result.error else None),
                    "artifacts": ({"import_manifest": "import_manifest.json", "import_checklist": "html_delete_checklist.json"} if evidence_valid else {}),
                }
            direct_job = self._direct_job
            direct_preflight = self._direct_preflight
            direct_document = None if direct_job is None else {
                "running": direct_job.running,
                "cancel_requested": direct_job.cancel.is_set(),
                "phase": direct_job.phase,
                "point_count": direct_job.point_count,
                "workers": direct_job.workers,
                "elapsed_seconds": direct_job.elapsed_seconds,
                "points_per_second": direct_job.points_per_second,
                "total_points": direct_job.total_points,
                "side": direct_job.side,
                "ordinal": direct_job.ordinal,
                "total": direct_job.total,
                "surface_id": direct_job.surface_id,
                "publication_state": direct_job.publication_state,
                "parent_surface_id": direct_job.parent_surface_id,
                "error": direct_job.error,
                "artifacts": {
                    name: filename
                    for name, (filename, _) in direct_job.artifacts.items()
                },
            }
            if direct_preflight is not None:
                request, preflight, token = direct_preflight
                preflight_document = self._direct_preflight_document(request, preflight, token)
                if direct_document is None:
                    direct_document = {
                        "running": False,
                        "phase": "PREFLIGHT_READY",
                        "point_count": 0,
                        "workers": 0,
                        "elapsed_seconds": 0.0,
                        "points_per_second": 0.0,
                        "total_points": 0,
                    }
                direct_document["preflight"] = preflight_document
            elif self._direct_selected_preflight is not None:
                state = self._direct_selected_preflight
                preflight_document = self._direct_preflight_document(
                    state.requests[0], state.preflights[0], state.token
                )
                preflight_document["selected_sides"] = [request.side for request in state.requests]
                preflight_document["coverage_token"] = state.coverage_scan.token
                if direct_document is None:
                    direct_document = {
                        "running": False,
                        "phase": "PREFLIGHT_READY",
                        "point_count": 0,
                        "workers": 0,
                        "elapsed_seconds": 0.0,
                        "points_per_second": 0.0,
                        "total_points": 0,
                    }
                direct_document["preflight"] = preflight_document
            analysis_job = self._analysis_job
            analysis_document = None if analysis_job is None else {
                "running": analysis_job.running,
                "phase": analysis_job.phase,
                "status": analysis_job.phase,
                "surface_id": analysis_job.surface_id,
                "run_id": analysis_job.run_id,
                "statistics": dict(analysis_job.statistics),
                "error": analysis_job.error,
            }
            source_v6_analysis_job = self._source_v6_analysis_job
            source_v6_analysis_document = (
                None
                if source_v6_analysis_job is None
                else self._source_v6_analysis_document(source_v6_analysis_job)
            )
            # The existing Analysis section is also the landing place for a
            # committed v6 run; keep the dedicated lifecycle document so the
            # UI can distinguish it from the legacy rerun controls.
            if source_v6_analysis_document is not None and (
                analysis_document is None or source_v6_analysis_document["running"]
            ):
                analysis_document = {
                    "running": source_v6_analysis_document["running"],
                    "phase": source_v6_analysis_document["phase"],
                    "status": source_v6_analysis_document["status"],
                    "surface_id": source_v6_analysis_document["surface_id"],
                    "run_id": source_v6_analysis_document["analysis_run_id"],
                    "statistics": {},
                    "error": source_v6_analysis_document["error"],
                    "source": "SOURCE_V6",
                    "manifest_sha256": source_v6_analysis_document["manifest_sha256"],
                    "frozen_facts_sha256": source_v6_analysis_document["frozen_facts_sha256"],
                    "listing_dates_sha256": source_v6_analysis_document["listing_dates_sha256"],
                    "config_sha256": source_v6_analysis_document["config_sha256"],
                    "scope": source_v6_analysis_document["scope"],
                    "start_ms": source_v6_analysis_document["start_ms"],
                    "end_ms": source_v6_analysis_document["end_ms"],
                    "provenance": source_v6_analysis_document["provenance"],
                }
            strategy_job = self._strategy_job
            strategy_document = None if strategy_job is None else {
                "running": strategy_job.running,
                "phase": strategy_job.phase,
                "run_id": strategy_job.run_id,
                "selected_scopes": [list(scope) for scope in strategy_job.selected_scopes],
                "strategies_path": str(strategy_job.strategies_path) if strategy_job.strategies_path else None,
                "manifest_path": str(strategy_job.manifest_path) if strategy_job.manifest_path else None,
                "v6_confirmation": dict(strategy_job.v6_confirmation),
                "strategy_count": strategy_job.strategy_count,
                "error": strategy_job.error,
            }
            source_v6_document = dict(self._source_v6_job) if self._source_v6_job is not None else None
            if source_v6_document is not None:
                source_v6_document.pop("fragments", None)
        return {
            "defaults": {
                "root": str(self.root),
                "config": str(self.default_config),
                "workflow": self._workflow_defaults(),
                "tester": self._tester_defaults(),
            },
            "job": job_document,
            "duckdb_import": import_document,
            "duckdb_import_preflight": preflight_document,
            "duckdb_direct": direct_document,
            "analysis": analysis_document,
            "source_v6_analysis": source_v6_analysis_document,
            "analysis_strategies": strategy_document,
            "source_v6": source_v6_document,
            "dashboard": dashboard,
        }

    def artifact(self, name: str) -> Path | tuple[str, bytes] | None:
        direct_artifact = self._direct_artifact(name)
        if direct_artifact is not None:
            return direct_artifact
        with self._lock:
            path = self._job.artifacts.get(name) if self._job else None
            baseline = self._job.artifact_baseline.get(name) if self._job else None
        if path is None or not path.is_file() or self._signature(path) == baseline:
            with self._lock:
                result = self._import_job.result if self._import_job else None
                evidence = self._import_evidence(result) if result is not None else None
            if result is None or evidence is None:
                return None
            return {"import_manifest": (result.manifest_path.name, evidence[0]), "import_checklist": (result.checklist_path.name, evidence[1])}.get(name)
        return path

    def _direct_artifact(self, name: str) -> tuple[str, bytes] | None:
        with self._lock:
            job = self._direct_job
            direct_artifact = self._direct_artifacts.get(name)
        if job is not None and name in job.artifacts:
            return job.artifacts[name]
        return direct_artifact


class _PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        controller: PanelController,
        *,
        restart_launcher: Callable[[], None] | None = None,
    ) -> None:
        self.controller = controller
        self._restart_launcher = restart_launcher or self._launch_restart_helper
        super().__init__(address, _PanelHandler)

    def _launch_restart_helper(self) -> None:
        script = self.controller.root / "scripts" / "restart_new_panel.bat"
        if not script.is_file():
            raise PanelJobError("RESTART_UNAVAILABLE")
        try:
            subprocess.Popen(
                ("cmd.exe", "/c", str(script), str(self.server_port)),
                cwd=self.controller.root,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            raise PanelJobError("RESTART_UNAVAILABLE") from None

    def restart_panel(self) -> dict[str, bool]:
        if any(job["state"] not in {"COMMITTED", "CANCELLED", "FAILED"} for job in self.controller.panel_jobs()):
            raise PanelJobError("RESTART_BLOCKED")
        self._restart_launcher()
        stop = threading.Timer(0.15, self.shutdown)
        stop.daemon = True
        stop.start()
        return {"restarting": True}


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
        if parsed.path == "/legacy":
            payload = PANEL_HTML.encode("utf-8")
            self._headers(200, "text/html; charset=utf-8", len(payload))
            self.wfile.write(payload)
            return
        if parsed.path == "/" and self.server.controller.panel_default_root() == "legacy":
            payload = PANEL_HTML.encode("utf-8")
            self._headers(200, "text/html; charset=utf-8", len(payload))
            self.wfile.write(payload)
            return
        static_paths = {"/": "index.html", "/panel-web/app.css": "app.css", "/panel-web/app.js": "app.js"}
        if parsed.path in static_paths and (parsed.path != "/" or self.server.controller.panel_default_root() == "static"):
            name = static_paths[parsed.path]
            try:
                payload = (_PANEL_WEB / name).read_bytes()
            except OSError:
                self._json(404, {"error": "not found"})
                return
            content_type = {"index.html": "text/html", "app.css": "text/css", "app.js": "text/javascript"}[name]
            self._headers(200, f"{content_type}; charset=utf-8", len(payload))
            self.wfile.write(payload)
            return
        if parsed.path == "/api/v2/bootstrap":
            self._json(200, self.server.controller.panel_bootstrap())
            return
        if parsed.path == "/api/v2/settings/reload":
            try:
                result = self.server.controller.panel_settings_reload()
            except PanelSettingsError as error:
                self._json(400, {"error": str(error)})
                return
            self._json(200, result)
            return
        if parsed.path == "/api/v2/jobs":
            self._json(200, {"jobs": self.server.controller.panel_jobs()})
            return
        if parsed.path == "/api/v2/testing/local/status":
            self._json(200, self.server.controller.local_testing_status())
            return
        if parsed.path == "/api/v2/testing/remote/status":
            self._json(200, self.server.controller.remote_testing_status())
            return
        if parsed.path == "/api/v2/testing/remote/progress":
            try:
                self._json(200, self.server.controller.remote_testing_progress())
            except PanelTestingError:
                self._json(400, {"error": "invalid testing request"})
            return
        if parsed.path == "/api/v2/source/local/jobs":
            self._json(200, {"jobs": self.server.controller.source_db_local_jobs()})
            return
        if parsed.path == "/api/v2/source/local/catalog":
            self._json(200, self.server.controller.source_db_local_catalog())
            return
        if parsed.path == "/api/v2/surfaces/catalog":
            self._json(200, self.server.controller.surface_catalog())
            return
        if parsed.path == "/api/v2/source/remote/status":
            try:
                job_id = parse_qs(parsed.query).get("job_id", [""])[0]
                self._json(200, self.server.controller.source_db_remote_status(job_id))
            except (RemoteSourceDbError, ValueError):
                self._json(400, {"error": "invalid source db request"})
            return
        if parsed.path == "/api/v2/surfaces/gaps":
            try:
                query = parse_qs(parsed.query)
                self._json(200, self.server.controller.surface_gaps({
                    "preflight_token": query.get("preflight_token", [""])[0],
                    "scope_key": query.get("scope_key", [""])[0],
                }))
            except ValueError:
                self._json(400, {"error": "invalid surface request"})
            return
        if parsed.path == "/api/v2/surfaces/publish/status":
            self._json(200, self.server.controller.surface_publish_status())
            return
        if parsed.path == "/api/v2/strategies/tester/status":
            try:
                job_id = parse_qs(parsed.query).get("job_id", [""])[0]
                self._json(200, self.server.controller.strategies_tester_status(job_id))
            except (KeyError, ValueError):
                self._json(400, {"error": "invalid strategy batch request"})
            return
        if parsed.path == "/api/v2/strategies/performance-dd5/status":
            try:
                job_id = parse_qs(parsed.query).get("job_id", [""])[0]
                self._json(200, self.server.controller.strategies_performance_dd5_status(job_id))
            except (KeyError, ValueError):
                self._json(400, {"error": "invalid performance db request"})
            return
        if parsed.path == "/api/ui/bootstrap":
            self._json(200, {"version": "panel-ui-v1", "defaults": {"runner": {"configured": False}}})
            return
        if parsed.path == "/api/status":
            self._json(200, self.server.controller.snapshot())
            return
        if parsed.path == "/api/duckdb-import/settings":
            try:
                self._json(200, self.server.controller.duckdb_import_settings())
            except ValueError as error:
                self._json(400, {"error": str(error)})
            return
        if parsed.path == "/api/source-v6/library":
            try:
                self._json(200, self.server.controller.source_v6_library())
            except ValueError as error:
                self._json(400, {"error": str(error)})
            return
        if parsed.path == "/api/source-v6/fresh/library":
            try:
                self._json(200, self.server.controller.source_v6_fresh_library())
            except ValueError as error:
                self._json(400, {"error": str(error)})
            return
        if parsed.path == "/api/source-v6/analysis/library":
            try:
                self._json(200, self.server.controller.source_v6_library())
            except ValueError as error:
                self._json(400, {"error": str(error)})
            return
        if parsed.path == "/api/source-v6/analysis/status":
            document = self.server.controller.snapshot().get("source_v6_analysis")
            self._json(200, document or {"phase": "IDLE", "running": False})
            return
        if parsed.path == "/api/artifact":
            name = parse_qs(parsed.query).get("name", [""])[0]
            artifact = self.server.controller.artifact(name)
            if artifact is None:
                self._json(404, {"error": "artifact is not available"})
                return
            if isinstance(artifact, Path):
                filename, data = artifact.name, None
                size = artifact.stat().st_size
            else:
                filename, data = artifact
                size = len(data)
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{filename.replace(chr(34), "")}"',
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if data is not None:
                self.wfile.write(data)
            else:
                with artifact.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        self.wfile.write(chunk)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._has_local_host():
            self._json(403, {"error": "local Host header required"})
            return
        endpoint = urlparse(self.path).path
        if endpoint not in {"/api/start", "/api/browse", "/api/duckdb-import/settings", "/api/duckdb-import/preflight", "/api/duckdb-import/start", "/api/duckdb-import/cancel", "/api/duckdb-import/migrate", "/api/duckdb-direct/coverage", "/api/duckdb-direct/preflight", "/api/duckdb-direct/start", "/api/duckdb-direct/cancel", "/api/analysis/library", "/api/analysis/initialize", "/api/analysis/rerun", "/api/analysis/compare", "/api/analysis/export", "/api/analysis/shortlist", "/api/analysis/filter-export", "/api/analysis/strategies", "/api/source-v6/preflight", "/api/source-v6/start", "/api/source-v6/fresh/multiscope/start", "/api/source-v6/fresh/multiscope/analysis/start", "/api/source-v6/cancel", "/api/source-v6/merge", "/api/source-v6/merge/preflight", "/api/source-v6/merge/start", "/api/source-v6/merge/cancel", "/api/source-v6/library", "/api/source-v6/gaps", "/api/source-v6/export", "/api/source-v6/analysis/library", "/api/source-v6/analysis/start", "/api/source-v6/analysis/status", "/api/source-v6/analysis/cancel", "/api/v2/panel/restart", "/api/v2/settings/validate", "/api/v2/settings/save", "/api/v2/jobs", "/api/v2/testing/local/fill", "/api/v2/testing/local/start", "/api/v2/testing/local/stop", "/api/v2/testing/remote/prepare", "/api/v2/testing/remote/fill", "/api/v2/testing/remote/start", "/api/v2/testing/remote/stop", "/api/v2/source/local/import/preflight", "/api/v2/source/local/import/start", "/api/v2/source/local/merge/preflight", "/api/v2/source/local/merge/start", "/api/v2/source/local/cancel", "/api/v2/source/remote/start", "/api/v2/source/remote/cancel", "/api/v2/surfaces/preflight", "/api/v2/surfaces/select", "/api/v2/surfaces/publish", "/api/v2/surfaces/publish/start", "/api/v2/strategies/fresh/analyze", "/api/v2/strategies/fresh/generate", "/api/v2/strategies/fresh/shortlist"}:
            self._json(404, {"error": "not found"})
            return
        content_type = self.headers.get("Content-Type", "").partition(";")[0]
        if content_type.strip().casefold() != "application/json":
            self._json(415, {"error": "invalid settings"} if endpoint.startswith("/api/v2/") else {"error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "invalid settings"} if endpoint.startswith("/api/v2/") else {"error": "invalid Content-Length"})
            return
        if length <= 0 or length > 65536:
            self._json(400, {"error": "invalid settings"} if endpoint.startswith("/api/v2/") else {"error": "JSON body must be between 1 and 65536 bytes"})
            return
        try:
            document = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(document, dict):
                raise ValueError("JSON body must be an object")
            if endpoint == "/api/v2/panel/restart":
                result = self.server.restart_panel()
            elif endpoint == "/api/v2/testing/local/fill":
                result = self.server.controller.local_testing_fill(document)
            elif endpoint == "/api/v2/testing/local/start":
                result = self.server.controller.local_testing_start()
            elif endpoint == "/api/v2/testing/local/stop":
                result = self.server.controller.local_testing_stop()
            elif endpoint == "/api/v2/testing/remote/prepare":
                result = self.server.controller.remote_testing_prepare(document)
            elif endpoint == "/api/v2/testing/remote/check-paths":
                result = self.server.controller.remote_testing_check_paths()
            elif endpoint == "/api/v2/testing/remote/fill":
                result = self.server.controller.remote_testing_fill(document)
            elif endpoint == "/api/v2/testing/remote/start":
                result = self.server.controller.remote_testing_start()
            elif endpoint == "/api/v2/testing/remote/stop":
                result = self.server.controller.remote_testing_stop()
            elif endpoint == "/api/v2/source/local/import/preflight":
                result = self.server.controller.source_db_local_import_preflight(document)
            elif endpoint == "/api/v2/source/local/import/start":
                result = self.server.controller.source_db_local_start(document, merge=False)
            elif endpoint == "/api/v2/source/local/merge/preflight":
                result = self.server.controller.source_db_local_merge_preflight(document)
            elif endpoint == "/api/v2/source/local/merge/start":
                result = self.server.controller.source_db_local_start(document, merge=True)
            elif endpoint == "/api/v2/source/local/cancel":
                result = self.server.controller.source_db_local_cancel(str(document.get("job_id", "")))
            elif endpoint == "/api/v2/source/remote/start":
                result = self.server.controller.source_db_remote_start(document)
            elif endpoint == "/api/v2/source/remote/cancel":
                result = self.server.controller.source_db_remote_cancel(str(document.get("job_id", "")))
            elif endpoint == "/api/v2/surfaces/preflight":
                result = self.server.controller.surface_preflight(document)
            elif endpoint == "/api/v2/surfaces/select":
                result = self.server.controller.surface_select(document)
            elif endpoint == "/api/v2/surfaces/publish":
                result = self.server.controller.surface_publish(document)
            elif endpoint == "/api/v2/surfaces/publish/start":
                result = self.server.controller.surface_publish_start(document)
            elif endpoint == "/api/v2/strategies/fresh/analyze":
                result = self.server.controller.strategies_fresh_analyze(document)
            elif endpoint == "/api/v2/strategies/fresh/generate":
                result = self.server.controller.strategies_fresh_generate(document)
            elif endpoint == "/api/v2/strategies/fresh/shortlist":
                result = self.server.controller.strategies_fresh_shortlist(document)
            elif endpoint == "/api/v2/jobs":
                result = {"job": self.server.controller.panel_job_submit(document)}
            elif endpoint == "/api/v2/settings/validate":
                result = self.server.controller.panel_settings_validate(document)
            elif endpoint == "/api/v2/settings/save":
                result = self.server.controller.panel_settings_save(document)
            elif endpoint == "/api/browse":
                kind = document.get("kind")
                multiple = document.get("multiple", False)
                if not isinstance(kind, str):
                    raise ValueError("browse kind must be a string")
                result = {"paths": self.server.controller.browse(kind, multiple)}
            elif endpoint == "/api/duckdb-import/settings":
                result = self.server.controller.duckdb_import_settings(document)
            elif endpoint == "/api/duckdb-import/preflight":
                result = self.server.controller.duckdb_import_preflight(document)
            elif endpoint == "/api/duckdb-import/start":
                result = self.server.controller.start_duckdb_import(document)
            elif endpoint == "/api/duckdb-import/cancel":
                result = self.server.controller.cancel_duckdb_import()
            elif endpoint == "/api/duckdb-import/migrate":
                result = self.server.controller.migrate_duckdb_import(document)
            elif endpoint == "/api/source-v6/preflight":
                result = self.server.controller.source_v6_preflight(document)
            elif endpoint == "/api/source-v6/start":
                result = self.server.controller.source_v6_start(document)
            elif endpoint == "/api/source-v6/fresh/multiscope/start":
                result = self.server.controller.source_v6_start_fresh(document)
            elif endpoint == "/api/source-v6/fresh/multiscope/analysis/start":
                result = self.server.controller.source_v6_start_fresh_analysis(document)
            elif endpoint == "/api/source-v6/cancel":
                result = self.server.controller.source_v6_cancel()
            elif endpoint == "/api/source-v6/merge/preflight":
                result = self.server.controller.source_v6_merge_preflight(document)
            elif endpoint == "/api/source-v6/merge/start":
                result = self.server.controller.source_v6_merge_start(document)
            elif endpoint == "/api/source-v6/merge/cancel":
                result = self.server.controller.source_v6_merge_cancel()
            elif endpoint == "/api/source-v6/merge":
                result = self.server.controller.source_v6_merge(document)
            elif endpoint == "/api/source-v6/library":
                result = self.server.controller.source_v6_library(document)
            elif endpoint == "/api/source-v6/gaps":
                result = self.server.controller.source_v6_gaps(document)
            elif endpoint == "/api/source-v6/export":
                result = self.server.controller.source_v6_export(document)
            elif endpoint == "/api/source-v6/analysis/library":
                result = self.server.controller.source_v6_library(document)
            elif endpoint == "/api/source-v6/analysis/start":
                result = self.server.controller.start_source_v6_analysis(document)
            elif endpoint == "/api/source-v6/analysis/status":
                result = self.server.controller.snapshot().get("source_v6_analysis")
            elif endpoint == "/api/source-v6/analysis/cancel":
                result = self.server.controller.cancel_source_v6_analysis()
            elif endpoint == "/api/duckdb-direct/coverage":
                result = self.server.controller.duckdb_direct_coverage(document)
            elif endpoint == "/api/duckdb-direct/preflight":
                result = self.server.controller.duckdb_direct_preflight(document)
            elif endpoint == "/api/duckdb-direct/start":
                result = self.server.controller.start_duckdb_direct(document)
            elif endpoint == "/api/duckdb-direct/cancel":
                result = self.server.controller.cancel_duckdb_direct()
            elif endpoint == "/api/analysis/library":
                result = self.server.controller.analysis_library(document)
            elif endpoint == "/api/analysis/initialize":
                result = self.server.controller.initialize_analysis()
            elif endpoint == "/api/analysis/rerun":
                result = self.server.controller.start_analysis_rerun(document)
            elif endpoint == "/api/analysis/compare":
                result = self.server.controller.compare_analysis(document)
            elif endpoint == "/api/analysis/export":
                result = self.server.controller.export_analysis(document)
            elif endpoint == "/api/analysis/shortlist":
                result = self.server.controller.analysis_shortlist(document)
            elif endpoint == "/api/analysis/filter-export":
                result = self.server.controller.export_analysis_filter(document)
            elif endpoint == "/api/analysis/strategies":
                result = self.server.controller.start_analysis_strategies(document)
            else:
                action = str(document.get("action", ""))
                result = self.server.controller.start(action, document)
        except PanelJobError as error:
            self._json(409 if error.code in {"RESOURCE_BUSY", "JOB_CAPACITY_EXHAUSTED", "IDEMPOTENCY_CONFLICT", "RESTART_BLOCKED"} else 400, {"error": error.code})
            return
        except RuntimeError as error:
            self._json(409, {"error": "invalid settings"} if endpoint.startswith("/api/v2/") else {"error": str(error)})
            return
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self._json(400, {"error": "invalid settings"} if endpoint.startswith("/api/v2/") else {"error": str(error)})
            return
        self._json(202 if endpoint in {"/api/start", "/api/duckdb-import/start", "/api/duckdb-direct/start", "/api/analysis/rerun", "/api/analysis/strategies", "/api/source-v6/analysis/start", "/api/source-v6/fresh/multiscope/start", "/api/source-v6/fresh/multiscope/analysis/start", "/api/v2/jobs", "/api/v2/surfaces/publish/start"} else 200, result)


def create_panel_server(
    host: str,
    port: int,
    controller: PanelController,
    *,
    restart_launcher: Callable[[], None] | None = None,
) -> ThreadingHTTPServer:
    if host.casefold() not in {"127.0.0.1", "localhost"}:
        raise ValueError("control panel must bind to a loopback host")
    if not 0 <= port <= 65535:
        raise ValueError("panel port must be between 0 and 65535")
    return _PanelServer((host, port), controller, restart_launcher=restart_launcher)


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

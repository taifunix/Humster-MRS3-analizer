from __future__ import annotations

from collections import deque
import csv
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import inspect
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

import duckdb

from .analysis_exports import export_analysis_run
from .analysis_storage import compare_analysis_runs, ensure_analysis_schema, list_surface_library, publish_analysis_run
from .config import AlgorithmConfig
from .config import DuckDBImportSettings, load_duckdb_import_settings, save_duckdb_import_settings
from .duckdb_import import ImportJobResult, ImportPreflight, ImportProgress, ImportRequest, SnapshotProgress, import_html_tree, preflight_html_import
from .duckdb_source_schema import migrate_source_database
from .duckdb_direct import DirectBuildRequest, DirectMaterializationError, DirectPreflight, preflight_duckdb_direct, run_panel_direct_build
from .models import Side
from .pipeline import run_published_pipeline
from .published_surface import load_published_surface


_DIRECT_MATERIALIZER_VERSION = "v1"
_DIRECT_POINT_CONFIG_HASH = sha256(
    b"event_mode=legacy_trades_proxy;point_event_count=TotalTrades"
).hexdigest()


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
    .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 13px 0; }
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
    @keyframes panel-in { from { opacity: .55; } to { opacity: 1; } }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: .01ms !important; transition-duration: .01ms !important; } }
    @media (prefers-reduced-transparency: reduce) { .card, .tablist { background: var(--panel); backdrop-filter: none; } }
    @media (prefers-contrast: more) { .card, .tablist, input, select { border-color: #fff; } }
    @media (max-width: 850px) { .grid { grid-template-columns: 1fr; } .stats, .decision-grid { grid-template-columns: 1fr 1fr; } }
  </style>
</head>
<body>
<main>
  <header>
    <div><h1>MRS3 Control Panel</h1><div class="subtitle">Локальное управление селектором и Hamster Bot Tester</div></div>
    <div class="badge">127.0.0.1 · отдельный процесс</div>
  </header>
  <div class="tablist" role="tablist" aria-label="Рабочие разделы MRS3">
    <button role="tab" id="tab-csv-source" aria-selected="true" aria-controls="panel-csv-source" tabindex="0">MRS2 · CSV</button>
    <button role="tab" id="tab-duckdb-source" aria-selected="false" aria-controls="panel-duckdb-source" tabindex="-1">MRS2 · DuckDB</button>
    <button role="tab" id="tab-candidates" aria-selected="false" aria-controls="panel-candidates" tabindex="-1">Кандидаты стратегий</button>
    <button role="tab" id="tab-portfolio" aria-selected="false" aria-controls="panel-portfolio" tabindex="-1">Анализатор портфелей</button>
    <button role="tab" id="tab-settings" aria-selected="false" aria-controls="panel-settings" tabindex="-1">Настройки</button>
  </div>
  <div class="grid">
    <section class="card stack">
      <section role="tabpanel" id="panel-csv-source" aria-labelledby="tab-csv-source">
        <h2>MRS2 · CSV</h2><p class="source-note">Точный UTC-интервал; PointEventCount = TotalTrades. Этот пакет нельзя смешивать с DuckDB-пакетом.</p>
        <div class="stack workflow-card">
          <label>CSV-файлы (через ;)<div class="path-control"><input id="source_csv_files" value="reports_history_bybit_long_day2.csv" type="text"><button type="button" class="secondary" onclick="browse('source_csv_files','csv',true)">Выбрать…</button></div></label>
          <fieldset class="row"><legend>Окно UTC</legend><label>Начало<input id="csv_start" value="2026-07-15T00:00:00Z" type="text"></label><label>Конец<input id="csv_end" value="2026-08-06T00:00:00Z" type="text"></label></fieldset>
          <div class="buttons"><button data-runnable="true" class="primary" onclick="startAction('source-csv')">Собрать CSV-пакет</button><span class="badge">legacy_trades_proxy</span></div>
        </div>
      </section>
      <section role="tabpanel" id="panel-duckdb-source" aria-labelledby="tab-duckdb-source" hidden>
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
            <div class="row"><label>Side<select id="direct_side"><option>LONG</option><option>SHORT</option></select></label><label>Symbols (; separated)<input id="direct_symbols" type="text" placeholder="BTCUSDT;ETHUSDT"></label></div>
            <fieldset class="row"><legend>Shift range, bp</legend><label>Start<input id="direct_shift_start" type="number" step="1"></label><label>End<input id="direct_shift_end" type="number" step="1"></label><label>Step<input id="direct_shift_step" type="number" min="1" step="1"></label></fieldset>
            <div class="buttons"><button type="button" onclick="directPreflight()">Check coverage</button><button type="button" class="primary" onclick="directBuild()">Build surface</button><button type="button" onclick="directCancel()">Cancel</button></div>
            <div id="directCoverage" role="group" aria-label="Direct surface symbols"></div><div id="directStatus" class="muted" aria-live="polite">No direct build.</div>
          </div></details>
          <details><summary>Analysis Library</summary><div class="stack">
            <div class="row"><label>Side<select id="analysis_side"><option value="">Any</option><option>LONG</option><option>SHORT</option></select></label><label>Build mode<select id="analysis_build_mode"><option value="">Any</option><option>DUCKDB_DIRECT</option></select></label></div><label>Symbol<input id="analysis_symbol" type="text"></label>
            <div class="row"><label>Period start<input id="analysis_period_start" type="datetime-local"></label><label>Period end<input id="analysis_period_end" type="datetime-local"></label></div>
            <div class="row"><label>Parent surface<input id="analysis_parent" type="text"></label><label>Source hash<input id="analysis_source_hash" type="text"></label></div>
            <div class="buttons"><button type="button" onclick="analysisInitialize()">Initialize / migrate v3</button><button type="button" onclick="analysisRefresh()">Refresh library</button></div>
            <label>Surface ID<input id="analysis_surface_id" type="text"></label><label>Run ID<input id="analysis_run_id" type="text"></label>
            <div class="stats"><div class="stat"><b id="analysis_unique">—</b><span>unique points</span></div><div class="stat"><b id="analysis_economic">—</b><span>economic eligible</span></div><div class="stat"><b id="analysis_event">—</b><span>event eligible</span></div><div class="stat"><b id="analysis_plateaus">—</b><span>plateaus</span></div><div class="stat"><b id="analysis_ready">—</b><span>READY</span></div></div>
            <label>Listing dates<div class="path-control"><input id="analysis_dates" type="text"><button type="button" class="secondary" onclick="browse('analysis_dates','dates',false)">Browse…</button></div></label>
            <label>Algorithm config<div class="path-control"><input id="analysis_config" type="text"><button type="button" class="secondary" onclick="browse('analysis_config','config',false)">Browse…</button></div></label>
            <label>Export directory<div class="path-control"><input id="analysis_output" type="text"><button type="button" class="secondary" onclick="browse('analysis_output','directory',false)">Browse…</button></div></label>
            <div class="row"><label>Left run<input id="analysis_left_run" type="text"></label><label>Right run<input id="analysis_right_run" type="text"></label></div>
            <div class="buttons"><button type="button" onclick="analysisRefine()">Refine</button><button type="button" class="primary" onclick="analysisRerun()">Re-run analysis</button><button type="button" onclick="analysisCompare()">Compare periods</button><button type="button" onclick="analysisExport()">Export</button></div>
            <div id="analysisLibrary"></div><div id="analysisStatus" class="muted" aria-live="polite">No analysis selected.</div>
          </div></details>
        </div>
      </section>
      <section role="tabpanel" id="panel-candidates" aria-labelledby="tab-candidates" hidden>
        <h2>Кандидаты стратегий</h2><p class="source-note">Source-метрики — диагностика, а не результат готовой MRS3-стратегии.</p>
        <div class="stack workflow-card"><h3>1. Собрать кандидатов</h3><label>Источник точек<select id="select_source_mode" onchange="syncCandidateSource()"><option value="csv">Совместимый CSV-вход</option><option value="package">Проверенный source-pack</option></select></label><label id="raw_csv_source">Совместимый CSV-вход (текущий путь)<div class="path-control"><input id="input_csv" value="reports_history_bybit_long_day2.csv" type="text"><button type="button" class="secondary" onclick="browse('input_csv','csv',false)">Выбрать…</button></div></label><label id="package_source" hidden>Каталог проверенного source-pack<div class="path-control"><input id="source_package" value="source_package" type="text"><button type="button" class="secondary" onclick="browse('source_package','directory',false)">Выбрать…</button></div></label><div class="row"><label>Даты листинга<div class="path-control"><input id="dates" value="dates.xlsx" type="text"><button type="button" class="secondary" onclick="browse('dates','dates',false)">Выбрать…</button></div></label><label>Шаблон JSON<div class="path-control"><input id="template" value="ADM_3_LONG_SHORT.json" type="text"><button type="button" class="secondary" onclick="browse('template','template',false)">Выбрать…</button></div></label></div><label>Сторона<select id="side"><option>LONG</option><option>SHORT</option></select></label><button data-runnable="true" onclick="startAction('select')">Запустить селектор</button></div>
        <div class="stack workflow-card"><h3>2. Проверить и тестировать</h3><label>Каталог JSON-стратегий<div class="path-control"><input id="strategies" value="output_long\strategies" type="text"><button type="button" class="secondary" onclick="browse('strategies','directory',false)">Выбрать…</button></div></label><div class="buttons"><button data-runnable="true" id="planButton" onclick="startAction('tester-plan')">Проверить план</button><button data-runnable="true" id="runButton" class="primary" onclick="startAction('tester-run')">Запустить тесты</button></div></div>
        <div class="stack workflow-card"><h3>3. DD5 после теста</h3><p class="source-note">Финальные выводы требуют реального tick-test и DD5 retest.</p><label>CSV результатов<div class="path-control"><input id="results_csv" value="results\mrs3_long_results.csv" type="text"><button type="button" class="secondary" onclick="browse('results_csv','results_csv',false)">Выбрать…</button></div></label><label>Audit XLSX<div class="path-control"><input id="audit_xlsx" value="output_long\audit.xlsx" type="text"><button type="button" class="secondary" onclick="browse('audit_xlsx','audit_xlsx',false)">Выбрать…</button></div></label><label>Каталог исходных стратегий<div class="path-control"><input id="posttest_strategies" value="output_long\strategies" type="text"><button type="button" class="secondary" onclick="browse('posttest_strategies','directory',false)">Выбрать…</button></div></label><button data-runnable="true" onclick="startAction('posttest')">Собрать DD5</button></div>
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
          <label>Каталог DD5<div class="path-control"><input id="posttest_output_dir" value="posttest_long" type="text"><button type="button" class="secondary" onclick="browse('posttest_output_dir','directory',false)">Выбрать…</button></div></label>
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
      <section class="decision-dashboard" aria-live="polite" aria-label="Статистика для решений">
        <h3>Статистика для решений</h3><p class="source-note">Только артефакты, созданные текущим процессом панели.</p>
        <div id="decisionDashboard" class="decision-grid"></div>
      </section>
    </section>
  </div>
</main>
<script>
const labels = {
  'tester-plan':'Проверка плана', 'tester-run':'Пакетное тестирование', 'select':'Создание стратегий', 'posttest':'DD5-анализ', 'source-csv':'CSV source-pack', 'source-duckdb':'DuckDB source-pack',
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
  if (action === 'source-csv') return {...base, input_csv:value('source_csv_files'), start:value('csv_start'), end:value('csv_end'), output_dir:value('csv_output_dir')};
  if (action === 'source-duckdb') return {...base, database:value('source_duckdb'), start:value('duckdb_start'), end:value('duckdb_end'), output_dir:value('duckdb_output_dir'), verify_html_root:value('verify_html_root'), verification_sample_count:value('verification_sample_count')};
  if (action === 'select') {
    const source = value('select_source_mode') === 'package'
      ? {source_package:value('source_package')}
      : {input_csv:value('input_csv')};
    return {...base, ...source, dates:value('dates'), template:value('template'), side:value('side'), output_dir:value('select_output_dir')};
  }
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
async function loadDuckdbSettings() { try { const response=await fetch('/api/duckdb-import/settings', {cache:'no-store'}); const settings=await response.json(); if (!response.ok) throw new Error(settings.error || 'Settings load failed'); showDuckdbSettings(settings); } catch (error) { document.getElementById('notice').textContent=error.message; } }
async function saveDuckdbSettings() { try { const settings=await duckdbRequest('/api/duckdb-import/settings', {source_duckdb_path:value('import_source_duckdb') || null, analysis_duckdb_path:value('import_analysis_duckdb') || null, default_html_root:value('import_default_html_root') || null, audit_root:value('import_audit_root') || null, workers:value('import_workers'), transaction_batch_size:value('import_batch_size')}); showDuckdbSettings(settings); document.getElementById('notice').textContent='Настройки импорта сохранены.'; } catch (error) { document.getElementById('notice').textContent=error.message; } }
async function migrateDuckdb() { try { const settings=await duckdbRequest('/api/duckdb-import/migrate', {target_path:value('migration_target')}); showDuckdbSettings(settings); document.getElementById('notice').textContent='Миграция проверена и активирована.'; } catch (error) { document.getElementById('notice').textContent=error.message; } }
async function duckdbPreflight() { try { render(await duckdbRequest('/api/duckdb-import/preflight', {root_path:value('import_html_root')})); document.getElementById('notice').textContent='Preflight started.'; } catch (error) { document.getElementById('notice').textContent=error.message; } }
async function duckdbImport() { try { const result = await duckdbRequest('/api/duckdb-import/start', {root_path:value('import_html_root'), preflight_token:duckdbPreflightToken}); render(result); } catch (error) { document.getElementById('notice').textContent=error.message; } }
async function duckdbCancel() { try { render(await duckdbRequest('/api/duckdb-import/cancel')); } catch (error) { document.getElementById('notice').textContent=error.message; } }
let directPreflightToken = '';
function directUtc(id) { const raw=value(id); if (!raw) return ''; return raw.endsWith('Z') ? raw : new Date(raw+'Z').toISOString(); }
function directPayload() { return {start_utc:directUtc('direct_start'), end_utc:directUtc('direct_end'), side:value('direct_side'), symbols:value('direct_symbols').split(';'), shift_start_bp:value('direct_shift_start'), shift_end_bp:value('direct_shift_end'), shift_step_bp:value('direct_shift_step')}; }
function renderDirectCoverage(result) {
  const target=document.getElementById('directCoverage'); target.replaceChildren();
  for (const [symbol,timeframes] of Object.entries(result.usable_timeframes || {})) { const label=document.createElement('label'); const box=document.createElement('input'); box.type='checkbox'; box.name='direct_selected_symbol'; box.value=symbol; box.checked=true; label.append(box, document.createTextNode(` ${symbol} · ${timeframes.join(', ')}`)); target.appendChild(label); }
  for (const symbol of Object.keys(result.unavailable_symbols || {})) { const row=document.createElement('div'); row.className='direct-unavailable'; const reasons=(result.coverage_issues || []).filter(item=>item.symbol===symbol).map(item=>`${item.code}: ${item.detail}`).join('; '); row.textContent=`⚠ ${symbol} · ${reasons || 'unavailable'}`; target.appendChild(row); }
}
async function directPreflight() { try { const result=await duckdbRequest('/api/duckdb-direct/preflight', directPayload()); directPreflightToken=result.token; renderDirectCoverage(result); document.getElementById('directStatus').textContent='Coverage checked.'; } catch(error) { document.getElementById('directStatus').textContent=error.message; } }
async function directBuild(parentSurfaceId='') { try { const selected=[...document.querySelectorAll('input[name="direct_selected_symbol"]:checked')].map(item=>item.value); const payload={...directPayload(), preflight_token:directPreflightToken, selected_symbols:selected}; if(parentSurfaceId) payload.parent_surface_id=parentSurfaceId; render(await duckdbRequest('/api/duckdb-direct/start', payload)); } catch(error) { document.getElementById('directStatus').textContent=error.message; } }
async function directCancel() { try { render(await duckdbRequest('/api/duckdb-direct/cancel')); } catch(error) { document.getElementById('directStatus').textContent=error.message; } }
function showAnalysisFacts(facts={}) { for (const [id,key] of [['analysis_unique','unique_point_count'],['analysis_economic','economic_eligible_point_count'],['analysis_event','event_eligible_point_count'],['analysis_plateaus','plateau_count'],['analysis_ready','ready_candidate_count']]) document.getElementById(id).textContent=facts[key] ?? '—'; }
function renderAnalysisLibrary(rows) {
  const target=document.getElementById('analysisLibrary'); target.replaceChildren();
  for(const surface of rows) { const button=document.createElement('button'); button.type='button'; button.className='secondary'; button.textContent=`${surface.period_start_utc} → ${surface.period_end_utc} · ${surface.side} · ${surface.unique_point_count} points`; button.onclick=()=>{ document.getElementById('analysis_surface_id').value=surface.surface_id; const run=(surface.runs||[])[0]; document.getElementById('analysis_run_id').value=run?.run_id||''; showAnalysisFacts(run?.facts||{unique_point_count:surface.unique_point_count}); document.getElementById('analysisStatus').textContent=`parent=${surface.parent_surface_id||'none'} · sources=${(surface.source_hashes||[]).length} · coverage=${(surface.coverage_reasons||[]).join(', ')||'OK'} · final=${run?.facts?.final_state||run?.facts?.facts_state||'surface only'}`; }; target.appendChild(button); }
}
async function analysisRefresh() { try { const rows=await duckdbRequest('/api/analysis/library',{side:value('analysis_side'),build_mode:value('analysis_build_mode'),symbol:value('analysis_symbol'),period_start_utc:value('analysis_period_start'),period_end_utc:value('analysis_period_end'),parent_surface_id:value('analysis_parent'),source_hash:value('analysis_source_hash')}); renderAnalysisLibrary(rows); document.getElementById('analysisStatus').textContent=`${rows.length} surfaces`; } catch(error){ document.getElementById('analysisStatus').textContent=error.message; } }
async function analysisInitialize(){ try { const result=await duckdbRequest('/api/analysis/initialize',{}); document.getElementById('analysisStatus').textContent=`Analysis schema v${result.schema_version} ready.`; } catch(error){ document.getElementById('analysisStatus').textContent=error.message; } }
function analysisRefine(){ const parent=value('analysis_surface_id'); if(!parent){ document.getElementById('analysisStatus').textContent='Select a parent surface.'; return; } directBuild(parent); }
async function analysisRerun(){ try { render(await duckdbRequest('/api/analysis/rerun',{surface_id:value('analysis_surface_id'),dates_path:value('analysis_dates'),config_path:value('analysis_config'),comparison_run_id:value('analysis_left_run')})); } catch(error){ document.getElementById('analysisStatus').textContent=error.message; } }
async function analysisCompare(){ try { const result=await duckdbRequest('/api/analysis/compare',{left_run_id:value('analysis_left_run'),right_run_id:value('analysis_right_run')}); document.getElementById('analysisStatus').textContent=JSON.stringify(result); } catch(error){ document.getElementById('analysisStatus').textContent=error.message; } }
async function analysisExport(){ try { const result=await duckdbRequest('/api/analysis/export',{run_id:value('analysis_run_id'),output_path:value('analysis_output')}); document.getElementById('analysisStatus').textContent=`Exported ${result.output} · ${result.manifest}`; } catch(error){ document.getElementById('analysisStatus').textContent=error.message; } }
function renderDashboard(dashboard) {
  const target = document.getElementById('decisionDashboard'); target.replaceChildren();
  const order = ['csv', 'duckdb', 'candidates', 'tester', 'posttest'];
  for (const key of order) {
    const item = dashboard?.[key]; if (!item) continue;
    const card = document.createElement('section'); card.className = 'decision-card';
    const heading = document.createElement('h4'); heading.textContent = item.title; card.appendChild(heading);
    const state = document.createElement('div'); const positive=['SELECTABLE','PACKAGE_COMPLETE','READY_FOR_TEST','COMPLETED']; const failed=['FAILED']; state.className = 'decision-state ' + (positive.includes(item.state) ? 'good' : (failed.includes(item.state) ? 'bad' : '')); state.textContent = item.state; card.appendChild(state);
    const metrics = document.createElement('div'); metrics.className = 'decision-metrics';
    for (const metric of (item.metrics || [])) { const block=document.createElement('div'); block.className='decision-metric'; const number=document.createElement('b'); number.textContent=metric.value; block.append(number, document.createTextNode(metric.label)); metrics.appendChild(block); }
    card.appendChild(metrics);
    const details = item.details || [];
    if (details.length) { const list=document.createElement('ul'); list.className='decision-details'; for (const detail of details) { const row=document.createElement('li'); row.textContent=detail; list.appendChild(row); } card.appendChild(list); }
    target.appendChild(card);
  }
}
function render(data) {
  if (!defaultsLoaded && data.defaults) { document.getElementById('config').value = data.defaults.config; defaultsLoaded = true; }
  renderDashboard(data.dashboard);
  const imported = data.duckdb_import;
  if (imported) { document.getElementById('duckdbImportStatus').textContent = `${imported.final_state} · safe_to_delete=${imported.safe_to_delete}`; for (const [name, count] of Object.entries(imported.counts || {})) { const item=document.getElementById('import_'+name); if (item) item.textContent=count; } }
  const preflight = data.duckdb_import_preflight;
  if (preflight) { const bytes=v=>`${(Number(v||0)/1073741824).toFixed(2)} GB`; document.getElementById('duckdbImportStatus').textContent = preflight.running ? `Preflight · ${preflight.snapshotted}/${preflight.discovered} files · ${bytes(preflight.processed_bytes)}/${bytes(preflight.total_bytes)}` : (preflight.error || `Preflight ready · ${preflight.discovered} reports`); if(preflight.token) duckdbPreflightToken=preflight.token; }
  const preflightBusy = Boolean(preflight?.running);
  const importBusy = Boolean(imported?.running);
  if (imported && !preflight && !preflightBusy) document.getElementById('duckdbImportStatus').textContent = `${imported.final_state} / safe_to_delete=${imported.safe_to_delete}`;
  document.querySelector('button[onclick="duckdbPreflight()"]')?.toggleAttribute('disabled', preflightBusy || importBusy);
  document.querySelector('button[onclick="duckdbImport()"]')?.toggleAttribute('disabled', preflightBusy || importBusy || !preflight?.token);
  const analysis = data.analysis;
  if (analysis) { document.getElementById('analysis_surface_id').value=analysis.surface_id||''; if(analysis.run_id) document.getElementById('analysis_run_id').value=analysis.run_id; showAnalysisFacts(analysis.statistics||{}); document.getElementById('analysisStatus').textContent=`${analysis.phase}${analysis.run_id?' · '+analysis.run_id:''}${analysis.error?' · '+analysis.error:''}`; }
  const direct = data.duckdb_direct;
  if (direct) document.getElementById('directStatus').textContent = `${direct.phase} · points=${direct.point_count || 0}${direct.surface_id ? ' · '+direct.surface_id : ''}`;
  const job = data.job;
  const buttons = document.querySelectorAll('[data-runnable]'); buttons.forEach(button => button.disabled = Boolean(job && job.running));
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
  document.getElementById('progressBar').setAttribute('aria-valuenow', String(Math.round(percent)));
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

    @property
    def running(self) -> bool:
        return self.status in {"STARTING", "RUNNING"}


@dataclass(slots=True)
class _ImportJob:
    token: str
    root: Path
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
    request: DirectBuildRequest
    preflight_request: DirectBuildRequest
    preflight: DirectPreflight
    cancel: threading.Event = field(default_factory=threading.Event)
    running: bool = True
    phase: str = "STARTING"
    surface_id: str | None = None
    point_count: int = 0
    publication_state: str = "PENDING"
    error: str | None = None
    parent_surface_id: str | None = None


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
        direct_preflight_func: Callable[[duckdb.DuckDBPyConnection, DirectBuildRequest], DirectPreflight] = preflight_duckdb_direct,
        direct_build_func: Callable[..., object] = run_panel_direct_build,
        analysis_library_func: Callable[..., object] = list_surface_library,
        analysis_compare_func: Callable[..., object] = compare_analysis_runs,
        analysis_load_func: Callable[..., object] = load_published_surface,
        analysis_run_func: Callable[..., object] = run_published_pipeline,
        analysis_publish_func: Callable[..., object] = publish_analysis_run,
        analysis_export_func: Callable[..., object] = export_analysis_run,
        analysis_config_loader: Callable[[Path], object] = AlgorithmConfig.from_json,
    ) -> None:
        self.root = root.resolve()
        self.default_config = self._path(default_config)
        self._process_factory = process_factory
        self._browse_factory = browse_factory
        self._lock = threading.RLock()
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
        self._direct_preflight_func = direct_preflight_func
        self._direct_build_func = direct_build_func
        self._direct_preflight: tuple[DirectBuildRequest, DirectPreflight, str] | None = None
        self._direct_job: _DirectJob | None = None
        self._analysis_job: _AnalysisJob | None = None
        self._analysis_library_func = analysis_library_func
        self._analysis_compare_func = analysis_compare_func
        self._analysis_load_func = analysis_load_func
        self._analysis_run_func = analysis_run_func
        self._analysis_publish_func = analysis_publish_func
        self._analysis_export_func = analysis_export_func
        self._analysis_config_loader = analysis_config_loader

    @staticmethod
    def _section(action: str) -> str:
        return {
            "source-csv": "csv",
            "source-duckdb": "duckdb",
            "select": "candidates",
            "tester-plan": "tester",
            "tester-run": "tester",
            "posttest": "posttest",
        }[action]

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

    @staticmethod
    def _optional_string(payload: Mapping[str, object], name: str) -> str:
        value = payload.get(name)
        return value.strip() if isinstance(value, str) else ""

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

    def browse(self, kind: str, multiple: bool) -> tuple[str, ...]:
        if not isinstance(multiple, bool):
            raise ValueError("multiple must be a boolean")
        if kind != "directory" and kind not in _BROWSE_FILE_TYPES:
            raise ValueError(f"unsupported browse kind: {kind}")
        paths = self._browse_factory(kind, multiple)
        return tuple(str(path.resolve()) for path in paths)

    def _import_settings(self, payload: Mapping[str, object] | None = None) -> DuckDBImportSettings:
        if payload is None:
            return load_duckdb_import_settings(self.default_config)
        previous = load_duckdb_import_settings(self.default_config)
        paths = {name: payload.get(name, getattr(previous, name)) for name in ("source_duckdb_path", "analysis_duckdb_path", "default_html_root", "audit_root")}
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
        return {name: (str(value) if isinstance(value, Path) else value) for name, value in ((name, getattr(settings, name)) for name in ("source_duckdb_path", "analysis_duckdb_path", "default_html_root", "audit_root", "workers", "transaction_batch_size"))}

    def duckdb_import_settings(self, payload: Mapping[str, object] | None = None) -> dict[str, object]:
        with self._lock:
            settings = self._import_settings(payload)
            if payload is not None: save_duckdb_import_settings(self.default_config, settings)
        return self._settings_document(settings)

    def _request(self, root: Path, settings: DuckDBImportSettings, *, token: str | None = None, cancellation_requested: Callable[[], bool] | None = None) -> ImportRequest:
        if settings.source_duckdb_path is None or settings.audit_root is None:
            raise ValueError("source_duckdb_path and audit_root must be configured")
        return ImportRequest(root, settings.source_duckdb_path, settings.audit_root, settings.workers, settings.transaction_batch_size, cancellation_requested=cancellation_requested, expected_preflight_token=token)

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
            job = _ImportJob(token, root); self._import_job = job
        threading.Thread(target=self._run_duckdb_import, args=(job,), name="mrs3-panel-duckdb-import", daemon=True).start()
        return self.snapshot()

    def _run_duckdb_import(self, job: _ImportJob) -> None:
        try:
            settings = self._import_settings()
            def progress(item: ImportProgress) -> None:
                with self._lock: job.phase, job.counts = item.final_state, item.counts
            result = self._import_func(self._request(job.root, settings, token=job.token, cancellation_requested=job.cancel.is_set), progress)
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
        def shifts() -> tuple[int, ...]:
            raw = payload.get("required_shifts_bp")
            if raw is not None:
                values = raw.split(";") if isinstance(raw, str) else raw
                try: return tuple(sorted({int(value) for value in values}))
                except (TypeError, ValueError): raise ValueError("required_shifts_bp must be integers") from None
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
            _DIRECT_MATERIALIZER_VERSION, _DIRECT_POINT_CONFIG_HASH,
        )

    @staticmethod
    def _direct_token(request: DirectBuildRequest, preflight: DirectPreflight) -> str:
        document = {
            "request": {name: list(value) if isinstance(value, tuple) else value for name, value in ((name, getattr(request, name)) for name in request.__dataclass_fields__)},
            "usable": {key: list(value) for key, value in preflight.usable_timeframes.items()},
            "unavailable": {key: list(value) for key, value in preflight.unavailable_symbols.items()},
            "issues": [(item.symbol, item.timeframe, item.code, item.detail) for item in preflight.coverage_issues],
            "grid": dict(preflight.grid_contract), "hashes": list(preflight.source_hashes),
            "manifest": list(preflight.manifest), "points": list(preflight.accepted_point_keys),
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
        return value

    @staticmethod
    def _direct_preflight_document(request: DirectBuildRequest, preflight: DirectPreflight, token: str) -> dict[str, object]:
        return {"token": token, "selected_symbols": list(preflight.usable_timeframes), "usable_timeframes": {key: list(value) for key, value in preflight.usable_timeframes.items()}, "unavailable_symbols": {key: list(value) for key, value in preflight.unavailable_symbols.items()}, "coverage_issues": [{"symbol": item.symbol, "timeframe": item.timeframe, "code": item.code, "detail": item.detail} for item in preflight.coverage_issues]}

    def duckdb_direct_preflight(self, payload: Mapping[str, object]) -> dict[str, object]:
        request = self._direct_request(payload)
        preflight = self._with_source(lambda source: self._direct_preflight_func(source, request))
        assert isinstance(preflight, DirectPreflight)
        token = self._direct_token(request, preflight)
        with self._lock: self._direct_preflight = (request, preflight, token)
        return self._direct_preflight_document(request, preflight, token)

    def start_duckdb_direct(self, payload: Mapping[str, object]) -> dict[str, object]:
        request, token = self._direct_request(payload), self._required(payload, "preflight_token")
        parent_surface_id = self._optional_string(payload, "parent_surface_id") or None
        with self._lock:
            if self._direct_job and self._direct_job.running: raise RuntimeError("another direct build is already running")
            if self._direct_preflight is None: raise ValueError("latest preflight token is required")
            original, preflight, expected = self._direct_preflight
            if token != expected or request != original: raise ValueError("latest preflight token is required")
            chosen = payload.get("selected_symbols", tuple(preflight.usable_timeframes))
            selected = self._direct_request({**payload, "symbols": chosen})
            if replace(selected, symbols=request.symbols) != request: raise ValueError("latest preflight token is required")
            if not set(selected.symbols).issubset(preflight.usable_timeframes): raise ValueError("selected symbol is unavailable")
        if parent_surface_id is not None:
            exists = self._with_analysis(
                True,
                lambda connection: connection.execute(
                    "select 1 from surfaces where surface_id=?", [parent_surface_id]
                ).fetchone(),
            )
            if exists is None:
                raise ValueError("unknown parent surface")
        with self._lock:
            if self._direct_job and self._direct_job.running:
                raise RuntimeError("another direct build is already running")
            if self._direct_preflight != (original, preflight, expected):
                raise ValueError("latest preflight token is required")
            job = _DirectJob(selected, original, preflight, parent_surface_id=parent_surface_id)
            self._direct_job = job
        threading.Thread(target=self._run_duckdb_direct, args=(job,), name="mrs3-panel-duckdb-direct", daemon=True).start()
        return self.snapshot()

    def _run_duckdb_direct(self, job: _DirectJob) -> None:
        source = analysis = None
        try:
            source_path, analysis_path = self._direct_paths()
            source = self._direct_connection_factory(str(source_path), read_only=True)
            if job.cancel.is_set(): raise DirectMaterializationError("direct build cancelled")
            active = self._direct_preflight_func(source, job.preflight_request)
            if active != job.preflight: raise DirectMaterializationError("active source changed after preflight")
            analysis = self._direct_connection_factory(str(analysis_path), read_only=False)
            def progress(phase: str, **facts: object) -> None:
                with self._lock: job.phase = phase; job.point_count = int(facts.get("materialized_points", job.point_count))
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
                job.error = "direct build cancelled" if job.cancel.is_set() else f"{type(error).__name__}: direct build failed"
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
            ("DD5 JSON", "scaled_strategy_count"),
        )
        metrics = [
            {"label": label, "value": self._integer(manifest.get(key)) if key != "target_dd_pct" else (self._number_text(manifest.get(key)) or "—")}
            for label, key in values
        ]
        return {
            "title": "DD5 после теста",
            "available": True,
            "state": "RETEST_REQUIRED" if manifest.get("scaled_strategies_require_retest") is not False else "COMPLETED",
            "metrics": metrics,
            "details": ["DD5 JSON требуют отдельного повторного tick-test."],
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
                    "final_state": (result.final_state if evidence_valid else "EVIDENCE_INVALID") if result else import_job.phase,
                    "counts": dict(import_job.counts),
                    "safe_to_delete": safe,
                    "error": import_job.error or ("import failed" if result and result.error else None),
                    "artifacts": ({"import_manifest": "import_manifest.json", "import_checklist": "html_delete_checklist.json"} if evidence_valid else {}),
                }
            direct_job = self._direct_job
            direct_document = None if direct_job is None else {
                "running": direct_job.running,
                "cancel_requested": direct_job.cancel.is_set(),
                "phase": direct_job.phase,
                "point_count": direct_job.point_count,
                "surface_id": direct_job.surface_id,
                "publication_state": direct_job.publication_state,
                "parent_surface_id": direct_job.parent_surface_id,
                "error": direct_job.error,
            }
            analysis_job = self._analysis_job
            analysis_document = None if analysis_job is None else {
                "running": analysis_job.running,
                "phase": analysis_job.phase,
                "surface_id": analysis_job.surface_id,
                "run_id": analysis_job.run_id,
                "statistics": dict(analysis_job.statistics),
                "error": analysis_job.error,
            }
        return {
            "defaults": {
                "root": str(self.root),
                "config": str(self.default_config),
            },
            "job": job_document,
            "duckdb_import": import_document,
            "duckdb_import_preflight": preflight_document,
            "duckdb_direct": direct_document,
            "analysis": analysis_document,
            "dashboard": dashboard,
        }

    def artifact(self, name: str) -> Path | tuple[str, bytes] | None:
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
        if parsed.path == "/api/duckdb-import/settings":
            try:
                self._json(200, self.server.controller.duckdb_import_settings())
            except ValueError as error:
                self._json(400, {"error": str(error)})
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
        if endpoint not in {"/api/start", "/api/browse", "/api/duckdb-import/settings", "/api/duckdb-import/preflight", "/api/duckdb-import/start", "/api/duckdb-import/cancel", "/api/duckdb-import/migrate", "/api/duckdb-direct/preflight", "/api/duckdb-direct/start", "/api/duckdb-direct/cancel", "/api/analysis/initialize", "/api/analysis/library", "/api/analysis/rerun", "/api/analysis/compare", "/api/analysis/export"}:
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
            if endpoint == "/api/browse":
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
            else:
                action = str(document.get("action", ""))
                result = self.server.controller.start(action, document)
        except RuntimeError as error:
            self._json(409, {"error": str(error)})
            return
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self._json(400, {"error": str(error)})
            return
        self._json(202 if endpoint in {"/api/start", "/api/duckdb-import/start", "/api/duckdb-direct/start", "/api/analysis/rerun"} else 200, result)


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

import json
from pathlib import Path
import re
import subprocess


PANEL_WEB = Path(__file__).parents[1] / "src" / "mrs3" / "panel_web"


def _read(name: str) -> str:
    return (PANEL_WEB / name).read_text(encoding="utf-8")


def test_static_shell_starts_with_all_accordions_collapsed_and_status_in_header() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert not re.search(r"<details\b[^>]*\bopen(?:\s|>)", html)
    assert "details.open = true" not in _read("app.js")
    assert "disk_free_bytes" in _read("app.js")
    header = html.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]
    assert 'id="panel-reload"' in header
    assert "'/api/v2/panel/restart'" in js
    assert "requestJson('/api/v2/bootstrap')" in js
    for text in (
        "Static panel shell loaded.",
        "Р—Р°РїСѓСЃРє РѕР¶РёРґР°РµС‚ backend.",
        "01 В· TWO INDEPENDENT TEST JOBS",
        "02 В· SOURCE V6 FRESH COMPACT",
        "03 В· CANONICAL SURFACES",
        "04 В· ANALYSIS в†’ TESTER в†’ DD5",
    ):
        assert text not in html


def test_static_shell_has_approved_navigation_and_exclusions() -> None:
    html = _read("index.html")

    for href in ("#testing", "#source-db", "#surfaces", "#strategies-dd5", "#settings"):
        assert f'href="{href}"' in html
    assert "Testing /" in html
    assert ">Оптимизатор портфеля" in html
    assert 'disabled' in html
    assert 'aria-disabled="true"' in html
    assert 'tabindex="-1"' in html
    assert 'id="artefacts"' not in html
    assert "CSV" not in html
    assert "DUCKDB_DIRECT" not in html


def test_static_shell_does_not_claim_unverified_artifacts() -> None:
    html = _read("index.html")

    assert "18 READY" not in html
    assert "2 / 2 scopes" not in html
    assert "selected-set.surface-v6.duckdb В· READY scopes" not in html
    assert "CXUSDT В· SHORT</th><td>1h" not in html


def test_testing_screen_has_two_independent_runner_cards_without_ssh_fields() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="runner-local"' in html
    assert 'id="runner-remote"' in html
    for runner in ("local", "remote"):
        assert f'id="{runner}-pair"' in html
        assert f'id="{runner}-side"' in html
        assert f'id="{runner}-start-date"' in html
        assert f'id="{runner}-end-date"' in html
    assert 'id="local-paths"' in html
    assert 'id="remote-paths"' not in html
    assert "runner" in html
    assert 'id="panel-reload"' in html
    assert 'name="host"' not in html
    assert 'name="user"' not in html
    assert "ssh" not in html.lower()
    assert 'id="remote-paths"' not in html
    local = html.split('id="runner-local"', 1)[1].split('id="runner-remote"', 1)[0]
    assert 'id="local-delete-old-reports"' in local
    assert 'type="checkbox"' in local
    assert 'class="check"' in local
    assert '<span>Удалить старые отчеты перед стартом</span>' in local
    assert 'id="local-fill"' in local
    assert local.index('id="local-check"') < local.index('id="local-fill"') < local.index('id="local-start"')
    assert "delete_old_reports: document.querySelector('#local-delete-old-reports')?.checked === true" in js
    assert "result.tester_status" in js


def test_screener_card_precedes_runners_and_spans_the_testing_grid() -> None:
    html = _read("index.html")
    css = _read("app.css")
    testing = html.split('<section id="testing"', 1)[1].split('<section id="source-db"', 1)[0]

    assert testing.index('id="screener-local"') < testing.index('id="runner-local"')
    assert testing.index('id="runner-local"') < testing.index('id="runner-remote"')
    assert "#screener-local { grid-column: 1 / -1; }" in css


def test_screener_table_groups_good_counts_and_best_passing_point_accessibly() -> None:
    html = _read("index.html")
    assert '<table class="screener-verdicts-table">' in html
    table = html.split('<table class="screener-verdicts-table">', 1)[1].split("</table>", 1)[0]
    head = table.split("<thead>", 1)[1].split("</thead>", 1)[0]

    assert table.count("<caption") == 1
    assert head.count("<tr>") == 2
    assert '<th scope="colgroup" colspan="2">Хорошие точки</th>' in head
    assert '<th scope="colgroup" colspan="5">Лучшая проходная точка</th>' in head
    assert head.count('scope="col"') == 12
    assert 'id="screener-big-shift-heading"' in head
    assert '>Большой сдвиг</th>' in head
    assert '<abbr title="MA close">MA</abbr>' in head
    assert 'id="screener-verdicts-legend"' in html
    assert "Прочерк означает, что ни одна точка не прошла экономический гейт." in html


def test_screener_table_has_its_own_compact_twelve_column_layout() -> None:
    html = _read("index.html")
    css = _read("app.css")

    assert 'class="shortlist-table screener-verdicts-table"' not in html
    table = html.split('<table class="screener-verdicts-table">', 1)[1].split("</table>", 1)[0]
    columns = table.split("<colgroup>", 1)[1].split("</colgroup>", 1)[0]

    assert columns.count("<col") == 12
    assert '.screener-verdicts-table { min-width: 860px; table-layout: fixed; }' in css
    assert ".screener-verdicts-table th, .screener-verdicts-table td { padding: 9px 6px; }" in css


def test_screener_ui_helpers_format_dynamic_threshold_and_display_metrics() -> None:
    js = _read("app.js")
    script = js.split("const ORDER_BUCKETS", 1)[0] + """
const h = globalThis.screenerUiHelpers;
const checks = {
  configuredThreshold: h.bigShiftHeading(110) === 'Сдвиг ≥ 1,1%',
  changedThreshold: h.bigShiftHeading(150) === 'Сдвиг ≥ 1,5%',
  missingThreshold: h.bigShiftHeading(undefined) === 'Большой сдвиг',
  malformedThreshold: h.bigShiftHeading('not-a-number') === 'Большой сдвиг',
  stringThreshold: h.bigShiftHeading('110') === 'Большой сдвиг',
  booleanThreshold: h.bigShiftHeading(true) === 'Большой сдвиг',
  zeroThreshold: h.bigShiftHeading(0) === 'Большой сдвиг',
  fractionalThreshold: h.bigShiftHeading(110.5) === 'Большой сдвиг',
  arrayThreshold: h.bigShiftHeading([110]) === 'Большой сдвиг',
  roundedPnl30: h.displayMetric('15.3223880597067', 0) === '15',
  roundedDd: h.displayMetric('4.49253767343284', 1) === '4,5',
  roundedDays: h.displayMetric('45.5779652777777', 0) === '46',
  absentMetric: h.displayMetric(null, 1) === '—',
};
if (!h || Object.values(checks).some((value) => !value)) process.exit(1);
"""
    completed = subprocess.run(("node", "-e", script), capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "cell(screenerUiHelpers.displayMetric(verdict.effective_days, 0))" in js


def test_source_surfaces_and_strategies_screens_have_approved_workflow_cards() -> None:
    html = _read("index.html")

    source = html.split('id="source-db"', 1)[1].split('id="surfaces"', 1)[0]
    assert source.count("<details") == 4
    assert "Manual merge" in source
    for control in ('id="source-local-html"', 'id="source-remote-html"', 'id="merge-start"'):
        assert control in source

    surfaces = html.split('id="surfaces"', 1)[1].split('id="strategies-dd5"', 1)[0]
    for label in ("Source DB", "Coverage preflight", "READY", "surface-publish-card"):
        assert label in surfaces

    strategies = html.split('id="strategies-dd5"', 1)[1].split('id="settings"', 1)[0]
    for label in ("Shortlist", "Tester", "DD5", "native SINGLE_MODE tester"):
        assert label in strategies
    assert "Source PnL" not in strategies


def test_settings_semantic_ids_and_static_js_use_v2_testing_endpoints() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert '<section id="settings"' in html
    assert '<form' in html
    assert 'for="settings-default-root"' in html
    assert 'id="settings-default-root"' in html
    assert 'id="settings-import-workers"' in html
    assert 'id="settings-workers-save"' in html
    assert "Общий лимит рабочих процессов" in html
    assert "operational.import_workers" in js
    assert "operational: { import_workers:" in js
    assert 'aria-live="polite"' in html
    assert 'value="legacy"' in html
    assert 'value="static"' in html
    assert "requestJson('/api/v2/bootstrap')" in js
    assert "requestJson('/api/v2/testing/local/status')" in js
    assert "requestJson('/api/v2/testing/remote/status')" in js
    assert "requestJson('/api/v2/testing/local/fill'" in js
    assert "error.code === 'TESTER_FILES_PREPARED'" in js
    assert "Файлы уже подготовлены. Если что-то изменилось, сначала нажмите Стоп." in js
    assert '`/api/v2/testing/local/${action}`' in js
    assert "/api/ui/" not in js
    assert "duckdb-direct" not in js
    assert "title.tabIndex = -1" in js
    assert "let jobTarget = ''" in js
    assert "algorithm_version: document.querySelector('#settings-algorithm')" in js
    assert "requestJson('/api/v2/settings/reload')" in js


def test_analysis_profile_is_one_flat_card_with_explicit_controls() -> None:
    html = _read("index.html")
    js = _read("app.js")

    profile = html.split('id="analysis-profile-card"', 1)[1].split('</details>', 1)[0]
    assert profile.count("<details") == 0
    assert "Вход и версия" not in profile
    assert "Импорт workers/batch" not in profile
    for label in (
        "Экономические фильтры",
        "История и ограничения выборки",
        "Ожидаемая частота сделок",
        "Соседние точки и плато",
        "Отбор READY-кандидатов",
        "Конструкция ордеров",
    ):
        assert label in profile
    for label in (
        "Граница shift для абсолютного минимума сделок, bp",
        "Минимум сделок при shift ≤ границы",
        "Минимум сделок при shift > границы",
        "Базовая минимальная частота сделок в день:",
        "Минимальный PnL/DD",
        "Радиус MA для проверки соседних точек, ±",
        "Минимальная однородность всего плато",
        "Допуск равноценности PnL и PnL/DD",
        "BASE 1ORD: минимум точек в плато",
        "BASE 1ORD: минимум событий плато за 30 дней",
        "Максимум BASE 1ORD на пару/сторону/TF",
        "Multi-order: минимум точек в каждом плато",
        "Multi-order: минимум событий каждого плато за 30 дней",
        "Диапазон меньшего shift: от",
        "Диапазон меньшего shift: до",
        "Минимальное расстояние до следующего ордера, bp",
    ):
        assert label in js
    assert 'id="analysis-profile-reload"' in profile
    assert 'id="analysis-profile-save"' in profile
    assert 'id="analysis-listing-dates-path"' in profile
    assert 'id="analysis-listing-dates-browse"' in profile
    assert "Файл дат листинга" in profile
    assert "'/api/v2/settings/analysis-profile'" in js
    assert "remoteRequest('/api/browse', { kind: 'dates', multiple: false })" in js
    assert "listing_dates_path: value('analysis-listing-dates-path').trim()" in js
    assert "function analysisProfilePayload()" in js
    assert "Целочисленные поля профиля должны быть заполнены целыми числами." in js
    assert "if (!/^-?\\d+$/.test(raw))" in js
    assert "lower_max_exclusive_bp: Number(pair.lower_max_exclusive_bp)" in js
    assert "'envelope_min'" in js
    assert "'base_slots'" in js
    assert "analysis-canonical-shifts" not in js
    assert "analysis-isolated" not in js
    assert "analysis-target-dd" not in js


def test_every_path_save_button_uses_the_settings_save_endpoint() -> None:
    html = _read("index.html")
    js = _read("app.js")

    for button_id in ("local-paths-save",):
        assert f'id="{button_id}"' in html
    assert 'id="remote-paths-save"' not in html
    assert "savePathDefaults" in js
    for path_key in ("local_reports_root", "local_source_db_root", "local_merge_target"):
        assert path_key in js
    assert "['settings-local-runner', 'local_runner_root']" in js
    assert 'id="source-remote-html"' in html
    assert 'id="source-remote-staging"' in html
    assert 'remote_import_html_root' in js
    assert 'remote_import_staging_path' in js
    assert 'remote_html_path: document.querySelector' in js
    assert 'remote_db_target: document.querySelector' in js
    assert "/api/v2/settings/save" in js
    assert js.index("['source-remote-html', 'remote_import_html_root']") < js.index("if (remoteHtml && !remoteHtml.value")
    assert "remoteHtml?.addEventListener('change', () => updateRemoteTarget(true))" in js


def test_local_source_target_derives_a_new_file_from_the_saved_directory() -> None:
    """A saved Source DB directory must never be posted to v6 preflight as target."""
    js = _read("app.js")

    assert "function sourceTargetDirectory(value)" in js
    assert js.index("function sourceTargetDirectory(value)") < js.index("async function loadSafeDefaults()")
    assert "value.toLowerCase().endsWith('.duckdb')" in js
    assert "localTarget.value = sourceTargetPath(paths.local_source_db_root, localHtml?.value || '');" in js
    assert "remoteLocalTarget.value = sourceTargetPath(paths.local_source_db_root, remoteHtml?.value || '');" in js
    assert "local_source_db_root: sourceTargetDirectory(inputValue('source-local-target'))" in js


def test_source_job_failure_shows_its_safe_backend_message() -> None:
    js = _read("app.js")

    assert "job.error?.message || 'Source DB operation failed.'" in js
    assert "sourceStatus(card, `FAILED: ${failure}`);" in js


def test_merge_card_offers_catalog_choices_and_visible_progress() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'list="merge-source-options"' in html
    assert 'id="merge-source-options"' in html
    assert 'id="merge-progress"' in html
    assert "merge-source-options" in js
    assert "input_paths: [document.querySelector('#merge-source-a')?.value || '', document.querySelector('#merge-source-b')?.value || '']" in js
    assert "progressTrack.style.width" in js
    assert "progressTrack.style.width = '100%'" in js
    assert "card.id === 'local-merge-card'" in js


def test_remote_source_card_renders_two_stage_progress_and_elapsed_time() -> None:
    js = _read("app.js")

    for text in ("renderRemoteSourceProgress", "SHA-256", "formatDuration", "formatBytes"):
        assert text in js
    assert "remoteSourceTrack.style.width" in js
    assert "stage_elapsed_seconds" in js
    assert "sourceEvidenceSummary" in js
    assert "accepted_count" in js
    assert "imported ${accepted}" in js
    assert "safe_to_delete" in js
    assert "source_content_digest" in js


def test_surface_materializer_loads_the_configured_source_db_catalog() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="surface-source-refresh"' in html
    assert "loadSourceCatalog" in js
    assert "'/api/v2/source/local/catalog'" in js
    assert "source.dispatchEvent(new Event('change'))" in js
    assert "surfacePreflightRunV2" in js
    assert "preflight must be started again" in js
    assert "sourceCatalogRun" in js
    assert "surfacePublishActive" in js
    assert "surfaceSource.disabled = true" in js
    assert "sourceRefresh.disabled = true" in js


def test_strategies_loads_the_persisted_valid_surface_catalog() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="analysis-surface"' in html
    assert "loadSurfaceCatalog" in js
    assert "'/api/v2/surfaces/catalog'" in js


def test_performance_v2_handoff_exposes_ready_gated_controls() -> None:
    html = _read("index.html")
    js = _read("app.js")

    card = html.split("3. Test and Import to Performance DB", 1)[1].split("</details>", 1)[0]
    assert "Inbox → Performance DB" not in html
    assert "Tester batch" not in html
    assert 'id="performance-inbox-verify"' in html
    assert 'id="performance-import-start"' in html
    assert 'id="performance-import-start" class="button button-primary" disabled' in html
    rows = re.findall(r'<div class="button-row">(.*?)</div>', card, re.S)
    actions = [row for row in rows if "shortlist-generate" in row or "performance-inbox-verify" in row]
    assert len(actions) == 2
    assert actions[0].index("shortlist-generate") < actions[0].index("tester-start") < actions[0].index("tester-stop")
    assert actions[1].index("performance-inbox-verify") < actions[1].index("performance-import-start")
    assert "let normalImportAuthorized = false;" in js
    assert "normalImportAuthorized = true;" in js
    assert "importStartV2.disabled = !ready" not in js
    assert "Cleanup warning" in js


def test_normal_test_and_import_card_is_unified_and_numbered() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert html.count("performance-inbox-verify") == 1
    assert html.count("performance-import-start") == 1
    assert html.count("3. Test and Import to Performance DB") == 1
    assert "4. Pareto and filters" in js
    assert "7. CHECK & RETEST" in js
    card = html.split("3. Test and Import to Performance DB", 1)[1].split("</details>", 1)[0]
    assert card.count('class="progress-block"') == 1
    assert card.count('role="status"') == 1
    assert "const v2CardOrder" in js
    assert "performanceV2WindowTitle) performanceV2WindowTitle.textContent = '5. A/B" in js


def test_strategy_dd5_performance_cards_have_the_exact_final_order_and_titles() -> None:
    html = _read("index.html")
    js = _read("app.js")

    strategies = html.split('id="strategies-dd5"', 1)[1].split('id="settings"', 1)[0]
    for card in ("performance-v2-selection-card", "performance-v2-window-card", "performance-v2-finalist-retest-card", "performance-v2-retest-card", "performance-v2-export-card"):
        assert strategies.count(f'id="{card}"') == 1
    for title in (
        "4. Pareto and filters",
        "5. A/B Performance analysis",
        "6. Bulk RETEST current FINALIST",
        "7. CHECK & RETEST",
        "8. EXPORT FROM PERFORMANCEDB",
    ):
        assert js.count(f"textContent = '{title}'") == 1
    assert "const v2CardOrder = ['performance-v2-selection-card', 'performance-v2-window-card', 'performance-v2-finalist-retest-card', 'performance-v2-retest-card', 'performance-v2-export-card'];" in js
    assert ".map((id) => document.getElementById(id))" in js
    assert "v2Cards.length !== v2CardOrder.length" in js
    assert "Performance v2 cards are not in the expected Strategies and DD5 layout." in js
    assert strategies.count("panel-performance-v2") == 5
    for card, title in (
        ("performance-v2-selection-card", "4. Pareto and filters"),
        ("performance-v2-window-card", "5. A/B Performance analysis"),
        ("performance-v2-finalist-retest-card", "6. Bulk RETEST current FINALIST"),
        ("performance-v2-retest-card", "7. CHECK &amp; RETEST"),
        ("performance-v2-export-card", "8. EXPORT FROM PERFORMANCEDB"),
    ):
        assert f'<details id="{card}"' in strategies
        assert f'<b>{title}</b>' in strategies
    assert "#strategies-dd5 > .panel-performance-v2" not in js


def test_performance_v2_export_card_has_status_filters_and_accessible_status() -> None:
    html = _read("index.html")
    card = html.split('id="performance-v2-export-card"', 1)[1].split("</details>", 1)[0]

    for control, label in (
        ("performance-v2-export-finalist", "FINALIST"),
        ("performance-v2-export-reserve", "RESERVE"),
        ("performance-v2-export-retest", "RETEST"),
        ("performance-v2-export-all-active", "ALL ACTIVE"),
    ):
        assert f'id="{control}"' in card
        assert f">{label}</span>" in card
    assert 'id="performance-v2-export-button"' in card
    assert ">Скачать XLSX</button>" in card
    assert 'id="performance-v2-export-status"' in card
    assert 'role="status"' in card and 'aria-live="polite"' in card


def test_performance_v2_export_query_uses_canonical_status_union_and_retest_modifier() -> None:
    js = _read("app.js")
    script = js.split("const ORDER_BUCKETS", 1)[0] + """
const h = globalThis.performanceV2ExportHelpers;
const checks = {
  empty: h.query([]) === '',
  finalist: h.query(['FINALIST']) === '?status=FINALIST',
  reserve: h.query(['RESERVE']) === '?status=RESERVE',
  union: h.query(['RESERVE', 'FINALIST']) === '?status=FINALIST&status=RESERVE',
  retest: h.query(['RETEST']) === '?retest=1',
  finalistRetest: h.query(['RETEST', 'FINALIST']) === '?status=FINALIST&retest=1',
  reserveRetest: h.query(['RESERVE', 'RETEST']) === '?status=RESERVE&retest=1',
  allActive: h.query(['ALL ACTIVE']) === '?all_active=true',
  allActiveWins: h.query(['ALL ACTIVE', 'FINALIST', 'RETEST']) === '?all_active=true',
};
if (!h || Object.values(checks).some((value) => !value)) process.exit(1);
"""
    completed = subprocess.run(("node", "-e", script), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_performance_v2_export_handler_is_get_only_and_server_named_blob_download() -> None:
    html = _read("index.html")
    js = _read("app.js")
    card = html.split('id="performance-v2-export-card"', 1)[1].split("</details>", 1)[0]
    handler = js.split("performanceV2ExportButton?.addEventListener", 1)[1].split("const settingsStatus", 1)[0]

    assert "/api/v2/strategies/performance-v2/export" in js
    assert "method: 'GET'" in handler
    assert "response.blob()" in handler
    assert "response.headers.get('Content-Disposition')" in handler
    assert "URL.revokeObjectURL(url)" in handler
    assert "download: filename" in handler
    assert "response.json()" in handler
    assert "performanceV2ExportStatus.textContent" in handler
    assert "POST" not in handler
    assert "import" not in handler.lower()
    assert "tester" not in handler.lower()
    assert "strategy_id" not in card and "result_id" not in card
    assert "strategy_ids" not in handler and "result_ids" not in handler
    assert "input.name" not in handler and "file.name" not in handler


def test_performance_v2_export_controls_enforce_all_active_mutual_exclusion() -> None:
    js = _read("app.js")
    handler = js.split("const performanceV2ExportButton", 1)[1].split("const settingsStatus", 1)[0]

    assert "performanceV2ExportAllActive.checked = false" in handler
    assert "performanceV2ExportAllActive.checked" in handler
    assert "control.disabled = performanceV2ExportAllActive?.checked === true" in handler
    assert "performanceV2ExportButton.disabled = !selected.length" in handler


def test_performance_v2_retest_card_uses_server_mapping_and_committed_inbox_gate() -> None:
    html = _read("index.html")
    js = _read("app.js")
    recovery = _read("retest_recovery.js")

    card = html.split('id="performance-v2-retest-card"', 1)[1].split("</details>", 1)[0]
    assert "CHECK &amp; RETEST" in card
    assert 'id="performance-v2-retest-count"' in card
    assert 'id="performance-v2-retest-start"' in card
    assert 'id="performance-v2-retest-end"' in card
    assert 'id="performance-v2-selection-review-file"' in card
    assert 'id="performance-v2-selection-review-import"' in card
    assert 'id="performance-v2-selection-review-results"' in card
    for control in ("performance-v2-selection-review-file", "performance-v2-selection-review-import", "performance-v2-selection-review-results"):
        assert html.count(f'id="{control}"') == 1
    assert 'id="performance-v2-retest-import" class="button button-primary" disabled' in card
    assert "/api/v2/strategies/performance-v2/retest/status" in js
    assert "/api/v2/strategies/performance-v2/retest/start" in js
    assert "/api/v2/strategies/performance-v2/retest/import" in js
    assert html.index('<script src="/panel-web/retest_recovery.js"></script>') < html.index('<script src="/panel-web/app.js"></script>')
    assert "selectRetestTester(jobs)" in js
    assert "job.inbox_ready === true" in js
    assert "tester_job_id: retestTesterJobId" in js
    assert "retestEndDate.max = testerMaxDate();" in js
    assert "end > testerMaxDate()" in js
    assert "value.setHours(0, 0, 0, 0)" in js
    assert "value.getFullYear()" in js and "value.getMonth() + 1" in js and "value.getDate()" in js
    retest_slice = js.split("const retestCard", 1)[1].split("const performanceV2WindowSelect", 1)[0]
    assert "replacement_strategy_ids" not in retest_slice
    assert "failure_report_available" in js
    assert "const retestTesters = (Array.isArray(jobs) ? jobs : [])" in recovery
    assert ".reverse();" in recovery
    assert "job.state === 'COMMITTED' && job.inbox_ready === true" in recovery


def test_finalist_retest_card_exposes_current_control_export_before_retest() -> None:
    html = _read("index.html")
    js = _read("app.js")
    card = html.split('id="performance-v2-finalist-retest-card"', 1)[1].split("</details>", 1)[0]
    export_handler = js.split("finalistRetestExport?.addEventListener", 1)[1].split("finalistRetestImport?.addEventListener", 1)[0]

    assert 'id="performance-v2-finalist-retest-export" class="button button-secondary"' in card
    assert 'id="performance-v2-finalist-retest-export" class="button button-secondary" hidden' not in card
    assert "/api/v2/strategies/performance-v2/finalist-retest/export?include_reserve=" in js
    assert "finalistRetestHasSuccessfulExport" in js
    assert "event.preventDefault()" in export_handler
    assert "Preparing control workbook..." in export_handler
    assert "fetch(finalistRetestExport.href)" in export_handler
    assert "SELECTION_CACHE_INCOMPLETE" in export_handler
    assert "Prepare or recalculate the selection cache first." in export_handler
    assert "response.blob()" in export_handler
    assert "response.headers.get('Content-Disposition')" in export_handler
    assert "filename=" in export_handler
    assert "download: filename" in export_handler
    assert "performance-v2-finalist-retest.xlsx" in export_handler
    assert "Control workbook downloaded." in export_handler
    assert "finalistRetestStatus.textContent" in export_handler


def test_finalist_retest_card_can_clear_reports_before_testing() -> None:
    html = _read("index.html")
    js = _read("app.js")
    card = html.split('id="performance-v2-finalist-retest-card"', 1)[1].split("</details>", 1)[0]

    reserve = '<label class="check"><input id="performance-v2-finalist-retest-reserve" type="checkbox">'
    clear = '<label class="check"><input id="performance-v2-finalist-retest-clear-reports" type="checkbox" checked>'
    assert reserve in card and clear in card
    assert card.index(reserve) < card.index(clear)
    assert "const finalistRetestClearReports" in js
    assert "clear_reports: Boolean(finalistRetestClearReports?.checked)" in js


def test_finalist_retest_import_reports_its_own_progress() -> None:
    js = _read("app.js")
    block = js.split("const finalistRetestStart", 1)[1].split("const performanceV2WindowSelect", 1)[0]

    assert "let finalistRetestImportJobId = ''" in block
    assert '"import_job_id"' in _read("../panel.py")
    assert "/api/v2/strategies/performance-v2/import/status?job_id=" in block
    assert "IMPORT & REPLACE: ${imported.phase || imported.state || 'IMPORTING'}" in block
    assert "IMPORT & REPLACE unavailable: run the retest from this panel first." in block
    assert "finalistRetestImportJobId = '';" in block
    assert "const recoverFinalistRetestJob = async () =>" in block
    assert "strategies.performance.v2.finalist-retest' && job.state === 'COMMITTED' && job.inbox_ready === true" in block
    assert "Recovering latest global finalist retest..." in block
    assert "recoverFinalistRetestJob();" in block


def test_finalist_retest_card_is_only_on_strategies_dd5_screen() -> None:
    html = _read("index.html")
    strategies_start = html.index('<section id="strategies-dd5"')
    strategies_end = html.index('<section id="portfolio"', strategies_start)

    assert html.count('id="performance-v2-finalist-retest-card"') == 1
    assert 'id="performance-v2-finalist-retest-card"' in html[strategies_start:strategies_end]


def test_retest_check_is_the_only_path_that_activates_a_recovered_job() -> None:
    js = _read("app.js")
    check = js.split("retestStart?.addEventListener", 1)[1].split("retestImport?.addEventListener", 1)[0]
    recovery = js.split("const recoverRetestJobs = async", 1)[1].split("retestCard?.addEventListener", 1)[0]

    assert "selectCommittedRetestTester(jobs)" in check
    assert "/api/v2/strategies/tester/verify-inbox" in check
    assert check.index("/api/v2/jobs") < check.index("/api/v2/strategies/tester/verify-inbox") < check.index("/api/v2/strategies/performance-v2/retest/start")
    assert "committed inbox is unavailable" in check
    assert "RETEST_SOURCE_ARTIFACTS_UNAVAILABLE" in check
    assert "retestStart.disabled = false; return;" in check
    invalid_dates = next(line for line in check.splitlines() if "Enter valid RETEST dates" in line)
    assert "retestStart.disabled = false" in invalid_dates
    assert "retestTesterJobId = tester.job_id" not in recovery
    assert "retestImportJobId = importJob.job_id" not in recovery


def test_performance_v2_selection_preview_exposes_ordered_finalist_stages_without_recalculation() -> None:
    html = _read("index.html")
    js = _read("app.js")
    css = _read("app.css")

    assert 'id="performance-v2-selection-preview"' not in html
    assert 'id="performance-v2-selection-recalculate-all"' in html
    assert "scheduleSelectionPreview" in js
    assert "setTimeout" in js
    assert "selectionPreviewButton" not in js
    assert "/api/v2/strategies/performance-v2/recalculate-all" in js
    assert "recalculate-all', {" in js
    assert "body: '{}'" in js
    assert "if (!payload.symbol || !payload.side) return;" in js
    strategies = html.split('id="strategies-dd5"', 1)[1].split('id="settings"', 1)[0]
    assert 'id="performance-v2-selection-card"' in strategies
    assert "4. Pareto and filters" in strategies
    expected_stage_order = [
        "filter_lot_variant_redundancy",
        "filter_holding_outlier",
        "filter_low_trades",
        "filter_min_shift",
        "ab_deterioration",
        "filter_best_trade_dependency",
        "filter_time_consistency",
        "pareto_robust",
        "pareto_shift_near_tie",
        "pareto_window_b",
        "pareto_window_b_dd_shift",
        "pareto_dd5_balanced",
        "pareto_plateau_points_per_order",
        "pareto_plateau_points_total",
        "pareto_efficiency_shift",
        "pareto_dd5_holding",
        "pareto_dd5_close_ma",
        "pareto_dd5_first_shift",
        "pareto_conditional_close_ma",
        "pareto_primary",
        "pareto_dd5_capital",
        "pareto_close_ma_near_tie",
    ]
    assert re.findall(r'data-selection-stage="([^"]+)"', strategies) == expected_stage_order
    assert 'data-selection-rank' in strategies
    assert strategies.count('data-selection-rank') == 1
    assert 'data-selection-pnl-tolerance' in strategies
    assert "rank_robust_top_n" in js
    assert "Final rank stage is unavailable." in js
    for stage_id in expected_stage_order:
        assert f'data-selection-stage="{stage_id}"' in strategies
    checked_stage_ids = {
        "filter_lot_variant_redundancy", "filter_holding_outlier", "ab_deterioration", "pareto_dd5_balanced",
        "filter_best_trade_dependency", "filter_time_consistency", "pareto_robust", "pareto_shift_near_tie",
    }
    for stage_id in checked_stage_ids:
        stage = re.search(rf'<li class="selection-stage" data-selection-stage="{stage_id}">(.*?)</li>', strategies, re.S)
        assert stage and '<input type="checkbox" checked>' in stage.group(1)
    for stage_id in set(expected_stage_order) - checked_stage_ids:
        stage = re.search(rf'<li class="selection-stage" data-selection-stage="{stage_id}">(.*?)</li>', strategies, re.S)
        assert stage and '<input type="checkbox" checked>' not in stage.group(1)
    assert "defaultSelectionStageOrder" in js
    assert "stage.querySelector('[data-selection-scope]').value =" not in js
    default_order = re.search(r"const defaultSelectionStageOrder = \[(.*?)\];", js, re.S)
    assert default_order
    assert re.findall(r"'([^']+)'", default_order.group(1)) == [
        "filter_lot_variant_redundancy", "filter_holding_outlier", "filter_low_trades", "filter_min_shift", "ab_deterioration",
        "filter_best_trade_dependency", "filter_time_consistency", "pareto_dd5_balanced",
        "pareto_robust", "pareto_shift_near_tie", "pareto_close_ma_near_tie",
    ]
    default_enabled = re.search(r"const defaultEnabledSelectionStages = new Set\(\[(.*?)\]\);", js, re.S)
    assert default_enabled
    assert "filter_low_trades" not in default_enabled.group(1)
    assert "filter_min_shift" not in default_enabled.group(1)
    assert "pareto_dd5_balanced" in default_enabled.group(1)
    assert "pareto_plateau_points_per_order" not in default_enabled.group(1)
    assert "pareto_close_ma_near_tie" not in default_enabled.group(1)
    assert 'data-selection-top-n type="number" min="1" step="1" value="20"' in strategies
    assert "near_tie_rank" not in strategies
    assert "data-selection-group" not in strategies
    min_shift_stage = re.search(r'<li class="selection-stage" data-selection-stage="filter_min_shift">(.*?)</li>', strategies, re.S)
    assert min_shift_stage and 'data-selection-min-shift' in min_shift_stage.group(1)
    assert 'value="0.3"' in min_shift_stage.group(1)
    lot_stage = re.search(r'<li class="selection-stage" data-selection-stage="filter_lot_variant_redundancy">(.*?)</li>', strategies, re.S)
    assert lot_stage and 'data-selection-scope="pair_side_timeframe"' in lot_stage.group(1)
    assert "fixedFirst" in js
    pair_side_stages = {"filter_holding_outlier", "filter_low_trades", "filter_min_shift", "ab_deterioration", "pareto_dd5_balanced"}
    for stage_id in pair_side_stages:
        stage = re.search(rf'<li class="selection-stage" data-selection-stage="{stage_id}">(.*?)</li>', strategies, re.S)
        assert stage and 'data-selection-scope="pair_side"' in stage.group(1)
    for stage_id in set(expected_stage_order) - pair_side_stages:
        stage = re.search(rf'<li class="selection-stage" data-selection-stage="{stage_id}">(.*?)</li>', strategies, re.S)
        assert stage and 'data-selection-scope="pair_side_timeframe"' in stage.group(1)
    assert '<select id="performance-v2-selection-pair">' in strategies
    assert 'id="performance-v2-selection-side"' in strategies
    assert 'id="performance-v2-selection-preview"' not in strategies
    assert 'id="performance-v2-selection-xls"' in strategies
    assert "Смотреть результаты в xls" in strategies
    assert "selectionPreviewDirty" in js
    assert "data-selection-move" in js
    assert "selectionPreviewStages" in js
    assert "syncPerformanceV2SelectionScope" in js
    assert "/api/v2/strategies/performance-v2/selection-preview" in js
    assert "selection-stage-summary" in js
    assert "selection-stage-summary-${className}" in js
    assert "grid-template-columns: 28px 24px minmax(0, 1fr) 112px 78px 175px minmax(80px, 105px) 66px" in css
    assert "gap: 16px" in css
    assert re.search(r"\.selection-stage \.check \{[^}]*grid-column: 2 / 4", css)
    for selector, column in (
        ("selection-stage-threshold", 5),
        ("selection-stage-scope", 6),
        ("selection-stage-summary", 7),
        ("selection-stage-controls", 8),
    ):
        assert re.search(rf"\.{selector} \{{[^}}]*grid-column: {column}", css)
    assert ".selection-stage-scope > span:first-child { display: none; }" in css
    assert re.search(r"\.selection-stage-kind \{[^}]*justify-self: start;[^}]*text-align: left", css)
    assert re.search(r"\.selection-stage-kind \{[^}]*width: max-content", css)
    assert ".selection-stage:has(.selection-stage-threshold) .selection-stage-scope > :last-child { margin-top: 14px; }" in css
    rank_stage = re.search(r'<div class="selection-stage selection-stage-fixed" data-selection-rank>(.*?)</div>', strategies, re.S)
    assert rank_stage and 'class="selection-stage-threshold"' in rank_stage.group(1)
    assert rank_stage and 'data-selection-top-n' in rank_stage.group(1)
    assert ".selection-stage-fixed .selection-stage-scope > span:last-child {" in css
    assert "line('Осталось', count.remaining, 'remaining')" in js
    assert "selectionPreviewRevision" in js
    assert "revision !== selectionPreviewRevision" in js
    assert "performanceV2SelectionCard?.addEventListener('toggle'" in js
    assert "selection_preview" not in js
    click_handler = js.split("selectionXlsButton?.addEventListener", 1)[1].split("renderSelectionPreviewOrder();", 1)[0]
    dirty_handler = js.split("const markSelectionPreviewDirty", 1)[1].split("const selectionStages", 1)[0]
    assert "fetch('/api/v2/strategies/performance-v2/selection'" in click_handler
    assert "SELECTION_CACHE_INCOMPLETE" in click_handler
    assert "Prepare or recalculate the selection cache first." in click_handler
    assert "selectionPreviewStatus" in click_handler
    assert "/api/v2/strategies/performance-v2/selection-cache-status" in js
    assert "selectionXlsButton) selectionXlsButton.disabled = !cache.ready" in js
    assert "selectionCacheStatusRevision" in js
    assert "revision !== selectionCacheStatusRevision" in js
    assert "fetch(" not in dirty_handler


def test_analysis_start_immediately_shows_running_phase_and_elapsed_time() -> None:
    js = _read("app.js")

    assert "analysisProgress" in js
    assert "Analysis is running" in js
    assert "Reading and validating surface" in js
    assert "analyzeFresh.disabled = true" in js
    assert "setInterval" in js


def test_surface_async_status_keeps_atomic_phases_indeterminate_and_ignores_stale_errors() -> None:
    js = _read("app.js")

    assert "const determinate = ['HYDRATING', 'MATERIALIZING', 'WRITING', 'VALIDATING'].includes(result.phase);" in js
    assert "publishProgressV2('running', details, determinate ? result.completed : 0, determinate ? result.total : 0);" in js
    assert "if (run === surfacePreflightRunV2 && sourcePath === (surfaceSource?.value || ''))" in js
    assert "result.error || result.phase || 'Analysis failed.'" in js


def test_surface_preflight_has_visible_progress_and_reveals_ready_results() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="surface-preflight-progress"' in html
    assert "renderSurfacePreflightProgress" in js
    assert "surfaceCards[1].open = true" in js
    assert "surfaceCards[2].open = true" in js
    assert "is-running" in js
    assert "'55%'" not in js


def test_surfaces_restore_the_approved_four_stage_accordion_and_scope_table() -> None:
    html = _read("index.html")
    css = _read("app.css")

    surfaces = html.split('id="surfaces"', 1)[1].split('id="strategies-dd5"', 1)[0]
    for control in (
        'id="surface-source-card"',
        'id="surface-preflight-card"',
        'id="surface-ready-card"',
        'id="surface-publish-card"',
        'id="scope-filter-pair"',
        'id="scope-filter-side"',
        'id="scope-filter-status"',
        'id="scope-select-all"',
        'id="scope-select-none"',
        'id="scope-select-visible"',
        'id="surface-publish-progress"',
    ):
        assert control in surfaces
    assert ".scope-table" in css
    assert ".scope-group" in css


def test_surface_publish_uses_a_polled_job_and_selection_changes_require_confirmation() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert "'/api/v2/surfaces/publish/start'" in js
    assert "'/api/v2/surfaces/publish/status'" in js
    assert "confirmedSurfaceScopesV2" in js
    assert "surface selection changed; confirm it before publishing" in js
    assert 'id="surface-target-save"' not in html
    assert 'name="surface_target_path"' in html
    assert 'name="surface_target_path" type="text" value=""' in html
    assert 'id="surface-target"' in html and 'readonly' in html
    assert 'placeholder="data/surfaces"' in html


def test_active_surface_publish_handler_uses_confirmed_snapshot_and_committed_file_path() -> None:
    js = _read("app.js")

    active = js.split("document.querySelector('#surface-publish-start')?.addEventListener", 1)[1]
    assert "'/api/v2/surfaces/publish/start'" in active
    assert "'/api/v2/surfaces/publish'" not in active
    assert "const selectionSnapshot" in active
    assert "const outputDir" in active
    assert "`${outputDir}\\\\${result.target}`" in active
    assert "result.error || 'surface publication failed'" in active
    assert "error instanceof Error ? error.message" in active


def test_surface_timeframe_rows_override_generic_scope_list_label_style() -> None:
    css = _read("app.css")

    assert ".scope-table .scope-timeframe-row { display: grid;" in css
    assert "border-radius: 0;" in css
    assert "color: #e3eaf5;" in css
    assert ".scope-table { --scope-grid-template:" in css
    assert "overflow-x: auto;" in css


def test_surface_scope_preview_uses_the_approved_columns_and_aggregate_contract() -> None:
    js = _read("app.js")
    css = _read("app.css")

    assert "'Период данных'" in js
    assert "'PnL > 10%'" in js
    assert "'PnL медиана / максимум'" in js
    assert "pnl_gt_10_count" in js
    assert "aggregate.common_interval" in js
    assert "aggregate.pnl_preview" in js
    assert "surfaceNumberV2(preview.pnl_gt_10_percent, 1)" in js
    assert "READY · ${ready.count} / ${ready.total}" in js
    assert "'Grid'" not in js
    assert "36px 190px 90px 170px 150px 170px" in css


def test_surface_gap_link_opens_a_visible_report_dialog() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="surface-gap-dialog"' in html
    assert 'id="surface-gap-report"' in html
    assert "showModal()" in js
    assert "gapRun !== surfacePreflightRunV2" in js
    assert "missing_witnesses" in js


def test_strategies_screen_hides_removed_dd5_result_stage_and_live_status() -> None:
    html = _read("index.html")
    js = _read("app.js")

    strategies = html.split('id="strategies-dd5"', 1)[1].split('id="settings"', 1)[0]
    assert 'id="strategy-dd5-card"' not in strategies
    assert 'id="strategy-dd5-status"' not in strategies
    assert "CALCULATION_ONLY" not in strategies
    assert "strategies.performance.dd5" not in js


def test_dd5_screen_removes_non_contract_manifest_and_path_controls() -> None:
    html = _read("index.html")
    strategies = html.split('id="strategies-dd5"', 1)[1].split('id="settings"', 1)[0]

    assert "Manifest Рё lineage" not in strategies
    assert "Export final shortlist" not in strategies
    assert 'id="strategies-output"' not in strategies
    assert 'id="tester-batch"' not in strategies
    assert 'id="tester-output"' not in strategies


def test_dd5_screen_has_no_dead_control_handlers_or_payload_fields() -> None:
    html = _read("index.html")
    js = _read("app.js")

    for text in ("analysis-lineage", "strategies-output", "tester-batch", "tester-output"):
        assert text not in html
        assert text not in js


def test_analysis_catalog_uses_relative_analysis_ref_for_open() -> None:
    js = _read("app.js")

    assert "row.analysis_ref" in js
    assert "analysis_ref: selected" in js
    assert "row.path" not in js


def test_tester_dates_and_local_ranges_are_sent_only_on_tester_start() -> None:
    html = _read("index.html")
    js = _read("app.js")

    for control in ("tester-start-date", "tester-end-date", "tester-range-1m", "tester-range-2m", "tester-range-3m"):
        assert f'id="{control}"' in html
    assert "start_date" in js and "end_date" in js
    assert "startDate > endDate" in js
    tester_handler = js.split("if (testerStart) testerStart.addEventListener", 1)[1].split("if (testerStop)", 1)[0]
    assert "testerStartDate?.value" in tester_handler
    assert "testerEndDate?.value" in tester_handler
    assert "start_date: startDate" in tester_handler
    assert "end_date: endDate" in tester_handler
    assert "tester-initial-balance" in js
    assert "initial_balance: initialBalance" in tester_handler
    for marker in ("shortlist", "analyze", "generate"):
        assert f"tester-range-{marker}" not in js


def test_each_single_mode_card_sends_its_initial_balance_only_to_its_start_route() -> None:
    js = _read("app.js")

    assert "performance-v2-finalist-retest-initial-balance" in js
    assert "performance-v2-retest-initial-balance" in js
    assert "payload.initial_balance = finalistInitialBalance" in js
    assert "initial_balance: retestInitialBalance" in js


def test_tester_card_exposes_single_mode_and_hides_fast_controls() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="tester-start"' in html
    assert 'id="tester-retry"' in html
    assert 'id="tester-retry" class="button button-secondary" disabled' in html
    assert "Продолжить незавершённый тест" in html
    assert "SINGLE_MODE" in html
    assert 'id="tester-start-fast"' not in html
    assert 'id="tester-retry-fast"' not in html
    assert "strategies.tester.fast.start" not in js
    assert "strategies.tester.fast.retry" not in js
    assert "kind: 'strategies.tester.start'" in js
    assert "testerRetry?.addEventListener('click'" in js
    assert "kind: 'strategies.tester.retry'" in js
    assert "request: { job_id: sourceJobId }" in js
    assert "inbox_ready" in js
    assert "READY" in js
    assert "setTesterControls(!testerIsTerminal(job))" in js


def test_normal_tester_jobs_default_to_single_mode_without_metadata() -> None:
    js = _read("app.js")
    render = js.split("const renderTester", 1)[1].split("const pollTester", 1)[0]

    single_mode = re.search(r"const singleMode = ([^;]+);", render)
    assert single_mode
    assert "job.kind === 'strategies.tester.start'" in single_mode.group(1)
    assert "job.request?.mode" not in single_mode.group(1)
    assert "const committed = singleMode && job.state === 'COMMITTED';" in render
    assert "const ready = committed && job.inbox_ready === true;" in render
    assert "inboxVerifyV2.disabled = !committed || importAllowed;" in render


def test_shortlist_active_selection_uses_ready_after_filters_without_http() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="shortlist-select-active"' in html
    assert "ready_after_filters" in js


def test_shortlist_keeps_phase_two_filters_visible_and_indents_tf_rows() -> None:
    css = _read("app.css")
    js = _read("app.js")

    assert "phase2Filters.open = true;" in js
    assert "filtersTitle.replaceWith(filtersTitleElement);" not in js
    assert ".phase2-filters > summary { display: block; margin-bottom: 12px; font-size: .94rem; font-weight: 700; pointer-events: none; }" in css
    assert ".shortlist-table tbody tr.is-timeframe > td:first-child { padding-left: 2.1rem; }" in css


def test_shortlist_controls_match_requested_compact_typography() -> None:
    css = _read("app.css")
    js = _read("app.js")

    assert "#shortlist-summary { display: none; }" in css
    assert ".shortlist-group-checkbox, .shortlist-tf-checkbox { width: 16px; min-width: 16px; height: 16px; min-height: 16px; vertical-align: middle; }" in css
    assert "disclosure.textContent = open ?" in js
    assert "if (event.key === 'Enter' || event.key === ' ') event.preventDefault();" in js
    assert ".shortlist-disclosure { display: inline-flex; align-items: center; justify-content: center; vertical-align: middle; position: relative; top: -1px;" in css


def test_phase_two_checkbox_change_refreshes_the_shortlist() -> None:
    css = _read("app.css")
    js = _read("app.js")

    assert "font-size: .9rem" in css
    assert "const refreshShortlist = async () =>" in js
    assert "document.querySelectorAll('.phase2-filters input[type=\"checkbox\"]').forEach((node) => {" in js
    assert "node.addEventListener('change', refreshShortlist);" in js


def test_pretest_ab_checkbox_is_native_optional_and_sent_outside_phase2_filters() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert '<input id="shortlist-filter-pretest-ab" type="checkbox">' in html
    assert '<input id="shortlist-filter-pretest-ab" type="checkbox" checked>' not in html
    controls = html.index('<div class="shortlist-filter-controls">')
    pretest = html.index('id="shortlist-filter-pretest-ab"')
    pareto = html.index('<details class="phase2-filters"><summary>Paretto filters</summary>')
    controls_end = html.index('</div>', pareto)
    assert controls < pretest < pareto < controls_end
    assert 'shortlist-filter-pretest-ab' not in html[pareto:html.index('</details>', pareto)]
    assert "const pretestAbEnabled = () =>" in js
    assert "pretest_ab_enabled: pretestAbEnabled()" in js
    assert "const filterControls = document.querySelector('.shortlist-filter-controls');" in js
    assert "actions.after(filterControls);" in js
    assert "document.querySelector('#shortlist-filter-pretest-ab')?.addEventListener('change', refreshShortlist);" in js


def test_accordion_status_badges_align_to_the_right() -> None:
    assert ".accordion > summary > .state-badge { margin-left: auto; }" in _read("app.css")


def test_shortlist_bulk_handlers_preserve_non_selection_state() -> None:
    js = _read("app.js")

    handlers = "\n".join(
        js.split(f"document.querySelector('{selector}')", 1)[1].split("});", 1)[0]
        for selector in ("#shortlist-select-all", "#shortlist-select-active", "#shortlist-select-none")
    )
    assert all(selector in js for selector in ("#shortlist-select-all", "#shortlist-select-active", "#shortlist-select-none"))
    assert handlers.count("renderShortlist();") == 3
    assert "remoteRequest" not in handlers
    assert "expandedPairs.add" not in handlers
    assert "expandedPairs.delete" not in handlers
    assert "tester-start-date" not in handlers
    assert "tester-end-date" not in handlers


def test_tester_date_guard_is_iso_validated_before_network_request() -> None:
    js = _read("app.js")

    tester = js.split("if (testerStart) testerStart.addEventListener", 1)[1].split("if (testerStop)", 1)[0]
    assert "validIsoDate" in tester
    assert "!validIsoDate(startDate) || !validIsoDate(endDate)" in tester
    assert "const maxDate = testerMaxDate();" in tester
    assert "endDate > maxDate" in tester
    assert "start_date" in tester and "end_date" in tester
    stop = js.split("if (testerStop) testerStop.addEventListener", 1)[1].split("const renderPerformance", 1)[0]
    assert "start_date" not in stop and "end_date" not in stop


def test_shortlist_table_has_phase_one_optional_group_columns_and_fallbacks() -> None:
    html = _read("index.html")
    js = _read("app.js")

    shortlist = html.split('class="shortlist-table"', 1)[1].split("</table>", 1)[0]
    assert shortlist.split("</thead>", 1)[0].count("<th ") == 12
    for field in ("plateau_count", "period", "deferred"):
        assert f"group.{field}" in js
    assert "вЂ”" in js or "РІР‚вЂќ" in js


def test_shortlist_rows_append_order_buckets_before_new_columns() -> None:
    js = _read("app.js")

    render = js.split("const renderShortlist", 1)[1].split("const applyShortlist", 1)[0]
    assert render.count("for (const bucket of ORDER_BUCKETS)") == 2
    assert "for (const bucket of ORDER_BUCKETS) row.append" in render
    assert "for (const bucket of ORDER_BUCKETS) child.append" in render
    assert "row.append(valueCell(undefined" in render
    assert "child.append(valueCell(group.plateau_count" in render


def test_tester_range_shortcuts_are_local_only() -> None:
    js = _read("app.js")

    ranges = js.split("[1, 2, 3].forEach", 1)[1].split("const testerIsTerminal", 1)[0]
    assert "testerStartDate" in ranges and "testerEndDate" in ranges
    assert "const testerMaxDate = () =>" in js
    assert "testerEndDate.max = testerMaxDate();" in js
    assert "const maxDate = testerMaxDate();" in ranges
    assert "remoteRequest" not in ranges and "fetch(" not in ranges


def test_surface_and_analysis_paths_have_editable_descriptive_names_and_saves() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="surface-name"' in html
    assert 'id="analysis-target-save"' in html
    assert 'data-path-root="analysis_db_root"' in html
    assert "suggested_filename" in js
    assert "analysis_db_root" in js
    assert "normalImportAuthorized" in js


def test_settings_analysis_profile_edits_listing_dates_workflow_path() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="analysis-listing-dates-path"' in html
    assert "result.listing_dates_path" in js
    assert "listing_dates_path: value('analysis-listing-dates-path').trim()" in js


def test_shortlist_has_one_grouped_renderer_and_shared_candidate_state() -> None:
    js = _read("app.js")

    assert js.count("const renderShortlist =") == 1
    assert js.count("let shortlistItems = [];") == 1
    assert js.index("let shortlistItems = [];") < js.index("const renderShortlist = () =>")
    assert "for (const item of shortlistItems)" not in js
    assert "shortlistItems = payload.items || [];" in js
    assert "applyShortlist(shortlist)" in js


def test_shortlist_keeps_native_nine_columns_and_independent_selection_controls() -> None:
    html = _read("index.html")
    js = _read("app.js")

    shortlist = html.split('class="shortlist-table"', 1)[1].split("</table>", 1)[0]
    assert shortlist.split("</thead>", 1)[0].count("<th ") == 12
    assert "shortlist-group-checkbox" in js
    assert "shortlist-tf-checkbox" in js
    assert "Select all READY TFs" in js
    assert "Expand/collapse" in js
    assert "const selectable = pair.timeframes.filter((group) => Number(group.ready_after_filters ?? group.ready ?? 0) > 0);" in js


def test_surface_selection_is_model_driven_and_preserves_open_groups() -> None:
    js = _read("app.js")

    update = js.split("const updateSurfaceV2", 1)[1].split("const showGapReportV2", 1)[0]
    assert "querySelectorAll" not in update
    assert "const filteredReadySurfaceKeysV2" in js
    assert "selectFilteredScopesV2" in js
    assert "setSelectedSurfaceScopesV2([...selectedSurfaceScopes, ...filteredReadySurfaceKeysV2()]);" in js
    assert "scope-select-visible" in js
    assert "selectFilteredButtonV2.textContent" in js
    assert "const expandedSurfacePairs = new Set();" in js
    assert "expandedSurfacePairs.has(groupKey)" in js
    assert "groupNode.addEventListener('toggle'" in js


def test_surface_table_reuses_one_grid_template_for_header_groups_and_timeframes() -> None:
    css = _read("app.css")

    assert "--scope-grid-template:" in css
    assert ".scope-table-row, .scope-group > summary, .scope-table .scope-timeframe-row" in css
    assert "grid-template-columns: var(--scope-grid-template)" in css


def test_shared_request_json_and_job_recovery_keep_errors_and_busy_state_truthful() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="status"' in html
    assert "const requestJson = async (endpoint, options = {})" in js
    assert "const code = typeof result?.error === 'string'" in js
    assert "Backend connection unavailable." in js
    assert "const setTesterControls = (busy)" in js
    assert "testerCommitted = committed;" in js
    assert "normalImportAuthorized = false;" in js
    assert "const recoverJobs = async () =>" in js
    assert "requestJson('/api/v2/jobs')" in js
    assert "recoverJobs();" in js
    assert 'id="remote-paths"' not in html
    assert "remote-paths-save" not in js


def test_request_error_helper_allows_only_declared_safe_string_endpoints() -> None:
    js = _read("app.js")
    script = js.split("const ORDER_BUCKETS", 1)[0] + """
const allows = globalThis.panelRequestErrorHelpers.allowsSafeString;
console.log(JSON.stringify({
  profile: allows('/api/v2/settings/analysis-profile'),
  fresh: allows('/api/v2/strategies/fresh/analyze'),
  arbitrary: allows('/api/v2/settings/arbitrary'),
}));
"""

    completed = subprocess.run(("node", "-e", script), capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "profile": True,
        "fresh": True,
        "arbitrary": False,
    }


def test_bulk_shortlist_actions_do_not_touch_pair_expansion_state() -> None:
    js = _read("app.js")

    for selector in ("#shortlist-select-all", "#shortlist-select-none", "#shortlist-refresh"):
        segment = js.split(f"document.querySelector('{selector}')", 1)[1].split("});", 1)[0]
        assert "expandedPairs" not in segment


def test_shared_json_requests_fail_safely_and_busy_job_controls_cleanup() -> None:
    js = _read("app.js")

    assert "const requestJson = async" in js
    helper = js.split("const requestJson = async", 1)[1].split("const remoteRequest", 1)[0]
    assert "response.ok" in helper
    assert "response.json()" in helper
    assert "Backend connection unavailable." in helper
    assert "requestJson('/api/v2/source/local/catalog')" in js
    assert "requestJson('/api/v2/strategies/tester/status?job_id='" in js
    assert "setTesterControls(true)" in js
    assert "finally" in js
    assert "setTesterControls(false)" in js
    assert "importStartV2.disabled = true" in js
    assert "authorizedTesterJobId !== testerJobId" in js


def test_reload_recovers_only_server_job_snapshots() -> None:
    js = _read("app.js")

    assert "const recoverJobs = async" in js
    render = js.split("const renderTester = (job) =>", 1)[1].split("const pollTester", 1)[0]
    assert "job.kind === 'strategies.tester.retry'" in render
    recovery = js.split("const recoverJobs = async", 1)[1].split("const settingsStatus", 1)[0]
    assert "requestJson('/api/v2/jobs')" in recovery
    assert "['strategies.tester.start', 'strategies.tester.retry'].includes(job.kind)" in recovery
    assert "const tester = testerJobs[0];" in recovery
    assert "normalImportAuthorized = false;" in recovery
    assert "CHECK REQUIRED" in js
    assert "kind: 'strategies.tester.start'" in js
    assert "renderTester(job);" in recovery
    assert "renderPerformance(job)" not in recovery
    assert "job.state = " not in recovery
    assert "recoverJobs();" in js


def test_import_result_does_not_repeat_the_pre_import_gate() -> None:
    js = _read("app.js")
    assert "const renderImportV2" in js
    assert "inboxVerifyV2?.addEventListener" in js
    assert "const renderTester" in js
    assert "const pollTester" in js
    import_result = js.split("const renderImportV2", 1)[1].split("inboxVerifyV2?.addEventListener", 1)[0]

    assert "CHECK REQUIRED before import" not in import_result
    assert "gate" not in import_result
    assert "CHECK REQUIRED" in js.split("const renderTester", 1)[1].split("const pollTester", 1)[0]


def test_normal_recovery_uses_created_date_and_suitable_job_priority() -> None:
    js = _read("app.js")
    recovery = js.split("const recoverJobs = async", 1)[1].split("const importStartV2", 1)[0]

    assert "['strategies.tester.start', 'strategies.tester.retry'].includes(job.kind) && job.retest !== true" in recovery
    assert "Number.isFinite(Date.parse(job.created_at_utc))" in recovery
    assert "['QUEUED', 'RUNNING', 'CANCELLING'].includes(job.state)" in recovery
    assert "job.state === 'COMMITTED' && job.inbox_ready === true" in recovery
    assert "['FAILED', 'CANCELLED'].includes(job.state)" in recovery
    assert "Date.parse(b.created_at_utc) - Date.parse(a.created_at_utc)" in recovery
    assert "String(b.job_id).localeCompare(String(a.job_id))" in recovery
    assert "const tester = testerJobs[0];" in recovery
    assert ".reverse()" not in recovery
    assert "testerJobs.find((job) => !testerIsTerminal(job))" not in recovery


def test_single_mode_retry_shows_recovery_progress_and_avoids_pending_as_failed() -> None:
    js = _read("app.js")

    render = js.split("const renderTester", 1)[1].split("const pollTester", 1)[0]
    retry = js.split("testerRetry?.addEventListener", 1)[1].split("if (testerStop) testerStop.addEventListener", 1)[0]
    assert "const progressTail = ['FAILED', 'CANCELLED'].includes(job.state) ? `failed ${failed}` : `remaining ${Math.max(0, total - checked)}`;" in render
    assert "const startedAt = Date.now();" in retry
    assert "Восстановление отчётов" in retry
    assert "window.setInterval(showRecovery, 1000)" in retry
    assert "window.clearInterval(testerRetryTimer); testerRetryTimer = 0;" in retry
    assert "setTesterControls(true);" in retry
    assert "testerStop.disabled = true;" in retry
    assert "testerRetryable = testerJobId === sourceJobId;" in retry


def test_import_recovery_ignores_stale_failed_jobs_and_uses_newest_suitable_snapshot() -> None:
    js = _read("app.js")
    recovery = js.split("const recoverSplitJobs", 1)[1].split("const retestCard", 1)[0]

    assert "job.kind === 'strategies.performance.v2.import' && job.retest !== true" in recovery
    assert "Number.isFinite(Date.parse(job.created_at_utc))" in recovery
    assert "job.state !== 'CANCELLED'" in recovery
    assert "job.state === 'FAILED'" not in recovery
    assert "Date.parse(b.created_at_utc) - Date.parse(a.created_at_utc)" in recovery
    assert "String(b.job_id).localeCompare(String(a.job_id))" in recovery
    assert ".reverse()" not in recovery


def test_inbox_verify_does_not_fail_silently() -> None:
    js = _read("app.js")
    handler = js.split("inboxVerifyV2?.addEventListener", 1)[1].split("importStartV2?.addEventListener", 1)[0]

    assert "if (!testerJobId || !testerCommitted || normalVerifyInFlight) {" in handler
    assert "committed tester job" in handler
    assert "verified inbox" in handler
    assert "method: 'POST'" in handler
    assert "verified?.inbox_ready !== true" in handler
    assert "const verifyJobId = testerJobId;" in handler
    assert "const verifyEpoch = ++normalVerifyEpoch;" in handler
    assert "verifyEpoch !== normalVerifyEpoch || verifyJobId !== testerJobId || !testerCommitted" in handler
    assert "strategies.tester.start" not in handler
    assert "performance-v2/retest/start" not in handler
    assert "catch (error)" in handler
    assert "error?.message || 'unknown error'" in handler


def test_normal_import_requires_a_fresh_check_after_terminal_import() -> None:
    js = _read("app.js")
    render = js.split("const renderImportV2", 1)[1].split("inboxVerifyV2?.addEventListener", 1)[0]
    handler = js.split("importStartV2?.addEventListener", 1)[1].split("const recoverSplitJobs", 1)[0]

    assert "if (terminal && job.job_id === importJobV2)" in render
    assert "normalImportAuthorized = false;" in render
    assert "authorizedTesterJobId = '';" in render
    assert "importStartV2.disabled = true;" in render
    assert "window.clearInterval(testerPoller); testerPoller = 0;" in handler
    assert "!normalImportAuthorized || authorizedTesterJobId !== testerJobId" in handler
    assert "performance-v2/retest" not in handler
    assert "REPLACE" not in handler


def test_terminal_tester_or_stale_verify_cannot_authorize_import() -> None:
    js = _read("app.js")
    render = js.split("const renderTester", 1)[1].split("const pollTester", 1)[0]

    assert "testerIsTerminal(job) && !committed && job.job_id === testerJobId" in render
    assert "normalVerifyEpoch += 1;" in js


def test_performance_import_keeps_typed_backend_error_reason() -> None:
    js = _read("app.js")
    handler = js.split("importStartV2?.addEventListener", 1)[1].split("const recoverSplitJobs", 1)[0]

    assert "catch (error)" in handler
    assert "error?.code" in handler
    assert "error?.message" in handler
    assert "Импорт Performance v2 не прошёл проверку.'" not in handler


def test_performance_cleanup_warning_formats_code_and_message() -> None:
    js = _read("app.js")
    render = js.split("const renderImportV2", 1)[1].split("inboxVerifyV2?.addEventListener", 1)[0]

    assert "typeof warning === 'object'" in render
    assert "warning.code" in render
    assert "warning.message" in render
    assert "${warning}." not in render


def test_performance_import_renders_the_existing_progress_bar() -> None:
    js = _read("app.js")
    render = js.split("const renderImportV2", 1)[1].split("inboxVerifyV2?.addEventListener", 1)[0]

    assert "const track = testerTrack;" in render
    assert "track.style.width" in render
    assert "batch" in render and "retries" in render and "failed" in render


def test_performance_v2_window_analysis_uses_native_utc_controls() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="performance-v2-window-strategy"' in html
    assert 'id="performance-v2-window-pair"' in js
    assert 'id="performance-v2-window-strategy-id"' in js
    assert 'id="performance-v2-window-finalists" type="checkbox" disabled' in js
    assert "const v2CardOrder" in js
    assert "4. Pareto and filters" in js
    assert "5. A/B Performance analysis" in js
    assert 'id="performance-v2-window-a-start" type="datetime-local" step="1"' in html
    assert "/api/v2/strategies/performance-v2/windows" in js


def test_performance_v2_window_analysis_includes_native_utc_controls() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="performance-v2-window-strategy"' in html
    assert 'id="performance-v2-window-pair"' in js
    assert 'id="performance-v2-window-strategy-id"' in js
    assert 'id="performance-v2-window-finalists" type="checkbox" disabled' in js
    assert "const v2CardOrder" in js
    assert "4. Pareto and filters" in js
    assert "6. Pareto and filters" not in js
    assert "5. A/B Performance analysis" in js
    assert "strategy.symbol === performanceV2WindowPair.value" in js
    assert "String(strategy.strategy_id).includes(query)" in js
    assert "strategy.is_latest_finalist" in js
    assert "performanceV2SelectionPairsWithRuns" in js
    for field in ("performance-v2-window-a-start", "performance-v2-window-a-end", "performance-v2-window-b-start", "performance-v2-window-b-end"):
        assert f'id="{field}" type="datetime-local" step="1"' in html
    assert "UTC" in html.split('id="performance-v2-window-card"', 1)[1].split("</details>", 1)[0]
    assert "/api/v2/strategies/performance-v2/catalog" in js
    assert "/api/v2/strategies/performance-v2/windows" in js
    assert "`${value}Z`" in js
    assert "performanceV2WindowCard?.addEventListener('toggle'" in js
    for control in ("performance-v2-window-a-entire", "performance-v2-window-b-2w", "performance-v2-window-b-1w"):
        assert f'id="{control}"' in html
    assert "performanceV2SetRecentWindow(14)" in js
    assert "performanceV2SetRecentWindow(7)" in js
    assert "Math.max(range[0].getTime(), range[1].getTime() - days * 86_400_000)" in js


def test_performance_v2_retest_tag_import_uses_a_folder_picker_and_bounded_endpoint() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="performance-v2-selection-review-file"' in html
    assert "multiple webkitdirectory hidden" in html
    retest_card = html.split('id="performance-v2-retest-card"', 1)[1].split("</details>", 1)[0]
    assert "Импортировать RETEST из XLS" in retest_card
    assert "hidden" not in retest_card.split('id="performance-v2-selection-review-import"', 1)[1].split(">", 1)[0]
    assert "Импортировать RETEST из XLS" in html
    assert "/api/v2/strategies/performance-v2/retest-tags-import" in js
    assert ".filter((file) => file.name.toLowerCase().endsWith('.xlsx'))" in js


def test_retest_tag_batch_import_reports_one_clear_status_per_file() -> None:
    html = _read("index.html")
    js = _read("app.js")
    handler = js.split("selectionReviewFile?.addEventListener", 1)[1].split("renderSelectionPreviewOrder();", 1)[0]

    assert 'id="performance-v2-selection-review-results"' in html
    assert "selectionReviewImportResults" in handler
    assert "RETEST_TAG_IMPORT_INVALID_FILE" in handler
    assert "RETEST_TAG_IMPORT_DATABASE_MISMATCH" in handler
    assert "RETEST_TAG_IMPORT_STRATEGY_MISMATCH" in handler
    assert "RETEST_TAG_IMPORT_INVALID_RETEST" in handler
    assert "SELECTION_REVIEW_INVALID_STATUS" not in handler
    assert "SELECTION_REVIEW_NOT_LATEST_RUN" not in handler
    assert "const catalogError = await loadPerformanceV2Catalog() || '';" in handler
    assert "finally {" in handler
    assert "await loadRetestStatus();" in handler
    assert "selectionReviewImportButton.disabled = false;" in handler
    assert js.count("selectionReviewImportButton?.addEventListener('click'") == 1
    assert js.count("selectionReviewFile?.addEventListener('change'") == 1
    assert "failed.join('; ')" not in handler


def test_finalist_retest_ui_loads_server_defaults_without_member_ids() -> None:
    html = _read("index.html")
    js = _read("app.js")
    block = js.split("const finalistRetestStart", 1)[1].split("const performanceV2WindowSelect", 1)[0]

    assert 'id="performance-v2-finalist-retest-reserve"' in html
    assert "/api/v2/strategies/performance-v2/finalist-retest/preview?include_reserve=" in block
    assert "preview.test_start" in block and "preview.test_end" in block
    assert "strategy_ids" not in block and "result_ids" not in block
    assert "job.outcomes_finalized === true" in block
    assert "finalistRetestTimer = window.setInterval(pollFinalistRetest, 1000)" in block


def test_performance_v2_window_analysis_renders_server_normalization_in_one_four_column_table() -> None:
    js = _read("app.js")
    render = js.split("const performanceV2MetricDefinitions", 1)[1].split("const loadPerformanceV2Catalog", 1)[0]

    assert render.count("document.createElement('table')") == 1
    assert "['Наименование', 'Значение в окне A', 'Значение в окне Б', 'Изменение']" in render
    assert "performanceV2MetricDefinitions" in render
    assert "performanceV2Change" in render
    assert "performanceV2Numeric" not in render
    assert "Math.log" not in render and "Math.exp" not in render
    assert "classList.add(change.className)" in render
    for metric in ("observed_days", "return_pct", "growth_factor", "trade_rate"):
        assert f"['{metric}'," in js
    for metric in ("return_pct", "daily_growth_pct", "return_dd_ratio", "profit_factor", "win_rate_pct", "trade_count"):
        assert f"['{metric}'," in js
    for metric in ("max_drawdown_pct", "fees_pct", "holding_seconds", "time_in_market_pct"):
        assert f"['{metric}'," in js
    for label in (
        "Статус эквивалента 30 дней", "Календарная длительность нормализации (дни)",
        "Доходность — эквивалент 30 дней", "Фактор роста — эквивалент 30 дней",
        "Сделок / 30д", "Время удержания (мин)", "raw; не нормализуется по длительности",
        "Запрошенное начало (UTC)", "Фактический конец (UTC)", "Причина недоступности",
    ):
        assert label in js
    assert "maximumFractionDigits: 2" in render
    assert "performanceV2StrategyDetails" in js
    assert "const minutes = Number(value) / 60;" in js
    assert "batch" not in render.lower()
    assert "rank" not in render.lower()


def test_performance_v2_window_analysis_highlights_effective_coverage_before_metrics() -> None:
    js = _read("app.js")
    render = js.split("const performanceV2MetricDefinitions", 1)[1].split("const loadPerformanceV2Catalog", 1)[0]

    assert "performanceV2WindowCoverage" in render
    assert "requested_start_utc" in render
    assert "effective_start_utc" in render
    assert "observed_days" in render
    assert "performance-v2-coverage-warning" in render
    assert "performanceV2UtcText(window?.requested_start_utc)" in render
    assert "performanceV2UtcText(window?.effective_start_utc)" in render
    assert "effectiveMs / requestedMs" not in render
    assert "performanceV2WindowCoverage(windowA, windowB), performanceV2WindowTable(windowA, windowB)" in render


def test_performance_v2_window_analysis_has_selected_strategy_parameters() -> None:
    html = _read("index.html")
    js = _read("app.js")
    render = js.split("const performanceV2MetricDefinitions", 1)[1].split("const loadPerformanceV2Catalog", 1)[0]

    card = html.split('id="performance-v2-window-card"', 1)[1].split("</details>", 1)[0]
    assert 'id="performance-v2-window-strategy-details"' in card
    assert "strategy.close_ma_len ?? '—'" in js
    assert "window?.normalization_30d" in js
    assert "status === 'ok'" in js
    assert "status === 'too_short'" in js


def test_performance_v2_window_analysis_keeps_missing_values_as_dash() -> None:
    js = _read("app.js")

    assert "if (kind === 'raw_minutes')" in js
    assert "if (value === null || value === undefined || value === '') return '—';" in js
    assert "String(kind ?? '').startsWith('raw')" in js
    assert "order.order_id ?? '—'" in js
    assert "order.open_ma_len ?? '—'" in js
    assert "order.shift_bp ?? '—'" in js
    assert "performanceV2StrategyDetails(null);" in js
    assert "status === 'invalid_duration'" in js
    assert "Эквивалент 30 дней — математический эквивалент исходного окна при постоянной ставке; это не прогноз, не tick-test и не PnL MRS3." in js


def test_testing_screen_does_not_expose_remote_runner_paths() -> None:
    html = _read("index.html")

    testing = html.split('id="runner-remote"', 1)[1].split('</article>', 1)[0]
    assert 'id="remote-paths"' not in testing
    for field in ("remote-bot-root", "remote-runner-root", "remote-reports-root", "remote-reports-archive-root"):
        assert f'id="{field}"' not in html


def test_settings_does_not_render_or_submit_remote_runner_path() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="settings-remote-runner"' not in html
    payload = js.split("const settingsPayload =", 1)[1].split("const settingsButtons", 1)[0]
    assert "remote_runner_root" not in payload


def test_request_json_distinguishes_non_json_and_server_validation_safely() -> None:
    js = _read("app.js")

    helper = js.split("const requestJson = async", 1)[1].split("const remoteRequest", 1)[0]
    assert "content-type" in helper
    assert "application/json" in helper
    assert "Backend returned invalid JSON." in helper
    assert "Server validation failed." in helper
    assert "panelRequestErrorHelpers.allowsSafeString(endpoint)" in helper
    assert "'/api/v2/surfaces/'" in js
    assert "Backend connection unavailable." in helper


def test_status_and_dynamic_controls_have_accessible_announcements() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="status"' in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html
    assert 'caption class="sr-only"' in html
    assert 'aria-expanded' in js
    assert 'aria-label' in js


def test_portfolio_screen_exposes_server_backed_launch_form() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'href="#portfolio"' in html
    assert 'data-screen-link="portfolio"' in html
    assert '<section id="portfolio"' in html
    for control in (
        'id="portfolio-readiness"', 'id="portfolio-pairs"',
        'id="portfolio-select-all"', 'id="portfolio-select-none"',
        'id="portfolio-profile-aggressive"', 'id="portfolio-profile-balanced"',
        'id="portfolio-profile-conservative"', 'id="portfolio-bank-available-aggressive"',
        'id="portfolio-candidates-aggressive"',
        'id="portfolio-run"', 'id="portfolio-new-calculation"',
    ):
        assert control in html
    assert "loadPortfolioScreen" in js
    assert "'/api/v2/portfolio/readiness'" in js
    assert "'/api/v2/portfolio/jobs/active'" in js
    assert "'/api/v2/portfolio/campaigns'" in js


def test_portfolio_profiles_have_balanced_default_optional_bank_and_disabled_actions() -> None:
    html = _read("index.html")
    js = _read("app.js")
    css = _read("app.css")
    profiles = html.split('class="portfolio-profile-table"', 1)[1].split("</table>", 1)[0]
    launch_card = html.split('id="portfolio-launch-card"', 1)[1].split("</article>", 1)[0]
    portfolio = js.split("function loadPortfolioScreen", 1)[1].split("function loadPortfolioSettings", 1)[0]

    assert '<label class="check"><input id="portfolio-profile-aggressive"' in profiles
    assert '<label class="check"><input id="portfolio-profile-balanced"' in profiles
    assert '<label class="check"><input id="portfolio-profile-conservative"' in profiles
    assert 'id="portfolio-profile-balanced" type="checkbox" checked' in profiles
    for profile in ("aggressive", "conservative"):
        assert f'id="portfolio-profile-{profile}" type="checkbox" checked' not in profiles
    for profile in ("aggressive", "balanced", "conservative"):
        assert f'id="portfolio-bank-available-{profile}" type="text"' in profiles
        assert f'id="portfolio-bank-available-{profile}" type="text" inputmode="decimal" placeholder="Без ограничения" aria-describedby="portfolio-bank-available-hint"' in profiles
    assert profiles.count("<tr>") == 4
    for profile, dd, reserve, mm in (
        ("AGGRESSIVE", "20%", "20%", "50%"),
        ("BALANCED", "10%", "40%", "35%"),
        ("CONSERVATIVE", "5%", "60%", "20%"),
    ):
        assert f'<th scope="row">{profile}</th><td>{dd}</td><td>{reserve}</td><td>{mm}</td>' in profiles

    assert "Complete valid pair, direction, profile, and budget fields." not in js
    assert "Complete valid pair, direction, profile, and candidate fields." not in js
    assert "Заполните обязательные поля и исправьте недопустимые лимиты." in js
    assert 'id="portfolio-bank-available-hint"' in launch_card
    assert "Пусто — без ограничения." in launch_card
    assert 'id="portfolio-run"' in launch_card
    assert 'id="portfolio-new-calculation"' in launch_card
    assert "const profilesValid = profiles.length > 0" in portfolio
    assert "runButton.disabled = state.locked || !launch.valid;" in js
    assert "#portfolio-launch-card .button:disabled {" in css


def test_portfolio_readiness_is_compact_and_stage1_only() -> None:
    html = _read("index.html")
    js = _read("app.js")
    portfolio = js.split("function loadPortfolioScreen", 1)[1].split("function loadPortfolioSettings", 1)[0]

    assert 'id="portfolio-readiness" class="portfolio-readiness-strip"' in html
    assert 'aria-labelledby="portfolio-readiness-label"' in html
    assert 'id="portfolio-readiness-state" class="state-badge state-pending" role="status" aria-live="polite"' in html
    assert 'id="portfolio-schema-version"' not in html
    assert 'id="portfolio-policy-version"' not in html
    assert 'id="portfolio-config-digest"' not in html
    assert 'id="portfolio-stage2-status"' not in html
    assert "...portfolioValues(readiness?.stage2?.blockers)" not in portfolio
    assert "blockers.hidden = reasons.length === 0" in portfolio
    assert "blockers.replaceChildren()" in portfolio


def test_portfolio_settings_shows_editable_profile_risk_policy() -> None:
    html = _read("index.html")
    settings = html.split('id="portfolio-settings"', 1)[1].split("</details>", 1)[0]
    policy = settings.split('class="portfolio-profile-policy-table"', 1)[1].split("</table>", 1)[0]

    assert 'class="section-subtitle"' in settings
    assert "Профиль" in policy
    assert "Макс. DD" in policy
    assert "Мин. запас свободной маржи" in policy
    assert "Макс. MM-нагрузка" in policy
    assert policy.count("<tr>") == 4


def test_portfolio_screen_and_settings_card_use_russian_user_labels() -> None:
    html = _read("index.html")
    js = _read("app.js")
    portfolio_html = html.split('id="portfolio"', 1)[1].split('id="settings"', 1)[0]
    settings_html = html.split('id="portfolio-settings"', 1)[1].split("</details>", 1)[0]
    portfolio_js = js.split("function loadPortfolioScreen", 1)[1].split("function loadPortfolioSettings", 1)[0]

    for text in (
        "Portfolio Optimizer", "Stage 1 calculation", "Pairs and directions", "Calculate variants", "New calculation",
        "Calculation progress", "Cancel calculation", "Results appear only after SUCCEEDED", "Download Stage 1 XLSX",
        "Tester handoff", "Submit to tester", "accepted v2 document", "Advanced controls used by the current adapter",
    ):
        assert text not in portfolio_html + settings_html
    for text in (
        "Ready to freeze this Campaign.", "Complete the required selections and fix invalid limits.",
        "No calculation is active.", "Results appear only after SUCCEEDED.", "Campaign frozen; creating server job",
        "Campaign frozen and queued on the server.", "New Campaign ready.", "Select ${row.pair}",
    ):
        assert text not in portfolio_js
    assert "PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED" in portfolio_html
    assert "profile_id: profile.profile.toUpperCase()" in portfolio_js


def test_portfolio_pair_selection_preserves_per_row_limits() -> None:
    html = _read("index.html")
    js = _read("app.js")
    portfolio = js.split("function loadPortfolioScreen", 1)[1].split("function loadPortfolioSettings", 1)[0]

    assert 'id="portfolio-default-long"' not in html
    assert 'id="portfolio-default-short"' not in html
    assert "selected: row.selected === true" in js
    assert ".filter((row) => row.pair && (row.finalistLong > 0 || row.finalistShort > 0))" in js
    assert "Нет пар с финалистами LONG или SHORT." in portfolio
    assert "copyPortfolioMaximum" not in portfolio
    assert "pairInfo.append(label, count); pairCell.append(pairInfo)" in portfolio
    assert "row.selected = selected.checked; updateControls();" in portfolio
    assert "portfolioSafeInteger(profile.candidates, 1) && Number(profile.candidates) <= 50" in portfolio


def test_portfolio_pair_picker_is_one_full_width_compact_table() -> None:
    html = _read("index.html")
    css = _read("app.css")
    js = _read("app.js")
    portfolio = js.split("function loadPortfolioScreen", 1)[1].split("function loadPortfolioSettings", 1)[0]

    assert '<table class="portfolio-pairs-table">' in html
    assert '<caption class="sr-only">' in html
    assert '<tbody id="portfolio-pairs">' in html
    assert 'id="portfolio-select-all"' in html
    assert 'id="portfolio-select-maximum"' in html
    assert 'title="Перезаписать лимиты каждой пары максимально доступными значениями"' in html
    assert 'id="portfolio-select-none"' in html
    assert '<th scope="col">Пара / доступно</th>' in html
    assert '<th scope="col">Финалистов LONG</th>' in html
    assert '<th scope="col">Финалистов SHORT</th>' in html
    assert "#portfolio-launch-card { grid-column: 1 / -1; }" in css
    assert ".portfolio-pair-info { display: flex;" in css
    assert ".portfolio-pairs-table tbody th { display: flex;" not in css
    assert ".portfolio-pair-count { margin: 0;" in css
    assert "const tableRow = document.createElement('tr')" in portfolio
    assert "const pairCell = document.createElement('th'); pairCell.scope = 'row'" in portfolio
    assert "state.pairRows.forEach((row) => { row.selected = true;" in portfolio
    assert "row.long = Math.max(0, Number(row.finalistLong) || 0)" in portfolio
    assert "row.short = Math.max(0, Number(row.finalistShort) || 0)" in portfolio
    assert "row[side.toLowerCase()] = Number(input.value)" in portfolio
    assert "input.max = String(side === 'LONG' ? row.finalistLong : row.finalistShort)" in portfolio
    assert "invalidPairLimits" in portfolio
    assert "state.pairRows.forEach((row) => { row.selected = false; })" in portfolio
    assert "field.textContent = `Maximum ${side}`" not in portfolio
    assert "portfolio-pair-grid" not in html + css + portfolio


def test_portfolio_launch_exposes_directional_finalist_limits() -> None:
    html = _read("index.html")
    js = _read("app.js")
    portfolio = js.split("function loadPortfolioScreen", 1)[1].split("function loadPortfolioSettings", 1)[0]

    assert "long: finalistLong > 0 ? 1 : 0" in js
    assert "short: finalistLong <= 0 && finalistShort > 0 ? 1 : 0" in js
    assert "max_finalist_long: row.long" in portfolio
    assert "max_finalist_short: row.short" in portfolio


def test_portfolio_recovery_and_polling_use_server_job_endpoints_only() -> None:
    js = _read("app.js")

    assert "'/api/v2/portfolio/jobs/'" in js
    assert "state.poller" in js
    assert "setInterval(pollPortfolioJob" in js
    assert "GET /api/v2/portfolio/jobs/active" not in js
    assert "localStorage" not in js
    assert "sessionStorage" not in js
    assert "PORTFOLIO_JOB_ACTIVE_DUPLICATE" in js
    assert "PORTFOLIO_JOB_BUSY" in js
    assert "newButton?.addEventListener('click', async" in js
    assert "await requestJson('/api/v2/portfolio/readiness')" in js


def test_portfolio_stage2_is_explicitly_disabled_without_tester_dispatch() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="portfolio-stage2-submit"' in html
    assert re.search(r'id="portfolio-stage2-submit"[^>]*disabled[^>]*aria-describedby="portfolio-stage2-reason"', html)
    assert 'id="portfolio-stage2-reason"' in html
    assert "PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED" in html
    assert "tester-submissions" not in js
    portfolio = js.split("function loadPortfolioScreen", 1)[1].split("function loadPortfolioSettings", 1)[0]
    assert "strategies.tester" not in portfolio


def test_portfolio_xlsx_is_rendered_only_after_succeeded_results() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="portfolio-xlsx"' in html
    assert 'id="portfolio-xlsx" hidden' in html
    assert "/api/v2/portfolio/campaigns/" in js
    assert "stage1.xlsx" in js
    assert "job.status === 'SUCCEEDED'" in js
    assert "portfolioXlsx.hidden = false" in js


def test_portfolio_settings_uses_full_document_compare_and_swap() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="portfolio-settings"' in html
    assert 'id="portfolio-settings-form"' in html
    assert 'id="portfolio-settings-document"' not in html
    assert 'id="portfolio-settings-save"' in html
    assert 'id="portfolio-settings-reload"' in html
    assert "'/api/v2/portfolio/settings'" in js
    assert "expected_digest" in js
    assert "JSON.parse" in js
    assert "document: payload" in js
    assert "window.confirm" in js
    assert "UNSUPPORTED_SCHEMA" in js
    assert "MISSING" in js
    assert "INVALID" in js


def test_portfolio_progress_hides_stale_or_terminal_eta() -> None:
    js = _read("app.js")
    render = js.split("const renderJob = (job) =>", 1)[1].split("const pollPortfolioJob", 1)[0]

    assert "const liveEtaUnavailable = liveStale || terminal(job);" in render
    assert "const liveEtaText = !liveEtaUnavailable && Number.isFinite(liveEta)" in render
    assert "stalled / ETA unknown" in render


def test_portfolio_empty_job_progress_uses_utf8_russian_literal() -> None:
    js = _read("app.js")
    portfolio_js = js.split("function loadPortfolioScreen", 1)[1].split("function loadPortfolioSettings", 1)[0]

    assert portfolio_js.count("Нет активного расчёта.") == 3
    assert "РќРµС‚ Р°РєС‚РёРІРЅРѕРіРѕ СЂР°СЃС‡С‘С‚Р°." not in portfolio_js
    progress_assignments = [line.strip() for line in portfolio_js.splitlines() if "text('#portfolio-progress-text'," in line]
    assert len(progress_assignments) == 2
    assert progress_assignments[-1].endswith(": 'Нет активного расчёта.');")


def test_portfolio_settings_shows_operational_controls_and_collapses_advanced_policy() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="portfolio-settings-document"' not in html
    assert 'id="portfolio-settings-form"' in html
    assert 'id="portfolio-settings-total-test-budget"' not in html
    assert 'id="portfolio-settings-profile-aggressive"' not in html
    assert 'id="portfolio-settings-profile-balanced"' not in html
    assert 'id="portfolio-settings-profile-conservative"' not in html
    for field in ("close-volume-participation-pct", "round-down-usdt", "backfill-write-enabled"):
        assert f'id="portfolio-settings-{field}"' in html
    assert 'id="portfolio-settings-advanced"' in html
    assert 'id="portfolio-settings-history"' in html
    assert 'id="portfolio-settings-phase13"' in html
    assert "Текущий этап 1 принудительно использует L=0 и priority=1, поэтому эти поля не влияют на расчёт." in html
    assert not re.search(r'<details id="portfolio-settings-(?:advanced|history|phase13)"[^>]*\bopen(?:\s|>)', html)
    for field in ("seed", "minimum-common-days"):
        assert f'id="portfolio-settings-{field}"' in html
    for field in ("max-enumerated-combinations", "minimum-coverage-pct", "maximum-age-hours", "archive-publication-lag-hours", "weighted-history-step-minutes", "weighted-lp-solutions-per-profile", "weighted-max-targets", "weighted-bootstrap-scenarios-per-block", "weighted-bootstrap-diagnostic-scenarios", "weighted-wall-time-seconds", "weighted-solver-time-seconds", "weighted-limiter-step", "weighted-limiter-controls", "weighted-limiter-stress-pct", "weighted-priority-groups", "weighted-priority-beta", "weighted-priority-close-ratio"):
        assert f'id="portfolio-settings-{field}"' in html
    assert 'id="portfolio-settings-weighted-lp-solutions-per-profile" type="number" inputmode="numeric" min="1" max="20" step="1"' in html
    for field in ("weekend-start-utc", "weekend-end-utc", "repair-attempts", "additional-passes", "base-vectors", "scenarios", "cdar-pct", "diagnostic-cdar-pct", "alternatives-per-profile", "p30-tolerance-pct", "bootstrap-block-days", "bootstrap-p95", "bootstrap-low-block-common-days", "scale-warning-multiple", "api-requests-per-second", "api-concurrency", "api-retries", "reference-max-age-hours", "archive-download-concurrency"):
        assert f'id="portfolio-settings-{field}"' not in html
    assert "^[1-9][0-9]*$" in js
    assert "settingsDecimalValue" in js
    assert "settingsWeekdayMinutes" in js
    assert "JSON.parse(JSON.stringify" in js
    assert "CONFIG_CHANGED" in js
    assert "Настройки изменены в другой сессии. Загружена серверная версия." in js
    assert "const refreshed = await load(true)" in js
    assert "Не удалось загрузить актуальные настройки после конфликта. Сохранение отключено." in js
    assert "aria-invalid" in js
    assert "linkDescriptions" in js
    assert "Введите корректное значение для поля." in js
    assert "const { clone, profiles, riskFields, riskDefaults, settingsPatch, validSettingsDocument, weightedSearchKeys, revealInvalidField, settingsValidationFeedback } = portfolioSettingsHelpers;" in js
    assert "settingsValidationFeedback(error, query, meta, revealInvalidField)" in js


def test_portfolio_settings_patches_only_exposed_leaves_and_preserves_money_lexemes() -> None:
    js = _read("app.js")

    assert "settingsMoneyValue" not in js
    assert "settingsIntegerValue" in js
    assert "settingsDecimalValue" in js
    assert "scenario[name].amount" not in js
    assert "scenario.sizing.upper_bound.amount" not in js
    assert "scenario.sizing.grid !== undefined" in js
    assert "payload.profiles[profile].ranking.top_n" not in js
    assert "const candidate = clone(result.document)" in js
    assert "if (validSettingsDocument(candidate)) state.document = candidate" in js
    assert "document: payload" in js
    assert "const result = await requestJson('/api/v2/portfolio/settings'" in js
    assert "state.conflictMessage = '';\n        clearInvalid();\n        let payload" in js


def test_portfolio_settings_helpers_patch_the_weighted_search_document_without_losing_hidden_fields() -> None:
    document = {
        "schema_version": 2,
        "scenarios": {
            profile: {
                "deposit": {"amount": 10, "currency": "USDT"},
                "collateral": {"amount": 20, "currency": "USDT"},
                "max_balance": {"amount": 30, "currency": "USDT"},
                "sizing": {"upper_bound": {"amount": 30, "currency": "USDT"}},
            }
            for profile in ("AGGRESSIVE", "BALANCED", "CONSERVATIVE")
        },
        "profiles": {profile: {"individual_max_dd_pct": 20, "individual_net_pnl_min_exclusive": 0, "ranking": {"top_n": 2}} for profile in ("AGGRESSIVE", "BALANCED", "CONSERVATIVE")},
        "liquidity": {
            "parameters": {"close_volume_participation_pct": 30},
            "round_down_usdt": 50,
            "minimum_coverage_pct": 90,
            "maximum_age_hours": 2,
            "weekend_start_utc": "SATURDAY 00:00",
            "weekend_end_utc": "MONDAY 00:00",
            "archive_publication_lag_hours": 6,
            "backfill_write_enabled": False,
            "spread_history_bypass_pretest": False,
        },
        "margin": {"parameters": {"open_fee_rate": "0", "close_fee_rate": "0.0002"}},
        "search": {
            "seed": 1,
            "total_test_budget": 9,
            "sizing_mode": "liquidity_cap_single",
            "max_enumerated_combinations": 100000,
            "composition": {"policy_id": "operator_supplied_composition_v1", "parameters": {"operator_supplied": True, "minimum_common_days": 14, "minimum_daily_coverage_pct": 90, "maximum_forward_fill_gap_days": 3}},
            "weighted_search": {
                "history_step_minutes": 5,
                "lp_solutions_per_profile": 20,
                "max_targets": 8,
                "bootstrap_scenarios_per_block": 1000,
                "bootstrap_diagnostic_scenarios": 100,
                "wall_time_seconds": 900,
                "solver_time_seconds": 30,
                "limiter_step": 1,
                "limiter_controls": 2,
                "limiter_stress_pct": 1.5,
                "priority_groups": 5,
                "priority_beta": 0.5,
                "priority_close_ratio": 2,
            },
        },
        "runner": {"root": "hidden", "token": {"keep": True}},
    }
    values = {
        "seed": "2",
        "minimum_common_days": "21",
        "max_enumerated_combinations": "100000",
        "close_volume_participation_pct": "30",
        "round_down_usdt": "50",
        "minimum_coverage_pct": "90",
        "maximum_age_hours": "2",
        "archive_publication_lag_hours": "6",
        "weekend_start_utc": "SATURDAY 00:00",
        "weekend_end_utc": "MONDAY 00:00",
        "backfill_write_enabled": False,
        "spread_history_bypass_pretest": False,
        "open_fee_rate": "0",
        "close_fee_rate": "0.0002",
        "profiles": {
            profile: {"deposit": "10", "collateral": "20", "max_balance": "30", "upper_bound": "30", "individual_max_dd_pct": "20", "individual_net_pnl_min_exclusive": "0", "top_n": "2"}
            for profile in ("AGGRESSIVE", "BALANCED", "CONSERVATIVE")
        },
            "weighted_search": {
                "history_step_minutes": "10",
                "lp_solutions_per_profile": "10",
                "max_targets": "7",
                "bootstrap_scenarios_per_block": "900",
                "bootstrap_diagnostic_scenarios": "90",
                "wall_time_seconds": "800",
                "solver_time_seconds": "20",
                "limiter_step": "2",
                "limiter_controls": "1",
                "limiter_stress_pct": "1.25",
                "priority_groups": "4",
                "priority_beta": "0.4",
                "priority_close_ratio": "2.5",
            }
    }
    script = _read("app.js").split("const portfolioResultHelpers", 1)[0] + f"""
const h = globalThis.portfolioSettingsHelpers;
const document = {json.dumps(document)};
const values = {json.dumps(values)};
const sourceBefore = JSON.stringify(document);
const errorField = (callback) => {{ try {{ callback(); return ''; }} catch (error) {{ return error.field || ''; }} }};
const weightedWith = (name, value) => ({{...values, weighted_search: {{...values.weighted_search, [name]: value}}}});
const patched = h.settingsPatch(document, values);
const onlyOpenFee = {{...values}}; delete onlyOpenFee.close_fee_rate;
const onlyCloseFee = {{...values}}; delete onlyCloseFee.open_fee_rate;
const weighted = patched.search.weighted_search;
const maximums = h.clone(values.weighted_search);
Object.assign(maximums, {{ lp_solutions_per_profile: '20', max_targets: '8', bootstrap_scenarios_per_block: '900', bootstrap_diagnostic_scenarios: '900', limiter_controls: '2', limiter_stress_pct: '99.9', priority_groups: '5', priority_beta: '1' }});
const zeros = h.clone(values.weighted_search);
Object.assign(zeros, {{ limiter_controls: '0', limiter_stress_pct: '0', priority_beta: '0' }});
const extraKey = h.clone(document); extraKey.search.weighted_search.extra = 1;
const missingKey = h.clone(document); delete missingKey.search.weighted_search.max_targets;
const invalidDiagnostic = h.clone(document); invalidDiagnostic.search.weighted_search.bootstrap_diagnostic_scenarios = 1001;
const scientificNumber = h.clone(document); scientificNumber.search.weighted_search.priority_beta = 1e-7;
const missingSeed = h.clone(document); delete missingSeed.search.seed;
const missingMinimumCommonDays = h.clone(document); delete missingMinimumCommonDays.search.composition.parameters.minimum_common_days;
const advancedDetails = {{ open: false }};
const advancedState = {{ ariaInvalid: '', validity: '', focused: false }};
const advancedLabel = {{ textContent: 'Advanced field' }};
const advancedGroup = {{ querySelector: () => advancedLabel }};
const advancedControl = {{ closest: (selector) => selector === 'details' ? advancedDetails : (selector === '.field-group' ? advancedGroup : null), setAttribute: (name, value) => {{ advancedState.ariaInvalid = value; }}, setCustomValidity: (value) => {{ advancedState.validity = value; }}, focus: () => {{ advancedState.focused = true; }} }};
const advancedMeta = {{ textContent: '' }};
const advancedQuery = (selector) => selector === '#portfolio-settings-weighted-bootstrap-diagnostic-scenarios' ? advancedControl : null;
const historyDetails = {{ open: false }};
const historyState = {{ focused: false }};
const historyControl = {{ closest: (selector) => selector === 'details' ? historyDetails : (selector === '.field-group' ? advancedGroup : null), setAttribute: () => {{}}, setCustomValidity: () => {{}}, focus: () => {{ historyState.focused = true; }} }};
const historyQuery = (selector) => selector === '#portfolio-settings-seed' ? historyControl : null;
const maxRuleField = (name, value) => errorField(() => h.settingsPatch(document, weightedWith(name, value)));
const roundTrip = (name, value, expected) => h.settingsPatch(document, weightedWith(name, value)).search.weighted_search[name] === expected;
const checks = {{
  hiddenPreserved: patched.runner.root === 'hidden' && patched.runner.token.keep === true,
  stage1InputsPatched: patched.search.seed === 2 && patched.search.composition.parameters.minimum_common_days === 21,
  feeRatesPatched: patched.margin.parameters.open_fee_rate === '0' && patched.margin.parameters.close_fee_rate === '0.0002',
  singleFeePatch: h.settingsPatch(document, onlyOpenFee).margin.parameters.close_fee_rate === '0.0002' && h.settingsPatch(document, onlyCloseFee).margin.parameters.open_fee_rate === '0',
  sourceUnchanged: JSON.stringify(document) === sourceBefore,
  exactWeightedKeys: JSON.stringify([...h.weightedSearchKeys].sort()) === JSON.stringify(['history_step_minutes', 'lp_solutions_per_profile', 'max_targets', 'bootstrap_scenarios_per_block', 'bootstrap_diagnostic_scenarios', 'wall_time_seconds', 'solver_time_seconds', 'limiter_step', 'limiter_controls', 'limiter_stress_pct', 'priority_groups', 'priority_beta', 'priority_close_ratio'].sort()),
  weightedInteger: weighted.history_step_minutes === 10 && weighted.lp_solutions_per_profile === 10 && weighted.max_targets === 7 && weighted.bootstrap_scenarios_per_block === 900 && weighted.bootstrap_diagnostic_scenarios === 90 && weighted.wall_time_seconds === 800 && weighted.solver_time_seconds === 20 && weighted.limiter_step === 2 && weighted.limiter_controls === 1 && weighted.priority_groups === 4,
  weightedFloat: weighted.limiter_stress_pct === 1.25 && weighted.priority_beta === 0.4 && weighted.priority_close_ratio === 2.5,
  phase13Patched: weighted.limiter_step === 2 && weighted.limiter_controls === 1 && weighted.limiter_stress_pct === 1.25 && weighted.priority_groups === 4 && weighted.priority_beta === 0.4 && weighted.priority_close_ratio === 2.5,
  invalidWeightedInteger: (() => {{ const invalid = h.clone(document); invalid.search.weighted_search.history_step_minutes = 0; return h.validSettingsDocument(invalid) === false; }})(),
  invalidSeed: errorField(() => h.settingsPatch(document, {{...values, seed: '-1'}})) === 'seed',
  invalidMinimumCommonDays: errorField(() => h.settingsPatch(document, {{...values, minimum_common_days: '0'}})) === 'minimum-common-days',
  missingRequiredInputsRejected: h.validSettingsDocument(missingSeed) === false && h.validSettingsDocument(missingMinimumCommonDays) === false,
  safeIntegerBoundary: h.settingsPatch(document, {{...values, seed: String(Number.MAX_SAFE_INTEGER), minimum_common_days: String(Number.MAX_SAFE_INTEGER)}}).search.seed === Number.MAX_SAFE_INTEGER,
  unsafeIntegerRejected: errorField(() => h.settingsPatch(document, {{...values, seed: '9007199254740992'}})) === 'seed',
  invalidStage1Lexemes: ['', ' ', '1e3', '2.5', 'abc'].every((value) => errorField(() => h.settingsPatch(document, {{...values, seed: value}})) === 'seed' && errorField(() => h.settingsPatch(document, {{...values, minimum_common_days: value}})) === 'minimum-common-days'),
  invalidWeightedFloat: (() => {{ const invalid = h.clone(document); invalid.search.weighted_search.priority_close_ratio = 1; return h.validSettingsDocument(invalid) === false; }})(),
  diagnosticBound: h.validSettingsDocument(invalidDiagnostic) === false && errorField(() => h.settingsPatch(document, {{...values, weighted_search: {{...values.weighted_search, bootstrap_diagnostic_scenarios: '901'}}}})) === 'weighted-bootstrap-diagnostic-scenarios',
  representativeMaximums: h.settingsPatch(document, {{...values, weighted_search: maximums}}).search.weighted_search.priority_groups === 5,
  zeroAllowed: (() => {{ const zero = h.settingsPatch(document, {{...values, weighted_search: zeros}}).search.weighted_search; return zero.limiter_controls === 0 && zero.limiter_stress_pct === 0 && zero.priority_beta === 0; }})(),
  strictWeightedKeys: h.validSettingsDocument(extraKey) === false && h.validSettingsDocument(missingKey) === false,
  advancedReveal: (h.settingsValidationFeedback({{field: 'weighted-bootstrap-diagnostic-scenarios'}}, advancedQuery, advancedMeta), advancedDetails.open === true && advancedState.focused && advancedState.ariaInvalid === 'true' && advancedState.validity === 'Введите корректное значение для поля.' && advancedMeta.textContent.includes('Advanced field')),
  historyReveal: (h.settingsValidationFeedback({{field: 'seed'}}, historyQuery, advancedMeta), historyDetails.open === true && historyState.focused),
  scientificNotationRejected: h.validSettingsDocument(scientificNumber) === false,
  roundTripAll: roundTrip('history_step_minutes', '11', 11) && roundTrip('lp_solutions_per_profile', '11', 11) && roundTrip('max_targets', '7', 7) && roundTrip('bootstrap_scenarios_per_block', '901', 901) && roundTrip('bootstrap_diagnostic_scenarios', '90', 90) && roundTrip('wall_time_seconds', '801', 801) && roundTrip('solver_time_seconds', '21', 21) && roundTrip('limiter_step', '2', 2) && roundTrip('limiter_controls', '1', 1) && roundTrip('limiter_stress_pct', '1.25', 1.25) && roundTrip('priority_groups', '4', 4) && roundTrip('priority_beta', '0.4', 0.4) && roundTrip('priority_close_ratio', '2.5', 2.5),
  positiveFieldsRejectZero: ['history_step_minutes', 'lp_solutions_per_profile', 'max_targets', 'bootstrap_scenarios_per_block', 'bootstrap_diagnostic_scenarios', 'wall_time_seconds', 'solver_time_seconds', 'limiter_step', 'priority_groups'].every((name) => !!maxRuleField(name, '0')),
  maxRuleFields: maxRuleField('lp_solutions_per_profile', '21') === 'weighted-lp-solutions-per-profile' && maxRuleField('max_targets', '9') === 'weighted-max-targets' && maxRuleField('limiter_controls', '3') === 'weighted-limiter-controls' && maxRuleField('limiter_stress_pct', '100') === 'weighted-limiter-stress-pct' && maxRuleField('priority_groups', '6') === 'weighted-priority-groups' && maxRuleField('priority_beta', '1.1') === 'weighted-priority-beta' && maxRuleField('priority_close_ratio', '1') === 'weighted-priority-close-ratio',
  invalidFeeRates: ['', '-0.1', '1', '1.0', '0.0000000000001'].every((value) => errorField(() => h.settingsPatch(document, {{...values, open_fee_rate: value}})) === 'open-fee-rate') && errorField(() => h.settingsPatch(document, {{...values, close_fee_rate: '1'}})) === 'close-fee-rate',
}};
if (Object.values(checks).some((value) => !value)) process.exit(1);
"""
    completed = subprocess.run(("node", "-e", script), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_portfolio_settings_exposes_only_the_exact_weighted_search_schema() -> None:
    html = _read("index.html")
    js = _read("app.js")
    weighted_fields = (
        "history_step_minutes", "lp_solutions_per_profile", "max_targets",
        "bootstrap_scenarios_per_block", "bootstrap_diagnostic_scenarios",
        "wall_time_seconds", "solver_time_seconds", "limiter_step", "limiter_controls",
        "limiter_stress_pct", "priority_groups", "priority_beta", "priority_close_ratio",
    )
    for field in weighted_fields:
        assert f'id="portfolio-settings-weighted-{field.replace("_", "-")}"' in html
    assert 'id="portfolio-settings-advanced"' in html
    assert 'id="portfolio-settings-phase13"' in html
    assert not re.search(r'<details id="portfolio-settings-(?:advanced|phase13)"[^>]*\bopen(?:\s|>)', html)
    for field in ("repair-attempts", "additional-passes", "base-vectors", "scenarios", "cdar-pct", "diagnostic-cdar-pct", "alternatives-per-profile", "p30-tolerance-pct", "bootstrap-block-days", "bootstrap-p95", "bootstrap-low-block-common-days", "scale-warning-multiple", "api-requests-per-second", "api-concurrency", "api-retries", "reference-max-age-hours", "archive-download-concurrency"):
        assert f'id="portfolio-settings-weighted-{field}"' not in html
    assert 'id="portfolio-settings-document"' not in html
    assert "weightedSearchValid" in js
    assert "payload.search.weighted_search" in js
    assert "values.weighted_search" in js


def test_portfolio_settings_helpers_validate_v2_lexemes_and_hidden_fields() -> None:
    document = {
        "schema_version": 2,
        "scenarios": {
            profile: {
                "deposit": {"amount": 10, "currency": "USDT"},
                "collateral": {"amount": 20, "currency": "USDT"},
                "max_balance": {"amount": 30, "currency": "USDT"},
                "sizing": {"upper_bound": {"amount": 30, "currency": "USDT"}},
            }
            for profile in ("AGGRESSIVE", "BALANCED", "CONSERVATIVE")
        },
        "profiles": {profile: {"individual_max_dd_pct": 20, "individual_net_pnl_min_exclusive": 0, "ranking": {"top_n": 2}} for profile in ("AGGRESSIVE", "BALANCED", "CONSERVATIVE")},
        "liquidity": {
            "parameters": {"close_volume_participation_pct": 30},
            "round_down_usdt": 50,
            "minimum_coverage_pct": 90,
            "maximum_age_hours": 2,
            "weekend_start_utc": "SATURDAY 00:00",
            "weekend_end_utc": "MONDAY 00:00",
            "archive_publication_lag_hours": 6,
            "backfill_write_enabled": False,
            "spread_history_bypass_pretest": False,
        },
        "search": {
            "seed": 1,
            "total_test_budget": 9,
            "sizing_mode": "liquidity_cap_single",
            "max_enumerated_combinations": 100000,
            "composition": {"policy_id": "operator_supplied_composition_v1", "parameters": {"operator_supplied": True, "minimum_common_days": 14, "minimum_daily_coverage_pct": 90, "maximum_forward_fill_gap_days": 3}},
            "weighted_search": {
                "history_step_minutes": 5, "lp_solutions_per_profile": 20, "max_targets": 8,
                "bootstrap_scenarios_per_block": 1000, "bootstrap_diagnostic_scenarios": 100,
                "wall_time_seconds": 900, "solver_time_seconds": 30, "limiter_step": 1,
                "limiter_controls": 2, "limiter_stress_pct": 1.5, "priority_groups": 5,
                "priority_beta": 0.5, "priority_close_ratio": 2,
            },
        },
        "runner": {"root": "hidden", "token": {"keep": True}},
    }
    values = {
        "seed": "1",
        "minimum_common_days": "14",
        "profiles": {
            profile: {"deposit": "10", "collateral": "20", "max_balance": "30", "upper_bound": "30", "individual_max_dd_pct": "20", "individual_net_pnl_min_exclusive": "0", "top_n": "2"}
            for profile in ("AGGRESSIVE", "BALANCED", "CONSERVATIVE")
        },
        "close_volume_participation_pct": "30",
        "round_down_usdt": "50",
        "minimum_coverage_pct": "90",
        "maximum_age_hours": "2",
        "archive_publication_lag_hours": "6",
        "weekend_start_utc": "SATURDAY 00:00",
        "weekend_end_utc": "MONDAY 00:00",
        "backfill_write_enabled": False,
        "spread_history_bypass_pretest": False,
        "weighted_search": {
            "history_step_minutes": "5", "lp_solutions_per_profile": "20", "max_targets": "8",
            "bootstrap_scenarios_per_block": "1000", "bootstrap_diagnostic_scenarios": "100",
            "wall_time_seconds": "900", "solver_time_seconds": "30", "limiter_step": "1",
            "limiter_controls": "2", "limiter_stress_pct": "1.5", "priority_groups": "5",
            "priority_beta": "0.5", "priority_close_ratio": "2",
        },
    }
    changed = json.loads(json.dumps(values))
    changed["round_down_usdt"] = "25"
    script = _read("app.js").split("const ORDER_BUCKETS", 1)[0] + f"""
const h = globalThis.portfolioSettingsHelpers;
const document = {json.dumps(document)};
const values = {json.dumps(values)};
const changed = {json.dumps(changed)};
const omitted = h.clone(values); delete omitted.weighted_search.limiter_step;
const unchanged = h.settingsPatch(document, values);
const edited = h.settingsPatch(document, changed);
const omittedResult = h.settingsPatch(document, omitted);
const floatDocument = h.clone(document); floatDocument.scenarios.AGGRESSIVE.deposit.amount = 1.5;
const mixedCurrency = h.clone(document); mixedCurrency.scenarios.AGGRESSIVE.collateral.currency = 'EUR';
const equalWeekendDocument = h.clone(document); equalWeekendDocument.liquidity.weekend_end_utc = 'SATURDAY 00:00';
const mustThrow = (callback) => {{ try {{ callback(); return false; }} catch (_) {{ return true; }} }};
const checks = {{
  unchanged: JSON.stringify(unchanged) === JSON.stringify(document),
  hiddenPreserved: edited.runner.root === 'hidden' && edited.runner.token.keep === true,
  scenariosPreserved: JSON.stringify(edited.scenarios) === JSON.stringify(document.scenarios),
  profilesPreserved: JSON.stringify(edited.profiles) === JSON.stringify(document.profiles),
  weekendPreserved: edited.liquidity.weekend_start_utc === document.liquidity.weekend_start_utc && edited.liquidity.weekend_end_utc === document.liquidity.weekend_end_utc,
  omittedWeightedPreserved: omittedResult.search.weighted_search.limiter_step === document.search.weighted_search.limiter_step,
  exposedChanged: edited.liquidity.round_down_usdt === 25,
  gridAbsent: edited.scenarios.AGGRESSIVE.sizing.grid === undefined,
  integerLexemes: ['1.0', '1e3', '0', '-1', ',', ''].every((value) => mustThrow(() => h.settingsIntegerValue(value))),
  floatInboundRejected: h.validSettingsDocument(floatDocument) === false && mustThrow(() => h.settingsPatch(floatDocument, values)),
  mixedCurrencyRejected: h.validSettingsDocument(mixedCurrency) === false && mustThrow(() => h.settingsPatch(mixedCurrency, values)),
  invalidParticipationRejected: mustThrow(() => h.settingsPatch(document, {{...values, close_volume_participation_pct: '201'}})),
  invalidRoundingRejected: mustThrow(() => h.settingsPatch(document, {{...values, round_down_usdt: '0'}})),
  invalidCoverageRejected: mustThrow(() => h.settingsPatch(document, {{...values, minimum_coverage_pct: '101'}})),
  invalidAgeRejected: mustThrow(() => h.settingsPatch(document, {{...values, maximum_age_hours: '0'}})),
  zeroLagAccepted: h.settingsPatch(document, {{...values, archive_publication_lag_hours: '0'}}).liquidity.archive_publication_lag_hours === 0,
  equalWeekendRejected: h.validSettingsDocument(equalWeekendDocument) === false,
}};
if (Object.values(checks).some((value) => !value)) process.exit(1);
"""
    completed = subprocess.run(("node", "-e", script), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_portfolio_settings_helpers_patch_all_nine_risk_leaves_and_reject_bad_decimal() -> None:
    js = _read("app.js")
    script = js.split("const ORDER_BUCKETS", 1)[0] + """
const h = globalThis.portfolioSettingsHelpers;
const profiles = ['AGGRESSIVE', 'BALANCED', 'CONSERVATIVE'];
const document = {
  schema_version: 2,
  scenarios: Object.fromEntries(profiles.map((profile) => [profile, {
    deposit: { amount: 10, currency: 'USDT' }, collateral: { amount: 20, currency: 'USDT' },
    max_balance: { amount: 30, currency: 'USDT' }, sizing: { upper_bound: { amount: 30, currency: 'USDT' } },
  }])),
  profiles: Object.fromEntries(profiles.map((profile) => [profile, {
    max_actual_equity_dd_pct: '20', min_calculated_free_margin_reserve_pct: '20', max_calculated_account_mm_load_pct: '50',
    individual_max_dd_pct: 20, individual_net_pnl_min_exclusive: 0, ranking: { top_n: 2 },
  }])),
  liquidity: { parameters: { close_volume_participation_pct: 30 }, round_down_usdt: 50, minimum_coverage_pct: 90, maximum_age_hours: 2, weekend_start_utc: 'SATURDAY 00:00', weekend_end_utc: 'MONDAY 00:00', archive_publication_lag_hours: 6, backfill_write_enabled: false, spread_history_bypass_pretest: false },
  search: { total_test_budget: 9, sizing_mode: 'liquidity_cap_single', max_enumerated_combinations: 100000, weighted_search: {
    history_step_minutes: 5, lp_solutions_per_profile: 20, max_targets: 8, bootstrap_scenarios_per_block: 1000, bootstrap_diagnostic_scenarios: 100, wall_time_seconds: 900, solver_time_seconds: 30, limiter_step: 1, limiter_controls: 2, limiter_stress_pct: 1.5, priority_groups: 5, priority_beta: 0.5, priority_close_ratio: 2,
  }, seed: 1, composition: { policy_id: 'operator_supplied_composition_v1', parameters: { operator_supplied: true, minimum_common_days: 14, minimum_daily_coverage_pct: 90, maximum_forward_fill_gap_days: 3 } } },
  runner: { root: 'hidden', token: { keep: true } },
};
const patchValues = (risk_policy) => ({ seed: '1', minimum_common_days: '14', max_enumerated_combinations: '100000', close_volume_participation_pct: '30', round_down_usdt: '50', minimum_coverage_pct: '90', maximum_age_hours: '2', archive_publication_lag_hours: '6', backfill_write_enabled: false, spread_history_bypass_pretest: false, weighted_search: { ...document.search.weighted_search }, risk_policy });
const values = patchValues(Object.fromEntries(profiles.map((profile) => [profile, { max_actual_equity_dd_pct: '10.000000000001', min_calculated_free_margin_reserve_pct: '40.000000000001', max_calculated_account_mm_load_pct: '35.000000000001' }])));
const before = JSON.stringify(document);
const patched = h.settingsPatch(document, values);
const bad = (field, value) => { try { const policy = Object.fromEntries(profiles.map((profile) => [profile, { max_actual_equity_dd_pct: '10', min_calculated_free_margin_reserve_pct: '40', max_calculated_account_mm_load_pct: '35' }])); policy.BALANCED[field] = value; h.settingsPatch(document, patchValues(policy)); return false; } catch (error) { return error.field === `risk-balanced-${field.replaceAll('_', '-')}`; } };
const checks = {
  sourceUnchanged: JSON.stringify(document) === before,
  allProfilesPatched: profiles.every((profile) => patched.profiles[profile].max_actual_equity_dd_pct === '10.000000000001' && patched.profiles[profile].min_calculated_free_margin_reserve_pct === '40.000000000001' && patched.profiles[profile].max_calculated_account_mm_load_pct === '35.000000000001'),
  precisionAccepted: h.settingsPatch(document, patchValues(Object.fromEntries(profiles.map((profile) => [profile, { max_actual_equity_dd_pct: '0.000000000001', min_calculated_free_margin_reserve_pct: '100', max_calculated_account_mm_load_pct: '0' }])))).profiles.AGGRESSIVE.max_actual_equity_dd_pct === '0.000000000001',
  blankRejected: bad('max_actual_equity_dd_pct', ''), commaRejected: bad('max_actual_equity_dd_pct', '1,2'), tooPreciseRejected: bad('max_actual_equity_dd_pct', '0.1234567890123'), tooLargeRejected: bad('max_actual_equity_dd_pct', '100.000000000001'),
};
if (Object.values(checks).some((value) => !value)) process.exit(1);
"""
    completed = subprocess.run(("node", "-e", script), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_portfolio_form_has_no_speculative_nullable_config_fields() -> None:
    html = _read("index.html")

    portfolio = html.split('id="portfolio"', 1)[1].split('id="settings"', 1)[0]
    assert 'name="max_balance_usdt"' not in portfolio
    assert 'name="default_max_balance"' not in portfolio
    assert 'schema_version="2"' not in portfolio


def test_portfolio_settings_exposes_one_pretest_spread_history_bypass_checkbox() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert html.count('id="portfolio-settings-spread-history-bypass-pretest"') == 1
    assert "Предварительный расчёт без истории стакана" in html
    assert "liquidity.spread_history_bypass_pretest" in js
    assert "spread-history-bypass-pretest" in js


def test_portfolio_panel_keeps_unknown_spread_blocker_codes_visible_verbatim() -> None:
    js = _read("app.js")
    script = js.split("const ORDER_BUCKETS", 1)[0] + """
const helper = globalThis.portfolioReasonHelpers;
const unknown = 'SPREAD_HISTORY_UNAVAILABLE';
if (helper.humanize(unknown) !== unknown) process.exit(1);
"""
    completed = subprocess.run(("node", "-e", script), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "portfolioReasonHelpers.humanize(typeof value === 'string' ? value" in js


def test_portfolio_panel_renders_pretest_spread_warning_from_result_summary() -> None:
    js = _read("app.js")
    css = _read("app.css")
    render_results = js.split("const renderResults = async (job) =>", 1)[1].split("const renderJob = (job) =>", 1)[0]

    assert "const values = result.summary || result;" in render_results
    assert "for (const [key, value] of Object.entries(values || {}))" not in render_results
    for label in (
        "Взято финалистов в расчёт", "В вариант №1 вошло", "Без позиции", "Вариант №1", "Остальные варианты находятся в XLSX.",
        "Банки", "Насыщение", "Целевой банк", "Минимальный банк для DD ≤", "Банк для DD ≤", "Банк для профильных лимитов",
        "Просадки", "MaxDD SUM", "Исторический рассчитанный DD", "Средняя глубина худших 20% просадок",
        "Средняя глубина худших 10% просадок", "Капитал и результат", "PnL", "IM", "MM",
        "Состав варианта", "Полный номинал позиции", "Множитель пары", "max_balance", "Распределение входов", "Технические данные",
        "целевой банк / банк насыщения",
    ):
        assert label in render_results
    for redundant in ("Прочитано финалистов", "Отобрано кандидатов", "USER_RANK_CUTOFF"):
        assert redundant not in render_results
    assert "values.finalists_read" not in render_results
    assert "values.excluded" not in render_results
    assert "values.optimizer_warnings" not in render_results
    for unsafe_html_sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert unsafe_html_sink not in render_results
    assert "label.textContent = labelText" in render_results
    assert "value.textContent = format(rawValue) || '—'" in render_results
    assert "if (!portfolioResultHelpers.known(rawValue)) continue" not in render_results
    assert "code.textContent = String(value)" in render_results
    assert "Number(values['Limiter L']) > 0" in render_results
    assert "portfolio-result-accepted" in css
    assert "portfolio-result-metrics" in css
    assert "portfolio-result-members" in css
    assert "portfolio-result-bank-group" in css
    assert "portfolio-result-drawdown-group" in css
    assert "portfolio-result-capital-group" in css


def test_portfolio_result_formatting_is_compact_and_hides_unknown_values() -> None:
    js = _read("app.js")
    script = js.split("const ORDER_BUCKETS", 1)[0] + """
const h = globalThis.portfolioResultHelpers;
const payload = { zero: 0, textZero: '0', exponentZero: '0E-18', negative: '-12.3456', falseValue: false };
const before = JSON.stringify(payload);
const checks = {
  longDecimal: h.metric('215.112765539596662941132816039291456392347838') === '215,11',
  percent: h.metric('20.0', 1, '%') === '20%',
  uncapped: h.bank('UNCAPPED') === 'Без ограничения',
  unknownHidden: h.known('UNKNOWN') === false && h.known('NOT_TESTED') === false,
  zeroKnown: h.known(payload.zero) && h.known(payload.textZero) && h.known(payload.exponentZero),
  falseKnown: h.known(payload.falseValue),
  negative: h.metric(payload.negative) === '-12,35',
  exponent: ['1 000', '1 000'].includes(h.metric('1E+3')),
  invalidPreserved: h.metric('not-a-number') === 'not-a-number',
  profile: h.profile('AGGRESSIVE') === 'Агрессивный',
  amountRatios: h.amountRatios('130.32', '26.064', '12.81') === '130,32 USDT · 26,06% / 12,81%',
  uncappedRatio: h.amountRatios('130.32', '—', '12.81') === '130,32 USDT · — / 12,81%',
  entryOrderPercentages: h.orderPercentages(['50', '50']) === '50% / 50%',
  inputUnchanged: JSON.stringify(payload) === before,
};
if (Object.values(checks).some((value) => !value)) process.exit(1);
"""
    completed = subprocess.run(("node", "-e", script), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_portfolio_launch_validates_decimal_and_profile_candidate_fields() -> None:
    js = _read("app.js")
    portfolio = js.split("function loadPortfolioScreen", 1)[1].split("function loadPortfolioSettings", 1)[0]

    assert "portfolioDecimal" in portfolio
    assert "digits.length - scale" in portfolio
    assert "Math.max(scale, 0) <= 12" in portfolio
    assert "Number.isFinite" in portfolio
    assert "Number.isSafeInteger" in portfolio
    assert "portfolioSafeInteger(profile.candidates, 1)" in portfolio
    assert "total_test_budget" not in portfolio
    assert "readiness?.search?.total_test_budget" not in portfolio
    assert "profileBudgetTotal" not in portfolio
    assert "Number(profile.candidates) <= budget" not in portfolio
    assert "addEventListener('input', updateControls)" in portfolio


def test_portfolio_duplicate_or_busy_recovers_and_keeps_campaign_frozen() -> None:
    js = _read("app.js")
    portfolio = js.split("function loadPortfolioScreen", 1)[1].split("function loadPortfolioSettings", 1)[0]

    assert "await recoverPortfolioJob(true)" in portfolio
    assert "setLocked(true)" in portfolio
    assert "PORTFOLIO_JOB_ACTIVE_DUPLICATE" in portfolio
    assert "PORTFOLIO_JOB_BUSY" in portfolio


def test_portfolio_overall_progress_does_not_claim_unknown_stage_is_known() -> None:
    js = _read("app.js")
    portfolio = js.split("function loadPortfolioScreen", 1)[1].split("function loadPortfolioSettings", 1)[0]

    assert "stageIndeterminate" in portfolio
    assert "точный прогресс неизвестен" in portfolio
    assert "overallPercent" in portfolio


def test_portfolio_settings_changed_since_freeze_is_latched_in_job_status() -> None:
    js = _read("app.js")
    portfolio = js.split("function loadPortfolioScreen", 1)[1].split("function loadPortfolioSettings", 1)[0]

    assert "settingsChanged" in portfolio
    assert "settings_changed_since_freeze" in portfolio
    assert "SETTINGS_CHANGED_SINCE_FREEZE" in portfolio


def test_portfolio_journal_renders_server_text_entries() -> None:
    js = _read("app.js")
    portfolio = js.split("function loadPortfolioScreen", 1)[1].split("function loadPortfolioSettings", 1)[0]

    assert "entry?.text || entry?.message" in portfolio


def test_portfolio_reason_codes_are_localized_without_losing_prefixes() -> None:
    js = _read("app.js")
    assert "INSUFFICIENT_DIRECTIONAL_UNIVERSE" in js
    assert "SIZE_BELOW_MINIMUM_QTY" in js
    assert "SIZE_ROUNDED_TO_ZERO" in js
    script = js.split("const ORDER_BUCKETS", 1)[0] + """
const humanize = globalThis.portfolioReasonHelpers.humanize;
const actual = humanize('AGGRESSIVE:INSUFFICIENT_DIRECTIONAL_UNIVERSE; GEUSDT:SIZE_BELOW_MINIMUM_QTY; XLKUSDT:SIZE_ROUNDED_TO_ZERO; UNKNOWN:OTHER');
if (!actual.includes('AGGRESSIVE: после фильтров не осталось ни одного допустимого состава')) process.exit(1);
if (!actual.includes('GEUSDT: после округления количество ниже минимального размера ордера Bybit')) process.exit(1);
if (!actual.includes('XLKUSDT: расчётный размер позиции меньше шага округления; уменьшите шаг или исключите пару из universe')) process.exit(1);
if (!actual.includes('UNKNOWN:OTHER')) process.exit(1);
"""
    completed = subprocess.run(("node", "-e", script), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr or completed.stdout
def test_portfolio_settings_has_editable_risk_controls_and_scoped_alignment() -> None:
    html = _read("index.html")
    css = _read("app.css")
    settings = html.split('id="portfolio-settings"', 1)[1].split("</details>", 1)[0]
    policy = settings.split('class="portfolio-profile-policy-table"', 1)[1].split("</table>", 1)[0]

    assert 'class="section-subtitle"' in settings
    assert "<strong>" in settings
    assert policy.count("<tr>") == 4
    assert policy.count('type="text"') == 9
    for profile in ("aggressive", "balanced", "conservative"):
        for field in ("max-actual-equity-dd-pct", "min-calculated-free-margin-reserve-pct", "max-calculated-account-mm-load-pct"):
            assert f'id="portfolio-settings-risk-{profile}-{field}"' in policy
    assert 'min="0" max="100" step="0.000000000001"' in policy
    assert 'class="field-grid portfolio-settings-field-grid"' in html
    assert ".portfolio-settings-field-grid { align-items: start; }" in css
    assert ".portfolio-settings-field-grid .field-group > label" in css

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
    assert ">Portfolio" in html
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
    assert 'aria-live="polite"' in html
    assert 'value="legacy"' in html
    assert 'value="static"' in html
    assert "requestJson('/api/v2/bootstrap')" in js
    assert "requestJson('/api/v2/testing/local/status')" in js
    assert "requestJson('/api/v2/testing/remote/status')" in js
    assert "requestJson('/api/v2/testing/local/fill'" in js
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
        "Сетка и уточнение",
        "Плато и Close MA",
        "READY-кандидаты",
        "Конструкция ордеров",
        "Количество параллельных процессов",
    ):
        assert label in profile
    assert 'id="analysis-profile-reload"' in profile
    assert 'id="analysis-profile-save"' in profile
    assert "'/api/v2/settings/analysis-profile'" in js
    assert "function analysisProfilePayload()" in js
    assert "Целочисленные поля профиля должны быть заполнены целыми числами." in js
    assert "if (!/^-?\\d+$/.test(raw))" in js
    assert "lower_max_exclusive_bp: Number(pair.lower_max_exclusive_bp)" in js
    assert "'envelope_min'" in js
    assert "'base_slots'" in js
    assert "shift должны быть целыми числами" in js


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
    assert "4. A/B анализ Performance" in html
    assert "5. CHECK &amp; RETEST" in html
    card = html.split("3. Test and Import to Performance DB", 1)[1].split("</details>", 1)[0]
    assert card.count('class="progress-block"') == 1
    assert card.count('role="status"') == 1
    assert "const v2CardOrder" in js
    assert "performanceV2WindowTitle) performanceV2WindowTitle.textContent = '4. A/B" in js


def test_performance_v2_retest_card_uses_server_mapping_and_committed_inbox_gate() -> None:
    html = _read("index.html")
    js = _read("app.js")
    recovery = _read("retest_recovery.js")

    card = html.split('id="performance-v2-retest-card"', 1)[1].split("</details>", 1)[0]
    assert "CHECK &amp; RETEST" in card
    assert 'id="performance-v2-retest-count"' in card
    assert 'id="performance-v2-retest-start"' in card
    assert 'id="performance-v2-retest-end"' in card
    assert 'id="performance-v2-retest-import" class="button button-primary" disabled' in card
    assert "/api/v2/strategies/performance-v2/retest/status" in js
    assert "/api/v2/strategies/performance-v2/retest/start" in js
    assert "/api/v2/strategies/performance-v2/retest/import" in js
    assert html.index('<script src="/panel-web/retest_recovery.js"></script>') < html.index('<script src="/panel-web/app.js"></script>')
    assert "selectRetestTester(jobs)" in js
    assert "job.inbox_ready === true" in js
    assert "tester_job_id: retestTesterJobId" in js
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

    assert 'id="performance-v2-finalist-retest-export" class="button button-secondary"' in card
    assert 'id="performance-v2-finalist-retest-export" class="button button-secondary" hidden' not in card
    assert "/api/v2/strategies/performance-v2/finalist-retest/export?include_reserve=" in js
    assert "finalistRetestHasSuccessfulExport" in js


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
    assert "6. Парето и фильтры" in strategies
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
    for marker in ("shortlist", "analyze", "generate"):
        assert f"tester-range-{marker}" not in js


def test_tester_card_exposes_single_mode_and_hides_fast_controls() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="tester-start"' in html
    assert "SINGLE_MODE" in html
    assert 'id="tester-start-fast"' not in html
    assert 'id="tester-retry-fast"' not in html
    assert "strategies.tester.fast.start" not in js
    assert "strategies.tester.fast.retry" not in js
    assert "kind: 'strategies.tester.start'" in js
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


def test_accordion_status_badges_align_to_the_right() -> None:
    assert ".accordion > summary > .state-badge { margin-left: auto; }" in _read("app.css")


def test_shortlist_bulk_handlers_preserve_non_selection_state() -> None:
    js = _read("app.js")

    handlers = js.split("document.querySelector('#shortlist-select-all')", 1)[1].split("const testerCard", 1)[0]
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


def test_settings_does_not_edit_listing_dates_outside_workflow_config() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="settings-dates"' not in html
    assert "listing_dates_path: document.querySelector('#settings-dates')?.value || ''" not in js


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
    assert "Date.parse(b.created_at_utc) - Date.parse(a.created_at_utc)" in recovery
    assert "String(b.job_id).localeCompare(String(a.job_id))" in recovery
    assert "const tester = testerJobs[0];" in recovery
    assert ".reverse()" not in recovery
    assert "testerJobs.find((job) => !testerIsTerminal(job))" not in recovery
    assert "job.state === 'FAILED'" not in recovery
    assert "job.state === 'CANCELLED'" not in recovery


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
    assert "5. Парето и фильтры" not in js
    assert "6. Парето и фильтры" in js
    assert "4. A/B анализ Performance" in js
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


def test_performance_v2_review_import_uses_a_folder_picker_and_bounded_endpoint() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="performance-v2-selection-review-file"' in html
    assert "multiple webkitdirectory hidden" in html
    assert "Обратный импорт XLS" in html
    assert "/api/v2/strategies/performance-v2/selection-review-import" in js
    assert ".filter((file) => file.name.toLowerCase().endsWith('.xlsx'))" in js


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
    assert "endpoint.startsWith('/api/v2/surfaces/')" in helper
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
        'id="portfolio-default-long"', 'id="portfolio-default-short"',
        'id="portfolio-profile-aggressive"', 'id="portfolio-profile-balanced"',
        'id="portfolio-profile-conservative"', 'id="portfolio-equity-aggressive"',
        'id="portfolio-max-balance-aggressive"', 'id="portfolio-candidates-aggressive"',
        'id="portfolio-run"', 'id="portfolio-new-calculation"',
    ):
        assert control in html
    assert "loadPortfolioScreen" in js
    assert "'/api/v2/portfolio/readiness'" in js
    assert "'/api/v2/portfolio/jobs/active'" in js
    assert "'/api/v2/portfolio/campaigns'" in js


def test_portfolio_pair_selection_copies_global_maxima_without_auto_selection() -> None:
    html = _read("index.html")
    js = _read("app.js")
    portfolio = js.split("function loadPortfolioScreen", 1)[1].split("function loadPortfolioSettings", 1)[0]

    assert 'id="portfolio-default-long"' in html
    assert 'id="portfolio-default-short"' in html
    assert "selected: row.selected === true" in js
    assert "copyPortfolioMaximum" in portfolio
    assert "label.append(selected, name, count)" in portfolio
    assert "#portfolio-default-long" in portfolio
    assert "#portfolio-default-short" in portfolio


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


def test_portfolio_settings_uses_human_form_for_all_profiles_and_keeps_technical_document_hidden() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="portfolio-settings-document"' not in html
    assert 'id="portfolio-settings-form"' in html
    assert 'id="portfolio-settings-total-test-budget"' not in html
    assert 'id="portfolio-settings-profile-aggressive"' in html
    assert 'id="portfolio-settings-profile-balanced"' in html
    assert 'id="portfolio-settings-profile-conservative"' in html
    for profile in ("aggressive", "balanced", "conservative"):
        for field in ("deposit", "collateral", "max-balance", "upper-bound", "individual-max-dd-pct", "individual-net-pnl-min-exclusive", "top-n"):
            assert f'id="portfolio-settings-{profile}-{field}"' in html
        assert f'id="portfolio-settings-{profile}-grid"' not in html
    for field in ("close-volume-participation-pct", "round-down-usdt", "minimum-coverage-pct", "maximum-age-hours", "archive-publication-lag-hours", "weekend-start-utc", "weekend-end-utc", "backfill-write-enabled"):
        assert f'id="portfolio-settings-{field}"' in html
    assert "Доля минутного объёма для позиции" in html
    assert "Шаг округления размера" in html
    assert "Минимальное покрытие данных стакана" in html
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


def test_portfolio_settings_patches_only_exposed_leaves_and_preserves_money_lexemes() -> None:
    js = _read("app.js")

    assert "settingsMoneyValue" in js
    assert "settingsIntegerValue" in js
    assert "settingsDecimalValue" in js
    assert "scenario[name].amount" in js
    assert "scenario.sizing.upper_bound.amount" in js
    assert "scenario.sizing.grid !== undefined" in js
    assert "payload.profiles[profile].ranking.top_n" in js
    assert "state.document = clone(result.document)" in js
    assert "document: payload" in js
    assert "const result = await requestJson('/api/v2/portfolio/settings'" in js
    assert "state.conflictMessage = '';\n        clearInvalid();\n        let payload" in js


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
        },
        "search": {"total_test_budget": 9, "sizing_mode": "liquidity_cap_single", "max_enumerated_combinations": 100000},
        "runner": {"root": "hidden", "token": {"keep": True}},
    }
    values = {
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
    }
    changed = json.loads(json.dumps(values))
    changed["round_down_usdt"] = "25"
    changed["profiles"]["AGGRESSIVE"].update({"max_balance": "31.25", "upper_bound": "40", "individual_max_dd_pct": "25.5", "individual_net_pnl_min_exclusive": "-0.5", "top_n": "3"})
    script = _read("app.js").split("const ORDER_BUCKETS", 1)[0] + f"""
const h = globalThis.portfolioSettingsHelpers;
const document = {json.dumps(document)};
const values = {json.dumps(values)};
const changed = {json.dumps(changed)};
const unchanged = h.settingsPatch(document, values);
const edited = h.settingsPatch(document, changed);
const scaleNumber = h.clone(document); scaleNumber.scenarios.AGGRESSIVE.sizing.upper_bound.amount = 100;
const scaleNumberValues = h.clone(values); scaleNumberValues.profiles.AGGRESSIVE.upper_bound = '100.0';
const scaleString = h.clone(document); scaleString.scenarios.AGGRESSIVE.sizing.upper_bound.amount = '100.00';
const scaleStringValues = h.clone(values); scaleStringValues.profiles.AGGRESSIVE.upper_bound = '100.00';
const floatDocument = h.clone(document); floatDocument.scenarios.AGGRESSIVE.deposit.amount = 1.5;
const mixedCurrency = h.clone(document); mixedCurrency.scenarios.AGGRESSIVE.collateral.currency = 'EUR';
const mustThrow = (callback) => {{ try {{ callback(); return false; }} catch (_) {{ return true; }} }};
const invalidMoney = ['0', '-1', '1e3', '1,5', ''];
const checks = {{
  unchanged: JSON.stringify(unchanged) === JSON.stringify(document),
  hiddenPreserved: edited.runner.root === 'hidden' && edited.runner.token.keep === true,
  stringDecimal: typeof edited.scenarios.AGGRESSIVE.max_balance.amount === 'string',
  stringIntegerEdit: typeof h.settingsMoneyValue('11', '10.00') === 'string',
  stringScalePreserved: h.settingsMoneyValue('10.00', '10.00') === '10.00',
  changedDdString: typeof edited.profiles.AGGRESSIVE.individual_max_dd_pct === 'string',
  changedPnlString: typeof edited.profiles.AGGRESSIVE.individual_net_pnl_min_exclusive === 'string',
  gridAbsent: edited.scenarios.AGGRESSIVE.sizing.grid === undefined,
  integerLexemes: ['1.0', '1e3', '0', '-1', ',', ''].every((value) => mustThrow(() => h.settingsIntegerValue(value))),
  moneyLexemes: invalidMoney.every((value) => mustThrow(() => h.settingsMoneyValue(value, 1))),
  unsafeIntegerRejected: mustThrow(() => h.settingsMoneyValue('9007199254740993', 1)),
  mixedScaleNumberAccepted: h.settingsPatch(scaleNumber, scaleNumberValues).scenarios.AGGRESSIVE.sizing.upper_bound.amount === '100.0',
  mixedScaleStringAccepted: h.settingsPatch(scaleString, scaleStringValues).scenarios.AGGRESSIVE.sizing.upper_bound.amount === '100.00',
  floatInboundRejected: h.validSettingsDocument(floatDocument) === false && mustThrow(() => h.settingsPatch(floatDocument, values)),
  mixedCurrencyRejected: h.validSettingsDocument(mixedCurrency) === false && mustThrow(() => h.settingsPatch(mixedCurrency, values)),
  invalidDdRejected: mustThrow(() => h.settingsPatch(document, {{...values, profiles: {{...values.profiles, AGGRESSIVE: {{...values.profiles.AGGRESSIVE, individual_max_dd_pct: '-1'}}}}}})),
  invalidPnlRejected: mustThrow(() => h.settingsPatch(document, {{...values, profiles: {{...values.profiles, AGGRESSIVE: {{...values.profiles.AGGRESSIVE, individual_net_pnl_min_exclusive: '1e3'}}}}}})),
  invalidParticipationRejected: mustThrow(() => h.settingsPatch(document, {{...values, close_volume_participation_pct: '201'}})),
  invalidRoundingRejected: mustThrow(() => h.settingsPatch(document, {{...values, round_down_usdt: '0'}})),
  invalidCoverageRejected: mustThrow(() => h.settingsPatch(document, {{...values, minimum_coverage_pct: '101'}})),
  invalidAgeRejected: mustThrow(() => h.settingsPatch(document, {{...values, maximum_age_hours: '0'}})),
  zeroLagAccepted: h.settingsPatch(document, {{...values, archive_publication_lag_hours: '0'}}).liquidity.archive_publication_lag_hours === 0,
  equalWeekendRejected: mustThrow(() => h.settingsPatch(document, {{...values, weekend_end_utc: 'SATURDAY 00:00'}})),
}};
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
    assert "indeterminate" in portfolio
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

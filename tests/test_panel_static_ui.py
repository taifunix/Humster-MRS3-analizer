from pathlib import Path
import re


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

    assert 'id="performance-inbox-verify"' in html
    assert 'id="performance-import-start"' in html
    assert 'id="performance-import-start" class="button button-primary" disabled' in html
    assert "let inboxReadyV2 = false;" in js
    assert "importStartV2.disabled = !inboxReadyV2" in js
    assert "Cleanup warning" in js


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
    assert 'id="surface-target-save"' in html
    assert 'name="surface_target_path"' in html
    assert "surface_target_path" in js


def test_active_surface_publish_handler_uses_confirmed_snapshot_and_committed_file_path() -> None:
    js = _read("app.js")

    active = js.split("document.querySelector('#surface-publish-start')?.addEventListener", 1)[1]
    assert "'/api/v2/surfaces/publish/start'" in active
    assert "'/api/v2/surfaces/publish'" not in active
    assert "const selectionSnapshot" in active
    assert "const outputDir" in active
    assert "`${outputDir}\\\\${result.target}`" in active


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
    assert "inboxReadyV2" in js


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
    assert "inboxReadyV2 = ready;" in js
    assert "importStartV2.disabled = !inboxReadyV2;" in js
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
    assert "importStartV2.disabled = !inboxReadyV2" in js


def test_reload_recovers_only_server_job_snapshots() -> None:
    js = _read("app.js")

    assert "const recoverJobs = async" in js
    recovery = js.split("const recoverJobs = async", 1)[1].split("const settingsStatus", 1)[0]
    assert "requestJson('/api/v2/jobs')" in recovery
    assert "job.kind === 'strategies.tester.start'" in recovery
    assert "job.kind === 'strategies.performance-dd5'" not in recovery
    assert "const tester = testerJobs.find" in recovery
    assert "job.state === 'COMMITTED' && job.inbox_ready === true" in recovery
    assert "kind: 'strategies.tester.start'" in js
    assert "renderTester(job);" in recovery
    assert "renderPerformance(job)" not in recovery
    assert "job.state = " not in recovery
    assert "recoverJobs();" in js


def test_inbox_verify_does_not_fail_silently() -> None:
    js = _read("app.js")
    handler = js.split("inboxVerifyV2?.addEventListener", 1)[1].split("const refreshPerformanceCatalog", 1)[0]

    assert "if (!testerJobId) {" in handler
    assert "tester job" in handler
    assert "verified inbox" in handler
    assert "catch (error)" in handler
    assert "error?.message || 'unknown error'" in handler


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

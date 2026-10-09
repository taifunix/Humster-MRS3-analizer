# Humster MRS3 Analyzer — PRD v0.7

## Продукт

Humster MRS3 Analyzer — локальный детерминированный pipeline для PerformanceDB,
импорта и замены результатов тестера, расчёта PnL/DD/equity-фактов,
последовательного применения фильтров Performance v2, подготовки shortlist и
READY JSON, XLSX-выгрузок и передачи кандидатов в tester. Каждая операция
сохраняет происхождение и проверяемые причины решений.

**Текущий статус (2026-10-08):** основные локальные контуры Performance v2,
equity regime, finalist RETEST, XLSX-контракта, Minimum Shift, ручного
обслуживания PerformanceDB и расчёта базового лота реализованы и прошли
независимую проверку. Открытые live-проверки, миграции и tester smoke указаны в
`progress.md` и не считаются выполненными по одному локальному evidence.
Диагностические/source-метрики не являются доказанным результатом готовой
MRS3-стратегии без реального tick-test и DD5 retest.

## Навигация для агента

Сначала всегда читать `AGENTS.md`, этот раздел и `progress.md`. Затем читать
только строку, соответствующую текущей задаче; не загружать весь реестр,
исторические приложения или соседние модули. Для выбранной строки читать
спецификацию, затем только явно указанные в ней ADR/план/evidence.

| Если задача касается | Обязательный контекст | Не читать без прямой ссылки |
| --- | --- | --- |
| PerformanceDB import/replace, RETEST, dedup | [v2 CHECK & RETEST](docs/specs/2026-09-03-performance-v2-retest-workflow.md), [typed-config dedup](docs/specs/2026-09-04-performance-v2-config-dedup.md), [SINGLE_MODE collection](docs/specs/2026-09-28-single-mode-report-collection.md) | legacy import/DD5 specs |
| Performance v2 filters, selection, ranking | [fixed filter sequence](docs/specs/2026-10-01-performance-v2-filter-sequence.md), [researched filters](docs/specs/2026-10-02-performance-v2-researched-filters.md), [robust ranking](docs/specs/2026-09-01-performance-v2-robust-finalist-ranking.md) | predecessor Panel Phase 2 and old Pareto contracts |
| Equity regime or equity-filter facts | [equity status map](docs/specs/2026-10-03-equity-regime-status-map.md), [production plan](docs/superpowers/plans/2026-10-04-equity-regime-production.md), [M3 research](docs/reports/2026-10-04-equity-regime-m3-research.md) | old equity-quality drafts unless linked by the status map |
| Finalist RETEST and «Применить эквити фильтр» | [finalist RETEST control](docs/specs/2026-09-09-performance-v2-finalist-retest-control.md) and its linked ADR/plan | general tester recovery notes |
| Shortlist, READY JSON, order structures, Minimum Shift | [Shortlist filters v2](docs/specs/2026-09-27-shortlist-filters-v2.md), [Minimum Shift](docs/specs/2026-10-08-fresh-shortlist-minimum-shift.md), linked ADRs/plans | historical event-filter and v0.6 generation specs |
| XLSX export or column order | [XLSX column contract](docs/specs/2026-10-07-performance-v2-xlsx-column-contract.md), [PerformanceDB export](docs/specs/2026-09-24-performance-db-xlsx-export.md) | old DD5/XLSX contracts |
| PerformanceDB maintenance or physical cleanup | [maintenance contract](docs/specs/2026-10-06-performance-db-maintenance.md), linked ADR/plan | old migration/cleanup rules |
| Bybit base lot or market-data collector | [base-lot export](docs/specs/2026-10-07-bybit-base-lot-export.md); read the [collector spec](docs/specs/2026-09-05-bybit-market-data-collector.md) only when collector behavior is changed | historical collector appendices |
| Canonical source/analysis materialization | [Canonical Phase 1](docs/specs/2026-08-16-mrs3-v07-canonical-phase1.md), [implementation plan](docs/superpowers/plans/2026-08-16-mrs3-v07-canonical-phase1.md) | v4 import, legacy selection and old readiness contracts |
| Portfolio Optimizer | [phased optimizer spec](docs/specs/2026-09-05-portfolio-optimizer.md), [Panel UI contract](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md), [liquidity model](docs/specs/2026-09-27-liquidity-lot-model.md), [Stage 2 decision](docs/decisions/0059-portfolio-stage2-sequential-batch.md) | Portfolio Analyzer v0.4 and unadopted M6–M8 evidence unless the task explicitly needs provenance |

`progress.md` is the status gate: it tells whether a linked item is live,
fixture-only, pending reload, or still blocked. `docs/decisions/` records
accepted invariants, `docs/superpowers/plans/` the implementation sequence,
and `docs/reports/` measured evidence. Sections after `## Исторические
приложения` retain provenance only and never add current instructions.

## Пользовательский результат

Для выбранной пары/стороны и сравнимого периода пользователь получает:

1. факты PerformanceDB с происхождением и причиной каждого решения фильтра;
2. воспроизводимые shortlist/READY структуры и JSON, валидные для тестера;
3. XLSX с единым составом колонок, equity-метриками и статусами;
4. результаты реального tick-test, DD5-нормализацию и individual ranking;
5. отдельные live/retest evidence только после фактического запуска и проверки.

## Текущий этап: v0.7 — PerformanceDB/Performance v2 hardening; Canonical Phase 1 Task 12C pending

Source v6 fresh compact multi-scope is complete. The active MRS3 delivery track
is [Canonical Phase 1 Task 12C](docs/specs/2026-08-16-mrs3-v07-canonical-phase1.md)
with its [implementation plan](docs/superpowers/plans/2026-08-16-mrs3-v07-canonical-phase1.md).
The Performance v2 fixed filter sequence is implemented and independently
reviewed: Equity, Lot variant, hard cutoffs, A/B deterioration and top-five
concentration. The contract and evidence are listed in the registry below.
Researched pair-side PnL and structural stages 6–9 are implemented in the Panel under
[their active contract](docs/specs/2026-10-02-performance-v2-researched-filters.md).
The Panel export uses its existing sheets, reason and analog columns.

PerformanceDB storage reduction is the current user-prioritized maintenance
work: [contract](docs/specs/2026-09-30-performance-db-lossless-compaction.md),
[measured investigation](docs/reports/2026-09-30-performance-db-storage.md).
Lossless prepared compression and fresh-file compaction have independent
plan approval and are being implemented; the actual database remains unchanged. This does not alter financial
facts, strategy admission or portfolio simulation scope.

Source DuckDB и Analysis DuckDB образуют реализованный слой хранения фактов,
immutable surfaces, analysis runs и lineage. Его текущий operational contract
определяет [Canonical Phase 1 specification](docs/specs/2026-08-16-mrs3-v07-canonical-phase1.md).
Открытым остаётся только Task 12C — fresh real-source smoke/performance — по
[активному плану](docs/superpowers/plans/2026-08-16-mrs3-v07-canonical-phase1.md).

Новая [Canonical Phase 1 specification](docs/specs/2026-08-16-mrs3-v07-canonical-phase1.md)
утверждена как активный контракт, а [ADR-0009](docs/decisions/0009-canonical-phase1-surface-selection-contract.md)
принят после независимого governance review. Они определяют свежие canonical
surfaces и MRS3 selection:

- exact canonical Shift grid `30..550`;
- один общий UTC-интервал и шесть readiness witnesses CloseMA `2..7`;
- exact preview/audit/preflight replay;
- bounded materialization using the shared `duckdb_import.workers` setting;
- frozen CMARepresentative / CloseMA continuity / BASE facts;
- 2/3/4ORD только из frozen representatives;
- независимый exact-scope 1ORD;
- hard rejection старых/non-canonical surfaces из нового operational flow.

Governance Task 0, canonical-config Task 1 и six-CloseMA readiness Task 2
завершены; Tasks 3–12B закрыты в implementation plan. Открыт только Task
12C. Task 2 проверен focused `79 passed`, `git diff --check` и независимым
Luna `PASS`.
Принятые ADR-0007 и ADR-0008 не переписываются; их конфликтующие части
superseded ADR-0009 только для новых canonical surfaces. Старый
[Common Close-MA Readiness plan](docs/superpowers/plans/2026-08-15-common-close-ma-readiness.md)
остаётся frozen/non-executable.

Source DuckDB остаётся единым пополняемым lossless-хранилищем HTML-отчётов, а
Analysis DuckDB — append-only хранилищем immutable materialized surfaces,
analysis runs и lineage согласно уже реализованной
[спецификации DuckDB analysis storage and importer](docs/specs/2026-08-11-v07-duckdb-analysis-storage-and-importer.md).

## Границы и safety rules

- Не выдавать диагностические/source-метрики за реализованный результат MRS3
  без реального tick-test и DD5 retest.
- Разделять read-only экспорт/расчёт фактов и операции, изменяющие базу;
  mutation выполняется только по активному контракту конкретной функции.
- Использовать общий лимит `duckdb_import.workers`; отдельные фиксированные
  значения потоков в PRD и локальных инструкциях не задавать.
- Не объявлять individual ranking результатом портфельной симуляции; для
  портфельных выводов нужны отдельные временные ряды equity/drawdown и
  ограничения, описанные в контракте оптимизатора.

## Не входит в текущий scope

- live trading, торговый admission и публикация рекомендаций без закрытых
  соответствующих gates;
- ML, regression score или per-pair/per-TF production thresholds;
- GitHub push, PR или публикация результатов без отдельного разрешения.

## Реестр документации и статусов

| Статус | Документ | Назначение | Зависимости |
| --- | --- | --- | --- |
| Closeout / merge pending | [Heavy database optimization](docs/specs/2026-09-28-heavy-database-optimization.md), [closeout plan](docs/superpowers/plans/2026-09-28-heavy-database-optimization.md), [deferred follow-ups](docs/superpowers/plans/2026-10-01-deferred-heavy-db-follow-ups.md) | проверенные мелкие хвосты PerformanceDB; Source/materialization и дорогие profile-gated задачи отложены в новую ветку | актуальные контракты каждого модуля; Performance import boundary на `8f59c2c` |
| Implemented / independently reviewed | [Performance v2 optional commission evidence](docs/specs/2026-09-30-performance-v2-optional-commission-evidence.md), [ADR-0049](docs/decisions/0049-performance-v2-optional-commission-evidence.md), [verification](progress.md) | `SINGLE_MODE`/collections can import verified HTML without tester fee settings; unknown `commission_rate` is SQL `NULL` in schema v7 | legacy FAST/RUNS and v1 contracts, HTML fee/PnL facts, v5/v6 read compatibility |
| Accepted | [Repository foundation](docs/specs/2026-08-10-mrs3-v07-repository-foundation.md) | структура репозитория и workflow | — |
| Historical / superseded | [Safe runner smoke-test](docs/specs/2026-08-10-v06-runner-safe-root-json-smoke.md) | прежняя проверка панели и одного прогона; не является текущим workflow | текущие Panel/tester contracts |
| Historical / superseded | [v0.7 legacy selection](docs/specs/2026-08-10-v07-legacy-selection.md) | прежний import → materializer → selector контур | текущие Performance v2 и Canonical Phase 1 contracts |
| Historical / superseded | [v0.7 event source packs](docs/specs/2026-08-10-v07-event-source-packs.md) | прежний CSV/DuckDB package и event-mode контракт | текущие source/PerformanceDB contracts |
| Implemented / provenance | [v0.7 DuckDB analysis storage and importer](docs/specs/2026-08-11-v07-duckdb-analysis-storage-and-importer.md) | реализованный слой source/analysis DuckDB и lineage | Canonical Phase 1 |
| Historical / provenance | [Trusted v4 migration performance](docs/specs/2026-08-11-v07-trusted-v4-migration-performance.md) | историческое evidence миграции v4→v5 | DuckDB analysis storage |
| Deferred / historical | [v0.7 CSV-DuckDB overlay](docs/specs/2026-08-11-v07-optional-csv-duckdb-overlay.md) | необязательное старое объединение CSV и DuckDB | только отдельная согласованная задача |
| Historical / accepted provenance | [ADR-0002](docs/decisions/0002-source-summary-and-window-metrics-verification.md) | раздельная full-horizon/windowed verification для прежних source packages | historical event-source contract |
| Superseded / historical | [Event filter and shortlist](docs/specs/v07-event-filter-and-shortlist.md) | прежние правила event-filter и shortlist | текущие Shortlist filters v2 и Canonical Phase 1 |
| Implemented / verified on production sample | [Pair screener](docs/specs/2026-09-18-pair-screener.md), [implementation plan](docs/superpowers/plans/2026-09-18-pair-screener-implementation.md) | дешёвый 304-точечный прогон на пару для отсева пар до полного 5472-точечного сбора; CSV-only оценка (6.1a закрыт); LONG+SHORT; реестр ликвидности (`input/bybit_tradfi_liquidity.xlsx`, лист «Скрининг»); экран SCREENER 01 (карточка, оценка, экспорт, передача в RUNNER 01) | RUNNER 01/`LocalTestingService`; полная кросс-валидация раздела 9 пропущена по решению пользователя, пороги не подтверждены на новых парах; вердикты по исходным 16 парам перепроверены 2026-09-19 через реальный `evaluate_pairs` на реальных отчётах — совпали побитово с первым прогоном (GO 3 / CHECK 8 / STOP 5) |
| Superseded / historical | [Source-potential calibration](docs/specs/v07-posttest-calibration-source-potential.md) | legacy posttest calibration retained for provenance | Performance DB v2 RETEST |
| Implemented locally / fixture-gated | [Portfolio Optimizer phased spec](docs/specs/2026-09-05-portfolio-optimizer.md), [Panel UI contract](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md), [unified liquidity](docs/specs/2026-09-27-liquidity-lot-model.md), [Stage 2 decision](docs/decisions/0059-portfolio-stage2-sequential-batch.md) | optimizer UI, prepared inputs and ordered Stage 2 batch are implemented/reviewed; fixture evidence does not authorize real tester, trading or live PerformanceDB writes | open PnL/liquidity/freshness/ranking/limiter gates and fresh user authorization; current status in `progress.md` |
| Implemented / accepted | [Performance v2 optimizer prepared inputs](docs/specs/2026-09-17-performance-v2-optimizer-prepared-inputs.md), [ADR-0037](docs/decisions/0037-performance-v2-optimizer-prepared-inputs.md), [ADR-0038](docs/decisions/0038-performance-v2-prepared-canonicalization-and-locking.md) | typed Price/Cost and sizing facts used by optimizer inputs | no live execution or trading permission |
| Accepted | [ADR-0026](docs/decisions/0026-bybit-orderbook-data-health.md), [ADR-0027](docs/decisions/0027-bybit-runtime-mode-markers.md), [ADR-0028](docs/decisions/0028-bybit-side-depth-completeness.md) | Bybit collector data-health and runtime-marker decisions | Bybit collector specification |
| Predecessor / Queued | [Portfolio Analyzer v0.4](docs/specs/2026-08-09-portfolio-analyzer-v04.md) | предшествующий контракт до принятия нового optimizer design | historical provenance; no new runtime activation |
| Superseded / historical | [Strategy performance DuckDB governing spec](docs/specs/2026-08-14-strategy-performance-duckdb.md) | former transactional import and DD5 contract; retained only for provenance | [ADR-0004](docs/decisions/0004-strategy-performance-evidence-store.md) |
| Superseded / predecessor | [Panel Phase 2 structural filters](docs/specs/2026-08-25-panel-phase2-structural-filters.md) | predecessor design for fresh-analysis shortlist filtering; current behavior is governed by Shortlist filters v2 and its linked ADR | [Shortlist filters v2](docs/specs/2026-09-27-shortlist-filters-v2.md), Panel Web |
| Implemented / independently reviewed | [Shortlist filters v2](docs/specs/2026-09-27-shortlist-filters-v2.md), [plan](docs/superpowers/plans/2026-09-27-shortlist-filters-v2.md), [ADR-0047](docs/decisions/0047-fresh-shortlist-applied-selection-v2.md) | preserve A/B, add Open MA proximity and one joint per-order Pareto, explicit apply, consistent audit/JSON selection and plateau totals | D3 PLAN_APPROVED; merged-tree suite 5363 passed, 8 skipped; Opus 5 CODE_REVIEW_PASS; browser/live smoke not run |
| Implemented / independently reviewed | [Fresh shortlist Minimum Shift](docs/specs/2026-10-08-fresh-shortlist-minimum-shift.md), [ADR-0060](docs/decisions/0060-fresh-shortlist-minimum-shift.md), [plan](docs/superpowers/plans/2026-10-08-fresh-shortlist-minimum-shift.md) | optional first-order-only threshold for 1ORD/2ORD/3ORD, exact percent-to-bp gate, canonical shortlist/audit/READY/RUNS provenance, legacy engine-1 compatibility | focused fresh shortlist/generation/export/tester/UI suites pass; Opus 5 CODE_REVIEW_PASS; no live tester or database run; Panel restart remains pending |
| Implemented / verified | [SINGLE_MODE report collection](docs/specs/2026-09-28-single-mode-report-collection.md), [implementation plan](docs/superpowers/plans/2026-09-28-single-mode-report-collection.md), [ADR-0063](docs/decisions/0063-panel-job-admission-on-journal-persistence.md) | opt-in server-owned collection controls in the ordinary tester card, exact collection verify/import handoff, non-destructive clear, durable job admission failure handling | native SINGLE_MODE tester, Performance v2 metadata inbox; RETEST unchanged |
| Active | [Panel Fresh Analysis Settings](docs/specs/2026-09-10-panel-fresh-analysis-settings.md) | explicit listing-date input and safe actionable fresh-analysis configuration errors | Panel Web, local configuration |
| Implemented / verified | [Panel Analysis Profile](docs/superpowers/specs/2026-09-10-panel-analysis-profile-design.md) | local typed editor for values that affect future fresh Source v6 analysis; atomic save and shared-worker notice | Panel Web, `config.local.json` |
| Implemented / verified | [Local tester preparation](docs/specs/2026-09-10-panel-local-tester-preparation.md) | opt-in stale-report cleanup, file preparation and Files-tab local tester start; per-strategy Table wizard remains excluded | Panel Web, local tester runner |
| Implemented / verified | [Tester run files](docs/specs/2026-08-25-tester-run-files.md) | five isolated tester snapshots from filtered READY candidates; manual `run_tester.bat` execution | Panel Web, local tester runner |
| Retired / superseded | [Panel Fast Strategy Test](docs/specs/2026-08-27-panel-fast-strategy-test.md) | historical bounded strategy-batch design; active panel dispatch/API/retry contour removed, with shared runner machinery retained for native `SINGLE_MODE` | READY generation manifest, local tester primitives |
| Implemented / verified | [Multi-order plateau admission](docs/specs/2026-08-25-multi-order-plateau-admission.md) | pre-combination 2ORD--4ORD structural width and independent-event admission | Canonical Phase 1, Shortlist filters v2 |
| Superseded / historical | [Performance report import to DuckDB](docs/specs/2026-08-14-performance-report-import-duckdb.md) | former HTML import and cleanup contract; retained only for provenance | Strategy performance DuckDB, ADR-0004--0006 |
| Superseded / historical | [DD5 calculation and finalist selection](docs/specs/2026-08-14-dd5-finalist-selection.md) | former DD5/Pareto/XLSX contract; retained only for provenance | Performance report import to DuckDB |
| Implemented / independently reviewed | [Performance v2 robust finalist ranking](docs/specs/2026-09-01-performance-v2-robust-finalist-ranking.md) | best-trade and temporal robustness filters, Shift-aware near-tie preference and deterministic Top-50 | Performance v2 finalist selection and XLSX |
| Implemented / independently reviewed | [Performance v2 fixed filter sequence](docs/specs/2026-10-01-performance-v2-filter-sequence.md), [plan](docs/superpowers/plans/2026-10-01-performance-v2-filter-implementation.md), [ADR-0052](docs/decisions/0052-performance-v2-hard-cutoff-rejected.md), [ADR-0053](docs/decisions/0053-performance-v2-dd-profit-guard.md), [ADR-0058](docs/decisions/0058-performance-v2-filter-rejected-status.md) | fixed Equity, Lot, hard-cutoff, A/B, top-five, **Performance v2 fixed-filter Minimum Shift** and PnL/structural prefix; the fixed-filter Minimum Shift is enabled by default at 0.3% and runs immediately before PnL DD5/30 + PnL B/30; guarded full-DD cutoff uses full PnL/30d; time windows remain diagnostics; published exclusions at Lot, hard-cutoff and A/B gain source-specific `User Status=REJECTED`; `RESERVE` rows count as excluded from later stages; optional Finalist/Reserved-only cohort filters preview, cache readiness, stage processing and XLSX by newest imported `User Status`, with relative stage math scoped to that cohort | original focused selection/UI suite 401 passed and broader consumers 341 passed with 4 platform symlink skips; mode extension focused tests 7 passed; full six-module suite 798 passed, 5 skipped, the same 3 failures reproduced on clean HEAD; independent Opus `CODE_REVIEW_PASS`; no live database, tester or browser run |
| Implemented / independently reviewed; live v9 migration pending | [Performance v2 selection review import](docs/specs/2026-09-02-performance-v2-selection-review-import.md), [ADR-0062](docs/decisions/0062-performance-v2-selection-review-clear-fields.md) | weighted Top-20, immutable selection snapshots, strict full-workbook review, and card-6 partial XLSX import where blank User Status/Rank cells clear saved values; schema v10 | Final focused group: 477 passed, 4 skipped across selection review, store/migration, compact, maintenance and Panel v2; clean-HEAD control reproduced 15 panel/legacy-benchmark failures and 18 passes; transactional v9-to-v10 migration covered; CODE_REVIEW_PASS; no live database access or Panel restart |
| R7.3 approved; M0–M4 accepted; M5 partial evidence | [Performance v2 equity quality](docs/specs/2026-09-25-performance-v2-equity-quality.md), [XLSX column contract](docs/specs/2026-10-07-performance-v2-xlsx-column-contract.md), [implementation plan](docs/superpowers/plans/2026-09-25-performance-v2-equity-quality.md), [M0 evidence](docs/superpowers/plans/2026-09-25-performance-v2-equity-quality-evidence.md), [M5 slice evidence](docs/superpowers/plans/2026-09-26-performance-v2-equity-quality-m5-slice-evidence.md), [ADR-0044](docs/decisions/0044-performance-v2-equity-quality-facts.md) | independent filter #2 and optional equity Top N; right-continuous equity-only 7/14/28d grid, quiet periods carried flat, short corrections demote only selected equity ranking while H grows (robust unchanged), explicit block rules prevent stale R7.3 cache from changing new/regime XLSX headers | 190/14,463 bounded M0 sample; M1–M4 independent `CODE_REVIEW_PASS`; M5 current-runtime warm preview measured on 512 strategies and cold/backfill worker profiles on 64, with three measured repeats; prior-runtime timing gates withdrawn by user; full-corpus and one-REPLACE timing plus user speed acceptance remain open; no predictive claims |
| Implemented and independently reviewed; live migration pending | [Equity regime status map](docs/specs/2026-10-03-equity-regime-status-map.md), [production plan](docs/superpowers/plans/2026-10-04-equity-regime-production.md), [M3 research](docs/reports/2026-10-04-equity-regime-m3-research.md), [ADR-0055](docs/decisions/0055-equity-filter-rejected-and-manual-fact-cleanup.md), [ADR-0056](docs/decisions/0056-equity-rejection-source-lifecycle.md) | geometry-based GROWING/WEAKENING/RESUMED/STALLED; hard equity failures publish existing effective `User Status=REJECTED`; card-9 manual maintenance physically deletes detail rows without cleanup markers or deletion timestamps under ADR-0057 | local classifier, schema v10, cache, selection, Panel and Excel integration; 627 tests passed, 4 skipped; frozen 30,940-result replay deterministic; independent Opus `CODE_REVIEW_PASS`; live v9-to-v10 migration remains pending; no live database mutation |
| Implemented / root verified | [Performance v2 global finalist retest control](docs/specs/2026-09-09-performance-v2-finalist-retest-control.md) | server-frozen FINALIST/optional RESERVE retest, exact successful-cohort ranking, one combined atomic control XLSX, and explicit asynchronous `Применить эквити фильтр` for the frozen imported cohort with typed progress/errors | existing SINGLE_MODE, REPLACE, selection review, [ADR-0034](docs/decisions/0034-performance-v2-global-finalist-retest-control.md) |
| Implemented / independently reviewed | [Bybit base-lot XLSX export](docs/specs/2026-10-07-bybit-base-lot-export.md) | one-command reuse/backfill of the optimizer's seven-day minute-liquidity window; writes `K*V25*A15` to `Actual` column C and current date/errors to column D | standalone script; no Panel, database, optimizer, tester, or live execution |
| Active implementation contract | [PerformanceDB XLSX export](docs/specs/2026-09-24-performance-db-xlsx-export.md), [ADR-0043](docs/decisions/0043-performance-db-read-only-xlsx-export.md) | read-only XLSX of current ACTIVE FINALIST/RESERVE/RETEST categories or all ACTIVE, and sequential cards 4–8 | Performance DB v2; no tester, import, recalculation, database write, or client identity filter |
| Implemented; independent Opus review passed | [PerformanceDB manual maintenance](docs/specs/2026-10-06-performance-db-maintenance.md), [implementation plan](docs/superpowers/plans/2026-10-06-performance-db-maintenance.md), [ADR-0057](docs/decisions/0057-performance-db-manual-maintenance.md), [ADR-0061](docs/decisions/0061-performance-db-rejected-retirement.md) | card 9: `Удалить Rejected` physically removes per-strategy facts, caches and selection rows, compacts the retained result to interval/provenance identity, retains only typed strategy identity/orders plus the tombstone for dedup, and marks the row `DISCARDED`; full deletion removes the retained tombstone and clears global import journals; transient progress and actionable errors; current schema v10 | maintenance behavior unchanged; v9-to-v10 migration and current-schema fixtures passed; no live database touched |
| Active implementation contract | [Tester Report Library and Fast Identity](docs/specs/2026-08-14-tester-report-library-and-fast-identity.md) | verified report library, fast embedded identity and deferred workflow/CLI integration | [Name-only runner contract](docs/specs/2026-08-14-tester-name-only-verification.md) |
| Active — Task 12C fresh real-source smoke/performance pending | [MRS3 v0.7 Canonical Phase 1](docs/specs/2026-08-16-mrs3-v07-canonical-phase1.md), [implementation plan](docs/superpowers/plans/2026-08-16-mrs3-v07-canonical-phase1.md) | fresh canonical `30..550` surfaces, six CloseMA readiness, exact audit/preflight replay, parallel materialization, frozen CMA/BASE and independent 1ORD | [ADR-0009](docs/decisions/0009-canonical-phase1-surface-selection-contract.md), DuckDB analysis storage |
| Accepted | [ADR-0009](docs/decisions/0009-canonical-phase1-surface-selection-contract.md) | current readiness semantics for fresh Phase 1 surfaces | Canonical Phase 1 spec |
| Historical / superseded | [DuckDB surface coverage review](docs/specs/2026-08-14-duckdb-surface-coverage-review.md), [ADR-0007](docs/decisions/0007-observed-sparse-surface-contract.md), [ADR-0008](docs/decisions/0008-common-close-ma-readiness-and-degenerate-row-isolation.md) | former one-MA-pair/readiness contracts kept for provenance | Canonical Phase 1 |

Полная навигация: [docs/README.md](docs/README.md). Оперативная точка: [progress.md](progress.md).

## Исторические приложения

Разделы ниже сохранены для происхождения решений и старых acceptance records.
Они не являются текущими требованиями. Для новой работы использовать реестр
выше, `progress.md` и ссылку на активный контракт конкретной функции.

## Bybit public market-data collector v1 (historical record, 2026-09-05)

The approved [Revision 2 specification](docs/specs/2026-09-05-bybit-market-data-collector.md)
records the implementation contract for a public, no-key Bybit linear market-data
collector. The implementation was delivered on `main`: strict UTF-8 TOML
configuration, five-second UTC scheduling and `liquidity_1m` aggregation, SQLite
WAL spool, hourly immutable Parquet, paginated reference data/raw JSON.gz, one
public linear WebSocket connection, symbol events, atomic health, CLI commands,
and Windows task scripts. Only `storage.root`, `symbols.items`, and
`logging.level` are accepted; revisions hash exact file bytes; invalid reload
candidates are rejected without changing the last accepted state; and a storage
root change is restart-only while valid symbol/log changes apply atomically.

Phase 4 evidence covers the WAL/NORMAL SQLite spool, canonical minute JSON,
first-winner duplicate/conflict handling, bounded BUSY/LOCKED retries, durable
hour markers, marker-only reader files, restart recovery, and reuse of the
existing `OutputDirectoryLock`. WebSocket frames, books, and five-second samples
remain RAM-only. No manifests, quarantine, or archive state machine is
delivered; those remain deferred.

### Bybit collector implementation status (2026-09-05)

Focused evidence is 255 passing tests plus module compilation and whitespace
checks. Public REST/WebSocket smoke and restart recovery passed for
BTCUSDT/ETHUSDT; a five-real-minute `--test-export-minutes 5` run produced 144
SQLite minute rows and one valid hourly Parquet marker with 62 rows. Long soak
and Windows boot evidence remain pending; source metrics are not presented as
MRS3 strategy results. The test flag only accelerates the process clock and is
not production evidence. The bugfix smoke then produced synchronized books for
both symbols, `coverage_recent=1.0`, `valid_sample_count_recent=24`, health `OK`,
and a valid `part-18.parquet` archive. A separate two-minute real-clock sanity
run produced one complete minute per symbol with `sample_count=12`,
`valid_sample_count=12`, `coverage_ratio=1.0`, and health markers
`runtime_mode=production`, `accelerated_clock=false`.
The liquidity schema now publishes side-specific bid/ask completeness ratios in
schema version 2 while retaining the combined fields.
## Canonical Phase 1 status addendum (2026-08-17)

Tasks 0–4 are complete and independently reviewed. Task 4 includes bounded
bulk payload reads, 15-process CPU materialization, deterministic workers=1/15
equivalence, cancellation-safe scheduling, frozen-manifest validation, and
side-aware progress telemetry. The next implementation task is Task 5.

## Proposed next architecture: Source v6 stitched surfaces (2026-08-18)

The next product stage is a fresh Source DuckDB v6 rebuilt from HTML without a
v5 migration. It normalises exact point/time facts, stitches compatible
Fixed-lot report periods with a minimum 96-hour overlap plus bridge-cycle
coverage, recalculates PnL/DD/trade metrics, and publishes each selected
surface as a separate self-describing DuckDB file.

| Status | Document | Purpose |
| --- | --- | --- |
| Accepted | [ADR-0010](docs/decisions/0010-source-v6-stitched-facts-and-surface-files.md) | fresh v6, stitch/metric/storage decision and supersession boundary |
| Accepted | [Source v6 specification](docs/specs/2026-08-18-source-v6-stitched-surfaces.md) | normative inputs, identities, overlap, metrics, coverage, surfaces, analysis and panel contract |
| Complete | [Source v6 implementation plan](docs/superpowers/plans/2026-08-18-source-v6-stitched-surfaces.md) | Ponytail-bounded TDD delivery sequence; full `.venv` suite and independent Terra review recorded in `progress.md` |
| In progress (acceptance) | [Source v6 analysis handoff](docs/specs/2026-08-19-source-v6-analysis-handoff.md) and [plan](docs/superpowers/plans/2026-08-19-source-v6-analysis-handoff.md) | Windows v6 surface -> analysis -> READY JSON -> tester/DD5 lineage; implementation Tasks 1–6 have evidence, final acceptance/review and real end-to-end fixture remain |

Pair deletion/compaction, v5 migration, exchange-mixed databases, missing-test
strategy generation, exact tick MAE/MFE, margin and portfolio simulation remain
outside this stage.

## Source v6 fresh compact multi-scope (2026-08-20)

The next product stage is the fresh-only `source-v6-fresh-compact-v1` format:
raw HTML is re-imported into compact indexed fragments, with deterministic
uncompressed fragment identity, audit/quarantine/stitch dispositions and
`source_content_digest`. It does not migrate or dual-read v3/v4/v5 artifacts,
and it does not store one row per sample, action, cycle or event.

| Status | Document | Purpose |
| --- | --- | --- |
| Implemented / independently reviewed - Stage 1 | [ADR-0012](docs/decisions/0012-source-v6-fresh-compact-v1.md), [ADR-0014](docs/decisions/0014-source-v6-compact-publication.md), [Stage 1 evidence](.codex/stage1-acceptance-ledger.md) | fresh-only compact Source/import/merge boundary, lineage, no duplicate fact payloads, compact publication and verified real-corpus/recovery evidence; root gate accepted |
| Implemented / independently reviewed | [ADR-0013](docs/decisions/0013-source-v6-incomplete-seam-cycle-exclusion.md) | old-owned compatible >=96h overlap, retained boundary cycles, period-local PnL/DD/PF; does not edit ADR-0011 |
| Complete | [Fresh compact multi-scope plan](docs/superpowers/plans/2026-08-20-source-v6-fresh-compact-multiscope.md) | gated Stage 2 multi-scope materialization, immutable surface publication, separate parallel analysis and panel flow; full tests and independent review recorded in `progress.md` |
| Implemented / independently reviewed | [Import publication throughput](docs/specs/2026-08-21-source-v6-publication-throughput.md) | linear reduce and publication, metadata-only tail, identity from stored bytes, set-based metadata writes, post-commit merge readback, parallel merge verification; 886 s to 299 s on the 5,859-report Debian corpus with an unchanged published digest, and the two-corpus merge from failing to complete at all to 543.7 s with an identical published artifact; `CODE_REVIEW_PASS` after seven rounds, and again for C9 |
| Implemented / acceptance-gated — Stages 3–4 | [Source v6 metric contract](docs/specs/2026-08-23-source-v6-metric-contract.md), [facts/metrics v2 ADR](docs/decisions/0017-source-v6-facts-and-metrics-v2.md), [minimal rebuild plan](docs/superpowers/plans/2026-08-23-source-v6-minimal-rebuild.md) | fresh facts-only payload v2, M7 declaration checks, merged metrics in the existing worker pass and zero-decode analysis; 684/684 clean real-report import, deterministic materialization and performance gate recorded in `progress.md`; rebuild only, no migration | ADR-0006, ADR-0016, W6/W7 |
| Implemented | [Materialization speed](docs/specs/2026-09-21-source-v6-materialization-speed.md), [ADR-0040](docs/decisions/0040-source-v6-read-time-identity-from-stored-bytes.md) | read-time fragment identity from the stored bytes with an opt-in strict canonical audit, and a hoisted sample cutoff in `calculate_metrics`; 2.59x faster per point on real data (three runs per state) with a byte-identical dump of every analysis row | ADR-0017, W6 |
| Implemented / independently reviewed | [Source PRETEST A/B filter](docs/specs/2026-09-20-source-pretest-ab-filter.md), [implementation plan](docs/superpowers/plans/2026-09-20-source-pretest-ab-filter.md) | optional strict source-only 14-day tail gate, persisted versioned evidence, shortlist/audit/generation/run provenance; source metrics remain diagnostic and are not final MRS3 results | ADR-0017, fresh compact analysis |

The [2026-08-19 handoff Task 8](docs/superpowers/plans/2026-08-19-source-v6-analysis-handoff.md#task-8-fresh-only-compact-source-v6-and-parallel-multi-scope-surfaces)
and the storage portion of the [2026-08-18 v6 contract](docs/specs/2026-08-18-source-v6-stitched-surfaces.md)
are superseded for new compact artifacts by the documents above. Existing
v6 artifacts and their historical evidence remain untouched; the accepted
stitching and boundary rules remain applicable unless explicitly replaced.

[ADR-0013](docs/decisions/0013-source-v6-incomplete-seam-cycle-exclusion.md)
amends only the compatible-overlap seam case. Its user-approved old-owned
policy is implemented and independently reviewed. The full real-pair metric
audit is in [.codex/task6-recovery-overlap-report.md](.codex/task6-recovery-overlap-report.md):
all 684 pairs resolved old-owned and verified two local periods, local PnL,
maximum period DD% and retained-action Profit Factor.

## Unified Performance Analytics v2 (2026-08-28)

The approved [v2 specification](docs/specs/2026-08-28-unified-performance-analytics-v2.md)
and [ADR-0020](docs/decisions/0020-unified-performance-analytics-v2.md) replace
the planned data model for new Performance work: one appendable local database,
one current replaceable tester result per logical strategy, order-to-plateau
facts, arbitrary UPNL-relative A/B windows, configurable ordered filters/Pareto,
panel/XLSX finalists and Portfolio Optimizer input. The active native
`SINGLE_MODE` path uses a trusted metadata-only inbox. The retained hidden Runs
backend/API currently uses its direct inbox; any future `RETEST` replacement
handler must explicitly adopt the trusted v2 importer contract.

Status: **native SINGLE_MODE handoff, v2 vertical slice, one-strategy A/B
window analysis and stateless finalist-selection Stage 2 implemented and verified;
broader v2 pipeline pending**. Native `SINGLE_MODE` creates one
metadata-only inbox whose strategy JSON stays under trusted `Output/strategies`
and whose current HTML stays under `tester/report/my_test`; the manifest records
exact paths, source hashes, dates, commission and provenance. The v2 importer
is the only authoritative full validation before staging/DB commit. After a
successful v2 `COMMITTED`, cleanup is limited to exact approved source roots;
inbox metadata and v2 audit remain provenance, and cleanup failure is a warning
on the committed result. Runs backend/API remains available while its UI is
hidden; Fast panel dispatch/API/retry was removed. The accepted handoff spans
commits `952bc22..3f535f4` and final Terra disposition is `CODE_REVIEW_PASS`.
Fresh evidence is 366 passed across v2/native/panel tests, 338 passed in the v1
non-disturbance suite, plus `node --check src/mrs3/panel_web/app.js` and
`git diff --check`. No v1 migration is required because no production
Performance DB exists.

Explicitly deferred from this vertical slice:

- `strategy_tags`, `DISCARDED`, and the durable `RETEST` handler. Its next
  phase must add a Panel date-range interface and reject each request unless
  `listing_date <= test_start < test_end`, with `listing_date` read from
  `dates.xlsx` for every selected symbol;
- cleanup of source paths outside the exact approved post-commit roots;
- DD5 proxy UI and any scaled-result claim;
- persisted selection runs/results, tags, discard, RETEST and Portfolio
  Optimizer input; persisted finalist snapshots are governed by
  [ADR-0021](docs/decisions/0021-performance-v2-persisted-selection-snapshots.md);
- deletion of v1 runtime and storage;
- `point_id`, arbitrary filter expressions, and portfolio simulation.

The delivered A/B panel is intentionally limited to one ACTIVE strategy at a
time. It resolves the authoritative current result server-side, accepts two
strict UTC windows, and writes only the versioned `window_metrics` cache. The
finalist Stage 2 button reads current ACTIVE v2 candidates for one selected
Pair + Side, applies its submitted 13 built-in stages in UI order and returns a
disposable XLSX; it creates no selection state. Batch analytics beyond this,
tags/discard, RETEST, Portfolio input, DD5 proxy UI, Runs UI and Fast/legacy
behavior remain deferred.

This status does not claim a completed MRS3 strategy, a tick-test result, a DD5
result, or a portfolio result; source metrics remain source analytics until the
deferred evidence-producing stages are implemented and verified.

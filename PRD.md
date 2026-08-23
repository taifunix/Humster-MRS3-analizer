# Humster MRS3 Analyzer — PRD v0.7

## Продукт

Humster MRS3 Analyzer — локальный, детерминированный pipeline для перехода от результатов MRS2 к проверяемым кандидатам MRS3. Он нормализует входные точки, применяет правила устойчивости/пригодности, строит 1ORD и 2–4ORD структуры, выпускает валидированные strategy JSON, запускает batch в Hamster Bot Tester и сравнивает реальные результаты после tick-test.

**Текущий статус:** код v0.6 перенесён в корневой пакет как baseline; продуктовая работа начинается с v0.7. Реальная эффективность MRS3 пока не доказана: до завершения materialization и tick-tests любые source-метрики — только диагностика.

## Пользовательский результат

Для одного сравнимого периода и одной стороны рынка пользователь получает:

1. audit происхождения каждой MRS2-точки и причины каждого исключения;
2. воспроизводимые READY структуры 1ORD/2ORD/3ORD/4ORD с EQUAL и INCOME lot variants;
3. JSON, технически валидные для тестера;
4. результаты реального tick-test, расчётная DD5-нормализация и individual ranking;
5. только после накопления результатов — калиброванный безопасный pre-test potential filter.

## Текущий этап: v0.7 Source v6 fresh compact multi-scope — complete

Предыдущий DuckDB analysis-storage/importer этап реализован и проверен:
source schema v5, управляемый импорт, immutable analysis surfaces, повторный
plateau-анализ, lineage, библиотека результатов и детерминированные экспорты
уже доступны. Это инфраструктурная база нового Phase 1, но она не доказывает
доходность готовых MRS3-стратегий.

Базовый [DuckDB surface coverage review](docs/specs/2026-08-14-duckdb-surface-coverage-review.md)
и его Priority-1 patch также реализованы и проверены как **исторический текущий
runtime**: double-zero isolation, stale-token clearing, диагностируемые direct
jobs, ordinal и coverage-artifact links при старом one-MA-pair readiness.
Readiness-поведение этого historical runtime заменено canonical six-CloseMA
core в Task 2; panel/storage selected-preflight integration остаётся Task 3.

Новая [Canonical Phase 1 specification](docs/specs/2026-08-16-mrs3-v07-canonical-phase1.md)
утверждена как активный контракт, а [ADR-0009](docs/decisions/0009-canonical-phase1-surface-selection-contract.md)
принят после независимого governance review. Они определяют свежие canonical
surfaces и MRS3 selection:

- exact canonical Shift grid `30..550`;
- один общий UTC-интервал и шесть readiness witnesses CloseMA `2..7`;
- exact preview/audit/preflight replay;
- bounded 15-process direct materialization;
- frozen CMARepresentative / CloseMA continuity / BASE facts;
- 2/3/4ORD только из frozen representatives;
- независимый exact-scope 1ORD;
- hard rejection старых/non-canonical surfaces из нового operational flow.

Governance Task 0, canonical-config Task 1 и six-CloseMA readiness Task 2
завершены; следующий шаг — Task 3 из отдельного плана. Task 2 проверен
focused `79 passed`, `git diff --check` и независимым Luna `PASS`.
Принятые ADR-0007 и ADR-0008 не переписываются; их конфликтующие части
superseded ADR-0009 только для новых canonical surfaces. Старый
[Common Close-MA Readiness plan](docs/superpowers/plans/2026-08-15-common-close-ma-readiness.md)
остаётся frozen/non-executable.

Source DuckDB остаётся единым пополняемым lossless-хранилищем HTML-отчётов, а
Analysis DuckDB — append-only хранилищем immutable materialized surfaces,
analysis runs и lineage согласно уже реализованной
[спецификации DuckDB analysis storage and importer](docs/specs/2026-08-11-v07-duckdb-analysis-storage-and-importer.md).

Соединение CSV с DuckDB остаётся Optional / Deferred по
[CSV-DuckDB overlay](docs/specs/2026-08-11-v07-optional-csv-duckdb-overlay.md).

### Этапы поставки

| № | Результат | Входной критерий | Выходной критерий |
| --- | --- | --- | --- |
| 0 | Репозиторий v0.7 | перенесён baseline | root package, tests, docs и Git готовы |
| 1 | Проверенный v4 import | база и audit доступны | schema v4, manifest, quarantine/checklist проверены |
| 2 | Source packages | CSV и raw payloads v4 | один declared event mode, window и audit на пакет |
| 3 | DuckDB materializer | raw payloads v4 | closed cycles, exclusions и `point_period_metrics` |
| 4 | Selector v0.7 | ровно один source package | event gate, full rebuild, audit и JSON |
| 5 | Реальные MRS3 results | READY JSON | raw tick-test + расчётное DD5 ranking и individual ranking |
| 6 | Source-potential calibration | достаточная пачка results | LOPO-validated optional cap |

## Границы и safety rules

- Не удалять raw HTML до подтверждённого v4 audit и `safe_to_delete=YES`.
- Не использовать `len(raw actions)` как `TotalTrades` без reconciliation.
- Не смешивать `legacy_trades_proxy` и `real_independent_events` в одном run.
- Не фильтровать готовые v0.6 structures задним числом: после event-filter пересобирать весь universe.
- Не объявлять `SourcePnLSum` потолком или прогнозом фактического MRS3 PnL без калибровки.
- Не выдавать individual ranking за portfolio simulation: для портфеля нужны time series equity/drawdown/occupancy/margin.

## Не входит в текущий scope

- портфельная симуляция и смешивание независимых событий с legacy proxy;
- реализация портфельного модуля до появления его обязательных входных данных;
- ML, regression score или per-pair/per-TF production thresholds;
- GitHub push, PR или публикация результатов без отдельного разрешения.

## Hook: Анализатор Портфеля

Отдельная команда может начать модуль по [спецификации Portfolio Analyzer v0.4](docs/specs/2026-08-09-portfolio-analyzer-v04.md). Статус модуля — **Queued**, он не блокирует текущий v0.7 legacy selection.

Перед началом реализации команда должна подтвердить и записать в `progress.md` модуля: формат individual MRS3 results, журналы входов/выходов с timestamp, договорённости о limiter (позиции или заявки; LONG/SHORT; hedge/one-way), а также доступность/правила L2 и margin data. Без этого разрешён только аналитический Layer A; сетовый симулятор и финальные рекомендации не запускать.

## Реестр активной документации

| Статус | Документ | Назначение | Зависимости |
| --- | --- | --- | --- |
| Accepted | [Repository foundation](docs/specs/2026-08-10-mrs3-v07-repository-foundation.md) | структура репозитория и workflow | — |
| Active prerequisite | [Safe runner smoke-test](docs/specs/2026-08-10-v06-runner-safe-root-json-smoke.md) | безопасная проверка панели и одного реального прогона | локальный tester; до v0.7 implementation |
| Active | [v0.7 legacy selection](docs/specs/2026-08-10-v07-legacy-selection.md) | последовательность import → materializer → unified input → selector | v4 evidence, event-filter spec |
| Active | [v0.7 event source packs](docs/specs/2026-08-10-v07-event-source-packs.md) | CSV/DuckDB пакеты, event modes и closed-cycle audit | v4 evidence, event-filter spec |
| Implemented / Verified | [v0.7 DuckDB analysis storage and importer](docs/specs/2026-08-11-v07-duckdb-analysis-storage-and-importer.md) | единый source DuckDB, импорт из панели, analysis DuckDB и plateau lineage | event source packs, event-filter spec |
| Implemented / verified on production archive | [Trusted v4 migration performance](docs/specs/2026-08-11-v07-trusted-v4-migration-performance.md) | bounded v4-to-v5 production migration | DuckDB analysis storage |
| Optional / Deferred | [v0.7 CSV-DuckDB overlay](docs/specs/2026-08-11-v07-optional-csv-duckdb-overlay.md) | необязательное объединение CSV coarse-grid и DuckDB fine-grid | DuckDB analysis storage, event-filter spec |
| Accepted | [ADR-0002](docs/decisions/0002-source-summary-and-window-metrics-verification.md) | раздельная full-horizon/windowed verification для real packages v2 | event source packs |
| Active dependency | [Event filter and shortlist](docs/specs/v07-event-filter-and-shortlist.md) | правила `PointEventCount`, representative и shortlist | unified input |
| Planned | [Source-potential calibration](docs/specs/v07-posttest-calibration-source-potential.md) | empirical cap без leakage | завершённые tick-tests |
| Queued — hook «Анализатор Портфеля» | [Portfolio Analyzer v0.4](docs/specs/2026-08-09-portfolio-analyzer-v04.md) | отдельный анализ готовых MRS3-стратегий и сетов | individual results, trade timestamps, limiter/L2/margin contract |
| Accepted | [ADR-0001](docs/decisions/0001-repository-and-documentation-model.md) | root v0.7 и модель документации | — |
| Pending production acceptance | [Strategy performance DuckDB governing spec](docs/specs/2026-08-14-strategy-performance-duckdb.md) | transactional performance import, DB-only DD5 and safe cleanup | [ADR-0004](docs/decisions/0004-strategy-performance-evidence-store.md) |
| Active implementation contract | [Performance report import to DuckDB](docs/specs/2026-08-14-performance-report-import-duckdb.md) | immutable HTML-report import, canonical metrics, transaction, idempotency and cleanup | Strategy performance DuckDB, ADR-0004--0006 |
| Active implementation contract | [DD5 calculation and finalist selection](docs/specs/2026-08-14-dd5-finalist-selection.md) | DD5 formulas, scoped filters, Pareto, finalists and XLSX contract | Performance report import to DuckDB |
| Active implementation contract | [Tester Report Library and Fast Identity](docs/specs/2026-08-14-tester-report-library-and-fast-identity.md) | verified report library, fast embedded identity and deferred workflow/CLI integration | [Name-only runner contract](docs/specs/2026-08-14-tester-name-only-verification.md) |
| Active — implementation pending | [MRS3 v0.7 Canonical Phase 1](docs/specs/2026-08-16-mrs3-v07-canonical-phase1.md) | fresh canonical `30..550` surfaces, six CloseMA readiness, exact audit/preflight replay, parallel materialization, frozen CMA/BASE and independent 1ORD | [ADR-0009](docs/decisions/0009-canonical-phase1-surface-selection-contract.md), DuckDB analysis storage, event filter |
| Accepted | [ADR-0009](docs/decisions/0009-canonical-phase1-surface-selection-contract.md) | supersedes conflicting ADR-0007/0008 readiness semantics for fresh Phase 1 surfaces without rewriting historical ADRs | Canonical Phase 1 spec |
| Implemented / Verified Priority-1 patch — historical runtime evidence | [DuckDB surface coverage review](docs/specs/2026-08-14-duckdb-surface-coverage-review.md) | verified old one-MA-pair runtime and Priority-1 operational fixes; future canonical behavior is defined by the active 2026-08-16 Phase 1 spec | DuckDB analysis storage, ADR-0007, ADR-0008 |
| Accepted | [ADR-0007](docs/decisions/0007-observed-sparse-surface-contract.md) | V1 unchanged; V2 evidence in existing `grid_contract_json`; one read transaction prepares selected sides; LONG then SHORT; `PARTIAL`/manual rerun; deferred retry/lease/path/schema-v5 | DuckDB surface coverage review |
| Accepted | [ADR-0008](docs/decisions/0008-common-close-ma-readiness-and-degenerate-row-isolation.md) | common Close MA `2..7` interval and six scope witnesses; structurally zero-duration rows ignored, other empty intersections fail closed | DuckDB surface coverage review |

Полная навигация: [docs/README.md](docs/README.md). Оперативная точка: [progress.md](progress.md).
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
| Approved for implementation | [Source v6 metric contract](docs/specs/2026-08-23-source-v6-metric-contract.md), [facts/metrics v2 ADR](docs/decisions/0017-source-v6-facts-and-metrics-v2.md), [minimal rebuild plan](docs/superpowers/plans/2026-08-23-source-v6-minimal-rebuild.md) | fresh facts-only payload v2, checked declarations, merged metrics in the existing worker pass and zero-decode analysis; rebuild only, no migration | ADR-0006, ADR-0016, W6/W7 |

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

# Документация Humster MRS3

## Читать в таком порядке

Новая рабочая сессия читает [AGENTS.md](../AGENTS.md) → [PRD.md](../PRD.md) → [progress.md](../progress.md) → активную спецификацию. Этого достаточно для большинства задач; архив открывается только по ссылке из active-doc.

## Актуальные документы

| Раздел | Документ | Когда читать |
| --- | --- | --- |
| Контекст | [PRD](../PRD.md) | всегда после AGENTS |
| Оперативный статус | [Progress](../progress.md) | всегда после PRD |
| Текущая поставка | [DuckDB analysis storage and importer](specs/2026-08-11-v07-duckdb-analysis-storage-and-importer.md) | для source DuckDB, импорта из панели, analysis DuckDB и plateau lineage |
| Active — implementation pending | [MRS3 v0.7 Canonical Phase 1](specs/2026-08-16-mrs3-v07-canonical-phase1.md) | Task 0 passed; свежие canonical surfaces и selection |
| Текущая поставка | [v0.7 legacy selection](specs/2026-08-10-v07-legacy-selection.md) | для работы над v0.7 |
| Event rules | [Event filter and shortlist](specs/v07-event-filter-and-shortlist.md) | при selector/event-filter изменениях |
| Source verification | [Event source packs](specs/2026-08-10-v07-event-source-packs.md) | при CSV/DuckDB package, materializer или selector изменениях |
| Необязательная фича — Deferred | [CSV-DuckDB overlay](specs/2026-08-11-v07-optional-csv-duckdb-overlay.md) | только если отдельно решено объединять CSV coarse-grid и DuckDB fine-grid |
| Post-test calibration | [Source-potential calibration](specs/v07-posttest-calibration-source-potential.md) | только после накопления results |
| Анализатор Портфеля — Queued | [Portfolio Analyzer v0.4](specs/2026-08-09-portfolio-analyzer-v04.md) | отдельной команде после проверки входных данных |
| Решения | [ADR-0001](decisions/0001-repository-and-documentation-model.md), [ADR-0002](decisions/0002-source-summary-and-window-metrics-verification.md), [ADR-0003](decisions/0003-source-integrity-action-metrics.md), [ADR-0009](decisions/0009-canonical-phase1-surface-selection-contract.md) | при вопросах структуры/workflow, source verification и Canonical Phase 1 governance |

## Правила обновления

- Новая функция или изменение поведения: active spec → тесты → реализация → `progress.md` → review → commit.
- Изменение границ или зависимости: обновить PRD.
- Архитектурное/safety решение: создать ADR.
- Публичное использование изменилось: обновить корневой README только после проверки.
- Исторические документы не редактировать для описания v0.7; вместо этого создать новую спецификацию или ADR.

## Архив

[archive/](archive/README.md) хранит v0.6 baseline, handoff и source-материалы. Он не определяет текущие требования; queued Portfolio Analyzer находится в `docs/specs/`.

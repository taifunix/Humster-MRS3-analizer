# Handoff — Performance DB v2: остаток работ и важные инварианты

**Дата:** 2026-09-03
**Читать вместе с:** [актуальным backlog](2026-09-03-performance-v2-remaining-work.md).

## Состояние на передачу

Реализован контур selection review (schema v3, snapshots, аналоги, Top-20,
XLSX review/import, `REJECTED`, «Только финалисты»); ручная Excel-приёмка ещё
не проведена. Независимое ревью не нашло High или блокирующих замечаний,
подтверждённые Medium/Low follow-up исправлены. Focused набор Performance v2:
**248 passed**; полный suite: **2118 passed, 2 skipped, 1 warning**.

В этом же незакоммиченном наборе есть исправление парсера current HTML:
`EndDate` tester включителен; event/equity ровно в конце диапазона теперь
допускаются. Парсер по-прежнему отклоняет timestamp строго позже конца или
строго раньше начала. Его focused test: `tests/test_performance_v2_html.py` —
**23 passed**.

Рабочая БД: `data/performance-v2/strategy_performance.duckdb`. После первого
доступа нового контура она автоматически мигрирует v2 → v3 в одной DuckDB
транзакции. Это не меняет существующие results/actions/equity и не требует
нового пересчёта кэша окон.

## Что в действительности готово

- Один v2 DuckDB с current result на strategy, actions/equity, order/plateau facts и versioned `window_metrics`.
- Все фильтры/Pareto панели применяются последовательно; preview/counters read-only, XLSX — явная операция сохранения результата.
- A/B и PnL/30 используют календарные границы tester report, а не first/last action span.
- Ranking: `0.30/0.15/0.15/0.12/0.10/0.09/0.09`; Top N default `20`; Close-MA near-tie выключен default.
- Analog grouping: exact Pair+Side+TF+ORD+Close MA, плюс строго ограниченная соседняя Close MA при полном совпадении plateau identity всех ордеров.
- XLSX export записывает immutable `selection_run`; в книге есть very-hidden `_MRS_SELECTION_META`. Import identity — только Strategy ID; hidden strategy/result — integrity evidence.
- Review import принимает только последний run той же Pair+Side и неизменившиеся current result IDs; каждая книга импортируется атомарно, импорт папки обрабатывает файлы независимо.
- `REJECTED` — единственный durable tag; не удаляет facts и может быть снят следующим валидным review.

## Данные и диагностика периодов

Сформирован одноразовый [period integrity audit](../../Output/performance-v2-period-integrity-audit-2026-09-02.xlsx): `0 CONFIRMED`, `49 HIGH`, `100 REVIEW`.

Классы означают:

- `CONFIRMED`: текущая БД содержит прямое нарушение (outside inclusive range, broken action indices, mismatch stored order count).
- `HIGH`: минимум два статистических сигнала хвоста/когорты/terminal cluster.
- `REVIEW`: ровно один такой сигнал.

Это правильно использовать как список кандидатов на повторный тест, но не как
доказательство, что любой старый исходный HTML был полностью импортирован:
старые raw HTML не сохранялись внутри этой БД. Не вводить правило, что last
action/equity должен совпадать с report end, и не считать разрывы equity
ошибкой — эталонный current tester HTML содержит допустимые большие интервалы
без сделок.

## Ближайший рекомендуемый порядок

1. Принять Stage 3 вручную: реальная schema migration на копии БД, Excel open/save/edit/import, пакет из нескольких книг, `REJECTED`, «Только финалисты».
2. После commit/push провести ручную приёмку текущего контура на реальной копии базы.
3. Решить, нужен ли продуктовый read-only экран/command для period integrity audit; отдельная спецификация нужна до реализации.
4. Затем отдельно проектировать RETEST, discard/reviewed deletion и Portfolio input. Они не входят в текущий diff.

## Не возвращать без нового решения

- Не добавлять V1 migration/dual read.
- Не считать DD5 proxy тестовым PnL или портфельным результатом.
- Не делать автоматическое удаление `REJECTED`.
- Не расширять теги «на всякий случай»: сейчас договорён только `REJECTED`; `RETEST` требует отдельного lifecycle-контракта.
- Не заменять строгую validation review workbook отображаемым strategy name.
- Не пересчитывать окна после schema v3 migration только ради migration.

## Документные коллизии

- Базовая спецификация finalist selection 2026-08-31 всё ещё описывает stateless Stage 2. Для snapshots, review и statuses она superseded спецификацией 2026-09-02.
- ADR-0021 superseded ADR-0022 в части отложенной persistence/review.
- robust-ranking spec 2026-09-01 исторически содержит Top-50/включённый Close-MA near-tie. Реальный current contract/code используют Top-20 и disabled-by-default Close-MA near-tie из 2026-09-02.

## Ключевые пути

- `src/mrs3/performance_v2_store.py` — schema v3/migration/catalog validation.
- `src/mrs3/performance_v2_html.py` — current HTML parser и integrity checks.
- `src/mrs3/performance_v2_windows.py` — safe flat-boundary windows.
- `src/mrs3/performance_v2_selection.py` — facts, stages, ranking, XLSX.
- `src/mrs3/performance_v2_selection_review.py` — immutable runs, review validation/import, effective finalists.
- `src/mrs3/panel_performance_v2.py`, `src/mrs3/panel.py`, `src/mrs3/panel_web/` — endpoints и UI.
- `tests/test_performance_v2_selection_review.py` — основной тестовый контракт Stage 3.

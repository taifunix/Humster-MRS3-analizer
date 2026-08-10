# Handoff: завершение v2 source-integrity

**Дата:** 2026-08-10
**Статус:** узкая доработка проверена и готова к scoped commit; следующий
операционный шаг после commit — read-only реальный package rebuild.

## Цель

Довести до конца проверку real DuckDB source package v2 и затем выполнить
read-only materialization с HTML samples и `select --source-package`. Это не
повторный импорт HTML в DuckDB: используются существующая v4 база и HTML лишь
для 3-5 проверочных образцов.

## Что уже находится в `main`

- `d802378 fix: match imported HTML source identity` — verifier использует ту
  же text-normalized SHA-256 семантику, что импортёр v3/v4; CRLF/LF больше не
  даёт ложный identity mismatch.
- `9872a4c feat: verify windowed DuckDB source packages` — format v2,
  source/window statuses, строгий loader и поддержка CSV с полями `ticker` /
  `launch`.
- Панель с вкладками, dashboard, множественным CSV выбором и Settings уже
  закоммичена и запушена отдельным коммитом; Portfolio остаётся queued.

## Текущее техническое состояние

Ниже перечислен scoped change set для уточнения source integrity:

- `src/mrs3/duckdb_events.py`
- `src/mrs3/package_loader.py`
- `tests/test_source_packs.py`
- `tests/test_package_loader.py`
- `docs/decisions/0003-source-integrity-action-metrics.md` (новый)
- `docs/specs/2026-08-10-v07-event-source-packs.md`
- `PRD.md`
- `progress.md`

Не включать в этот коммит:

- whitespace-only изменение `docs/decisions/0002-source-summary-and-window-metrics-verification.md`;
- `docs/superpowers/plans/2026-08-10-safe-runner-root-json-smoke.md`;
- любые `.pytest-*` временные каталоги и generated outputs.

### Принятый контракт

Реальные образцы доказали следующее:

- `TotalTrades`, `WinRate`, `ProfitFactor` из full-report HTML совпадают с
  decoded actions и являются единственными `EQUAL` source-integrity metrics.
- `TotalPnL` и `MaxDrawdown` в HTML не имеют эквивалентной семантики в
  compact payload (отчётная логика/сэмплирование). Они сохраняются в audit, но
  обязаны иметь `comparison` и `cause` равные
  `NOT_COMPARABLE_WINDOW_SCOPE`; они не должны ни подтверждать, ни отклонять
  `source_summary_status`.
- Для v2 `source_summary_status=VERIFIED` требует 3-5 samples, identity,
  full-horizon range, action-audit linkage, ровно три `EQUAL` action metrics
  и ровно две явные non-comparable diagnostics. `window_metrics_status` —
  отдельное доказательство происхождения `[start,end)` значений.
- ADR-0003 является новым superseding ADR для набора метрик; ADR-0002 не
  переписывать, он сохраняет решение о разделении horizon evidence.

## Последнее обязательное review finding

Независимый Terra review вернул `REQUEST_CHANGES`:

`src/mrs3/package_loader.py::_validate_real_v2_numeric_evidence` пропускает
валидацию значений PnL/DD. Пакет с корректной структурой, но нечисловыми или
подменёнными `source_raw`, `source_value` либо `calculated_value` у двух
`NOT_COMPARABLE_WINDOW_SCOPE` строк сейчас может загрузиться как `VERIFIED`.

Нужно минимально исправить loader и добавить negative tests:

1. для PnL/DD требовать parseable finite numeric `source_raw`, `source_value`
   и `calculated_value`;
2. проверять соответствие `source_raw` и `source_value` той же parser/rounding
   семантикой, что у builder;
3. не требовать equality `calculated_value` с HTML — это именно
   non-comparable diagnostic;
4. запустить targeted tests, затем full suite, затем обязательный Terra
   re-review.

Не ослаблять gate и не менять import/runtime v3/v4.

## Свежие evidence

- До последнего review: focused `.venv\\Scripts\\python.exe -m pytest
  tests\\test_source_packs.py tests\\test_package_loader.py -q
  -p no:cacheprovider --basetemp <writable-temp>` — **69 passed**.
- Agent-implementation evidence до review correction: full suite **289 passed,
  1 skipped** (Windows symlink privilege skip); это не покрывает ещё не
  внесённую review correction.
- `git diff --check` был чистым.
- Попытка root запустить full suite была прервана пользователем; не заявлять
  свежий full-green до повторного запуска.

## Продолжение в новой сессии

1. Прочитать `AGENTS.md`, `PRD.md`, `progress.md`,
   `docs/specs/2026-08-10-v07-event-source-packs.md`, ADR-0002, ADR-0003 и
   этот handoff.
2. Реализовать четыре пункта из review finding с TDD.
3. Проверить focused и full pytest. В обычной Windows sandbox pytest может
   падать на ACL temp; в таком случае запускать с уникальным writable
   `--basetemp` через approved elevated command.
4. Выполнить `git diff --check`, staged diff review независимым Terra agent и
   re-review после исправлений.
5. Создать один scoped commit, например
   `fix: validate source-integrity diagnostics`, и push в `origin/main`.
6. Только после этого повторно materialize реальный package read-only с
   3 samples. При `source_summary_status=VERIFIED` выполнить selector с уже
   предоставленными listing-date CSV и strategy template. Артефакты остаются
   ignored/local; локальные абсолютные пути не записывать в tracked docs.

## Быстрый промпт для новой сессии

> Продолжи по `docs/handoffs/2026-08-10-source-integrity-finalization.md`.
> Работай только над v2 source-integrity review finding, соблюдай AGENTS.md,
> TDD и обязательный независимый Terra review перед коммитом. Не переимпортируй
> HTML в DuckDB и не записывай локальные пути в документацию.

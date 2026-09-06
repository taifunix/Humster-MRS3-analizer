# Portfolio Optimizer — handoff чистой M0-сессии

**Статус:** стартовый контекст для M0. Не заменяет
[specification](../../specs/2026-09-05-portfolio-optimizer.md),
[implementation plan](2026-09-05-portfolio-optimizer.md) или ADR-0025/0029.

## Скопировать в новую сессию

> Реализуй только M0 Portfolio Optimizer: read-only inventory существующих
> contracts и capability ledger. Сначала прочитай `AGENTS.md`, `PRD.md`,
> `progress.md`, эту handoff, spec, ADR-0025, ADR-0029 и план. Не начинай M1,
> не запускай tester/bot, не делай public/private API request, не создавай
> runtime config и не меняй Performance DB. Подтверждай каждый факт ссылкой на
> код, schema или sanitised fixture; неизвестное оставляй UNKNOWN.

## Что уже решено

- D4 architecture и D5 policy amendment имеют независимый Opus
  `PLAN_APPROVED`; это не `CODE_REVIEW_PASS` и не trading permission.
- Один portfolio — отдельный Bybit Unified Cross account/subaccount; collateral
  разных accounts не неттируется. Нет product cap на число pairs.
- Кандидаты — только заранее протестированные mean-reversion strategies.
  Их MA/shifts/geometry/internal `lot_x`/exit immutable; optimizer меняет только
  allowed directional scalar. Один symbol имеет одну net direction; BOTH требует
  joint test.
- Limiter: `L=0` выключен; `L>0` считает non-flat PairSlot; priority=0 exempt
  только от limiter, но не от margin/liquidity. Margin envelope включает L+1 и
  больше до confirmed cancel/flat.
- `max_balance` ограничивает sizing base, не collateral.
- `portfolio_optimizer_research_risk_v1` — research-only: Aggressive
  DD/free-margin/MM = 20%/20%/50%, Balanced = 10%/40%/35%, Conservative =
  5%/60%/20%. Это не разрешает `RECOMMENDATION_READY`, tester run или trading.
- Primary PnL/DD берутся только из actual joint portfolio equity series; сумма
  individual PnL — не portfolio result.

## Известные факты, которые нельзя «исправлять» догадкой

- В Performance input пока нет public turnover по symbol/day/month, реальных
  account fee rates и отдельно requested quantity; пустой `raw_action_json`
  также не является свидетельством исполнения. Эти пробелы — capability facts,
  не повод создавать synthetic data.
- Текущий runner/panel не имеет cross-process tester-target lock. `PanelJobRegistry`
  покрывает лишь один panel process; legacy runner lock привязан к output CSV.
  Общий lock — задача M5. До M5 любой real tester run и shared-target write
  запрещены.
- Collector Revision 2/ADR-0024 переиспользуется. Второй long-running collector,
  копия Performance DB, matching engine и private API в MVP не создаются.

## M0: точная работа

1. Прочитать указанные в плане §4/M0 и spec §2, §4, §18; затем trace реальных
   callers, а не только названий файлов.
2. Проверить read-only Performance DB contract: schema/version, current result
   replacement, selection/window side effects, возможность одной consistent
   transaction и обязательный `cache_only=True`. Если consistent read нельзя
   подтвердить — описать fail-closed outcome, не копировать открытую DB и не
   останавливать writer.
3. Проверить collector read contract: `published_hours`, read-only storage,
   reference/instrument/tier completeness, coverage/freshness и доступные
   liquidity fields. Не запускать collector и не делать ticker request.
4. Проверить tester/runner contract: portfolio-mode config/report fields,
   dual-TF, leverage, limiter, opposite orders, sizing/max_balance, fee/funding,
   report attribution, target ownership and cleanup. Исследовать код и existing
   sanitised fixtures; executable probe запрещён.
5. Сопоставить Q01–Q12 с owner, точным evidence, fixture (если есть),
   `CONFIRMED_CAPABILITY` / `APPROVED_CONSERVATIVE_BOUND` /
   `BLOCKING_UNKNOWN` и конкретно блокируемой M-веткой.

### Куда смотреть сначала

| Контур | Точки входа |
| --- | --- |
| Performance v2 | `src/mrs3/performance_v2_store.py`, `performance_v2_input.py`, `performance_v2_selection.py`, `performance_v2_retest.py` |
| Runner/panel | `src/mrs3/runner/`, `src/mrs3/panel.py`, `src/mrs3/panel_jobs.py`, `src/mrs3/panel_fast_strategy_test.py` |
| Collector | `src/mrs3/bybit_collector/storage.py`, `archive.py`, `reference.py` |
| Existing contracts/tests | указанные callers и focused tests из M0 в основном плане |

## Дисциплина результата

- Для каждого утверждения сохранить `path:line` или schema/fixture identity.
- `UNKNOWN` — нормальный результат; не подставлять zero/default и не расширять
  scope, чтобы получить PASS.
- Если M0 меняет контракт, обновить canonical spec/ADR только на основании
  доказанного факта; `progress.md` обновляется вместе с принятым evidence.
- Не создавать пустой `src/mrs3/portfolio/` и не реализовывать M1 «заодно».
- Перед commit: scoped diff, пропорциональные checks, independent review и один
  conventional commit по `AGENTS.md`.

## M0 готов к сдаче, когда

1. Есть versioned capability/field matrix для Q01–Q12 с evidence и owner.
2. Все обязательные неизвестности имеют named fail-closed behavior и точный
   блокируемый этап.
3. Проверены source read-only boundary и отсутствие разрешения на tester target.
4. Нет tester/API/runtime/config/Performance DB mutation.
5. Независимый review принят, а `progress.md` указывает следующий безопасный
   шаг — M1 либо конкретный закрываемый capability gap.

**После M0:** новая сессия получает M0 evidence plus этот файл; M1 начинается
только после принятия M0, с TDD и без real tester runs.

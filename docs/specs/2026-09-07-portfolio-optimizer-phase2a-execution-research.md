# Portfolio Optimizer Phase 2A — execution research
**Дата:** 2026-09-07

**Статус:** `ACCEPTED_FIXTURE_BOUNDARY` после независимого
`CODE_REVIEW_PASS`. Это не разрешение real tester/live use.

**Связано:** [основная спецификация](2026-09-05-portfolio-optimizer.md) §11,
[ADR-0025](../decisions/0025-portfolio-optimizer-evidence-and-phases.md),
[ADR-0030](../decisions/0030-portfolio-optimizer-m2-admission-and-sizing-contract.md),
[план Phase 2A–2B](../superpowers/plans/2026-09-07-portfolio-optimizer-phase2a-2b.md).

## 1. Цель и границы

Phase 2A добавляет воспроизводимое исследование фактического исполнения к
текущим liquidity/margin proxy. Оно должно показать, как requested size
превращается в fills, сколько занимает первое и полное исполнение, где остаются
частичные и отменённые объёмы и какие ограничения можно обосновать данными.

Входом служат работающий Phase 1 (достаточно `RESEARCH_ONLY`) и versioned
execution facts из fixtures/fakes. Phase 1 `READY` не требуется.

### Non-goals

- точный queue/fill simulator, полная UTA replica или биржевая liquidation
  engine;
- real exchange/tester run, credentials, trading, bot/config mutation,
  notifications, Panel route или `RECOMMENDATION_READY`;
- исправление прошлых evaluations, замена истории или выдача source-метрик за
  результат joint MRS3 portfolio;
- перенос coarse multi-overflow и общей liquidity-проверки из Phase 1.

## 2. Входы и выходы

Входной пакет содержит immutable `execution_evidence_schema_version`, источник,
`execution_campaign_id`/`trading_run_id`, read timestamp, venue/account alias
без секретов и упорядоченные lifecycle events. События должны позволять
сопоставить requested order, fills, cancel/replace и reduce-only lifecycle с
теми же symbol, side и timeframe, которые были в Campaign.

Выход — новый immutable research record с:

- рассчитанными requested/filled объёмами, временем до first/full fill,
  partial count и remaining quantity на cancel/replace;
- degradation curves и coverage по базовым strata `symbol × side × timeframe`,
  затем по явно переданному optional `regime`;
- сравнением измеренного исполнения с текущим proxy и versioned calibration
  applicability/confidence; это только descriptive `RESEARCH_ONLY` evidence;
- typed evidence для уточнений Order Loss, collateral haircut, borrow, close
  fees и active-order reserve, когда факты позволяют их посчитать;
- одной versioned correlated stress-сценарийной записью, использующей текущие
  margin/liquidity inputs и simultaneous deepest fill;
- disposition `NEEDS_RETEST`, если новая capacity/size меняет executable
  decision. Старая evaluation при этом остаётся неизменной.

## 3. Нормативная модель

### 3.0. Каноническое evidence и числовая воспроизводимость

Все public и decoded evidence records проходят рекурсивную canonical
проверку. Ключи каждой mapping должны быть `str`; преобразование ключей в
строки запрещено, поскольку `1` и `"1"` не должны сливаться. Python `float`,
`set`, `frozenset`, `bytearray` и любые другие mutable/custom leaves
отклоняются. Допустимые leaves: finite `Decimal`, `int` (но не `bool` как
числовой тип), `str`, `bool`, `None` и timezone-aware `datetime`, если поле
допускает timestamp. Dataclass допускается только через явно описанные
рекурсивные поля; произвольный объект не является evidence.

Канонический Decimal сохраняет representation scale, включая trailing zeros.
Поэтому `Decimal("1.0")` и `Decimal("1.00")` намеренно имеют разные
исторические digests, хотя численно равны. Это representation-sensitive
свойство является частью immutable replay contract и не должно нормализоваться
задним числом.

Все mean/ratio/difference и stress arithmetic выполняются в локальном Decimal
context, заданном реализацией для evidence. Ambient precision, rounding,
`Emin`/`Emax` и traps вызывающего процесса не должны менять canonical values,
bytes или digests.

### 3.1. Immutable order events и identity

Каждая запись имеет `event_schema_version`, `event_id`,
`execution_campaign_id`, `trading_run_id`, secret-free `account_alias`,
`order_id`, `order_revision`, `symbol`, `side`, `timeframe`, optional
`regime`, `event_type`, source sequence/index и source timestamp. `requested_qty`
nullable; `fill_qty`, `cumulative_filled_qty`, `fill_delta_qty`,
`remaining_qty` и event timestamp также допускают null, если источник их не
сообщает. Нельзя подставлять ноль или estimated time.

Legacy `fill_qty` и canonical `cumulative_filled_qty` означают cumulative
filled quantity для данного `order_revision`, а не delta. `fill_qty` сохраняется
для совместимости и при наличии обоих полей обязан совпадать с
`cumulative_filled_qty`. `fill_delta_qty` optional и, если передан, равен
разнице текущего cumulative значения и предыдущего cumulative значения той же
revision. Cumulative fill монотонно неубывает и не превышает requested quantity
данной revision. Mismatch между legacy/canonical полями, decrease, overfill или
неверный delta дают `INCONSISTENT`. Отсутствующий cumulative факт на
`PARTIAL_FILL`/`REDUCE_ONLY_FILL` остаётся unknown; `FULL_FILL` может доказать
requested cumulative только если requested quantity известен. При replace
создаётся новая revision с отдельным cumulative baseline и явной link на
предыдущую revision; старый baseline не переносится молча.

Каноническая identity события:
`(execution_campaign_id, account_alias, order_id, order_revision, event_id)`.
Повтор с тем же identity и теми же каноническими полями идемпотентен. Повтор с
другим payload — `INCONSISTENT` и не объединяется молча. Разные event IDs с
одним `source_sequence` — ambiguous sequence и `INCONSISTENT`; arrival order
никогда не разрешает такую неоднозначность. Lifecycle не
переписывается: correction/late fact — новое событие с provenance и новой
schema version.

Поддерживаемые типы включают `PLACED`, `ACKNOWLEDGED`, `PARTIAL_FILL`,
`FULL_FILL`, `CANCEL_REQUESTED`, `CANCEL_CONFIRMED`, `REPLACE_REQUESTED`,
`REPLACE_CONFIRMED`, `REDUCE_ONLY_FILL` и `REJECTED`. Неизвестный тип является
ошибкой схемы, а не новым состоянием.


For `FULL_FILL`, `AVAILABLE` requires a known requested quantity and a known `cumulative_filled_qty` exactly equal to that requested quantity. Missing quantity facts remain `UNKNOWN`; a known mismatch is `INCONSISTENT`. A supplied `fill_delta_qty` must equal the cumulative difference from the prior event, including for `FULL_FILL`.

### 3.2. Расчёты и reserve

`fill_ratio` публикуется только если `requested_qty` известен, положителен,
совместим с unit/side и имеет достаточные fills. `time_to_first_fill` требует
valid placement и first-fill timestamps; `time_to_full_fill` требует full-fill
timestamp. Missing requested не даёт fill ratio, а missing timestamps даёт
`UNKNOWN` для соответствующей latency.

Partial count — число канонических partial-fill событий. На каждом
`CANCEL_CONFIRMED` или подтверждённом replace сохраняются observed
`remaining_qty` и reserve state. До cancel confirmation исходный reserve
остаётся активным, даже если отмена была запрошена или WS сообщает неполный
статус. Replace получает новую `order_revision`; старый revision сохраняет
свой остаток.

Агрегация идёт в фиксированном порядке: сначала
`symbol × side × timeframe`, затем только при наличии явного и versioned
`regime`. Нельзя выводить regime из даты, размера или missing поля. Каждая
curve хранит sample count, coverage, units, source digest, calibration version
и applicability; малую выборку нельзя выдавать за устойчивую bound.

### 3.3. Calibration и margin evidence

Proxy и empirical result сравниваются описательно: значения, разность,
coverage, confidence/applicability и версии входных данных. Thresholds,
percentiles, grace и policy не зашиваются в коде; они приходят из versioned
research settings или явно обозначены как `OPEN_POLICY`.

Margin refinement допускается только для typed facts с unit, currency,
provenance, observed-at и expiry. Missing, stale, conflicting или
non-positive mandatory fact даёт `UNKNOWN` и не наследует last-known-good.
Неизвестный Order Loss/haircut/borrow/fee не превращается в нулевой расход.

Correlated stress — ровно один воспроизводимый сценарий на запуск: correlated
price/equity shock, liquidity/leverage deterioration и simultaneous deepest
fill. Он повторно применяет текущие M2/M3 liquidity, leverage, margin и
individual-DD gates; новые stress thresholds не создаются. Результат является
diagnostic evidence и не заменяет joint tick-test.

## 4. Состояния отказа и безопасность

Каждая серия имеет `AVAILABLE`, `PARTIAL`, `UNKNOWN` или `INCONSISTENT`:

- `PARTIAL` — известна только часть lifecycle; доступные counts сохраняются,
  но недоказанные ratios/latencies не заполняются;
- `UNKNOWN` — отсутствует обязательный факт, timestamp, unit, currency,
  applicability или calibration input;
- `INCONSISTENT` — конфликт identity/payload, невозможный порядок lifecycle или
  несовместимые campaign/manifest facts.

Любое изменение sizing, capacity, reserve или margin envelope создаёт новый
versioned evidence/evaluation lineage и disposition `NEEDS_RETEST`. Исторические
facts и evaluations не обновляются и не пересчитываются задним числом. Для
изменения margin envelope lineage создаётся также при stress gate `UNKNOWN` или
`FAIL`: child может не иметь capacity, а parent digest, status, bytes и row
остаются неизменными.

## 5. TDD-порядок

Реализация после отдельного review этой спецификации выполняется малыми
шагами:

1. Добавить failing tests для schema version, nullable requested, canonical
   identity, duplicate/conflict, equal source sequence и неизвестного event type
   в
   `tests/test_portfolio_execution_research.py`; затем реализовать
   `src/mrs3/portfolio/execution_research.py`.
2. Добавить fixtures с placement/fill/cancel/replace/reduce-only lifecycle и
   тесты first/full latency, partial count, remaining и reserve до
   `CANCEL_CONFIRMED`.
3. Добавить failing tests на base strata и явный optional regime; реализовать
   curves с coverage/units/provenance и fail-closed missing fields.
4. Добавить failing tests на recursive evidence leaves: key collision,
   non-string key, Python `float`, set/frozenset, unsupported mutable/custom
   value, Decimal scale и hostile Decimal context. Добавить tests cumulative
   fill mismatch/decrease/overfill/delta и revision baseline reset.
5. Добавить tests сравнения proxy/calibration и typed margin facts в
   `tests/test_portfolio_liquidity.py` и `tests/test_portfolio_margin.py`,
   используя существующие расчётные контракты.
6. Добавить тест одного correlated stress, повторно использующего текущие
   gates, и `NEEDS_RETEST` без изменения старой evaluation; persistence wiring
   проверять в `tests/test_portfolio_store.py`, включая envelope lineage при
   `UNKNOWN`/`FAIL` stress gate и отсутствии capacity.
7. Запустить focused и related portfolio suites, затем `git diff --check`.

## 6. Acceptance evidence / DoD

До отдельной приёмки нужны:

- fixtures с duplicate, conflict, missing requested, partial lifecycle,
  cancel-before-confirmation, replace revision, stale/conflicting margin facts,
  explicit regime, equal-sequence ambiguity и cumulative fill
  mismatch/decrease/overfill/delta cases;
- assertions, что public/decoded evidence recursively rejects non-string
  mapping keys, Python `float`, sets/frozensets и unsupported mutable/custom
  leaves, сохраняя только разрешённые immutable leaves;
- assertions, что неизвестные ratios/latencies/reserve не становятся нулём,
  aggregate не перескакивает через base strata, а повторный event не удваивает
  fill; cumulative fill semantics, revision reset и equal-sequence ambiguity
  также проверены;
- golden canonical identity/schema vectors и проверка сохранения source digest,
  units, currency, timestamps, expiry и applicability;
- hostile Decimal contexts с non-terminating means/ratios и широким exponent
  span дают byte-identical values/digests; representation-sensitive trailing
  zero digest остаётся стабильным;
- descriptive proxy-vs-empirical report со статусом `RESEARCH_ONLY`, одним
  correlated stress и новым lineage `NEEDS_RETEST` при изменении size; lineage
  для envelope change сохраняет parent без изменений даже при stress gate
  `UNKNOWN`/`FAIL` и отсутствии capacity;
- focused tests из §5, relevant existing portfolio tests, Python compile и
  `git diff --check`.

Acceptance не заявляется до независимого review и отдельного решения о
следующих gates.

## 7. Зависимости и отложенные gates

Зависимости: Phase 1 contracts, M2 liquidity/reference, M3 margin/limiter,
M4 typed variants, immutable Campaign/TradingRun и ADR-0030. Монитор Phase 2B
может читать только принятые, secret-free outputs этого этапа.

Отложены: real execution facts, queue model, UTA details, real tester,
exchange credentials, exact confidence/threshold policies, PnL/freshness/
ranking gates, `RECOMMENDATION_READY`, trading admission и live deployment.

# MRS3 v0.7 — минимальное ТЗ: Independent Events + безопасное сокращение кандидатов

> Superseded for performance selection and post-test reporting by `docs/specs/2026-09-03-performance-v2-retest-workflow.md`; retained for historical provenance only.

## 0. Назначение

Это дополнение/коррекция к MRS3 v0.6.

Цель:

1. не допускать в MRS3 геометрически красивых, но статистически пустых плато;
2. использовать реальные независимые рыночные события вместо грубого proxy по `Trades`;
3. после этого, если MRS3-кандидатов все еще слишком много, сокращать только очевидно избыточные варианты;
4. не вводить сложные score, Jaccard, coverage-оптимизацию и другие дополнительные уровни без необходимости.

Главный принцип:

> Геометрия отвечает за устойчивость параметров.  
> Independent Events отвечают за повторяемость поведения во времени.  
> Оба условия нужны одновременно.

---

# 1. Что НЕ меняется относительно v0.6

Оставить без изменений:

- economic gates;
- Effective History;
- standalone sample rules;
- геометрию Plateau;
- CORE continuity `90%`;
- Plateau envelope `75%`;
- 5% правило practically-equivalent points;
- CloseMA support:
  - CORE `>=90%`;
  - SUPPORTED `>=75%`;
- один ордер MRS3 = одно отдельное Plateau;
- CommonCloseMA для всей MRS3;
- gap:
  - `<1.5%` → `0.6 п.п.`;
  - `1.5–4.0%` → `0.8 п.п.`;
  - `>4%` → отдельное исследование;
- генерацию 2/3/4ORD независимо;
- EQUAL и INCOME lot variants;
- JSON generation;
- DD5-normalization и post-test comparison по расчётным `projected_pnl_dd5` и `projected_dd_pct`; DD5 JSON и отдельный DD5 retest не входят в workflow.

### JSON serialization order

Generated strategy JSON is a human-reviewable derivative of the selected
template. Its object-key order and the order of unchanged arrays must remain
identical to the template at every nesting level. Generation may only replace
the documented MRS3 values and the active order-array contents; it must not
alphabetically sort strategy JSON. Canonical key sorting remains permitted only
for internal hashes and manifests.

Меняется только статистическая пригодность points/plateaus для MRS3 и последующий redundancy filter.

---

# 2. Источник Independent Events

Предполагается, что отдельный модуль/парсер уже умеет определить, к какому независимому рыночному событию относится каждая сделка MRS2.

На вход селектор получает для каждой сделки минимум:

```text
symbol
side
timeframe
point_id
trade_id
event_id
```

Опционально:

```text
entry_timestamp
exit_timestamp
```

Требование к `event_id`:

- сделки разных MRS2 points, относящиеся к одной и той же рыночной экскурсии, должны иметь одинаковый `event_id`;
- разные независимые рыночные события должны иметь разные `event_id`;
- `event_id` должен быть детерминированным.

Алгоритм формирования `event_id` находится вне этого ТЗ.

---

# 3. EventCount каждой MRS2 point

Для каждой MRS2 point:

```text
PointEventSet(point) =
    unique(event_id всех сделок этой point)
```

```text
PointEventCount(point) =
    count(PointEventSet(point))
```

Добавить поля:

```text
point_event_count
point_event_ids_hash
```

Опционально в отдельном audit-файле хранить полный список `event_id`.

---

# 4. Новый hard gate для MRS3 point

Старое временное правило:

```text
Trades >= 3 для depth-order
```

УДАЛИТЬ.

Вместо него:

```text
PointEventCount >= 3
```

Это обязательное условие для любой конкретной point, которая попадет в JSON как ордер MRS3.

То есть:

```text
event_eligible =
    economic_pass
    AND PointEventCount >= 3
```

Raw `Trades` продолжает храниться как статистика, но больше не определяет depth eligibility.

---

# 5. Standalone eligibility

Для standalone 1ORD старые sample rules v0.6 сохраняются полностью:

```text
shift <=2% -> absolute Trades floor 10
shift >2%  -> absolute Trades floor 5
+
relative MinTrades
```

Дополнительно требуется:

```text
PointEventCount >= 3
```

Итого:

```text
standalone_eligible =
    economic_pass
    AND history_pass
    AND old_v0.6_sample_pass
    AND PointEventCount >= 3
    AND point belongs to READY geometric plateau
```

Independent Events не заменяют standalone sample filter, а дополняют его.

---

# 6. Depth eligibility

Новая логика:

```text
depth_eligible =
    economic_pass
    AND PointEventCount >= 3
    AND point belongs to READY geometric plateau
```

Standalone sample floor для depth-order не применяется.

---

# 7. EventCount самого Plateau

Для каждого Plateau считать:

```text
PlateauEventSet =
    union(PointEventSet(point))
    по всем points Plateau
```

```text
PlateauEventCount =
    count(PlateauEventSet)
```

Добавить в Plateau Library:

```text
plateau_event_count
plateau_event_ids_hash
```

ВАЖНО:

`PlateauEventCount` пока является только диагностикой.

Не вводить отдельный hard gate по PlateauEventCount.

Причина:

для использования MRS3 все равно проверяется конкретная selected point с `PointEventCount >=3`.

---

# 8. Когда Plateau считается пригодным для MRS3

Геометрическая классификация Plateau остается v0.6.

Дополнительно:

```text
mrs3_usable_plateau =
    READY geometric plateau
    AND существует хотя бы одна point:
        economic_pass
        AND PointEventCount >=3
```

Если таких points нет:

```text
status = INSUFFICIENT_INDEPENDENT_EVENTS
```

Такое Plateau:

- остается в Plateau Library;
- не используется в MRS3 generation;
- не удаляется физически;
- может стать пригодным автоматически после увеличения истории.

Пример:

```text
CORE points = 36
PlateauEventCount = 1
все points имеют PointEventCount = 1
```

Результат:

```text
INSUFFICIENT_INDEPENDENT_EVENTS
```

Несмотря на 36 CORE points.

---

# 9. Plateau + CommonCloseMA

Для каждой рабочей `Plateau + CommonCloseMA`:

1. взять реальные points этого Plateau с:
   ```text
   close_ma == CommonCloseMA
   ```
2. оставить economic-pass points;
3. определить max-PnL reference как в v0.6;
4. построить 5%-equivalent group:
   - PnL difference <=5%;
   - PnL/DD difference <=5%;
5. удалить из этой группы:
   ```text
   PointEventCount <3
   ```
6. если группа пустая:
   ```text
   Plateau не поддерживает эту CommonCloseMA для MRS3
   ```
   даже если `CloseSupportRetention >=0.75`;
7. если points остались — выбрать representative.

Выбор выполняется ровно один раз для каждой комбинации
`Plateau + Pair + Side + TF + CommonCloseMA` до перебора комбинаций Plateau.
Candidate generation получает только выбранную representative point и не
перебирает остальные 5%-equivalent points этого Plateau.

---

# 10. Новый порядок выбора representative внутри 5%-group

Среди оставшихся event-eligible points:

```text
1. PointEventCount DESC
2. Shift DESC
3. PnL DESC
4. PnL/DD DESC
5. Trades DESC
6. DD ASC
7. PointID ASC
```

Логика:

- сначала подтвержденность независимыми событиями;
- затем прежнее предпочтение большего shift среди практически равноценных точек.

Не вводить отдельный score.

---

# 11. CloseMA support

`CloseSupportRetention` считать как в v0.6.

Но CommonCloseMA считается доступной для конкретного Plateau в MRS3 только если одновременно:

```text
CloseSupportRetention >=0.75
```

и

```text
существует event-eligible representative
с PointEventCount >=3
на exact CommonCloseMA
```

То есть сильная геометрическая/экономическая CloseMA не компенсирует отсутствие повторяемой конкретной entry-point.

---

# 12. Пересборка MRS3 после Event Filter

После внедрения обязательно пересобрать весь MRS3 universe с нуля.

Порядок:

```text
MRS2 points
-> v0.6 economic/history/sample calculations
-> v0.6 geometric Plateau
-> attach PointEventCount
-> mark event-eligible points
-> mark MRS3-usable Plateaus
-> rebuild CommonCloseMA representatives
-> rebuild CloseMA families
-> rebuild 2/3/4ORD combinations
-> rebuild EQUAL/INCOME JSON
```

Не использовать старые количества v0.6 как новый target.

После пересборки вывести:

```text
geometric_plateaus_total
mrs3_usable_plateaus
insufficient_event_plateaus

structures_before_event_filter
structures_after_event_filter

json_before_event_filter
json_after_event_filter
```

Также вывести распределение:

```text
PointEventCount
PlateauEventCount
```

---

# 13. Первый этап сокращения кандидатов

После Event Filter сначала проверить фактическое количество JSON.

Если объем уже приемлем — НИЧЕГО БОЛЬШЕ НЕ ФИЛЬТРОВАТЬ.

Это основной принцип минимализма.

---

# 14. Второй этап сокращения — только если кандидатов все еще слишком много

Разрешено откладывать только стратегии, которые фактически показывают одно и то же наблюдаемое entry-поведение.

Для Phase 2 candidates сравниваются внутри одной структурной группы:

```text
ComparisonKey =
    Pair
    + Side
    + TF
    + OrderCount
    + CommonCloseMA
```

`PointEventCount` и остальные order-level метрики не входят в `ComparisonKey`:
они являются критериями dominance. Полные event memberships остаются immutable
audit facts, но не дробят comparison groups. Кандидаты из разных
`ComparisonKey` не сравниваются.

Не использовать fuzzy similarity.

---

# 15. Safe redundancy filter внутри structural comparison group

Сравнивать кандидатов только внутри одного `ComparisonKey`.

Кандидат B можно перевести в:

```text
DEFERRED_REDUNDANT
```

если существует A, у которой для каждого соответствующего ордера:

```text
source PnL_A >= source PnL_B
source PnL/DD_A >= source PnL/DD_B
CloseSupport_A >= CloseSupport_B
PointEventCount_A >= PointEventCount_B
```

и хотя бы одно значение строго лучше.

Дополнительно:

```text
min CloseSupport_A >= min CloseSupport_B
```

Это обычное Pareto dominance без weighted score.

Если ни один кандидат не доминирует другой полностью — оставить оба.

---

# 16. Что делать с DEFERRED_REDUNDANT

Не удалять.

Хранить:

```text
status = DEFERRED_REDUNDANT
deferred_by = StructureID
defer_reason = SAME_STRUCTURE_DOMINATED
```

Такую стратегию можно вернуть во вторую тестовую волну.

---

# 17. Чего пока НЕ внедрять

Не добавлять сейчас:

- weighted Plateau/Event score;
- CORE-edge event confirmation;
- Jaccard similarity;
- fuzzy behavior similarity;
- `structure_event_union_count` как hard gate;
- set-cover / coverage optimizer;
- обязательное сохранение каждого exact shift vector;
- arbitrary Top-N на Pair/TF;
- fixed shortlist size типа 299/333/471;
- parameter-only dominance между разными behavior;
- event-based штрафы к lot_x;
- отдельные категории weak/medium/strong events.

Если после Event Filter + exact structural-group dominance объем все еще неприемлем — сначала показать новый отчет, затем отдельно проектировать следующий фильтр.

---

# 18. Новые статусы

## Point

```text
EVENT_ELIGIBLE
INSUFFICIENT_POINT_EVENTS
```

## Plateau

```text
GEOMETRIC_READY
MRS3_USABLE
INSUFFICIENT_INDEPENDENT_EVENTS
REFINE_REQUIRED
```

## MRS3

```text
READY_ALL
TEST_PRIMARY
DEFERRED_REDUNDANT
REJECTED_INVALID_STRUCTURE
```

Если redundancy filter не нужен, все `READY_ALL` становятся `TEST_PRIMARY`.

---

# 19. Excel / Audit

Добавить минимум два новых листа.

## `Point_Events`

```text
PointID
PlateauID
Pair
Side
TF
OpenMA
CloseMA
Shift
Trades
PointEventCount
EventEligible
EventIDsHash
```

## `Plateau_Events`

```text
PlateauID
Pair
Side
TF
CorePoints
AllPoints
PlateauEventCount
MRS3Usable
Status
```

В существующий `MRS3_Structures` добавить:

```text
Order1EventCount
Order2EventCount
Order3EventCount
Order4EventCount

ComparisonKey
Status
DeferredBy
DeferReason
```

---

# 20. Config

```yaml
event_filter:
  min_point_events: 3

redundancy_filter:
  enabled: true
  exact_comparison_key_only: true
```

`min_point_events` обязательно хранить в config, а не hardcode.

---

# 21. Unit tests

Минимальный набор.

### Event eligibility

1. Point имеет trades=20, но один `event_id`:
   ```text
   PointEventCount=1
   -> event ineligible
   ```

2. Point имеет 3 разных event_id:
   ```text
   PointEventCount=3
   -> event eligible
   ```

3. Plateau имеет 36 CORE points, но ни одна point не имеет 3 events:
   ```text
   Plateau -> INSUFFICIENT_INDEPENDENT_EVENTS
   ```

4. Plateau имеет одну point с 3 events:
   ```text
   Plateau может быть MRS3_USABLE
   ```

5. На CommonCloseMA лучший PnL point имеет 2 events, а 5%-equivalent neighbor имеет 4:
   ```text
   выбрать neighbor с 4 events
   ```

6. На CommonCloseMA все 5%-points имеют <3 events:
   ```text
   Plateau недоступно на этой CMA
   ```

### Representative

7. Две 5%-equivalent points:
   ```text
   A events=5 shift=3.5
   B events=3 shift=3.9
   ```
   выбрать A.

8. EventCount одинаков:
   выбрать больший shift.

### Structural comparison group

9. Разные OpenMA/shift и event sets, но одинаковые Pair/Side/TF/OrderCount/CommonCloseMA:
   одинаковый `ComparisonKey`.

10. Та же структура, но другая CommonCloseMA:
    разные `ComparisonKey`.

11. Одинаковый ComparisonKey, A полностью доминирует B:
    B -> `DEFERRED_REDUNDANT`.

12. У A выше PnL, но у B выше PointEventCount:
    ни один не доминирует -> оставить оба.

### Determinism

13. Повторный запуск дает те же:
    - PointEventCount;
    - selected representatives;
    - ComparisonKey;
    - statuses.

---

# 22. Порядок внедрения

## Phase 1 — обязательная

1. Подключить `event_id`.
2. Рассчитать PointEventCount.
3. Добавить `PointEventCount >=3`.
4. Перевыбрать representatives.
5. Пересобрать CMA families.
6. Пересобрать MRS3 structures.
7. Пересобрать JSON.
8. Сформировать отчет до/после.

Остановиться и посмотреть результат.

## Phase 2 — только если стратегий все еще много

9. Добавить exact `ComparisonKey`.
10. Добавить same-structure Pareto dominance.
11. Перевести dominated candidates в `DEFERRED_REDUNDANT`.
12. Снова вывести количество TEST_PRIMARY JSON.

После этого снова остановиться.

Не внедрять третий уровень фильтра без отдельного анализа нового отчета.

---

# 23. Definition of Done

Изменение считается реализованным, если:

1. большая группа CORE points, образованная одним событием, больше не создает рабочие MRS3;
2. конкретная MRS3 point имеет минимум 3 independent events;
3. standalone rules v0.6 не сломаны;
4. representatives перевыбираются в пользу большего EventCount внутри 5%-equivalent group;
5. MRS3 universe полностью пересобирается;
6. старые данные не удаляются, а остаются в audit;
7. если включен redundancy filter, откладываются только dominated candidates внутри exact `ComparisonKey`;
8. никакие weighted scores или arbitrary Top-N не используются;
9. результат детерминирован.

---

# 24. DUCKDB_DIRECT independent events и интерактивный Phase 2

Этот раздел уточняет контракт для v4/v5 HTML, загруженных в source DuckDB, и
заменяет прежнее временное поведение `DUCKDB_DIRECT`, в котором
`PointEventCount` приравнивался к `TotalTrades`.

## 24.1. Real independent events в immutable surface

При `Build surface` materializer обязан восстановить закрытые position cycles
из compact actions каждого принятого отчёта строго по контракту
`docs/specs/2026-08-10-v07-event-source-packs.md`, включая порядок actions,
неполные/пересекающие границы окна cycles и invalid-order exclusions. Для
каждого полностью попавшего в выбранное half-open UTC-окно цикла используется
существующий детерминированный `event_id`:

```text
sha256(Pair + "|" + PositionSide + "|" + TF + "|" + OpenedAtUtcNs)
```

Для каждой point:

```text
PointEventSet = unique(event_id)
PointEventCount = size(PointEventSet)
OrderEventSignature = sha256(sorted(PointEventSet))
event_mode = real_independent_events
```

`OpenedAtUtcNs` — целое число nanoseconds from Unix epoch, полученное после
преобразования в `pandas.Timestamp` и нормализации в UTC. Поэтому текстовые
представления одного instant (`Z`, `+00:00`, лишние нули fraction) дают одну
строку hash input. Общий event-id helper используется и DuckDB package, и
`DUCKDB_DIRECT`. Этот operational event contract для текущего этапа уточняет
раздел 2: independent event здесь означает один reconstructed closed cycle с
тем же Pair/PositionSide/TF и exact opening instant. Более широкая кластеризация
нескольких циклов в одну market excursion является отдельной будущей
калибровкой и в этот scope не входит.

`TotalTrades` остаётся независимо пересчитанной window-метрикой и не является
источником `PointEventCount`, даже если их значения совпали. Проверка window
metrics выполняется по ADR
`docs/decisions/0002-source-summary-and-window-metrics-verification.md` и тому
же `calculate_point_metrics`, который использует DuckDB package. Ошибка
декодирования, reconstruction или нарушения структурных инвариантов блокирует
публикацию; расхождение с full-horizon HTML summary само по себе не является
ошибкой для другого UTC-окна.

Analysis DuckDB хранит membership событий как immutable facts, связанные с
`surface_id` и `canonical_point_key`. Существующие опубликованные
`legacy_trades_proxy` surface не изменяются задним числом. Для применения
нового контракта пользователь повторяет `Build surface` и analysis run из уже
импортированного source DuckDB; повторный HTML-импорт не требуется.
`event_mode` входит в canonical surface identity. Переход на этот контракт
обязан увеличить `materializer_version` и изменить
`point_materialization_config_hash`, поэтому real-events build всегда получает
новый `surface_id` и не может дедуплицироваться в legacy surface.

## 24.2. Неразрушающее применение Phase 2

Phase 2 является воспроизводимым представлением immutable analysis run и не
перезаписывает сохранённые candidates. Structural `ComparisonKey` всегда является
обязательной границей сравнения. Кандидаты из разных `ComparisonKey` никогда не
сравниваются.

Панель предоставляет независимые checkbox-критерии:

- `Source PnL`;
- `PnL/DD`;
- `CloseSupport`;
- `PointEventCount`.

`PointEventCount` сравнивается как ordered vector внутри `ComparisonKey` и может
самостоятельно откладывать dominated candidates. Event IDs не используются как
граница сравнения.

При выбранном наборе критериев сравниваются ordered orders A и B с одинаковыми
позициями внутри общего `ComparisonKey`. Для каждого включённого критерия A
должен быть не хуже B **на каждом соответствующем order**. Для
`CloseSupport` дополнительно явно сравнивается minimum по всем orders. Хотя бы
одно из всех сравниваемых order-level значений должно быть строго лучше.
Суммирование или усреднение `Source PnL` orders запрещено: `Source PnL`
сравнивается только как ordered vector `Order1 A >= Order1 B`, `Order2 A >=
Order2 B` и так далее. Aggregate strategy PnL не вычисляется и не участвует ни
в dominance, ни в criterion sheets XLS.

В терминах раздела 18 исходный persisted `READY_MRS3_STRUCTURE` соответствует
`READY_ALL`. Если условие выполнено, view-level `filter_status` B становится
`DEFERRED_REDUNDANT`; в противном случае он остаётся
`READY_AFTER_FILTERS`, что соответствует `TEST_PRIMARY`. Исходный сохранённый status
`READY_MRS3_STRUCTURE` не изменяется. Если B доминируют несколько candidates,
`deferred_by` равен `StructureID` лексикографически минимальной пары
`(StructureID, candidate_id)` среди всех валидных dominators. При отсутствии
выбранных критериев все исходные READY
candidates получают `READY_AFTER_FILTERS`. Результат обязан содержать
`deferred_by`, `deferred_by_candidate_id`, список включённых критериев и
сравниваемые order-level значения A/B. Удаление candidates запрещено.

Один включённый критерий является диагностическим single-dimension view.
Одновременное включение всех четырёх критериев является safe Pareto filter из
раздела 15. Улучшение по одному критерию не компенсирует ухудшение по другому:
такие candidates остаются `READY_AFTER_FILTERS`. Генерация JSON использует
только `READY_AFTER_FILTERS` текущего представления, но исходный analysis run
остаётся неизменным.

## 24.3. XLS audit выбранных фильтров

Панель предоставляет `Export filter audit XLS`. Workbook создаётся для
конкретных `run_id`, набора критериев и версии алгоритма и содержит минимум:

- `Summary` — входное количество, активные критерии, READY и deferred counts;
- `READY_AFTER_FILTERS` — итоговый список оставшихся candidates;
- отдельный лист для каждого включённого критерия — candidates, которые этот
  критерий отложил бы при самостоятельном применении внутри exact
  `ComparisonKey`;
- `DEFERRED_COMBINED` — фактически отложенные текущей комбинацией критериев.

Каждая строка исключения содержит candidate ID, `ComparisonKey`, `deferred_by`,
критерий или комбинацию критериев, значения A/B по всем четырём измерениям и
детерминированную причину. Листы создаются в перечисленном выше порядке,
criterion sheets — в порядке `Source PnL`, `PnL/DD`, `CloseSupport`,
`PointEventCount`, а строки каждого листа сортируются по `ComparisonKey`, затем
`candidate_id`. Даже пустой включённый criterion sheet содержит фиксированные
headers. Числа экспортируются как числа без предварительного округления.
Повторный export с теми же `run_id`, algorithm version и criteria обязан давать
эквивалентные табличные данные; byte-identical XLSX не требуется.

## 24.4. Дополнительные acceptance tests

1. Две points одного Pair/Side/TF с одинаковым временем открытия получают
   общий `event_id`, даже если их shift/MA различаются.
2. `PointEventCount` равен числу уникальных восстановленных циклов, а не слепо
   скопированному `TotalTrades`.
3. Эквивалентные timestamp representations дают одинаковый `event_id`, а
   legacy и real-events builds имеют разные `surface_id`.
4. Published surface загружает exact event membership без обращения к source
   DuckDB.
5. `ComparisonKey` не меняется при изменении event membership; при этом может
   измениться order-level `PointEventCount`.
6. Каждый checkbox работает отдельно; комбинация требует одного общего
   dominator A, не худшего по каждому order-level значению всех включённых
   критериев.
7. Если A лучше по одному включённому критерию, но хуже по другому, ни A, ни B
   не откладываются этой парой.
8. Фильтр не сравнивает разные ComparisonKey и не изменяет analysis run.
9. При нескольких dominators `deferred_by` выбирается детерминированно.
10. XLS содержит отдельные criterion sheets и итоговый combined sheet с полным
    audit trail.

## 24.5. Агрегированный shortlist в панели

Панель не отображает candidate-level описания и индивидуальные checkbox. Для
каждого `Pair + TF` она показывает только количества структур на 2, 3 и 4
ордера, а также итоговые `READY`, `DEFERRED` и `ALL` для текущего набора Phase 2
критериев. Pair и TF доступны как необязательные scope-фильтры.

`Generate READY JSON` не принимает выбранные в браузере candidate IDs. Сервер
повторно строит неразрушающее shortlist-представление для тех же criteria,
выбирает все `READY_AFTER_FILTERS` внутри текущего Pair/TF scope и передаёт их
существующему генератору. Пустой scope означает все Pair/TF выбранного run.
Полный candidate-level audit остаётся только в XLS export.

## 25. Post-test position holding audit

`posttest` derives position holding only from the tester row's immutable
`trades_json`. A position cycle starts at the first `opened` action for one
`strategy_name + symbol + position_side` and ends only at `Action=closed` with
numeric `Post Size=0`. `increased` and `decreased` actions never end or restart
the cycle. A `closed` action is assigned to LONG for `Side=sell` and SHORT for
`Side=buy`.

`posttest.xlsx` and `posttest_csv` contain `19_Position_Holding_Cycles` and
`20_Position_Holding_Exclusions`. The final comparison carries the number of
full cycles, mean/median/p95 holding minutes, time-in-market percentage and
the number of exclusions. Missing, malformed, overlapping or unclosed actions
are audit exclusions and are never guessed.

Holding metrics are diagnostic in this version. They do not alter DD5 Pareto or
ranking until their computation has been checked against real tester reports.

`18_Final_Comparison` exposes the immutable entry `shift_bp_vector` recovered
from each strategy's active MRS3 multipliers. Its report-facing numeric values,
including `lots` and `scaled_lots`, are rounded to two decimals. Normalized
values remain unrounded for DD5 calculations, Pareto selection and audit.

The final comparison also exposes independently filterable, scoped Pareto
flags. Each compares all order counts within the same `symbol + side + timeframe`.
All maximize `pnl30_dd5`; the variants respectively minimize capital proxy,
holding p95, close MA, maximize the first entry shift, or apply all four
objectives together. `order_count` is descriptive only and never prevents a
1ORD strategy from dominating or being dominated by a 2–4ORD strategy. These
are diagnostic alternative fronts and do not change
the primary DD5 ranking.

Before the tester batch, JSON generation derives up to three 1ORD structures
independently for every selected `Pair + Side + TF` scope from standalone-ready
points. Selecting `All` timeframes never applies one global top-three limit.
Within each scope it ranks by DD5 PnL descending, raw DD ascending,
`PointEventCount` descending, first shift descending, then point ID. The
resulting 1ORD JSON are tested alongside 2–4ORD strategies and enter post-test
Pareto only from their real tester results.

## 26. Sequential post-test selection

The final post-test selection is calculated independently inside each
`symbol + side + timeframe`; candidate counts from different timeframes are
never combined. Before any selection Pareto, an adverse-only Tukey IQR filter
rejects a row when `holding_p95_minutes > Q3 + 1.5 * IQR` or
`trades < Q1 - 1.5 * IQR`. Low holding and high trade counts are never rejected
as outliers.

Eligible rows pass through these stages in order:

1. maximize `pnl30_dd5` and minimize `capital_requirement_proxy`;
2. among stage-1 survivors, maximize `capital_efficiency_30` and
   `first_shift_bp`;
3. only when stage 2 leaves more than three rows in that same scope, maximize
   `capital_efficiency_30` and minimize `common_close_ma`.

When stage 2 leaves three or fewer rows, all are final. Stage 3 retains its
entire Pareto front and does not force a top-three truncation. The final sheet
exposes scope thresholds, filter eligibility, every stage flag, the final flag
and an exclusion reason. Existing independent Pareto flags remain diagnostic
and do not define this sequential result.

# MRS3 v0.7 — минимальное ТЗ: Independent Events + безопасное сокращение кандидатов

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
- DD5-normalization и post-test comparison.

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

Для каждого selected order хранить:

```text
OrderEventSignature =
    hash(sorted(PointEventSet(selected_point)))
```

Для MRS3:

```text
BehaviorKey =
    Pair
    + Side
    + TF
    + OrderCount
    + CommonCloseMA
    + ordered OrderEventSignature[]
```

Две стратегии считаются `same_behavior` только если их `BehaviorKey` полностью одинаков.

Не использовать fuzzy similarity.

---

# 15. Safe redundancy filter внутри same_behavior

Сравнивать кандидатов только внутри одной `BehaviorKey`.

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
defer_reason = SAME_BEHAVIOR_DOMINATED
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

Если после Event Filter + exact same-behavior dominance объем все еще неприемлем — сначала показать новый отчет, затем отдельно проектировать следующий фильтр.

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

BehaviorKey
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
  exact_same_behavior_only: true
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

### Same behavior

9. Разные OpenMA/shift, но identical order event sets и CommonCloseMA:
   одинаковый `BehaviorKey`.

10. Те же event sets, но другая CommonCloseMA:
    разные `BehaviorKey`.

11. Одинаковый BehaviorKey, A полностью доминирует B:
    B -> `DEFERRED_REDUNDANT`.

12. У A выше PnL, но у B выше PointEventCount:
    ни один не доминирует -> оставить оба.

### Determinism

13. Повторный запуск дает те же:
    - PointEventCount;
    - selected representatives;
    - BehaviorKey;
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

9. Добавить exact `BehaviorKey`.
10. Добавить same-behavior Pareto dominance.
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
7. если включен redundancy filter, откладываются только exact same-behavior dominated candidates;
8. никакие weighted scores или arbitrary Top-N не используются;
9. результат детерминирован.

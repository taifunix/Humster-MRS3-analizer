# MRS3 v0.7 — ТЗ на обработку результатов большой MRS3-выборки

> Superseded by the Performance DB v2 and RETEST contracts; retained for historical provenance only. The removed posttest workflow is not a current runtime path.
## Калибровка безопасного pre-test фильтра по Source PnL

## 0. Цель

Текущую большую пачку MRS3-кандидатов (~2000 JSON) использовать как **калибровочную выборку**.

На этой пачке нужно эмпирически установить:

1. насколько фактический MRS3 PnL связан с PnL исходных MRS2 entry-points;
2. насколько фактический MRS3 PnL@DD5 связан с теми же source-метриками;
3. можно ли получить достаточно устойчивый **оптимистический верхний предел** результата будущей MRS3 до tick-test;
4. если предел устойчив — использовать его для `DEFER_LOW_POTENTIAL` в будущих отборах.

ВАЖНО:

`sum(source PnL)` НЕ считать математически доказанным потолком MRS3.

Это ТЗ должно проверить это эмпирически.

Текущую калибровочную пачку нельзя заранее сокращать новым Source-PnL фильтром: сначала ее нужно протестировать максимально полно, иначе получится selection bias.

---

# 1. Требуемые входы

Нужны два набора данных.

## 1.1. Candidate Manifest — ДО MRS3-тестирования

Для каждого JSON:

```text
strategy_id
structure_id
symbol
side
timeframe
common_close_ma
order_count
lot_method            # EQUAL / INCOME

order_1_point_id
order_1_plateau_id
order_1_shift
order_1_open_ma
order_1_source_pnl
order_1_source_dd
order_1_source_eff
order_1_event_count
order_1_lot_x

... order_2 ...
... order_3 ...
... order_4 ...

initial_total_lot_x   # ожидается 1.0

effective_days
test_period_start
test_period_end
algorithm_version
```

`source_pnl`, `source_dd`, `source_eff` — метрики именно тех конкретных MRS2 points, которые вошли в JSON, на выбранной CommonCloseMA.

---

## 1.2. MRS3 Test Results — ПОСЛЕ тестирования

Для каждого `strategy_id`:

```text
actual_pnl_pct
actual_dd_pct
actual_winrate_pct
actual_pf
actual_trades

test_period_start
test_period_end

test_status
```

Если тест не завершен/испорчен:

```text
test_status != OK
```

такую строку не использовать в калибровке, но обязательно оставить в audit.

---

# 2. Join и hard validation

Соединение только по `strategy_id`.

Перед анализом проверить:

```text
symbol / side / TF совпадают
test periods source и MRS3 совместимы
actual_dd_pct > 0
initial_total_lot_x ≈ 1.0
все используемые source points существуют
lot_x >= 0
sum(lot_x) ≈ 1.0
```

Если source MRS2 и MRS3 относятся к несовместимым периодам:

```text
CALIBRATION_PERIOD_MISMATCH
```

и строку не использовать для определения коэффициентов.

---

# 3. Source-метрики каждой MRS3

Для N ордеров рассчитать.

## 3.1. SourcePnLSum

```text
SourcePnLSum =
    Σ source_pnl_i
```

Это намеренно очень щедрая невзвешенная сумма.

---

## 3.2. WeightedSourcePnL

```text
WeightedSourcePnL =
    Σ (lot_x_i * source_pnl_i)
```

Это более естественный линейный baseline для конкретного lot allocation.

Рассчитывать отдельно для каждого JSON, поэтому EQUAL и INCOME могут иметь разные значения.

---

## 3.3. SourcePnLMax / Min

```text
SourcePnLMax = max(source_pnl_i)
SourcePnLMin = min(source_pnl_i)
```

Только диагностика.

---

## 3.4. SourceEffMean / Min

```text
SourceEffMean = mean(source_pnl_i / source_dd_i)
SourceEffMin  = min(source_pnl_i / source_dd_i)
```

Только диагностика.

---

# 4. Actual-метрики

## 4.1. Raw

```text
ActualPnL = actual_pnl_pct
ActualDD  = actual_dd_pct
```

---

## 4.2. Теоретический DD5 результат

Для анализа всей большой пачки использовать:

```text
ActualPnL_DD5_Theoretical =
    ActualPnL * 5 / ActualDD
```

Это НЕ заменяет будущий фактический DD5-retest финалистов.

Здесь эта величина нужна только как единая risk-normalized диагностическая метрика для калибровки pre-test фильтра.

---

## 4.3. Теоретическая capital efficiency при DD5

Так как initial `Σlot_x = 1`:

```text
k = 5 / ActualDD
TotalLot_DD5_Theoretical = k

CapitalRequirementProxy =
    TotalLot_DD5_Theoretical + 0.05
```

```text
CapitalEfficiency_DD5_Theoretical =
    ActualPnL_DD5_Theoretical
    / CapitalRequirementProxy
```

Это диагностический proxy, пока нет фактического margin-time series.

---

# 5. Основные conversion ratios

Для каждой стратегии:

## 5.1. От невзвешенной суммы source PnL

```text
K_RAW_SUM =
    ActualPnL / SourcePnLSum
```

```text
K_DD5_SUM =
    ActualPnL_DD5_Theoretical / SourcePnLSum
```

```text
K_CAPEFF_SUM =
    CapitalEfficiency_DD5_Theoretical / SourcePnLSum
```

---

## 5.2. От WeightedSourcePnL

```text
K_RAW_WEIGHTED =
    ActualPnL / WeightedSourcePnL
```

```text
K_DD5_WEIGHTED =
    ActualPnL_DD5_Theoretical / WeightedSourcePnL
```

```text
K_CAPEFF_WEIGHTED =
    CapitalEfficiency_DD5_Theoretical / WeightedSourcePnL
```

Если denominator <= 0 — ratio не рассчитывать и строку пометить.

---

# 6. Что нужно выяснить на калибровочной выборке

Для каждого из шести коэффициентов вывести:

```text
N
min
median
p75
p90
p95
p99
max
```

Дополнительно показать статистику по:

```text
OrderCount = 2 / 3 / 4
LotMethod  = EQUAL / INCOME
Pair
TimeFrame
```

Но НЕ создавать отдельные production thresholds для каждой группы автоматически.

Сначала проверить, нужен ли вообще такой уровень детализации.

---

# 7. Главный вопрос №1: является ли SourcePnLSum практически верхней границей Raw PnL

Отдельно вывести:

```text
count(ActualPnL > SourcePnLSum)
max(ActualPnL / SourcePnLSum)
```

и таблицу всех случаев:

```text
K_RAW_SUM > 1.0
```

Если таких случаев нет — это сильный эмпирический результат, но все равно не объявлять его математической теоремой.

Также проверить пороги:

```text
K_RAW_SUM > 0.50
K_RAW_SUM > 0.75
K_RAW_SUM > 0.90
K_RAW_SUM > 1.00
```

---

# 8. Главный вопрос №2: какой source predictor лучше ограничивает DD5-потенциал

Сравнить два простых predictors:

```text
PREDICTOR_A = SourcePnLSum
PREDICTOR_B = WeightedSourcePnL
```

Для каждого смотреть:

```text
K_DD5 max
K_DD5 p99
разброс по Pair
разброс по OrderCount
разброс по LotMethod
```

Предпочтение отдавать predictor, у которого верхняя граница:

- ниже;
- стабильнее между парами;
- не требует большого числа отдельных правил.

Минимализм имеет приоритет.

Если `SourcePnLSum` дает достаточно стабильный cap — использовать его, даже если Weighted predictor немного точнее.

---

# 9. Валидация upper bound без leakage

Нельзя просто взять `max()` на всей выборке и объявить это будущим hard cap.

Нужно сделать **Leave-One-Pair-Out validation**.

Для каждого Symbol:

1. все стратегии данного Symbol = HOLDOUT;
2. остальные Symbols = TRAIN;
3. на TRAIN найти:
   ```text
   train_max_K
   ```
4. построить простой conservative limit:
   ```text
   train_limit_K = train_max_K * 1.10
   ```
5. проверить HOLDOUT:
   ```text
   holdout_max_K <= train_limit_K ?
   ```

Сделать отдельно для:

```text
K_RAW
K_DD5
K_CAPEFF
```

и для обоих predictors.

Почему Pair-level split:

EQUAL/INCOME и множество близких structures одной пары сильно зависимы между собой. Случайный row split даст ложное ощущение надежности.

---

# 10. Условие признания cap пригодным

Для конкретного predictor + target metric cap можно рекомендовать для будущего отбора только если:

```text
во всех Leave-One-Pair-Out folds:
holdout_max_K <= train_max_K * 1.10
```

То есть ни одна полностью невиданная в train пара не пробила 10%-ный запас.

Если условие не выполняется:

```text
CAP_NOT_STABLE
```

и этот cap пока НЕ использовать как автоматический filter.

10% safety buffer должен быть параметром config:

```yaml
calibration:
  upper_bound_safety_margin: 0.10
```

---

# 11. Финальный коэффициент после успешной валидации

Если cap прошел LOPO-validation:

```text
FinalKCap =
    global_max_K * 1.10
```

Сохранить отдельно:

```text
K_RAW_CAP
K_DD5_CAP
K_CAPEFF_CAP
```

для выбранного predictor.

Пример структуры результата:

```yaml
mrs3_pretest_potential:
  calibrated: true
  calibration_version: "2026-08-batch-1"
  predictor: "SourcePnLSum"
  sample_size: 1834
  symbols: 12
  safety_margin: 0.10

  k_raw_cap: ...
  k_dd5_cap: ...
  k_capeff_cap: ...

  source_data_hash: ...
```

Не придумывать значения до фактического расчета.

---

# 12. Как применять cap к БУДУЩИМ кандидатам

Для будущей MRS3 до tick-test:

```text
SourcePotential = выбранный predictor
```

Рассчитать оптимистические оценки:

```text
OptimisticRawPnL =
    SourcePotential * K_RAW_CAP
```

```text
OptimisticPnL_DD5 =
    SourcePotential * K_DD5_CAP
```

```text
OptimisticCapitalEfficiency =
    SourcePotential * K_CAPEFF_CAP
```

Это не прогноз.

Это **эмпирический optimistic upper bound**, построенный по калибровочной выборке + safety margin.

---

# 13. Как использовать optimistic bound для фильтра

Новый фильтр не должен говорить:

```text
REJECT_FOREVER
```

Использовать:

```text
DEFER_LOW_SOURCE_POTENTIAL
```

Кандидат откладывается до второй волны, только если он заведомо не проходит заданные минимальные цели даже по optimistic bounds.

Рекомендуемый интерфейс:

```yaml
pretest_filter:
  min_interesting_raw_pnl: null
  min_interesting_dd5_pnl: null
  min_interesting_capital_efficiency: null
```

Применять только те targets, которые явно заданы.

---

# 14. Минималистичный production-rule

Если основной objective на данном этапе — PnL@DD5:

```text
IF OptimisticPnL_DD5 < MinInterestingPnL_DD5
THEN DEFER_LOW_SOURCE_POTENTIAL
```

Например `MinInterestingPnL_DD5` можно задавать как:

- заранее установленный минимально интересный PnL@DD5;
- либо DD5 результат лучшей доступной 1ORD benchmark для этой Pair.

Не зашивать этот выбор в analyzer.

Analyzer только рассчитывает calibrated cap.

---

# 15. Более безопасный rule при учете capital efficiency

Если используются одновременно PnL@DD5 и капитальная эффективность:

откладывать только если кандидат не способен пройти НИ ОДНУ цель:

```text
OptimisticPnL_DD5 < MinInterestingPnL_DD5
AND
OptimisticCapitalEfficiency < MinInterestingCapitalEfficiency
```

То есть высокий потенциальный capital efficiency может спасти кандидата с меньшим DD5 PnL.

---

# 16. Не смешивать калибровку и реальный DD5-retest

Большая пачка дает:

```text
ActualPnL_DD5_Theoretical
```

из raw MRS3 result.

Финальные лучшие стратегии позже по-прежнему должны проходить:

```text
lot scaling -> реальный tick retest
```

Чтобы оценить нелинейность масштабирования.

После накопления достаточного количества фактических DD5-retests можно создать Calibration v2 уже на actual DD5 results.

---

# 17. Required output — Excel/CSV

Создать минимум:

## `Calibration_All`

Одна строка = один протестированный JSON.

Поля:

```text
strategy_id
structure_id
symbol
TF
ORD
lot_method

SourcePnLSum
WeightedSourcePnL

ActualPnL
ActualDD
ActualPnL_DD5_Theoretical
CapitalEfficiency_DD5_Theoretical

K_RAW_SUM
K_DD5_SUM
K_CAPEFF_SUM

K_RAW_WEIGHTED
K_DD5_WEIGHTED
K_CAPEFF_WEIGHTED
```

---

## `Calibration_Summary`

Статистики:

```text
metric
predictor
N
median
p90
p95
p99
max
```

---

## `LOPO_Validation`

```text
metric
predictor
holdout_symbol
train_N
holdout_N
train_max_K
train_limit_K
holdout_max_K
passed
max_violation
```

---

## `Outliers`

Все строки, которые:

```text
K_RAW_SUM > 1
OR
K_DD5_* > p99
OR
K_CAPEFF_* > p99
```

С полным набором source/actual данных.

---

## `Recommended_Config`

Финальный машинно-читаемый блок:

```text
calibrated true/false
chosen_predictor
sample_N
symbol_N
safety_margin
K_RAW_CAP
K_DD5_CAP
K_CAPEFF_CAP
validation_pass
```

---

# 18. Дополнительные sanity checks

Обязательно проверить:

1. нет ли PnL/DD выброса из-за `ActualDD` около нуля;
2. нет ли битых MRS3 reports;
3. source и MRS3 период совпадают;
4. нет ли повторно импортированных одного и того же `strategy_id`;
5. EQUAL и INCOME не смешаны случайно;
6. один structure может иметь два lot variants — это нормально;
7. вся калибровка использует только реально завершенные тесты;
8. никакая строка не исключается только потому, что она выглядит как outlier.

Outlier нужно исследовать, а не автоматически удалять.

---

# 19. Что НЕ делать

Не строить сейчас:

- ML model;
- regression score;
- neural network;
- weighted multi-factor score;
- отдельные пороги по каждому TF без необходимости;
- отдельные пороги по каждой Pair;
- фильтр по median/p95 как будто это hard ceiling;
- автоматическое удаление outliers;
- random train/test split.

Цель — получить простой, проверяемый upper-bound coefficient.

---

# 20. Порядок работы после завершения ~2000 тестов

1. Импортировать все MRS3 results.
2. Join с Candidate Manifest.
3. Рассчитать source predictors.
4. Рассчитать Actual Raw / DD5 / CapitalEfficiency.
5. Рассчитать conversion ratios.
6. Проверить, превышал ли ActualPnL `SourcePnLSum`.
7. Сравнить SourcePnLSum vs WeightedSourcePnL.
8. Выполнить Leave-One-Pair-Out.
9. Определить, существует ли стабильный cap.
10. Если да — записать `Recommended_Config`.
11. НЕ внедрять его в selector автоматически без просмотра отчета.
12. После ручного подтверждения включить `DEFER_LOW_SOURCE_POTENTIAL` для будущих batch.

---

# 21. Definition of Done

Калибратор считается готовым, если после подачи завершенной большой MRS3-пачки он однозначно отвечает:

1. Каков максимальный наблюдавшийся:
   ```text
   ActualPnL / SourcePnLSum
   ```
2. Были ли случаи:
   ```text
   ActualPnL > SourcePnLSum
   ```
3. Каков максимальный наблюдавшийся:
   ```text
   ActualPnL_DD5 / Source predictor
   ```
4. Какой простой predictor лучше подходит для optimistic cap.
5. Проходит ли этот cap Leave-One-Pair-Out с 10% запасом.
6. Какой `K_*_CAP` рекомендуется записать в config.
7. Сколько будущих кандидатов было бы отложено при выбранном `MinInteresting...` threshold.
8. Какие кандидаты были бы отложены ошибочно, если применить правило ретроспективно к текущей калибровочной пачке.

Последний пункт обязателен:

после определения cap нужно прогнать его назад по всей калибровочной выборке и показать:

```text
would_defer_count
would_keep_count

best_actual_result_among_deferred
best_actual_dd5_among_deferred
best_actual_capeff_among_deferred
```

Если среди `would_defer` остается стратегия, которая по факту была бы финальным конкурентом, production threshold необходимо ослабить.

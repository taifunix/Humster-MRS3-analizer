# Техническое задание: MRS3 selector / generator
## Implementation baseline v0.6
Дата: 2026-08-08

Цель: реализовать воспроизводимую обвязку, которая принимает MRS2 CSV + listing dates + MRS3 JSON template и выдает Plateau Library, MRS3 JSON-кандидаты, audit workbook и таблицы для последующей DD5-нормализации.

Документ должен быть достаточен для реализации в новой сессии без знания истории обсуждения.

---

# A. Требуемые артефакты реализации

Минимально:

1. `config.yaml` или `config.json`
2. `mrs2_loader`
3. `history_normalizer`
4. `filters`
5. `refine_detector`
6. `plateau_builder`
7. `close_profile_builder`
8. `one_order_selector`
9. `mrs3_family_builder`
10. `lot_allocator`
11. `json_generator`
12. `json_validator`
13. `audit_exporter`
14. `posttest_normalizer`
15. CLI / entrypoint
16. unit tests

Допустим Python или TypeScript. Логика должна быть детерминированной и покрытой тестами.

---

# B. Конфигурация

```yaml
algorithm_version: "v0.6"

timeframes:
  - 5m
  - 15m
  - 30m
  - 45m
  - 1h
  - 2h
  - 3h
  - 4h

min_effective_days: 7

economic:
  min_pnl: 0
  min_winrate: 70
  max_dd: 11
  min_pnl_dd: 3

absolute_trade_floor:
  shift_le_2: 10
  shift_gt_2: 5

base_rate_tf:
  5m: 2.27
  15m: 1.82
  30m: 1.59
  45m: 1.36
  1h: 1.36
  2h: 1.14
  3h: 0.91
  4h: 0.91

shift_factor:
  - {min: 0.3, max: 1.5, value: 1.00}
  - {min_exclusive: 1.5, max: 2.0, value: 0.90}
  - {min_exclusive: 2.0, max: 3.1, value: 0.30}
  - {min_exclusive: 3.1, max: 4.7, value: 0.20}

plateau:
  core_link_min: 0.90
  envelope_min: 0.75
  equivalent_tolerance: 0.05

close_support:
  core_min: 0.90
  supported_min: 0.75

gap:
  lower_shift_lt_1_5: 0.6
  lower_shift_1_5_to_4: 0.8
  lower_shift_gt_4: null

close_multiplier:
  long: 1.003
  short: 0.997

shift_domain:
  min: 0.3
  max: configurable

initial_lot_sum: 1.0
target_dd: 5.0
```

`30m`, shift factors и часть порогов помечаются в audit как параметры, подлежащие будущей рекалибровке.

---

# C. Нормализованная модель точки MRS2

```typescript
type Side = "LONG" | "SHORT";

interface MRS2Point {
  pointId: string;
  symbol: string;
  side: Side;
  timeframe: string;

  openMa: number;
  closeMa: number;
  shiftPct: number;

  pnlPct: number;
  ddPct: number;
  winRatePct: number;
  profitFactor: number | null;
  trades: number;

  reportStart: string;
  reportEnd: string;
  listingDate: string;
  effectiveStart: string;
  effectiveDays: number;

  efficiency: number;            // pnlPct / ddPct
  pnlDd5Theoretical: number;     // pnlPct * 5 / ddPct

  economicPass: boolean;
  relativeMinTrades: number | null;
  absoluteMinTrades: number;
  requiredMinTrades: number | null;
  standaloneSamplePass: boolean;
  historyPass: boolean;

  refineRequired: boolean;
  missingTests: MissingTest[];

  plateauId: string | null;
  standaloneEligible: boolean;
  depthEligible: boolean;

  rejectReason?: string;
}
```

Для `shift > 4.7` `relativeMinTrades = null` до отдельной калибровки. Такая точка не должна автоматически получать `standaloneSamplePass=true` только по придуманному factor.

---

# D. Загрузка и аудит входа

## D1. CSV mapping

Сделать mapping имен колонок конфигурируемым.

Для текущих Bybit CSV известны примеры:

- symbol
- timeframe
- open MA
- close MA
- multiplier
- `TotalPnLPercent`
- `MaxDrawdownPercent`
- `WinRate`
- `ProfitFactor`
- `TotalTrades`

Shift:

LONG:
`shift = (1 - openMultiplier) * 100`

SHORT:
`shift = (openMultiplier - 1) * 100`

Округлять только для идентификации сетки с заранее заданным tolerance; внутренние вычисления хранить в float/decimal.

## D2. Service rows

Строки без symbol / обязательных параметров исключать как service rows и считать отдельно.

## D3. Missing grid

Построить фактически имеющуюся сетку:

`Symbol × TF × CloseMA × OpenMA × Shift`

Ничего не интерполировать.

Каждый отсутствующий cell фиксировать.

---

# E. Effective history

```text
effectiveStart = max(reportStart, listingDate)
effectiveDays = (reportEnd - effectiveStart) / 86400
```

Если `<7`:

`historyPass=false`

Точка может быть сохранена в audit, но не получает standalone eligibility.

---

# F. Economic filter

```text
economicPass =
  pnlPct > 0
  AND ddPct > 0
  AND winRatePct >= 70
  AND ddPct <= 11
  AND pnlPct/ddPct >= 3
```

Если `ddPct <= 0`, efficiency/DD5 normalization undefined; пометить отдельной причиной.

---

# G. Sample filter

## G1. Absolute floor

```text
shift <= 2.0 => 10
shift > 2.0  => 5
```

## G2. Relative

```text
relative = ceil(effectiveDays * baseRate(timeframe) * shiftFactor(shift))
required = max(relative, absoluteFloor)
```

Для shift >4.7 relative пока undefined.

## G3. Point flags

```text
standaloneSamplePass = trades >= required
```

`depthEligible` не зависит от standalone sample floor, но требует economicPass + membership in READY plateau.

---

# H. Refine detector / геометрия соседей

Функция:

```text
requiredShiftNeighbors(centerShift, testedDomain, config) -> set<shift>
```

Правила:

### H1. center <1.5
Нужна fine-grid 0.1 в радиусе ±0.3, обрезанная только нижней границей 0.3 и верхней configured shift_domain.max.

### H2. center 1.5..1.7
Вниз радиус 0.3, вверх 0.5.

### H3. center >1.7
Использовать все фактически протестированные shift values в ±0.5.
Если внутри declared search domain в направлении нет ни одного значения на расстоянии <=0.5 — сформировать missing test request.

### H4. center=0.3
Не требовать значений ниже 0.3.

Для каждого shift-neighbor проверять MA-grid:

```text
openMa in centerOpen±1
closeMa in centerClose±1
```

с обрезкой по реально заданным MA bounds.

Если нужный cell отсутствует:

`refineRequired=true`

Не угадывать результат.

---

# I. Геометрическая соседность двух точек

```text
abs(openMaA-openMaB) <= 1
abs(closeMaA-closeMaB) <= 1
AND shiftNeighbor(A.shift, B.shift) == true
```

`shiftNeighbor` должен учитывать fine/boundary/coarse rules симметрично. Связь допускается только если обе точки принадлежат допустимым окнам друг друга.

---

# J. Plateau builder

В plateau graph участвуют только:

- `economicPass=true`;
- достаточные source data для финальной оценки (`refineRequired=false`).

Sample floor не удаляет точки из graph.

## J1. Edge metrics

```text
pnlRetention = min(pnlA,pnlB)/max(pnlA,pnlB)
effRetention = min(effA,effB)/max(effA,effB)
coreLink = min(pnlRetention, effRetention)
```

CORE edge:

`coreLink >= 0.90`

## J2. Component envelope

Для любого набора S:

```text
pnlEnvelope = min(S.pnl)/max(S.pnl)
effEnvelope = min(S.eff)/max(S.eff)
envelope = min(pnlEnvelope, effEnvelope)
```

Требование:

`envelope >=0.75`

## J3. Алгоритм

1. Построить все CORE edges.
2. Сортировать:
   - `coreLink DESC`;
   - затем стабильный `(pointIdA, pointIdB)`.
3. Union-Find:
   - перед merge вычислить envelope объединения;
   - merge только если envelope >=0.75.
4. CORE-component считается основой plateau, если core-size >=2.
5. После этого рассмотреть экономически допустимые singleton/border points:
   - point должен быть геометрическим соседом минимум одного CORE-member;
   - max local retention to CORE >=0.75;
   - добавление сохраняет overall envelope >=0.75.
6. Attached supported point не используется как источник для дальнейшего bridge.
7. Если point подходит нескольким plateau:
   - выбрать max local support;
   - затем plateau max PnL;
   - затем stable plateau ID.
8. Singleton без CORE-link не становится READY plateau.

Назначить стабильный `plateau_id`, например hash от:
`symbol|side|tf|sorted(core point ids)`.

---

# K. Isolated peaks

Если singleton:

- economic pass;
- standalone sample pass;
- history pass;
- нет READY plateau;

и:

```text
pnl >= 0.90 * bestPnLWithinPairTF
OR
eff >= 0.90 * bestEffWithinPairTF
```

то сохранить `ISOLATED_PEAK`.

Не использовать в MRS3 автоматически.

---

# L. Plateau model

```typescript
interface Plateau {
  plateauId: string;
  symbol: string;
  side: Side;
  timeframe: string;

  corePointIds: string[];
  supportedPointIds: string[];
  allPointIds: string[];

  minShift: number;
  maxShift: number;
  openMaMin: number;
  openMaMax: number;
  closeMaMin: number;
  closeMaMax: number;

  bestPnlPointId: string;
  bestEffPointId: string;

  ready: boolean;
  standaloneEligiblePointIds: string[];
  depthEligiblePointIds: string[];

  primaryCloseMa: number;
  closeProfile: CloseProfileEntry[];
}
```

---

# M. Практически равноценные точки

Для reference R и candidate X:

```text
pnlDiff = abs(X.pnl-R.pnl) / max(X.pnl,R.pnl)
effDiff = abs(X.eff-R.eff) / max(X.eff,R.eff)

equivalent = pnlDiff <=0.05 AND effDiff <=0.05
```

Внутри одной equivalent-group default = максимальный shift.

Дальнейший tie-break:

1. PnL DESC
2. Efficiency DESC
3. Trades DESC
4. DD ASC
5. pointId ASC

---

# N. Close profile

## N1. Primary representative

Выбрать max-PnL point в plateau; среди 5%-equivalent предпочесть больший shift.

`primaryCloseMa = point.closeMa`

## N2. Candidate adjacent close

Проверять CloseMA по периодам ±1 от primary и двигаться наружу последовательно.

Для candidate close C:

- зафиксировать `shift = primary.shift`;
- разрешить `openMa = primary.openMa ±1`;
- взять лучший реально существующий point plateau с `closeMa=C`;
- point обязан economicPass.

```text
pnlRet = alt.pnl / primary.pnl
effRet = alt.eff / primary.eff
support = min(pnlRet, effRet)
```

Статус:

- primary → PRIMARY_CLOSE;
- support >=0.90 → CORE_CLOSE;
- 0.75 <= support <0.90 → SUPPORTED_CLOSE;
- support <0.75 → stop expansion in this direction.

Если required cell отсутствует, close-profile/plateau получает refine diagnostic; не интерполировать.

---

# O. Базовая 1ORD

По каждой Pair+Side:

Candidates = все READY plateau points с `standaloneEligible=true`.

Считать:

`pnlDd5 = pnl * 5 / dd`

Выбрать max `pnlDd5`.

До глобального сравнения сначала применить plateau-local 5% equivalent rule, чтобы один plateau не плодил почти одинаковые точки.

Tie-break между разными plateau/TF:

1. `pnlDd5 DESC`;
2. raw PnL DESC;
3. Trades DESC;
4. DD ASC;
5. stable pointId.

Сохранить как `BASE_1ORD`.

Для финального MRS3 comparison позже допускается выбрать дополнительный `TF_1ORD_BENCHMARK` на конкретном TF по той же логике.

---

# P. MRS3 CloseMA family

Group scope:

`symbol + side + timeframe + commonCloseMa`

Eligible plateau:

```text
plateau.ready == true
AND closeProfile[commonCloseMa].support >=0.75
```

Нужно минимум 2 plateau.

---

# Q. Entry candidates на CommonCloseMA

Для каждого plateau:

1. points = plateau points where `closeMa == commonCloseMa`;
2. оставить economicPass;
3. reference = max PnL;
4. `equivalentPoints = points equivalent to reference by 5% rule`;
5. default = max shift in equivalentPoints.

Сохранить весь equivalent set.

---

# R. Генерация plateau combinations

Для K compatible plateau generate:

```text
all combinations C(K,2)
all combinations C(K,3)
all combinations C(K,4)
```

Не ограничивать K искусственно.

Каждый plateau_id может использоваться максимум один раз в structure.

---

# S. Representative product / gap validation

Для каждой plateau-combination:

1. построить Cartesian product equivalent sets;
2. каждый tuple отсортировать по shift;
3. shifts должны быть строго возрастающими;
4. smallest point must `standaloneEligible=true`;
5. all later points must `depthEligible=true`.

Gap function:

```text
if lowerShift < 1.5: requiredGap=0.6
else if lowerShift <=4.0: requiredGap=0.8
else: DEEP_GAP_RESEARCH
```

Проверить каждую соседнюю пару.

Tuple со случаем `lowerShift >4` не считать обычным READY.

Из READY tuples выбрать один детерминированно:

1. `sum(shifts) DESC`;
2. `sum(sourcePnL) DESC`;
3. `mean(sourceEfficiency) DESC`;
4. `sum(trades) DESC`;
5. lexicographic pointIds ASC.

Результат = одна конкретная structure на данный plateau-set + commonCloseMa.

---

# T. Structure model

```typescript
interface MRS3Structure {
  structureId: string;
  symbol: string;
  side: Side;
  timeframe: string;
  commonCloseMa: number;
  orderCount: 2 | 3 | 4;

  orders: {
    id: number;
    plateauId: string;
    pointId: string;
    openMa: number;
    shiftPct: number;
    sourcePnlPct: number;
    sourceDdPct: number;
    sourceEff: number;
    trades: number;
    closeSupport: number;
    standaloneEligible: boolean;
    depthEligible: boolean;
  }[];

  minCloseSupport: number;
  sourcePnlSum: number;
  sourceEffMean: number;
  lowSampleDepthCount: number;

  status:
    | "READY_MRS3_STRUCTURE"
    | "DEEP_GAP_RESEARCH"
    | "REJECTED";
  rejectReason?: string;
}
```

2/3/4ORD independent; no nesting requirement.

---

# U. Pre-test priority only

No predictive ComboScore.

Optional stable queue sorting:

1. minCloseSupport DESC
2. sourcePnlSum DESC
3. sourceEffMean DESC
4. lowSampleDepthCount ASC
5. sum(shifts) DESC
6. structureId ASC

Не удалять READY structure по этой сортировке.

---

# V. Lot allocator

На каждый READY MRS3 structure создать два variants.

## V1 EQUAL

```text
N=2 -> [0.5, 0.5]
N=3 -> [1/3,1/3,1/3]
N=4 -> [0.25,0.25,0.25,0.25]
```

Последний lot корректировать на rounding residue:

`last = 1 - sum(previous)`

## V2 INCOME

```text
lot_i = sourcePnl_i / sum(sourcePnl)
```

Так как points уже economicPass, PnL положителен.

Validate:

`sum(lot)=1 ± numericTolerance`

---

# W. JSON generation

Работать от supplied template.

Обязательно:

```text
name = unique generated name
is_runing = false
basic.symbol = symbol
basic.time_frame = TF
```

LONG:

```text
basic.use_long = true
basic.use_short = false

mrs3.ma_long[i].id = i+1
mrs3.ma_long[i].len = openMa
mrs3.ma_long[i].multiplier = 1 - shiftPct/100
mrs3.ma_long[i].lot_x = lot

mrs3.ma_close_long.len = commonCloseMa
mrs3.ma_close_long.multiplier = 1.003
```

SHORT:

```text
basic.use_long = false
basic.use_short = true

mrs3.ma_short[i].id = i+1
mrs3.ma_short[i].len = openMa
mrs3.ma_short[i].multiplier = 1 + shiftPct/100
mrs3.ma_short[i].lot_x = lot

mrs3.ma_close_short.len = commonCloseMa
mrs3.ma_close_short.multiplier = 0.997
```

Preserve all unrelated template fields.

Naming must include enough information for roundtrip back to audit, e.g.:

`SYMBOL_TF_SIDE_NORD_CMAx_STRUCTUREID_LOTMETHOD`

---

# X. JSON validation

Hard assertions:

- valid JSON;
- unique names;
- flags correct;
- `is_runing=false`;
- active array length=N;
- ids exactly 1..N;
- each chosen point exists in source audit;
- unique plateauId per order;
- commonClose supported >=0.75 by all plateau;
- initial sum lot=1;
- multiplier mathematically matches shift;
- gap valid;
- first selected point standalone eligible;
- later selected points depth eligible;
- REFINE_REQUIRED not exported as normal READY;
- DEEP_GAP_RESEARCH not exported as normal READY;
- no fabricated/interpolated MRS2 point.

---

# Y. Audit workbook

Required sheets:

```text
00_Input_Audit
01_Pair_History
02_Filtering
03_Refine_Required
04_Plateau_Points
05_Plateau_Library
06_CloseMA_Profile
07_Isolated_Peaks
08_1ORD
09_CloseMA_Families
10_MRS3_Structures
11_Lot_Variants
12_Ready_JSON
13_Deep_Gap_Research
14_Recalibration
15_Config
```

Post-test:

```text
16_Raw_MRS3_Results
17_DD5_Normalized
18_Final_Comparison
```

Every rejection must contain machine-readable reason.

---

# Z. Post-test comparison

## Z1. Raw MRS3 test

Initial variants have `sum lot_x=1`.

Import actual:

- PnL;
- MaxDD;
- WR;
- PF;
- Trades;
- any additional test metadata.

## Z2. DD5 scaling

```text
k = 5 / DD_raw
scaled_lot_i = raw_lot_i * k
```

Do NOT cap `lot_x <=1` at analytical normalization stage.

For MRS3 generate scaled JSON and retest.

Projection:

```text
projectedPnL = rawPnL * k
projectedDD = rawDD * k
```

is diagnostic only.

For 1ORD linear DD5 normalization may be used directly.

## Z3. Capital proxy

```text
totalLotX = sum(scaled_lot_i)
capitalRequirementProxy =
    totalLotX + actualNormalizedDDPct / 100
```

This assumes `lot_x` corresponds to reserved fraction of deposit and is intentionally conservative.

## Z4. Time normalization

```text
pnl30Dd5 = pnlDd5Actual * 30 / effectiveDays
capitalEfficiency30 = pnl30Dd5 / capitalRequirementProxy
```

## Z5. Pareto

A dominates B if:

```text
A.pnl30Dd5 >= B.pnl30Dd5
AND
A.capitalRequirementProxy <= B.capitalRequirementProxy
AND at least one strict
```

Keep non-dominated set.

## Z6. Near tie

If relative difference in `pnl30Dd5 <=5%`:

1. CapitalEfficiency30 DESC
2. CapitalRequirementProxy ASC
3. Trades DESC
4. stable ID

---

# AA. Required unit tests

At least:

1. LONG/SHORT shift calculation.
2. Effective history with listing date after report start.
3. 30m BaseRate.
4. absolute floor boundary exactly 2.0.
5. shift factor boundaries 1.5 / 2.0 / 3.1 / 4.7.
6. fine neighborhood 1.0.
7. boundary neighborhood 1.5 / 1.6 / 1.7.
8. 0.3 one-sided behavior.
9. coarse neighbor behavior at 0.2/0.3/0.4/0.5 grid.
10. CORE link exactly 0.90.
11. envelope exactly 0.75.
12. supported point cannot bridge two core components.
13. equivalent exactly 5%.
14. larger shift chosen inside equivalent group.
15. close support 0.90/0.75 boundaries.
16. close support contiguity stops after failed period.
17. same plateau cannot produce two orders.
18. first order standalone requirement.
19. deep order sample failure allowed.
20. gap <1.5 =>0.6.
21. gap 1.5..4=>0.8.
22. lower shift >4 => research status.
23. 2/3/4 independent combination generation.
24. EQUAL lots sum exactly 1.
25. INCOME lots sum exactly 1.
26. JSON LONG multiplier.
27. JSON SHORT multiplier.
28. no lot_x cap in DD5 scaling.
29. Pareto dominance.
30. near-tie 5%.

---

# AB. Recalibration registry

Implementation must keep all threshold values in config, not hardcode them.

Questions to revisit:

- equivalent 5%;
- CORE 90%;
- envelope 75%;
- BaseRate_TF / 30m;
- ShiftFactor;
- floor 10/5;
- economic gates;
- close support 90/75;
- shift 0.3;
- fine geometry;
- gaps 0.6/0.8;
- deep gap >4 with tests to at least 7%;
- EQUAL vs INCOME;
- SHORT close multiplier;
- DD5 scaling accuracy;
- capitalRequirementProxy vs measured margin load;
- position holding/occupancy data.

---

# AC. Definition of Done

Implementation считается готовой, если один CLI-run на входном CSV:

1. создает deterministic audit;
2. создает Plateau Library;
3. выводит refine requests;
4. выбирает BASE_1ORD;
5. строит все valid MRS3 structures;
6. создает EQUAL/INCOME variants;
7. создает validated JSON;
8. создает audit workbook;
9. повторный run на тех же inputs дает идентичные IDs, rows и JSON;
10. unit tests проходят.

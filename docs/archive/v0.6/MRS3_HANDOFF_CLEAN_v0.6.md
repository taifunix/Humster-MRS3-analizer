# MRS3 CLEAN HANDOFF
## Актуальный контекст для новой чистой сессии
Версия базового алгоритма: v0.6
Дата: 2026-08-08

Этот файл содержит только действующие решения и реальные ограничения текущего проекта. Исторические отмененные варианты сюда не включены.

---

# 1. Задача проекта

Нужно реализовать воспроизводимую обвязку, которая превращает массовые MRS2-тесты в:

- устойчивые entry plateaus;
- Plateau Library;
- одну базовую 1ORD;
- MRS3 2/3/4ORD structures;
- EQUAL + INCOME lot variants;
- JSON для tick-test;
- полный audit XLS;
- последующую DD5/capital-efficiency оценку.

Ключевая концепция:

> **Хранить устойчивые зоны входа и то, на каких Close MA каждая зона сохраняет силу.**

---

# 2. Стратегия

MRS — mean reversion.

Entry:
- LONG ниже своей Open MA;
- SHORT выше своей Open MA.

MRS3:
- до 4 entry orders;
- у каждого собственные Open MA / shift / lot_x;
- одна общая Close MA для всей позиции.

---

# 3. Актуальные исходные файлы текущего исследования

В текущем рабочем окружении использовались:

- `reports_history_bybit_long_day2.csv`
- `reports_history_bybit_short_day2(1).csv`
- `ADM_3_LONG_SHORT_1(1).json`
- `MRS - Mean Reversion Strategy.md`

Текущие MRS2 LONG/SHORT CSV имеют coarse shift-grid 0.4 п.п. и не содержат обязательную будущую fine-grid 0.1 для малых shifts. Поэтому часть plateaus на реальных новых запусках должна попадать в `REFINE_REQUIRED`.

---

# 4. Даты листинга текущих Bybit-инструментов

- APLDUSDT — 2026-07-17
- APPSTOCKUSDT — 2026-07-16
- CXMTUSDT — 2026-07-27
- GIGADEVICEUSDT — 2026-07-28
- IONQUSDT — 2026-07-17
- MINIMAXUSDT — 2026-07-28
- NVDLUSDT — 2026-07-21
- ONUSDT — 2026-07-21
- RDWUSDT — 2026-07-16
- SAMSUNGUSDT — 2026-06-04
- TSLLUSDT — 2026-07-20
- XLKUSDT — 2026-07-21

Всегда:

`EffectiveStart=max(ReportStart,ListingDate)`

`D_eff=ReportEnd-EffectiveStart`

Main selection требует `D_eff>=7d`.

---

# 5. Поддерживаемые TF

`5m, 15m, 30m, 45m, 1h, 2h, 3h, 4h`

30m отсутствовал в первых прогонных CSV только ради ускорения тестирования. В алгоритме он полноценный.

Текущий provisional BaseRate для 30m:

`1.59 trades/day`

---

# 6. Economic gates

Точка допускается в экономическую область при:

- PnL > 0
- WR >=70%
- DD <=11%
- PnL/DD >=3

---

# 7. Sample eligibility

Standalone absolute floor:

- shift <=2% → 10 trades
- shift >2% → 5 trades

Relative:

`ceil(D_eff * BaseRate_TF * ShiftFactor)`

BaseRate:

- 5m 2.27
- 15m 1.82
- 30m 1.59
- 45m 1.36
- 1h 1.36
- 2h 1.14
- 3h 0.91
- 4h 0.91

Current ShiftFactor:

- 0.3–1.5: 1.00
- >1.5–2.0: 0.90
- >2.0–3.1: 0.30
- >3.1–4.7: 0.20
- >4.7: uncalibrated

`MinTrades=max(absolute,relative)`.

Important:
- standalone/main point must pass sample rule;
- deep MRS3 orders are not hard-rejected for failing standalone trade floor;
- keep separate `standalone_eligible` and `depth_eligible`.

---

# 8. Shift geometry

Plateau lives in:

`Shift × OpenMA × CloseMA`

MA neighbors: ±1.

Shift rules:

- shift <1.5: fine-grid 0.1, radius ±0.3;
- 1.5–1.7: down max 0.3, up max 0.5;
- >1.7: all tested neighbors within ±0.5;
- shift 0.3: one-sided upward; do not request <0.3.

Missing required tests => `SHIFT_REFINE_REQUIRED`.

Shift 0.3 is used only with favorable nonzero close shift. Current LONG 1.003 and SHORT baseline 0.997 satisfy that mechanical condition.

---

# 9. Plateau definition

No single weighted PlateauScore.

Neighbor CORE link:

`min(PnL retention, PnL/DD retention) >= 0.90`

Whole plateau:

`min(minPnL/maxPnL, minEff/maxEff) >=0.75`

CORE links connect the plateau.

75–90% supported border points may be included if they directly touch CORE and the whole envelope remains >=75%, but they cannot bridge two independent CORE components.

READY plateau requires at least 2 CORE-connected points.

---

# 10. Equivalent points

Inside one plateau, points are practically equivalent if difference:

- PnL <=5%
- AND PnL/DD <=5%

Among equivalent variants prefer larger shift.

This is a tie-break only.

Empirical check on current LONG+SHORT data supported 5% as a useful compromise.

---

# 11. Isolated peaks

Strong singleton without plateau is not used automatically.

Audit-only if:

- passes gates/sample;
- no plateau;
- and PnL >=90% best Pair×TF PnL OR PnL/DD >=90% best Pair×TF efficiency.

Store in `Isolated_Peaks`.

---

# 12. Close-MA support

Each plateau stores a Close-MA profile.

Primary close = close MA of plateau's primary max-PnL representative, after local 5% tie rule.

For neighboring close MA:
- same primary shift;
- OpenMA can change ±1;
- compare best real point.

`CloseSupportRetention=min(PnL_retention, Eff_retention)`

- PRIMARY
- CORE_CLOSE >=90%
- SUPPORTED_CLOSE >=75%
- below75% not supported

Support chain must be contiguous by Close MA period. Expansion in one direction stops on first failure.

Empirical check on current LONG+SHORT data supported 90/75 as usable thresholds.

---

# 13. Base 1ORD

Before MRS3 choose one global standalone baseline per Pair+Side.

Only READY + standalone-eligible points.

Compare by:

`PnL_DD5 = PnL * 5 / DD`

For 1ORD direct linear DD5 normalization is accepted for selection.

If later a final MRS3 exists on another TF, optionally add one best 1ORD on that TF for direct benchmark. Do not generate unnecessary 1ORDs.

---

# 14. MRS3 structure rules

Scope:

`Pair + Side + TF + CommonCloseMA`

Compatible plateau if CommonCloseMA has support >=75%.

Hard rules:

- every order from a different plateau_id;
- no required first anchor;
- no need to include smallest available shift;
- generate all combinations of 2, 3, 4 plateaus;
- if plateaus >4, do not pre-cut;
- 2ORD/3ORD/4ORD independent.

For each plateau on CommonCloseMA:
- choose max-PnL point;
- build 5%-equivalent group;
- default to largest shift;
- retain all equivalent points for gap resolution.

Smallest selected order must be standalone-eligible.
Orders 2–4 may be depth-eligible.

---

# 15. Gap

After sorting selected entries by shift:

- lower shift <1.5 → gap >=0.6 p.p.
- lower shift 1.5..4.0 → gap >=0.8 p.p.
- lower shift >4.0 → not calibrated

If default representatives violate gap, enumerate equivalent points of the same plateaus.

If multiple valid tuples:
1. max sum shifts
2. max source PnL sum
3. max mean source efficiency
4. max total trades
5. stable ID

For >4 use `DEEP_GAP_RESEARCH`.

Planned work: extend a subset of pairs to shift ~7% and determine deep-gap empirically.

---

# 16. Lots

Every READY multi-order structure gets two initial variants, both with sum lot=1.

EQUAL:
- 2ORD 0.5/0.5
- 3ORD equal thirds
- 4ORD 0.25 each

INCOME:
`lot_i = source MRS2 PnL of exact chosen point / sum source PnLs`

Order number does not define weight. A deeper order may have larger lot.

---

# 17. Close multiplier

LONG:
`ma_close_long.multiplier=1.003`

This has already been supported by a dedicated LONG sweep.

SHORT:
`ma_close_short.multiplier=0.997`

This is provisional baseline. After first full SHORT batch, run a dedicated close-multiplier study and then set the permanent SHORT default.

---

# 18. JSON rules

Template: current MRS3 template.

Set:

- name
- is_runing=false
- symbol
- TF
- use_long/use_short

LONG entry:
`multiplier=1-shift/100`

SHORT:
`multiplier=1+shift/100`

IDs 1..N after sorting by shift.

One common Close MA.

Preserve unrelated template fields.

No fabricated MRS2 values.

---

# 19. Pre-test selection

Do not predict MRS3 PnL by adding MRS2 PnLs.

Не использовать искусственный агрегированный рейтинг как hard filter.

Все структурно валидные комбинации допускаются к тестированию.

Source metrics are only for queue ordering.

---

# 20. DD5 comparison after MRS3 test

Initial MRS3 variants have sum lot=1.

After raw result:

`k=5/DD_raw`

`new lot_i=old lot_i*k`

There is **no analytical lot_x<=1 cap**.

MRS3 must be retested after scaling.

For 1ORD direct linear normalization is allowed.

---

# 21. Capital efficiency

Until real margin/occupancy time-series exists:

`TotalLotX=sum(normalized lot_x)`

`CapitalRequirementProxy=TotalLotX + DD_normalized/100`

`CapitalEfficiency=PnL_DD5 / CapitalRequirementProxy`

Across histories:

`PnL30_DD5=PnL_DD5*30/D_eff`

`CapitalEfficiency30=PnL30_DD5/CapitalRequirementProxy`

This is a conservative proxy, not measured Peak Margin Load.

---

# 22. Final strategy comparison

Main Pareto dimensions:

- maximize PnL30_DD5
- minimize CapitalRequirementProxy

Dominated strategy:
another strategy has no lower PnL30 and no higher capital requirement, with at least one strict improvement.

If PnL30 difference <=5%:

1. higher CapitalEfficiency30
2. lower CapitalRequirementProxy
3. more Trades

Non-dominated strategies go to portfolio stage.

---

# 23. Required audit

Workbook must contain:

- input audit
- effective history
- filters/reject reasons
- refine requests
- plateau points
- plateau library
- close profiles
- isolated peaks
- base/benchmark 1ORD
- close families
- all MRS3 structures
- lot variants
- ready JSON registry
- deep-gap research
- recalibration registry
- config
- raw MRS3 results
- DD5 results
- final comparison

---

# 24. Questions to recalibrate as data grows

Keep as a live registry:

1. 5% equivalent-point threshold
2. 90% CORE link
3. 75% plateau envelope
4. 30m BaseRate and all BaseRates
5. ShiftFactor, especially fine-grid and >4.7
6. absolute 10/5 trade floor
7. DD11 / WR70 / PnL-DD3 gates
8. Close support 90/75
9. shift 0.3 economics
10. fine/boundary/coarse geometry
11. gap 0.6
12. gap 0.8
13. deep-gap >4 using tests up to ~7%
14. EQUAL vs INCOME
15. SHORT close multiplier
16. DD5 scaling accuracy for MRS3
17. CapitalRequirementProxy vs measured margin load
18. position holding time / capital occupancy
19. PnL30 reliability on short histories

---

# 25. Immediate next task in the new session

Implement the selector/generator from `MRS3_Implementation_Spec_v0.6.md`.

Recommended first practical order:

1. parse current LONG CSV;
2. reproduce normalized point table and audit counts;
3. implement Effective History + filters;
4. implement refine detector;
5. implement plateau graph 90/75;
6. inspect Plateau Library manually on several known pairs;
7. only after visual/audit validation implement CloseMA profile and MRS3 family-builder;
8. generate JSON;
9. test against current source files;
10. only then process SHORT and additional fine-grid data.

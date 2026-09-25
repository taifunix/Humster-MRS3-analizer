# Performance v2: качество роста equity — анализ и проект

**Статус:** R7.3 утверждена; M0–M1 приняты, M2–M5 ожидают выполнения.
**Версия:** R7.3, 2026-09-25.
**Назначение:** спецификация для обсуждения и независимой проверки плана.

**Engineering review:** canonical R7.3 получил независимое `PLAN_APPROVED`
2026-09-25 (Opus 5). R7.3 заменяет конфликтующие правила R6.1 ниже; это не
результат benchmark; разрешение на внедрение дано пользователем отдельно. Findings и этапы — в
[плане внедрения](../superpowers/plans/2026-09-25-performance-v2-equity-quality.md).

## 0. Canonical R7.3 contract (normative; supersedes R6.1)

The following is the accepted contract. Older sections below preserve design
history; where they mention action/position-based gap proof, age-selected
edge-anchor availability, a common-T preflight, a <=5% coverage-unrankable
release gate, `CONTINUATION_UNASSESSED`, or a short-window ERF block, those
statements are **SUPERSEDED** and must not guide implementation.
Defaults remain OFF and the existing robust method remains the default. Four
existing XLSX columns remain unchanged. Implementation proceeds under the
user's separate authorization, not by this specification or its approval alone.

### Source, validity, and windows

- Facts are equity-only. The engine receives the current result's stored
  `[report_start_utc, T=report_end_utc]` and raw equity rows. It must not read
  actions, positions, timeframes, trade counts, or frequencies. Optional M0
  trip-rate labels are outside the equity calculation and must have separate
  query/row counters. A diagnostic with no trip-rate labels performs zero
  action-fact queries and reads zero action rows.
- All timestamps are native timezone-aware UTC values (DuckDB `TIMESTAMPTZ`);
  check type, UTC offset, and decoded datetime values. Do not interpret them as
  epoch numbers or silently guess units. Convert numeric equity directly to
  Decimal: preserve Decimal inputs; otherwise parse `Decimal(str(value))`,
  with no float round-trip. Malformed or non-finite conversions are structural
  source invalidity.
- Apply outcome precedence exactly: structural source validation (report
  interval/range, ownership, conversion/finite values, and sample-index
  ordering) -> in-report raw Decimal <= 0 -> age/window availability ->
  metrics/class/score. Structurally invalid source is
  `UNKNOWN_INVALID_SOURCE` / NOT_EVALUATED even if it also contains an
  in-report nonpositive value. Only structurally valid source with an
  in-report raw equity <= 0 is `NONPOSITIVE_EQUITY` / BLOCK, before age,
  baseline, or logarithms; this includes under-seven-day histories and points
  outside selected H. Neither invalid nor nonpositive rows supply a metric.
- On valid source rows, right-continuous `E(t)` is the greatest ordered
  in-report observation at or before `t`. For duplicate timestamps, greatest
  `sample_index` is effective for `E(t)`; earlier duplicates remain raw risk
  observations. Carry `E(t)` unconditionally until the next observation and
  through T. Internal gaps and quiet terminal tails never invalidate facts.
- A W-day baseline is available iff `report_start_utc <= T-W` and there is an
  in-report equity observation at or before `T-W`. No pre-report/future
  backfill. H is the longest available of 28/14/7; a shorter H is normal when
  the longer baseline is unavailable. Invalid/corrupt source never falls back.
  With no baseline, age under 7 days is `INSUFFICIENT_HISTORY`, otherwise
  `MISSING_BASELINE`; both are NOT_EVALUATED.

### Metrics, risk path, and decisions

- For every available W, use the fixed T-anchored 6-hour grid from `T-W`
  through T, including both endpoints. One baseline observation can fill a
  whole window and yield valid FLAT facts. There are no endpoint, occupancy,
  update-count, gap-duration, position-state, or activity thresholds.
- At Decimal precision 38, with grid x in days from `T-W` and
  `y=ln(E(t)/E_start)`: `trend30=3000*OLS_slope`,
  `endpoint30=3000*ln(E_T/E_start)/W`, and
  `return_pct=100*(E_T/E_start-1)`. `ER=net_log/sum(abs(grid_log_step))`,
  with zero denominator giving zero. `eps=1e-8` log-pp/30d.
- Raw H risk path covers `[L=T-H,T]`. If no raw row is exactly L, prepend
  `E(L)`; otherwise include every raw L duplicate once in sample-index order
  and do not prepend. Append `E(T)` only when no raw row is exactly T. Each
  raw observation is included once. `D=max(1-equity/peak)` and
  `P=1-E(T)/max_equity` use this raw path. Grid ER and raw D/P intentionally
  describe different samplings.
- Let `G=min(trend30,endpoint30)`, `Q=max(0,ER)`. Score is
  `G*Q*(1-D)*(1-P)` for G>eps, zero for `abs(G)<=eps`, and
  `G*(1+D+P)` for G<-eps; quantize HALF_EVEN to 12 decimals.
- A window is UP iff both trend and endpoint > eps, NONDECLINING iff both are
  >= -eps, FLAT iff both absolute values <= eps. For H28 inspect shorts 7/14;
  H14 inspect 7; H7 inspect none. H-UP plus all shorts NONDECLINING is
  GROWING class 0 / PASS. H-UP plus any short decline is WEAKENING class 1 /
  PASS. FLAT H is class 2 / BLOCK only when ERF is enabled. Other valid
  non-UP is DECLINING_OR_MIXED class 2 or 3 / BLOCK. Invalid, under-seven-day,
  and missing-baseline rows are NOT_EVALUATED; nonpositive is BLOCK and
  unscoreable. Short decline alone never blocks. A quiet multi-day tail with
  H still UP and flat shorts remains GROWING / PASS.
- `CONTINUATION_UNASSESSED` is deleted. Optional equity ranking uses the exact
  ascending tuple `(class,-score12,D,P,-H,strategy_id)`. Unscoreable rows have
  null score and are RESERVE, consuming no N slot. Different candidates may
  use different T; mixed report ends are valid. There is no common-T gate,
  freshness rule, or report-date tie-break. T is part of revision/digest; a
  T-only change recomputes facts.

### R7.3 M0 acceptance evidence

M0 must check the contract with deterministic self-tests: 5m/4h label
invariance; quiet multi-day tails; H-UP short decline; H-flat; one-baseline
windows; arbitrary internal/final gaps; in-report predecessor; exact L/T and
duplicate raw paths; invalid pre-report/future-only inputs; H availability
transitions; malformed/non-finite/duplicate-index/out-of-interval collisions
with an in-report nonpositive value; Decimal values below binary64 precision
and negative zero; duplicate-driven raw D/P deltas; grid/logging invariance;
mixed T; and zero short-only blocks. Structural invalidity must win each
collision. Verify streamed per-result row accounting covers each selected
result exactly once, including empty and final groups, and that per-result raw
row counts sum to the query's scanned-row count. Ordered streaming is by
`result_id,sample_index`, so groups stay contiguous across fetch chunks.
Self-check output must repeat byte-identically.

The read-only live diagnostic reports overall/by-age availability, invalid,
out-of-interval and nonpositive source rows, H/baseline distribution, raw/grid
counts, max gaps, elapsed time since last sample to T (a carry duration, not
proof equity was economically flat), classes, scores, ranking/Top-N effects,
and timeframe/inactivity cross-tabs when those metadata exist. Action-derived
`r=0`, `0<r<=3`, `r>3` strata are optional and separately counted; omitting
them is valid and requires zero action queries/rows for equity calculation.
Report duplicate-driven raw D/P delta statistics; compare 1/3/6h grid ER and
raw-versus-grid ER when feasible without changing canonical 6h facts.

M0 preserves the earlier warm-cache baseline and, when feasible, measures the
R7.3 diagnostic with one code warmup plus three measured repeats, recording
wall/RSS/query/rows/writes and before/after database identity. No budgets pass
without comparable measurements. Proposed gates: cold median <= baseline +
max(20%, 0.5s), warm <= baseline + max(10%, 0.05s), RSS <= baseline +
max(25%, 64MiB). No live writes, migration, cache backfill, tester, Panel, or
service restart is allowed for M0.

> **Historical R6.1 material follows.** Sections 1 onward preserve context and
> older analysis. Treat them as non-normative wherever they differ from §0;
> revise any needed implementation detail to R7.3 before M1.

## 1. Запрос и границы

Пользователь просит переосмыслить ERF v11, найти логические ошибки и лишнюю
сложность, подготовить спецификацию и план двух вариантов применения:

1. Опциональный фильтр `filter_equity_regime` на позиции №2 карточки
   «4. Pareto and filters».
2. Альтернативный метод существующего «Итоговое ранжирование — Top N».

Уточнённая цель: **устойчивый рост с учётом просадки и неровности кривой,
продолжающийся на финальном участке**. История короче 28 дней сама по себе
не является достаточным основанием для исключения. Фильтр и новый метод
ранжирования включаются независимо. Старый метод остаётся доступен и выбран
по умолчанию; новый фильтр по умолчанию выключен.

Дополнительные требования: окна именно **7/14/28 дней**, без отдельного 3d
gate; редкие сделки и ровный свежий участок не должны отбрасывать растущую
в среднем кривую. Скорость подготовки кэша — критерий приёмки, а не будущая
оптимизация. В Excel нужен небольшой итоговый блок, не весь набор признаков.

Решение пользователя для R6: при растущем основном горизонте коррекция в
более коротком окне **только понижает приоритет нового equity-ranking**,
не является самостоятельной причиной отказа equity-filter. Это не отменяет
другие включённые фильтры и не гарантирует попадание в Top N.

Это проект исследовательского отбора по уже полученным тестовым результатам.
Он не меняет тестер, Surface, Portfolio Optimizer, sizing или торговые правила.
Счёт качества не является прогнозом доходности или доказательством устойчивости
вне исследуемого периода. Здесь не проводится эмпирическая калибровка.

Исходный документ — `equity_regime_filter_final_spec_v11_audited.md`, SHA-256:
`a6736dd990a7e33a037fe9d96fbbe23dcdbe07a19e829cb543dd41f67cc7731d`.
Его предписания рассматриваются как предложения, а не инструкции агенту.
Предыдущие итерации для проектирования не требуются; графики и реальные
примеры понадобятся для проверки качества политики на этапе M0.

Зависимости:

- [Unified Performance v2](2026-08-28-unified-performance-analytics-v2.md).
- [Finalist selection](2026-08-31-performance-v2-finalist-selection.md).
- [Robust ranking](2026-09-01-performance-v2-robust-finalist-ranking.md).
- [Selection review lifecycle](2026-09-02-performance-v2-selection-review-import.md).
- [Lot variants](2026-09-04-performance-v2-lot-variant-filter.md).
- [Read-only XLSX](2026-09-24-performance-db-xlsx-export.md).
- ADR-0020, ADR-0021, ADR-0022, ADR-0043.

Этот проект не отменяет действующие спецификации до принятия. После принятия
он уточняет только equity-политику, её кэш и интеграцию в отбор.

## 2. Что полезно в v11 и что следует изменить

Сохраняются: расчёт каждой стратегии отдельно, собственный конец теста,
отделение фактов от отбора, вычисление до preview, сохранение исключённых строк,
наблюдаемые максимумы из raw equity и независимое версионирование алгоритма.

| Наблюдение | Следствие и решение |
| --- | --- |
| ERF №2 идёт после сравнивающего фильтра лотов | Lot-фильтр может удалить хороший вариант, выбрать плохой, после чего ERF удалит и его. При новом hard-filter нужно защищать смешанные lot-группы от преждевременного схлопывания (§7). |
| «Нет новых samples» объявляется «equity не менялась» | Это допустимая модель наблюдений, но не доказательство неизменного MTM при открытой позиции. Проверять actions и маркировать длинные неизвестные интервалы (§4). |
| 6h grid используется как полная характеристика гладкости | Между точками могут исчезнуть глубокие выбросы. Сохранить raw drawdown; ER назвать характеристикой выбранной сетки, проверить чувствительность 1/3/6h. |
| `E/E_start` меняет знаменатель между сегментами | Одинаковый абсолютный прирост может выглядеть замедляющимся в относительных единицах. Это не ошибка арифметики, но недостаточное доказательство деградации. Выбрать явную log-rate модель, не обещая, что она устранит экономическое различие абсолютного и относительного роста. |
| 7/14/28 перекрываются | Это зависимые описания одной кривой, а не три независимых подтверждения. Называть глубиной наблюдения, не статистической уверенностью. |
| Historical Q25 требует четыре старых 14d блока | Правило включается только при истории от 84 дней: `28 + 4*14`. Молодые и старые стратегии проходят разные тесты. Убрать исторический baseline из первой версии. |
| Знаки и Q25 объявлены «без порогов» | 6h, 7/14/28, Q25, четыре блока, семь дней HWM и strict recovery BLOCK — тоже решения политики. Проверять их на примерах и во времени. |
| Full-history HWM участвует в peer comparison | Длинная история имеет больше возможностей накопить старый максимум. Использовать максимум внутри выбранного текущего горизонта, не считать all-history HWM. |
| `WEAKENING_ALIVE` спасается при отсутствии конкурента | Плохая абсолютная кривая может пройти только из-за бедного набора peers. Допуск и сравнительную предпочтительность разделить. Удалить rescue-dominance из первой реализации. |
| Допуск разрешает «немного хуже по остальным осям» | Такое доминирование может иметь циклы. Три строки способны удалить друг друга. Новый фильтр не использует peer dominance. |
| `FLAT` объединяет боковик, mixed signs и недостаточную направленность | Использовать отдельные состояния роста, восстановления, ослабления и неопределённости. Не приравнивать отсутствие данных к плохой equity. |
| Запрещена история <28d | Возраст не равен качеству. Полные 7/14d окна пригодны для частичной оценки; отсутствие 28d не блокирует. |
| Весь новый кэш обязателен даже при отключённом ERF | Не ломать прежний workflow: readiness зависит от реально включённых equity-потребителей. |
| Кэш привязан только к result_id и algo_version | REPLACE переиспользует result_id. Нужна привязка к ревизии источника и повторная проверка перед публикацией. |
| `stage_trace_json` объявлен достаточным для порядка | Сейчас это canonical JSON-map boolean-полей, ключи сортируются. Effective order нужно хранить отдельным массивом в metadata. |
| Excel должен сохранить произвольную Decimal precision | Числовые Excel cells ограничены примерно 15 значащими цифрами. Полная точность остаётся в DB/JSON; Excel не использовать для повторного вычисления решения. |

Последнее ограничение подтверждено [документацией Microsoft](https://learn.microsoft.com/en-us/troubleshoot/microsoft-365-apps/excel/floating-point-arithmetic-inaccurate-result).
`ffill` означает перенос последнего наблюдения, а не восстановление отсутствующего
рыночного пути: [документация pandas](https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.reindex.html).

### Проверенные контрпримеры

Read-only расчёты через `.venv/Scripts/python.exe`:

- Постоянный абсолютный прирост `10/day`, начальные equity сегментов
  `1000, 1140, 1210`: нормированные линейные Trend30 равны
  `30, 26.315789, 24.793388`. Это относительное замедление без изменения
  абсолютной скорости; оснований автоматически назвать стратегию нежизнеспособной нет.
- При допуске `e > 0` максимизируемые векторы
  `A=(2e,e,0), B=(0,2e,e), C=(e,0,2e)` дают `A>B`, `B>C`, `C>A`
  по правилу v11 «не хуже с допуском, строго лучше хотя бы по одной оси».
- На синтетическом hourly пути с внутрисеточными всплесками ER по 6h grid
  равен `1`, по исходным наблюдениям — около `0.01485`.
- В ограниченной выборке восьми ACTIVE результатов реально доступны equity
  и actions; длина отчётов 56–70 дней, последний `post_size=0` во всех восьми.
  Максимальные разрывы наблюдений составили примерно 95–265 часов.
  Это не репрезентативный аудит: проверка всего корпуса и позиций внутри
  разрывов ещё не проведена. Последняя закрытая позиция не доказывает flat
  для каждого внутреннего разрыва.
- Для новой политики проверена синтетическая 28d лестница: шесть небольших
  приростов на днях 3/6/10/13/17/20 и полностью ровная последняя неделя.
  Получены 7d slope/endpoint/ER = 0/0/0, 14d trend/endpoint ≈4.929/4.082,
  28d ≈7.313/6.243 log-pp/30d: PASS. Уменьшение последнего equity на5 при
  исходном1000 даёт отрицательные 7d slope/endpoint: R5 предлагал BLOCK,
  R6 при сохранённом H UP даёт PASS / WEAKENING с пониженным приоритетом.
  Численный probe выполнен до изменения policy. Это проверка
  формулы через `.venv`, не тест реализованного модуля и не торговая валидация.

## 3. Варианты архитектуры

| Вариант | Плюсы | Недостатки |
| --- | --- | --- |
| Перенести v11 почти буквально | Знакомые labels и правила | Жёсткие эвристики, rescue и множество неиспользуемых признаков; не решён ranking и конфликт lot-stage |
| Только новый ranking | Минимум новых правил допуска, удобно исследовать | Нет отдельного фильтра №2; downstream Pareto может удалить кандидата раньше ранжирования |
| **Один набор фактов, независимый фильтр и метод Top N** | Общие вычисления, прозрачные режимы, пригоден для постепенной проверки | Нужны новая версия кэша, совместимость snapshots и явная политика короткой истории |

Рекомендуется третий вариант. Никаких новых сервисов, плагинного реестра,
ML, отдельных очередей или второго Excel-writer.

```text
strategy_equity + strategy_actions + report revision
                  |
          явный пересчёт фактов
                  |
         equity_quality_metrics
             /             \
     фильтр #2         метод final Top N
             \             /
       существующий selection result / XLSX / review
```

## 4. Источник, время и качество наблюдений

Идентичность: `strategy_id + current result_id + source_revision`.
`T = report_end_utc` каждой стратегии; текущие часы компьютера не участвуют.
Все вычисления в UTC. Окна не выдвигаются за report interval и не сдвигаются
к flat boundary: мы измеряем наблюдаемую MTM equity, а не существующую
wallet-based A/B доходность. Старый калькулятор A/B не менять.

Чтение equity упорядочено по `(timestamp_utc, sample_index)`;
при одинаковом timestamp для grid используется последний sample_index.
Для raw drawdown/HWM сохраняются все наблюдения в этом порядке, включая
внутритimestamp максимум: схлопывание timestamp не должно скрывать просадку.
Actions упорядочены по `(timestamp_utc, action_index)`.

Grid: `T - k*6h` с обязательными границами 7/14/28 дней и T.
Значение — последнее наблюдение не позже grid time. До первого наблюдения
backfill запрещён; initial_balance не заменяет отсутствующее наблюдение.

Различать два вида заполнения интервала от sample до следующего sample или T:

- Пока все состояния позиции известны и `post_size=0`, разрешён перенос
  последнего equity. Это модель event-based отчёта; она не восстанавливает
  незафиксированные денежные потоки.
- При открытой либо неизвестной позиции перенос допустим не более 6 часов
  с последнего наблюдения. Непокрытый участок длиннее 6 часов делает
  затронутое окно `UNKNOWN_GAP`, даже если не попал на grid point.

При смене состояния позиции внутри разрыва interval разделяется по actions;
переход в flat без нового equity sample не создаёт новое наблюдение.
Перед первым action состояние UNKNOWN, если нет явного начального flat evidence.
Консервативное правило: длинный перенос разрешён только от sample, в момент
которого состояние уже flat и остаётся flat на всём переносимом участке.

Доказательство flat: последний action на/до equity sample имеет post_size=0
и все последующие actions до оцениваемого времени сохраняют flat; либо
есть явное начальное flat evidence источника и полная последующая история
actions. Equity predecessor сам по себе, равные equity/wallet или пустой
список actions этого не доказывают. Tail loader читает последнее состояние
**на/до predecessor equity sample**, а не только перед левой границей H,
и все actions от predecessor sample до T.

Лимит 6h — версия исследовательской модели, не доказательство полноты MTM.
Сохранять в audit JSON качество покрытия и долю grid points, использующих перенос
при открытой/неизвестной позиции. На M0 проверить чувствительность 1/3/6h;
не подбирать шаг по числу прошедших кандидатов. Raw DD остаётся нижней оценкой
риска между отсутствующими наблюдениями.

Ошибки timestamps, противоречивые порядковые номера, нечисловые/non-finite
equity дают `UNKNOWN_INVALID_SOURCE`. Они не подменяются нулём.
Наблюдаемая `equity <= 0` в текущем оцениваемом окне — отдельное
`NONPOSITIVE_EQUITY`; логарифмический ranking недоступен, hard-filter блокирует.
Такое событие только в старой истории не портит валидное текущее окно,
но явно отражается в исторической диагностике.

## 5. Минимальный набор фактов и численная модель

Окна:

- 7d — свежий хвост и минимальное полное окно оценки;
- 14d — средний горизонт;
- 28d — основная глубина, когда она доступна по возрасту.

H — самый длинный полный возрастно доступный горизонт из 28/14/7.
Отсутствие 28d не блокирует; испорченное покрытие H не позволяет незаметно
перейти к более короткому окну. Все имеющиеся окна описывают одну кривую.

Для каждого полного пригодного окна W:

```text
x_i = elapsed UTC days from window start
y_i = ln(E_i / E_start)
b_W = sum((x-mean(x))*(y-mean(y))) / sum((x-mean(x))**2)
log_trend30_W = 3000*b_W
log_endpoint30_W = 3000*ln(E_end/E_start)/W
return_W_pct = 100*(E_end/E_start - 1)
ER_W = (y_end-y_start) / sum(abs(y_i-y_(i-1)))
```

ER здесь намеренно в log-equity units; flat denominator даёт ER=0.
Это другая версия признака, чем ER исходной v11; нельзя смешивать старые и
новые данные под одним algo_version. `Log trend /30d` — логарифмический темп,
не обычная процентная месячная прибыль. В первой версии не экспоненцировать
его ради видимости прогноза; observed Return сохраняется в audit JSON.

Drawdown вычисляется по всем raw observations окна плюс достоверным boundary
values; running peak начинается на левой границе:

```text
D_W = max(1 - E_i / running_peak_i)       # fraction, [0,1)
P_W = 1 - E_T / max(E in W)              # fraction, [0,1)
```

Не пересчитывать D/P по сглаженной сетке. Для первой версии нужны D/P только
для H. Не вычислять all-history HWM, days-since-HWM, исторические блоки,
R², segment ratios, Q25/Q75 и дублирующие дельты: они не участвуют в решении.
Одна строка facts содержит три оконных описания, качество покрытия и D/P H.

Детерминизм: исходные Decimal; `localcontext(prec=38)` для ln и арифметики
малого grid; фиксированный порядок суммирования. Derived numbers сохраняются
в canonical JSON как plain decimal strings; None=null, NaN/Inf запрещены.
Для решения направление считается положительным при log trend и log endpoint
строго выше `1e-8` в log-percentage-point/30d units, отрицательным ниже
`-1e-8`, иначе neutral. Этот technical epsilon не участвует в peer dominance.
Итоговый score перед сравнением округляется ROUND_HALF_EVEN до 12 знаков
после точки; одинаковые значения разрешаются явными tie-breaks. Обычные
Excel formats не участвуют в расчёте.

## 6. Возраст, доступность и состояния

Возрастная доступность определяется только авторитетным report interval.
Наличие наблюдаемого sample на/до left boundary проверяется отдельно как
качество покрытия. Поздний первый sample или разрыв внутри окна — качество
данных, а не «короткая история»; нельзя сокращать H, чтобы скрыть этот дефект.

| Доступная история | Hard-filter | Новый ranking |
| --- | --- | --- |
| <7d | NOT_EVALUATED, не исключает только за возраст | Без score; представитель RESERVE с причиной недостаточной истории |
| 7–<14d | Оценивает 7d | PROVISIONAL_7D, H=7 |
| 14–<28d | Оценивает 7d+14d | PARTIAL_14D, H=14 |
| >=28d | Оценивает 7d+14d+28d | FULL_28D, H=28 |
| Возраст достаточен, нужное окно испорчено/не покрыто | NOT_EVALUATED с причиной данных | Без score; RESERVE |

Короткая история не получает заполненных нулём длинных окон или
перенормированных весов. Кандидат с 10 днями может стать FINALIST через новый
ranking, но будет отмечен как предварительный. Нет отдельного штрафа за
возраст и безусловного приоритета старых стратегий. Сопоставление разных H
по темпу /30d — практическая эвристика, не равная статистическая надёжность.
При полном равенстве остальных признаков больший H является tie-break (§8).

Состояния для диагностики, а не девять жёстких классов v11:

- `GROWING`: H UP, все более короткие окна NONDECLINING;
- `WEAKENING`: H UP, хотя бы одно короткое окно не NONDECLINING;
- `FLAT`: обе характеристики H в пределах epsilon;
- `DECLINING_OR_MIXED`: остальные валидные сочетания;
- `INSUFFICIENT_HISTORY`, `UNKNOWN_DATA`, `NONPOSITIVE_EQUITY` — отдельные
  причины отсутствия оценки/блокировки.

UP означает slope и endpoint > epsilon; NONDECLINING — оба >= -epsilon.
FLAT по короткому окну разрешён и не требует сделок либо нового максимума.
Порядок classifier: наблюдаемый NONPOSITIVE -> нехватка/ошибки покрытия ->
GROWING -> WEAKENING -> FLAT -> DECLINING_OR_MIXED. Nonpositive проверяется
в H либо во всей доступной текущей истории, если возраст <7d. Старый убыток
вне H не меняет текущую классификацию. UNKNOWN не означает плохую стратегию.

## 7. Опциональный фильтр №2

`filter_equity_regime`, checkbox по умолчанию OFF, визуально и фактически
сразу после lot-stage. Фильтр self-only; scope selector ему не нужен.
Для совместимости stage имеет фиксированный `scope="pair_side"`; другой
scope parser отклоняет. Group-relative конкуренция относится к ranking.

| Условие | Решение |
| --- | --- |
| H UP, полная валидная оценка; короткие окна могут снижаться | PASS (GROWING либо WEAKENING) |
| Полная валидная оценка, но условие выше не выполнено | BLOCK |
| Доказанная nonpositive equity в обязательном окне | BLOCK |
| История <7d, invalid source либо неизвестное покрытие | NOT_EVALUATED; стадия не исключает |

Растущая основная кривая проходит и с ровной, и с отрицательной последней
неделей. При H=28 снижение 7d или14d не блокирует, а даёт WEAKENING; при
H=14 то же относится к7d. Не вводить порог «допустимой коррекции»:
решение пользователя реализуется разделением допуска и предпочтительности.
Нет minimum trades/week, требования нового HWM за7d или прибыли каждую
неделю. При H=7 отрицательные7d — уже основной горизонт, а не короткая
коррекция, поэтому правило H UP по-прежнему не выполнено. Положительное
восстановление при отрицательном H также не проходит hard-filter.

Состояние WEAKENING понижает относительный приоритет только при enabled
`equity_quality_v1`; robust method не меняется. Filter-only не переставляет
строки. Другие включённые фильтры, lot collapse и analog/Top N продолжают
работать по своим правилам; новый фильтр не восстанавливает удалённых ими.
Это не обещание фиксированного числа потерянных мест или FINALIST.

Явное ограничение независимых opt-in режимов: **ERF ON + robust_v1** пропускает
WEAKENING без нового equity-штрафа, порядок определяет прежний robust.
Чтобы получить именно понижение за короткую коррекцию, нужно выбрать
equity_quality_v1. Help рядом с методом и XLSX comment должны это объяснять;
включение фильтра не переключает метод автоматически.

Filter policy R6: `equity-regime-v2` вместо проектной строгой policy R5.
Версия facts и формула ranking не меняются, cache пересчитывать не требуется;
policy version записывается в snapshot. R5 не реализована: эта правка сама
по себе не требует дополнительной миграции DB.

Все исходные строки сохраняются. BLOCK заполняет
`eliminated_by_filter_equity_regime=True`, `elimination_reason=FILTER_EQUITY_REGIME`;
детальная причина и NOT_EVALUATED видны в аудите. Standard counters сохраняют
`enabled/eliminated/remaining`; возле фильтра короткое сообщение о числе
неоценённых, чтобы «осталось» не воспринималось как «качество подтверждено».

### Lot-stage: исправление причинной ошибки без скрытой стадии

До selection уже доступны self-only facts всех кандидатов. Если ERF включён,
существующая lot-группа схлопывается обычным алгоритмом **только если каждый
её участник имеет decisive PASS ERF**. NOT_EVALUATED проходит сквозь фильтр,
но **не является PASS** для lot guard. Если хотя бы один NOT_EVALUATED,
вся группа остаётся с trace reason `LOT_GROUP_EQUITY_UNASSESSED`; иначе при
хотя бы одном BLOCK — `LOT_GROUP_EQUITY_BLOCKED`. All-unassessed тоже не
схлопывается; all-PASS сохраняет старый winner. Никто не записывается ложным
«прошедшим ERF».

В R6 decisive PASS включает GROWING **и WEAKENING**. Смешанная группа этих
двух состояний считается all-PASS и схлопывается обычным lot-фильтром;
WEAKENING нельзя ошибочно трактовать как BLOCK из старой policy.

Это сохраняет видимые позиции №1/№2 и не удаляет good variant ради bad winner.
Допускается остаточная избыточность: ERF потом удалит плохих, аналог-группы
при Top N обрабатываются существующим механизмом. Повторный lot-pass не добавлять.
ERF выключен — прежний lot-selection без изменений. В rank-only режиме lot
остаётся пользовательским prefilter: он может исключить будущего equity-лидера.
Чтобы сравнить все варианты лотов новым ranking, оператор выключает этот
существующий фильтр; UI показывает эту зависимость без автоматической смены флагов.

Backend нормализует только включённый в запрос ERF в fixed prefix после
существующего explicit/implicit lot-stage. Отсутствующий ERF не вставляется
в legacy request. Остальные стадии сохраняют относительный порядок;
submitted и effective orders сохраняются отдельно. Rank-stage всегда последний.
UI не разрешает соседним movable stages пересекать prefix.

## 8. Альтернативный метод «Итоговое ранжирование — Top N»

Одна существующая final stage, один checkbox, один N (текущий default 20),
новый native select:

- `robust_v1` — существующее комплексное ранжирование;
- `equity_quality_v1` — устойчивость роста equity.

Stage ID `rank_robust_top_n` сохранить как совместимый технический ID.
Опциональное поле `method` разрешить только у него; отсутствие означает
legacy robust. Нельзя включить два последовательных ranker. Disabled final
stage полностью инертна независимо от выбранного method.

### Прозрачная исследовательская формула v1

H=28/14/7 по возрастной доступности (§6). Некачественное покрытие H не
разрешает переход к более короткому горизонту.

```text
G = min(log_trend30_H, log_endpoint30_H)
Q = max(0, ER_H)
D = D_H
P = P_H

G > eps:   score = G * Q * (1-D) * (1-P)
abs(G)<=eps: score = 0
G < -eps: score = G * (1+D+P)
```

G проверяет и slope, и фактический endpoint основного горизонта. Не брать
минимум темпа по всем окнам: ровная последняя неделя обнулила бы качество
положительного ступенчатого роста. Свежая динамика учитывается отдельным
классом продолжения роста перед score (§8 ниже).
Q понижает неровный рост на одинаковой grid. D учитывает максимальную raw
просадку; P дополнительно штрафует незавершённое восстановление в текущем H.
При G<0 штрафы делают score хуже, а не приближают его к нулю. Нулевая DD
не создаёт бесконечный score. Ни hidden percentile, ни подгоняемых весов нет.

Но это **эвристика качества**, не статистически выведенный оптимум:
OLS и endpoint реагируют на положение редких сделок, D и P частично связаны,
а относительный рост остаётся зависимым от sizing. Более доходная, но рискованная
кривая иногда получит больший score. Проверка на M0 обязательна; название
«качество equity» не заменяет отдельные лимиты риска и действующие фильтры.

### Точный порядок и downstream semantics

Сортировка — кортеж, меньший лучше:

```text
(growth_class, -score12, D_H, P_H, -H, strategy_id)
growth_class = 0 if GROWING
               1 if WEAKENING
               2 if neither above and G >= -eps
               3 otherwise
```

Так продолжение роста важнее величины score: растущая молодая стратегия
может оказаться выше зрелой с падающим хвостом. Внутри класса сначала
качество, не возраст. Более длинный горизонт — только tie-break. Класс и
H показываются рядом с существующим score. Разные H имеют разную дисперсию
оценки даже после нормировки на 30d; M0 обязан показать сравнение молодых
и зрелых кривых и пограничные случаи перехода 7->14->28.
Сортировка только по видимому score не воспроизводит Final rank.

Adversarial acceptance: WEAKENING со score10 обязан быть ниже GROWING со
score1, поскольку class первичен. Внутри каждого класса сохраняется весь
прежний tie-order. H28 UP с одновременно отрицательными7d и14d — PASS/class1.

Последний tie-break уникален: loader собирает `candidates` по strategy_id,
order-join rows объединяются в одну строку, current result один на стратегию.
Разные lot variants имеют разные strategy_id. На входе ranker проверить
уникальность strategy_id; duplicate candidate IDs — typed invalid input,
не случайный порядок строк. Equal-metric variants обязаны давать одинаковые
места при перестановке входа и числе workers.

Новый метод возвращает scores и ordered indexes в существующий общий блок
аналогов/представителей/Top N. Сохраняются prior REJECTED -> RESERVE,
невыбранные rankable представители -> RESERVE, аналоги -> ANALOG,
неоценённый представитель -> RESERVE. N применяется к представителям,
а не строкам до analog grouping. Если final rank включён при выключенном
ERF, отрицательные score остаются rankable и могут заполнить N; это
ранжирование, не скрытый фильтр. Включённый ERF даёт описанный допуск
с явным NOT_EVALUATED для неизвестных данных.

Ранжирование охватывает surviving Pair+Side после всех enabled filters.
Не возвращает ранее исключённых. Метод сам не отключает A/B, Pareto или
lot-stage. Для нового ranking все current ACTIVE результаты **входного**
Pair+Side/cohort до применения фильтров должны иметь одинаковый report_end_utc.
Это сознательное ограничение сопоставимости, проверяемое дешёвым metadata
preflight до source load, workers и cache publication selection-запроса.
Иначе `EQUITY_RANK_END_MISMATCH` с result_id/date pairs; нет скрытого общего
cutoff, автоматического удаления чужих дат или ranking по группам дат.
Оператор может явно получить единый период ретестом либо использовать
robust/filter-only; сам запрос тестер не запускает. Общая явная подготовка
facts без нового ranking-запроса может кэшировать разные T: self-only facts
не требуют общей даты. M0 проверяет применимость ограничения на реальном
cohort. Пересечение фильтров и rank-only явно показывается в UI.

## 9. Кэш, ревизии и жизненный цикл

Один новый pure module `performance_v2_equity_quality.py`: типы sample/window,
проверка пригодности, geometry, self-only diagnostics, deterministic math.
Принимает equity и минимальные position-state facts; не импортирует DuckDB,
HTML, filesystem, network. Это необходимое расширение входа относительно v11.

Один adapter `performance_v2_equity_cache.py`: bounded loading, publication,
readiness, strict decode. Не вводить общий framework кэшей и не рефакторить
весь `panel.py`. Интеграция переиспользует `_selection_window_job` и его
`_load_source`: один source load на result для old windows и equity facts
вместе, без изменения wallet/A/B семантики. Второй полный проход по DB и
второй worker pool для equity не допускаются.

Таблица `equity_quality_metrics`:

```text
result_id BIGINT
source_revision VARCHAR
algo_version VARCHAR
facts_json VARCHAR
facts_sha256 VARCHAR
calculated_at_utc TIMESTAMPTZ
PRIMARY KEY(result_id, algo_version)
```

Cache stores facts/availability, **не final rank, не stage gate, не peers**.
Политика classifier/filter/ranking версионируется отдельно и вычисляется из
готовых facts. Canonical JSON включает version и точные keys/types, timestamps
UTC, Decimal strings; decoder проверяет finite, диапазоны и digest.

Revision = canonical digest текущих `result_id`, `imported_at_utc`, report
и effective bounds, плюс сохранённый source report SHA при его наличии.
Не требовать optimizer-prepared inputs или пригодного sizing. Все штатные
REPLACE writers меняют imported_at; прямое ручное редактирование DB вне
writer-контракта не поддерживается. Не добавлять повторную сериализацию и
hash всего raw потока ради нового кэша: source revision и сохранённого
import source SHA достаточно в рамках штатного writer-контракта.

Workers читают одну согласованную source revision в read transaction;
single writer перед upsert проверяет ту же current revision. При изменении
не публиковать старые facts: typed `EQUITY_SOURCE_CHANGED`, повторный явный
пересчёт. Те же revisions и facts digests проверяются перед сохранением
selection snapshot, поскольку один result_id не обнаруживает REPLACE race.
Не импортировать эту проблему из существующего ID-only stale check.

Schema v5 -> v6: через existing migration chain и exact catalog validation,
не opportunistic CREATE в preview. Не добавлять FK автоматически: следовать
стилю `window_metrics` и протестировать штатный REPLACE/prune с текущей
версией DuckDB; child lifecycle следует описанным ниже writer boundaries. Fresh DB и
поддержанные 2->3->4->5 chains должны достигать v6. До релиза создать ADR
о новом data contract; старые ADR не переписывать.

### Read-only совместимость до миграции

Exact validation не ослабляется до «таблица опциональна». Небольшой helper
`require_performance_v2_readable(connection) -> int` принимает **точный**
валидный каталог v5 либо v6 и возвращает его версию, без DDL/DML. Обычный
`require_performance_v2` остаётся строгим для v6 writers. Матрица:

| Открытая база | Read-only export / legacy review-read / candidate read | Equity readiness |
| --- | --- | --- |
| Valid v5 | Прежние колонки/поведение, без SELECT отсутствующей equity table | При enabled consumer not-ready `EQUITY_SCHEMA_UPGRADE_REQUIRED`; disabled — legacy readiness |
| Valid v6 | Текущий контракт, cached facts только при наличии/валидности | Проверка presence/revision/algo/digest |
| v6 marker, но отсутствует новая table / другой catalog defect | Typed schema error, не fallback к v5 | Schema error |
| v2/v3/v4 | Typed upgrade-required; read-only поддержка не расширяется | Upgrade required |

Текущий код до фичи требует именно v5 для read-only, поэтому v2–4 не являются
новой регрессией. Явный existing initializer в writable workflow мигрирует
поддержанные версии до v6; обычное открытие read-only connection никогда
не инициирует migration/repair. Snapshot persistence и review **import** —
writers, требуют инициализированной v6; review **read** v1 на v5 доступен.
Read-only v5 Performance XLSX сохраняет прежнюю форму без equity block, а не
создаёт четыре бессодержательных колонки. Tests проверяют catalog/hash до/после,
нулевые writes, explicit initialize v5->v6 и fail-closed damaged v6.

REPLACE удаляет derived cache в той же import transaction, что обновляет
imported_at_utc и source rows. Для prune сохранить существующую границу
атомарности: writer lock, checkpoint, проверенный whole-DB backup, удаление
child/parent, restore backup при ошибке; не обещать несуществующую общую
DuckDB transaction для текущего prune.

Проверенная карта lifecycle: `performance_v2_import.py` REPLACE child loop;
`performance_v2_prune.py` `_CHILD_TABLES`, `_plan` count lists, `_delete` child
lists и parent deletion. Другого production single-result/bulk/cohort delete
пути по поиску текущего дерева не найдено. Prune одного/нескольких результатов
покрывает оба размера удаления; backup/restore всего файла включает новую
таблицу. При реализации повторить поиск, включить каждый появившийся writer.
Read-only XLSX не копирует/не меняет DB и не инвалидирует кэш. Проверки:
zero orphan rows после prune, полное восстановление новой таблицы при сбое,
same-ID REPLACE никогда не возвращает stale facts.
Пропавшая строка кэша — не poor equity. При включённом ERF или включённом
новом ranking отсутствие/старая версия/повреждение cache останавливает request
с `EQUITY_CACHE_INCOMPLETE`; cached UNKNOWN считается рассчитанным результатом.
При выключенных потребителях старый selection работает без нового кэша.

Readiness = old window readiness **AND** equity readiness, когда включён
ERF либо enabled final rank с equity method. Equity readiness требует для
каждого входного result точную текущую revision, algo_version, наличие строки,
валидную canonical структуру и совпадение facts_sha256. Missing/stale/corrupt
дают not-ready; cached UNKNOWN ready. При выключенных обоих потребителях —
ровно прежняя readiness. Toggle checkbox/method обновляет эту проверку,
но не инвалидирует рассчитанные facts. Scoped RETEST не проверяет чужие IDs.

«Перерассчитать факты» и «Все пары» могут подготовить новые facts даже при
выключенных потребителях: явный запуск, targeted missing IDs, существующий
единый runtime workers limit (загружается из локальных import settings,
обычно 16), один writer. REPEAT без изменений не читает raw повторно.
Bounded memory: не загружать все equity всех стратегий в один DataFrame.

Candidate memory-cache identity расширяется algo version + scoped revisions
+ scoped facts digests; freshness не зависит только от count/max timestamp.
LRU хранит только settings-independent candidates/facts, не final decisions.
Checkbox/method/N/order/policy применяются заново после lookup, поэтому не
нуждаются в raw recompute; это не разрешение возвращать старый final result.

Разрешение cached facts не зависит от toggle: для v6 candidate loader всегда
делает дешёвую попытку прочитать имеющиеся facts, даже при обоих OFF, без
compute/write и без обязательности их наличия. Для valid v5 — schema sentinel,
без запроса несуществующей таблицы. Per-result key component:

- свежие facts: `(source_revision, algo_version, facts_sha256)`;
- отсутствуют: `(ABSENT, result_id, source_revision)`;
- stale: `(STALE, result_id, source_revision, stored_algo, stored_digest)`;
- invalid payload: `(INVALID, result_id, source_revision, observed_payload_hash)`;
- v5: `(SCHEMA5, result_id, source_revision)`.

Disabled legacy request не падает из-за отсутствующих/invalid equity facts;
enabled consumer не использует sentinel как оценку и возвращает not-ready.
Warm entry, созданное при OFF и уже существующих свежих facts, сохраняется
при ON/method/N/order changes. Cold sentinel -> явная публикация новых facts
меняет key: это **правильный LRU miss**. Не обещать single-entry reuse между
cold и warm. Переключение ON при cold не запускает расчёт в preview.
При RETEST_COHORT все queries и decisions ограничены точным server-owned
набором успешных замен; чужие строки не влияют даже на readiness/date check.

### Скорость — обязательный контракт реализации

В текущем коде `_selection_window_job` уже читает source один раз для всех
недостающих старых окон. `prepare_selection_window_cache` использует
ThreadPoolExecutor, а затем одного writer. Это точка расширения, не повод
создавать ещё один путь подготовки.

1. Одним scoped запросом определить missing/stale old windows и equity facts.
   Warm результат не отправлять в worker и не переписывать в DB. Смена
   checkbox, N, метода, порядка стадий и policy-only версии не пересчитывает
   исходную геометрию. Изменение ab_final_days не инвалидирует equity cache.
2. Если нужны old windows, передать тот же уже загруженный source двум pure
   вычислителям. Если нужен только equity cache, читать только последние
   H дней плюс один предшествующий sample и состояние позиции на/до него;
   actions от predecessor sample до T нужны целиком. В обоих случаях source согласован
   по revision. Не читать далёкую историю для отсутствующих historical metrics.
3. Один merge-проход equity/actions, до 113 grid points (28d/6h+1).
   Логарифмы вычислить один раз для grid, не для каждого raw sample и окна.
   Три OLS/ER на slices того же массива; один raw DD/P проход для H.
   Raw Decimal сравнения сохраняют spikes; Decimal ln только на малой grid.
   Целевая сложность O(samples + actions + grid), без pandas resample/concat
   и DataFrame на каждую стратегию. Новых зависимостей нет.
4. Один существующий workers limit, обычно 16; DuckDB worker `threads=1`.
   Не обещать 16-кратного ускорения Python/Decimal в ThreadPool. На M0/M5
   сравнить 1/4/8/16 workers; другой executor вводить только после профиля.
   При pool size w одновременно обрабатывается <=w результатов. Размер
   порции <=2*w jobs: ещё не начатые jobs содержат только metadata; завершённые
   — только compact metrics/facts. Одновременно живы raw arrays не более w
   результатов, они освобождаются по завершении result. Это не абсолютный
   bound в байтах: одна очень большая стратегия может доминировать по памяти.
   Профили 1/4/8/16 используют именно этот одинаковый контракт.
5. Сначала закрыть read-only connections порции, затем одной write transaction
   проверить revisions и batch-upsert её результаты. Не держать одновременно
   несовместимые read_only/read_write DuckDB handles. При race порцию откатить,
   выдать typed conflict; уже записанные валидные порции остаются пригодными.
6. Новый warm path: ноль raw equity reads, ноль дополнительных raw actions
   reads, ноль cache writes. Это не утверждение о всём старом preview:
   legacy holding/best-trade facts пока читают actions. В тестах отделять
   неизменный baseline от новых reads, а не скрывать существующее поведение.

Benchmark protocol: frozen DB copy, один и тот же cohort/hardware/settings,
раздельные состояния old-cold+new-cold, old-warm+new-cold, all-warm и один
REPLACE. Три измеренных прогона после одного прогрева кода; cold означает
пустые derived tables на отдельной копии, не заявление о холодном OS cache.
Записывать median wall time, max observed time, peak RSS, raw rows/queries,
cache writes и версии. Исходный baseline — код до интеграции, не новый
код с выключенным UI checkbox, который всё равно мог бы считать facts.

Предлагаемые бюджеты для принятия на M0: дополнительный cold median
<= max(20% baseline, 0.5s), warm median <= baseline+max(10% baseline, 0.05s);
peak RSS <= baseline+max(25% baseline, 64 MiB). Это targets, **не измеренные
обещания**. Для old-warm+new-cold отдельно показать стоимость одноразового
backfill — её нельзя выдать за нулевой warm overhead. Если target не выполнен,
профилировать load/compute/publish и пересмотреть реализацию либо согласовать
изменение бюджета, не объявлять незаметно пройденную приёмку.

## 10. Snapshot, review, export и UI

Для нового behavior сохранять selection contract v2, method, policy version,
effective stage order как массив, per-result revision и facts digest, а также
фактически использованные decision metrics/quality/evidence class в immutable
snapshot metadata. Использовать JSON-поля существующих selection tables;
не хранить raw series и не создавать ещё одну selection table. При удалении
кэша snapshot должен объяснять прежнее решение по сохранённым входам.

Legacy request без equity fields и с robust default сохраняет старую canonical
форму/hash. `asdict()` с новым полем method не должен молча менять все hashes:
нужна version-aware canonical projection. Review importer поддерживает прежний
contract v1 и новый v2, не переписывает старые snapshots. Workbook schema можно
оставить v1, если additive columns не меняют его защищённые поля; dispatch по
selection contract должен быть явным. RETEST-only import по ID/database identity
сохраняет прежние правила. В новом review сравнивать revisions, а не один ID.

New metadata не должна исчезать в export, bulk final retest recovery и
equivalent-run checks. Все фактические rank consumers используют сохранённый
method или legacy default, а не второй неявный выбор алгоритма.

XLSX writer остаётся общий для Pareto/Performance export/control. В конце
All candidates и Finalists один компактный equity block:

```text
Equity state | Equity basis | Equity DD, % | Equity smoothness
```

Всего **четыре новые видимые колонки**. State — класс роста либо причина
неоценённости; basis — например `28d / OK`, `14d / PARTIAL`, `7d / PROVISIONAL`
или `28d / UNKNOWN_GAP`; DD — 100*D_H; smoothness — ER_H. Для invalid facts
числа blank, а причина видна. Метрики заполняются для всех исходных строк
из свежего cache, включая удалённые lot-stage. Отсутствующий кэш не должен
выглядеть как SHORT/плохой результат.

Score и rank используют **существующие** final-score/final-rank колонки,
без второй пары. Score — только для выбранного equity method и фактически
оценённых ранкером строк. Название метода записывается в **комментарий к
существующей score cell/header cell и workbook metadata**; ни одна existing
protected header string review-import не меняется. Старый robust
score нельзя выдавать за equity score и наоборот. Полные 7/14/28 facts,
G/Q/D/P, source revision, quality reasons и версии — только snapshot/audit
JSON, не ещё 15 hidden columns. В existing hidden metadata — ссылка/ID и
минимальный контракт; Excel не дублирует всю DB и не служит audit source.

При полностью legacy request сохраняется прежний набор колонок. Для нового
selection block включён; read-only Performance export добавляет его только
при наличии свежих equity facts хотя бы у одной экспортируемой строки.
Null -> blank, unassessed stage -> N/A, не PASS и не ноль. Не применять generic
quantize(.01) к исследовательским полям. Excel numeric cells сохраняют допустимую
числовую точность; formats 4–6 decimals служат только для показа. DB JSON хранит
полную canonical precision. User Status/RETEST и review-editable поля сохраняют
прежний смысл и взаимный порядок.

Read-only Performance export не пересчитывает и не мигрирует DB: показывает
имеющиеся свежие facts, отсутствующие — blank с quality marker. Он не выбирает
новый ranking при экспорте; effective statuses остаются из сохранённого review.
Ручной FINALIST override сохраняется: ERF — автоматический отбор, не глобальный
запрет операторского решения. Не менять portfolio admission в рамках этой фичи.

UI: checkbox №2 без move/scope controls, native method select в существующем
Top N, текущий input N. При смене checkbox/method обновляются readiness и preview
с revision guard от устаревших HTTP responses; тяжелый compute только по явной
кнопке. Нет нового dashboard или отдельного ERF API. Сохранять клавиатурную
доступность, связанные labels, aria-live статусы и fixed-prefix barrier.

## 11. Acceptance и внедрение

1. M0: диагностический аудит наблюдений и synthetic cases; без изменения
   finalists/DB. Зафиксировать ограничения sampling, сравнение 1/3/6h и
   репрезентативные примеры continued growth, slow-up, plateau, jump, crash,
   recovery и <28d. Отдельно согласовать политику возраста/score по этим примерам.
2. M1: pure engine + tests + единая быстрая подготовка, cache lifecycle/migration
   на fixtures/DB-copy; измерения нагрузки обязательны.
3. M2: опциональный фильтр, lot guard и preview traces.
4. M3: метод ranking, общая analog/Top N логика, snapshots/review/backward compatibility.
5. M4: UI/shared XLSX/readiness/RETEST integration, focused verification и review.
6. M5: read-only comparison old/filter-only/rank-only/both на одном frozen
   cohort; operator acceptance. Новые defaults не включать автоматически.

Дополнительный M0 release gate для редкой торговли: репрезентативный вручную
проверенный cohort с report age >=7d. Частота `r = completed round trips
within H * 7/H`; strata r=0 / 0<r<=3 / r>3 в неделю, без пропуска дробных
частот. Считать долю coverage-NOT_EVALUATED и RESERVE-due-to-coverage
отдельно по причинам missing boundary, unknown initial position, mid-window
open/unknown gap; другие invalid источники считать отдельно, не терять их.
Предлагаемый предел: <=5% coverage-unrankable в целом и в каждом sparse
stratum (r=0 и 0<r<=3); для hand-verified complete flat histories и synthetic
quiet-week — **0 ложных unassessed/coverage-RESERVE**. Порог 5% — проектный
release budget для согласования на M0, не уже полученный результат и не
изменение формулы. Размеры strata и все доли публикуются; пустая stratum
не считается пройденной. Если качество отчётов не позволяет выполнить gate,
показать дефицит MTM evidence и согласовать изменение источника/дизайна,
а не незаметно ослаблять coverage или исключать непокрытые строки из знаменателя.

M0 проверяет решение пользователя R6: **ноль ERF BLOCK исключительно из-за
короткой коррекции при валидном H UP**. Для тех же strata показать долю
WEAKENING и изменение порядка в equity-ranking, denominators всех строк и
H-UP строк, распределения коротких slope/endpoint/return и примеры графиков.
Отказ других фильтров, lot/analog selection и RESERVE за пределами Top N
не смешивать с ERF BLOCK. Выбор «отсекать либо только понижать» уже сделан;
не запрашивать повторное согласование отсева и не подбирать новый tolerance.
Оценка покрытия, формы ранжирования и бюджета скорости на M0 остаётся.
Technical epsilon отвечает только за численный шум. Уменьшение1060 до1055
в probe — около0.472%, не0.05%.

Проверки обязаны включать: масштабирование equity, перестановки входа,
равные timestamps, intra-grid drawdown, flat tails, open unknown gaps,
nonpositive equity, 6.99/7/13.99/14/27.99/28 дней, испорченное старшее окно
без fallback, редкие сделки и ровные 7d при растущих 14/28, отрицательные 7d
при растущем H, lot-good/bad winner, переходы возрастных H,
negative-score monotonicity, unchanged legacy hashes, REPLACE с
тем же ID во время worker/publish/snapshot, cache-only raw-read prohibition,
prune/rollback, old/new review round-trip, точный RETEST cohort,
one-source-load, unchanged-cache zero writes и описанные speed/memory budgets.

Воспроизводимость тестов и trace — критерий реализации. Предиктивное улучшение
проверяется отдельно walk-forward на отложенных периодах с фиксированной policy,
без подбора окон/штрафов по будущим победителям; этот проект не объявляет его
доказанным. Наличие инженерного PLAN_APPROVED тоже не доказывает качество score.

Уточнения к evidence после review: показать распределение live gaps рядом
с coverage denominators, включая отличие от ограниченной выборки восьми
результатов; correction examples привязать к strategy_id. В benchmark
отдельно измерить v6 с обоими controls OFF, где optional facts read всё
равно выполняется. Зафиксировать штатный writable initialization entry point
панели и проверить переход v5 upgrade-required -> migrated v6 -> ready.

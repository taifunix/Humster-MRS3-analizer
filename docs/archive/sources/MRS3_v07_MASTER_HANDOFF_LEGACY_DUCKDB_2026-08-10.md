# MRS3 v0.7 — полный handoff: DuckDB, legacy-режим и переход к портфельному анализу

**Дата:** 10 августа 2026  
**Статус на момент handoff:** пользователь запустил параллельный компактный импорт HTML в DuckDB. Сам импорт, materializer выбранного периода и MRS3 v0.7 ещё не завершены и не должны считаться выполненными.

Этот документ заменяет прежний неполный handoff. Он охватывает весь ближайший контур работы: завершение сохранения HTML, построение единого набора точек, legacy-запуск MRS3 v0.7, подготовку JSON-кандидатов, tick-тесты и данные для следующего портфельного этапа.

---

## 1. Цель ближайшего этапа

Нужно превратить массив HTML-отчётов MRS2 в постоянную компактную DuckDB, после чего HTML можно будет удалить. На основе этой БД и старого summary CSV выполнить **MRS3 v0.7 в едином временном legacy-режиме** и получить проверяемый набор 1ORD / 2ORD / 3ORD / 4ORD кандидатов для реальных MRS3 tick-тестов.

Текущий результат не должен имитировать то, чего в исходниках пока нет:

- у fine HTML есть исходные операции и точный wallet/equity ряд;
- у старого coarse CSV операций нет;
- поэтому единый запуск сейчас обязан использовать один и тот же proxy для обеих групп;
- полноценный event-aware MRS3 возможен позднее, когда raw HTML покроет всю рабочую сетку shifts.

Стратегия MRS3: LONG открывается ниже своей Open MA, SHORT — выше; для 2–4 входов используются собственные Open MA и shifts, но у всей позиции одна общая Close MA.

---

## 2. Источники, их роль и единое сравнительное окно

| Источник | Сетка и покрытие | Что фактически хранится | Роль в текущем запуске |
|---|---|---|---|
| Fine HTML | shifts `0.3–1.5%`, шаг `0.1%`; LONG и SHORT | настройки точки, все raw операции, полный equity, изменения wallet, исходные timestamps | После materialize даёт точные point metrics на общем окне. Raw данные сохраняются для будущего event-aware режима, но сейчас не меняют отбор. |
| Старый coarse CSV | step `0.4%`; в том числе shifts выше fine-диапазона; нет операций | summary MRS2: параметры и агрегированные PnL/DD/WR/PF/TotalTrades | Дополняет общий universe, но только когда его `StartDate` и `EndDate` в точности совпадают с общим окном. |
| MRS3 template JSON | LONG/SHORT шаблон | структура параметров для `mrs3.ma_long`, `mrs3.ma_short`, общих Close MA | Из него генерируются candidate JSON. |
| Listing dates | даты начала торговли каждого инструмента | символ → дата листинга | Для `EffectiveStart=max(ReportStart, ListingDate)` и проверки достаточности истории. |

**Единственное окно сравнения текущего legacy-run:**

```text
[2026-07-15 00:00:00, 2026-08-06 00:00:00)
```

Правая граница исключается. Она не выводится из имени HTML. В raw HTML обнаруживался диапазон `2026-07-01 — 2026-08-08`, но для текущего сравнения это не меняет заданное окно.

### Дедупликация и источник истины

Ключ точки для объединённого набора:

```text
symbol + side + timeframe + open_ma + close_ma + entry_multiplier/shift_bp
```

Дополнительно все сравниваемые значения должны относиться к одному и тому же периоду `[2026-07-15, 2026-08-06)`.

- Если совпадает только SHA-256 исходного HTML, повтор пропускается как `SKIPPED_IDENTICAL`.
- Если совпадают параметры точки и временная сетка, но HTML другой, импортёр отправляет его в quarantine как canonical conflict: молча подменять данные нельзя.
- Если fine materialized point и coarse CSV дают одну и ту же точку на общем окне, в финальном legacy-input остаётся **fine** как источник с более полным первичным материалом; coarse строка помечается `COARSE_SHADOWED_BY_FINE` в audit.
- Если метрики fine и coarse для такой дублирующей точки заметно расходятся, это не «усредняется». Строка попадает в отдельный `source_conflicts` audit и требует проверки параметров/периода до MRS3 run.
- Позже raw HTML для shifts выше `1.5%` дополняются тем же образом. Ранее оставшаяся coarse точка заменяется raw-derived fine point только при точном совпадении ключа и периода; пересечений в universe быть не должно.

---

## 3. Текущее состояние DuckDB-импорта

### 3.1. Используемая версия

Пользователь сейчас должен запускать именно v4:

```text
mrs3_html_parallel_compact_importer_v4.py
RUN_MRS3_PARALLEL_COMPACT_IMPORTER_V4_20_WORKERS.bat
```

На SSD установлено `WORKERS=20`. Архитектура:

```text
20 процессов: parse HTML + lossless compression
                 ↓
          один процесс DuckDB writer
```

Один writer намеренно сохраняет целостность базы и дедупликацию. Параллельная запись несколькими процессами в один DuckDB файл не используется.

v4 использует проверенный codec из `mrs3_html_compact_importer_v3.py`, поэтому **v3 Python-файл должен лежать рядом с v4**. Это не означает запускать v3 `.bat` или создавать v3-базу.

В Windows папке пользователя должны лежать:

```text
D:\SHARE\!MN\hamster\hb\tester\report\
├── mrs3_html_parallel_compact_importer_v4.py
├── RUN_MRS3_PARALLEL_COMPACT_IMPORTER_V4_20_WORKERS.bat
├── mrs3_html_compact_importer_v3.py
└── my_test1\
    └── *.html
```

v4 создаёт только:

```text
D:\SHARE\!MN\hamster\hb\tester\report\mrs3_parallel_compact_v4.duckdb
D:\SHARE\!MN\hamster\hb\tester\report\mrs3_import_audit_v4\
```

### 3.2. Что в базе хранится действительно

Это компактное **lossless** raw-хранилище, не сводная таблица сделок:

| Таблица | Содержимое |
|---|---|
| `schema_info` | Версия схемы: должна быть `4`; режим хранения. |
| `point_configs` | Параметры единичной MRS2-точки: symbol, side, TF, Open MA, multiplier, Close MA. |
| `time_grids` | Общая сжатая сетка timestamp для отчётов с одинаковой временной сеткой. |
| `report_runs` | Один импортированный HTML: SHA-256, canonical key, настройки, число raw actions, число equity samples, число wallet changes. |
| `report_payloads` | Один набор трёх сжатых BLOB на HTML: actions, equity, wallet changes. |
| `rejected_imports` | HTML, которые нельзя безопасно импортировать без разбора причины. |

Хранение не содержит построчных `equity_samples`, `wallet_changes` или `trade_actions`; если такие крупные таблицы появились, запущена не v4.

Внутри каждого HTML сохраняются:

- все исходные строки операций, включая `opened`, `increased`, `decreased`, `closed`;
- каждый исходный sample equity;
- все изменения wallet; значение wallet в произвольный момент восстанавливается последним изменением не позднее этого момента;
- settings, источник и временная сетка.

На проверочном реальном HTML были losslessly восстановлены 257 операций, 4 201 значение equity и 184 изменения wallet. Это проверка формата на sample, а не подтверждение завершения полного пользовательского batch.

### 3.3. Что считать успешным завершением импорта

После окончания v4 в `mrs3_import_audit_v4\import_manifest.json` должны быть:

```json
"schema_version": "4",
"workers": 20,
"quarantined_reports": 0
```

И в `html_delete_checklist.csv` для каждого удаляемого HTML должна быть строка с `safe_to_delete=YES`.

До этого HTML не удалять. Старую строчную базу `mrs3_source.duckdb` также пока не удалять: она была v1, с миллионами row samples, и не должна смешиваться с v4.

**Запрещённые выводы до проверки:**

- не утверждать, что итоговая БД точно займёт заданное число гигабайт: оценка по одному HTML не заменяет bulk-замер;
- не называть все raw action rows «сделками»: 257 действий не равно автоматически `TotalTrades` summary;
- не утверждать, что импорт завершён только по наличию файла базы: важны schema `4`, manifest и quarantine.

---

## 4. Materializer: из raw DuckDB в точки общего периода

Следующий программный компонент — отдельный тестируемый **materializer**. Он не должен изменять raw payloads и не должен заново читать HTML.

Для каждого `report_id` он:

1. Декодирует `time_grid`, equity, wallet changes и raw actions.
2. Берёт интервал `[2026-07-15 00:00:00, 2026-08-06 00:00:00)`.
3. Берёт последнее состояние не позднее начала окна как boundary baseline, затем samples внутри окна. Это нужно, чтобы начало кривой и MaxDD не обрывались искусственно.
4. Восстанавливает wallet/equity на границах в согласованной с HTML семантике.
5. Рассчитывает и записывает точечные метрики периода.
6. Формирует строку `point_period_metrics` с ключом:

```text
report_id + window_start_ms + window_end_ms + materializer_version
```

### Обязательные поля `point_period_metrics`

| Группа | Поля |
|---|---|
| Идентификация | `report_id`, `point_id`, symbol, side, timeframe, Open MA type/source/len, entry multiplier, `shift_bp`, Close MA type/source/len. |
| Период и происхождение | `window_start`, `window_end`, `data_source=FINE_HTML`, `raw_coverage=FULL`, `materializer_version`, входной SHA-256. |
| Результат | начальный/конечный wallet, начальный/конечный equity, `pnl_abs`, `pnl_pct`, `final_balance`, `max_drawdown_abs`, `max_drawdown_pct`. |
| Торговые агрегаты | `total_trades`, wins, losses, `win_rate_pct`, `profit_factor`; плюс `trade_count_definition`. |
| Качество | `boundary_baseline_found`, `equity_sample_count_in_window`, `action_count_in_window`, `metric_status`, `metric_notes`. |

### Критическая проверка семантики `TotalTrades`

Нельзя принять `len(raw actions)` за число сделок. В проверочном HTML присутствуют частичные увеличения и сокращения позиции. В разных отчётах summary `TotalTrades` может означать цикл, закрытие или другую агрегированную единицу.

Перед массовым legacy-output materializer обязан на 3–5 отчётах сверить с исходной статистикой HTML и/или прошлым fine CSV:

- PnL / `TotalPnLPercent`;
- MaxDD;
- `TotalTrades`;
- WinRate;
- ProfitFactor.

До подтверждения алгоритма строки получают `metric_status=UNVERIFIED_TRADE_AGGREGATION` и не должны попадать в MRS3. PnL и DD можно анализировать отдельно, но нельзя подставлять выдуманное `TotalTrades` в eligibility и proxy-events.

---

## 5. MRS3 v0.7: что сохраняется и что меняется

### 5.1. Сохраняется из v0.6

Геометрия и экономическая логика v0.6 остаются действующими:

- 3D пространство `Shift × OpenMA × CloseMA`;
- экономические gates: `PnL > 0`, `WinRate >= 70%`, `DD > 0`, `DD <= 11%`, `PnL/DD >= 3`;
- `EffectiveStart=max(ReportStart, ListingDate)`, основной historical gate: не менее 7 дней;
- CORE link между соседями: минимум retention по PnL и PnL/DD не ниже 90%;
- envelope plateau: не ниже 75%; READY plateau содержит минимум две CORE-связанные точки;
- 5%-equivalent: отличие не более 5% одновременно по PnL и PnL/DD; это tie-break, а не замена robustness;
- Close MA support: `PRIMARY`, затем contiguous `CORE_CLOSE >=90%` и `SUPPORTED_CLOSE >=75%`; первое непрохождение прерывает расширение;
- первая MRS3 entry-точка обязана быть `standalone_eligible`; глубинные точки могут быть только `depth_eligible`;
- только 2, 3 или 4 order structures; shifts строго возрастают; один plateau нельзя использовать дважды;
- gap rules: если левый shift `<1.5%`, минимум 0.6%; от `1.5%` до `4.0%`, минимум 0.8%; глубже `4.0%` — отдельный `DEEP_GAP_RESEARCH`;
- варианты лотов: `EQUAL` и `INCOME`; оба генерируются для каждой READY структуры;
- Close multiplier: LONG `1.003`, SHORT `0.997`;
- JSON создаётся с `is_runing:false`; LONG и SHORT проходят раздельно через `use_long/use_short`.

Sample eligibility также сохраняется отдельно от нового event-filter:

| Shift | Absolute floor | Relative factor |
|---|---:|---:|
| `<=2.0%` | 10 trades | `1.00` до 1.5%; `0.90` от 1.5% до 2.0% |
| `>2.0%` | 5 trades | `0.30` до 3.1%; `0.20` до 4.7% |

`MinTrades=max(absolute floor, ceil(EffectiveDays × BaseRateTF × ShiftFactor))`.

### 5.2. Новое правило v0.7 — PointEventCount

Старая идея `raw Trades >= 3` для depth-order **удалена**. Вместо неё:

```text
Любая MRS2-точка, попадающая как любой ордер MRS3,
должна иметь PointEventCount >= 3.
```

Для будущего real-event режима `PointEventCount` — число уникальных независимых `event_id` именно этой MRS2-точки, а не число raw action rows.

Plateau пригодно для MRS3, только если в нём есть хотя бы одна economic-pass point с `PointEventCount >= 3`.

`PlateauEventCount` сохраняется только как диагностика; он не заменяет point-level gate и не является отдельным ranking factor.

### 5.3. Детали отбора после event-фильтра

Для Plateau + CommonCloseMA порядок обязателен:

1. Сначала сформировать 5%-equivalent group по прежним PnL и efficiency правилам.
2. Затем исключить точки с `PointEventCount < 3`.
3. Representative выбирать по:

```text
PointEventCount DESC
→ Shift DESC
→ PnL DESC
→ PnL/DD DESC
→ point_id ASC как финальный детерминированный tie-break
```

После event-filter заново пересобрать весь MRS3 universe: применимость plateau, CloseMA families, combinations, structures, lot variants, JSON и audit. Нельзя оставить готовые v0.6 structures и лишь отфильтровать их список задним числом.

---

## 6. Текущий временный режим: `legacy_trades_proxy`

### 6.1. Единственное правило текущего run

В объединённом запуске v0.7:

```text
event_mode = legacy_trades_proxy
PointEventCount = TotalTrades
```

Это применяется **ко всем** точкам: fine HTML и coarse CSV. У fine уже есть raw операции, но реальные independent events сейчас намеренно не используются в выборе.

Причина: реальные event counts для fine и proxy `TotalTrades` для coarse — разные шкалы. Их смешивание в одном ranking/selection сделает результат систематически перекошенным в сторону того источника, который измеряется иначе.

### 6.2. Что должно быть в каждой входной строке legacy run

| Поле | Fine HTML after materializer | Coarse CSV |
|---|---|---|
| `data_source` | `FINE_HTML` | `COARSE_CSV` |
| `raw_coverage` | `FULL` | `NONE` |
| `TotalTrades` | materialized и сверенный с определением метрики | исходная summary-метрика |
| `PointEventCount` | `TotalTrades` | `TotalTrades` |
| `event_count_mode` | `legacy_trades_proxy` | `legacy_trades_proxy` |
| `real_event_count` | может храниться только как raw diagnostic, но не используется | `NULL` |
| `report_start/report_end` | строго заданное общее окно | только строки с точно тем же окном |

В manifest, Excel и CSV audit запуска обязательно записываются:

```text
algorithm_version = 0.7
event_mode = legacy_trades_proxy
comparison_window = [2026-07-15 00:00:00, 2026-08-06 00:00:00)
fine_raw_event_data_used_for_selection = false
```

Не использовать статус `EVENTS_UNAVAILABLE`: отсутствие raw actions у coarse — известное свойство legacy-режима, а не ошибка входа.

### 6.3. Что legacy run позволяет и чего не доказывает

Он позволяет честно сравнить всю доступную сетку одинаковым historical proxy и выбрать геометрически устойчивые кандидаты.

Он **не** доказывает независимость сделок и не заменяет будущий event-aware run. При появлении raw покрытий coarse shifts весь universe надо заново materialize и запустить с `event_mode=real_independent_events`; смешанного режима быть не должно.

---

## 7. План реализации от текущей точки

### Шаг 1. Дождаться завершения v4 и проверить импорт

1. Не останавливать процесс, если он штатно продвигается и SSD/CPU не троттлятся.
2. По завершении проверить `schema_info.schema_version = 4`, manifest и `quarantined_reports`.
3. Измерить реальный размер БД и payload statistics на достаточно большой пачке; зафиксировать фактический средний размер на отчёт в audit.
4. Не удалять HTML, пока checklist не содержит `safe_to_delete=YES` для всех файлов, которые будут удалены.

### Шаг 2. Реализовать и проверить materializer

1. Ввести `point_period_metrics` и неизменяемый ключ версии materializer.
2. Декодировать compact raw и получить metrics строго на общем окне.
3. Верифицировать PnL/DD/Trades/WR/PF на 3–5 sample reports.
4. Записать отдельный `fine_materialization_audit.csv`: report ID, ключ точки, sample counts, исходные и вычисленные summary values, дельты, статус проверки.
5. Только verified rows допустить в MRS3 legacy input.

### Шаг 3. Нормализовать coarse CSV и собрать единый legacy input

1. Из coarse CSV оставить только LONG/SHORT строки, у которых начало и конец равны `[2026-07-15, 2026-08-06)`.
2. Привести имена полей к контракту MRS3 loader: symbol, side, timeframe, Open MA, Close MA, multiplier, shift, PnL, DD, WinRate, PF, `TotalTrades`, start/end, run ID.
3. Materialized fine points привести к тому же контракту.
4. Дедуплицировать по полному ключу точки; fine имеет приоритет, все решения видны в audit.
5. Для обеих групп выставить `PointEventCount=TotalTrades` и `event_count_mode=legacy_trades_proxy`.
6. Сохранить в DuckDB `legacy_run_points_v07` и экспортировать детерминированный совместимый CSV для первого запуска. Для loader, который ожидает numeric `Run id`, генерировать стабильный уникальный integer и проверять отсутствие коллизий.

### Шаг 4. Доработать код `mrs3_v06` до v0.7

Базовый код находится в:

```text
/workspace/scratch/3ebb8caf3a10/mrs3_v06
```

Рекомендуемая минимальная, безопасная реализация:

1. Сохранить v0.6 как неизменяемый baseline; создать отдельную ветку/папку v0.7 либо явно версионированный режим.
2. Расширить нормализованную point model полями `point_event_count`, `event_count_mode`, `data_source`, `raw_coverage`.
3. Сохранить `trades` для старых sample/economic отчётов; `point_event_count` использовать только для новой MRS3 order eligibility.
4. В legacy adapter заполнить `point_event_count=trades` для каждой строки.
5. В real-event adapter принимать только единый real events input; не добавлять fallback на `trades` внутри такого запуска.
6. Добавить point-level filter `point_event_count >= 3` в отбор order candidates и plateau suitability.
7. Изменить выбор representative согласно порядку из раздела 5.3.
8. Перестроить plateaus → close profiles → families → structures после event-filter.
9. Внести новые поля и режим в every audit table, workbook и run manifest.
10. Добавить unit tests: legacy mappings, запрет смешения режимов, event gate, representative ordering, plateau viability, rebuild after filtering, duplicate source policy.

Для первого legacy запуска безопаснее экспортировать совместимый CSV и задействовать существующий loader. Прямой DuckDB-loader имеет смысл только после успешной валидации CSV пути.

### Шаг 5. Выполнить MRS3 v0.7 legacy selection

Запуск должен создавать отдельную папку результатов и не перезаписывать `real_run*` v0.6. Минимальный состав:

```text
mrs3_v07_legacy_2026-07-15_to_2026-08-06/
├── run_manifest.json
├── audit.xlsx
├── audit_csv/
└── strategies/
```

После просмотра результата — отдельный эксперимент, не смешанный с базовым: hard gate `минимальный PnL любого MRS3 order >= 5%`, с фиксацией того, сколько READY структур осталось.

### Шаг 6. Tick-тест выбранных MRS3 JSON

Selector генерирует кандидаты, но не заменяет реальный MRS3 тест. Для каждого JSON и каждого lot variant запустить один и тот же тестовый период/параметры, собрать output CSV, затем провести фактическую DD5 нормализацию и final individual comparison.

---

## 8. Что именно выйдет из MRS3 v0.7 до MRS3 tick-теста

Это **предтестовый** результат. Он говорит, почему структура отобрана из MRS2-точек, но не выдает её реальную общую доходность/просадку как MRS3.

### 8.1. По каждой исходной MRS2-точке

- идентификатор и все параметры: pair, side, TF, Open MA, shift/multiplier, Close MA;
- источник (`FINE_HTML`/`COARSE_CSV`), raw coverage, сравнительное окно;
- PnL, DD, PnL/DD, theoretical `PnL_DD5`, WinRate, PF, `TotalTrades`;
- history и sample calculations: EffectiveStart, EffectiveDays, required minimum trades, `standalone_eligible`, `depth_eligible`;
- economic pass/reject и все причины reject;
- `PointEventCount`, его mode, прохождение `>=3`;
- принадлежность plateau, CORE/SUPPORTED роль, 5%-equivalent status, refine status.

### 8.2. По каждому plateau

- `plateau_id`, pair/side/TF, диапазоны shifts/Open MA/Close MA;
- все point IDs, CORE и supported точки;
- min/max PnL и efficiency, envelope, status READY/REFINE_REQUIRED;
- best PnL и best efficiency points;
- список standalone/depth candidates после event-filter;
- диагностический `PlateauEventCount` и явная маркировка `legacy_trades_proxy`;
- применимость в MRS3 (`plateau_mrs3_eligible`) и причина исключения при её отсутствии;
- CloseMA profile: primary close, поддержанные common Close MA, support values, continuous support range.

### 8.3. По базовой 1ORD

Для каждого Pair + Side будет максимум одна глобальная baseline 1ORD:

- параметры выбранной точки и её plateau;
- исходные MRS2 PnL/DD/WR/PF/Trades;
- `PnL_DD5_theoretical = PnL × 5 / DD`;
- eligibility и event mode;
- JSON и traceability до исходной point.

Это baseline для сравнения с будущими фактическими MRS3 2–4ORD test results, а не готовая портфельная рекомендация.

### 8.4. По каждой готовой 2ORD / 3ORD / 4ORD структуре

| Блок | Что фиксируется |
|---|---|
| Идентификация | `structure_id`, symbol, side, TF, common Close MA, order count, event mode, период. |
| Каждый order | order ID, source `point_id`, `plateau_id`, Open MA, shift, multiplier, source PnL/DD/PnL-DD, Trades, `PointEventCount`, CloseMA support, standalone/depth eligibility. |
| Геометрия | строго возрастающие shifts, gap проверки, distinct plateau check, общая Close MA и её support. |
| Статус | `READY_MRS3_STRUCTURE`, `DEEP_GAP_RESEARCH` или точная причина reject. |
| Предтестовые diagnostics | сумма source PnL, средняя source efficiency, число low-sample depth orders, priority order. Это **не** PnL/ DD готовой MRS3 стратегии. |
| Лоты | EQUAL: равные `lot_x`; INCOME: пропорционально source PnL; в каждом варианте сумма `lot_x=1`. |
| Исполняемый артефакт | две JSON на READY structure: `_EQUAL.json` и `_INCOME.json`, с полной трассировкой в audit. |

### 8.5. Полный audit v0.7

Обязательные таблицы Excel/CSV:

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
16_Event_Mode_and_Source_Audit
```

`run_manifest.json` должен хранить hash входов, версию алгоритма, окно, событийнный режим, source counts, dedup/shadow/conflict counts и все параметры алгоритма. Это необходимо, чтобы результат можно было повторить после удаления HTML.

---

## 9. Что появится только после реальных MRS3 tick-тестов

Для каждой выбранной структуры и каждого варианта лотов tick-тест даст реальные метрики **всей комбинированной MRS3 стратегии**, которые нельзя корректно сложить из MRS2 orders:

| Поле | Назначение |
|---|---|
| `RawPnL`, `RawPnLPercent`, FinalBalance | Фактический результат совокупной 2–4ORD логики. |
| `RawMaxDrawdownPercent` и drawdown/equity series, если отдаёт тестер | Реальная совокупная просадка, включая взаимодействие ордеров. |
| TotalTrades, WinRate, PF | Фактическая торговая статистика комбинированной стратегии. |
| Тестовый период, начальный баланс, комиссии, leverage, настройки | Условия сопоставимости кандидатов. |
| JSON hash и `structure_id` | Однозначная связь результата с отобранной структурой. |

Затем для каждого кандидата выполняется **реальный DD5 re-test**, а не только линейная оценка:

```text
k = 5% / RawMaxDrawdownPercent
lot_x_DD5[i] = lot_x_raw[i] × k
```

На DD5 run сохраняются:

- `PnL_DD5_actual`;
- `DD_DD5_actual`;
- `lot_x_DD5[]`;
- `TotalLotX_DD5 = sum(lot_x_DD5)`;
- `EffectiveDays`;
- `PnL30_DD5 = PnL_DD5_actual × 30 / EffectiveDays`;
- `CapitalRequirementProxy = TotalLotX_DD5 + DD_DD5_actual/100`;
- `CapitalEfficiency = PnL_DD5_actual / CapitalRequirementProxy`;
- `CapitalEfficiency30 = PnL30_DD5 / CapitalRequirementProxy`;
- Pareto status и near-tie decision.

`PnL30` — нормализованная скорость на данном коротком окне, не прогноз следующего месяца.

### Individual final ranking до портфеля

Pareto dominance для individual candidates:

- максимизировать `PnL30_DD5`;
- минимизировать `CapitalRequirementProxy`.

Если разница `PnL30_DD5` не превышает 5%, приоритет:

1. выше `CapitalEfficiency30`;
2. ниже `CapitalRequirementProxy`.

---

## 10. Данные, требуемые уже для оптимизации портфеля

Сразу после v0.7 selection у нас будут хорошие structural и individual pre-test данные. После MRS3 raw + DD5 tests появится сравниваемый individual strategy panel.

Но полноценную портфельную модель нельзя честно завершить без временных рядов уже готовых MRS3 стратегий. На портфельном этапе нужны:

| Категория | Нужные данные | Почему |
|---|---|---|
| Доходность | DD5 equity/PnL time series каждой готовой стратегии на одинаковом timestamp grid | Чтобы считать совместную доходность и зависимость результатов. |
| Риск | Drawdown path / equity path, а не только один MaxDD | Корреляция отдельных MaxDD не является портфельной просадкой. |
| Занятость | entry/close timestamps, размер позиции, available/free margin или хотя бы occupancy state | Для одновременной нагрузки на капитал и лимитера. |
| Маржа | фактический margin load по времени, leverage, liquidation buffer | `CapitalRequirementProxy` полезен временно, но не заменяет peak simultaneous margin. |
| Ограничения | лимитер, приоритет постановки, число одновременных стратегий/ордеров, депозит | Чтобы портфельный симулятор отражал фактическое исполнение. |

До появления этих рядов можно сделать только промежуточный **individual-candidate portfolio shortlist** через `PnL30_DD5` и `CapitalRequirementProxy`. Нельзя выдавать его за симуляцию портфеля.

Отложенные до портфельного этапа сущности:

- корреляция просадок;
- общий limiter;
- совместная margin load;
- liquidation buffer;
- фактическое время занятости капитала;
- очередность конкурирующих сигналов.

---

## 11. Будущий real event-aware режим

Его запускать только после достаточного raw HTML coverage на полной используемой сетке, включая shifts выше `1.5%`.

Тогда:

1. Декодировать raw операции каждой точки.
2. Определить единый алгоритм независимого `event_id` между MRS2-точками; он должен быть основан на экономическом событии, а не на количестве partial actions.
3. Рассчитать `PointEventCount` как уникальные event ID на каждую точку.
4. Запустить только `event_mode=real_independent_events`.
5. Полностью пересобрать plateaus/structures и сравнить с legacy run как отдельные результаты.

Нельзя делать переход частично: fine points с real events и coarse points с `TotalTrades` в одном run запрещены.

---

## 12. Контрольный список новой сессии

1. Сначала попросить результат v4: console tail, размер `mrs3_parallel_compact_v4.duckdb`, `import_manifest.json`, число quarantine. Не предполагать результат без этих фактов.
2. Проверить schema version `4`, отсутствие row-per-sample tables, и audit checklist.
3. Реализовать materializer только после подтверждения хранения raw data.
4. Сверить метрики materializer с 3–5 HTML/summary samples, особенно определение `TotalTrades`.
5. Собрать единый `[2026-07-15, 2026-08-06)` legacy input; fine wins over exact coarse duplicate, все расхождения логируются.
6. Внедрить v0.7 только с `event_mode=legacy_trades_proxy`; `PointEventCount=TotalTrades` для всех строк.
7. Не применять реальные fine `event_id` в текущем run.
8. Перестроить весь universe после event filter, не фильтровать готовые v0.6 structures задним числом.
9. Сгенерировать audit + JSON для EQUAL и INCOME.
10. Провести реальные MRS3 raw и DD5 tick-тесты перед любыми выводами о лучшей 2–4ORD стратегии или портфеле.

---

## 13. Доступные в текущем scratch ориентиры

```text
/workspace/scratch/3ebb8caf3a10/project_sources/01-MRS-Mean-Reversion-Strategy.md
    Краткое описание логики MRS/MRS3.

/workspace/scratch/3ebb8caf3a10/upload/MRS3_HANDOFF_CLEAN_v0.6.md
    Базовые неизменённые правила v0.6.

/workspace/scratch/3ebb8caf3a10/upload/MRS3_Algorithm_RU_v0.6.md
    Развёрнутое описание алгоритма v0.6.

/workspace/scratch/3ebb8caf3a10/upload/MRS3_Implementation_Spec_v0.6.md
    Технический контракт baseline v0.6.

/workspace/scratch/3ebb8caf3a10/mrs3_v06/
    Рабочая реализация v0.6, тесты, audit/JSON/run tooling.

/workspace/scratch/3ebb8caf3a10/deliverables/mrs3_html_parallel_compact_importer_v4.py
/workspace/scratch/3ebb8caf3a10/deliverables/RUN_MRS3_PARALLEL_COMPACT_IMPORTER_V4_20_WORKERS.bat
/workspace/scratch/3ebb8caf3a10/deliverables/mrs3_html_compact_importer_v3.py
    Актуальная параллельная compact-схема импорта и обязательный v3 codec.
```

### Самые важные запреты для продолжения

- Не использовать старый `legacy_fine_html_extract.py` для наполнения постоянной базы.
- Не дополнять старую `mrs3_source.duckdb` v1.
- Не удалять HTML до успешного v4 audit.
- Не считать raw action rows готовыми сделками без проверки.
- Не объединять fine и coarse вне единого окна.
- Не смешивать real events и legacy proxy в одном v0.7 run.
- Не выдавать source-PnL суммы MRS2 orders за реальный PnL сформированной MRS3 strategy.
- Не выдавать individual proxy ranking за полноценную портфельную симуляцию.

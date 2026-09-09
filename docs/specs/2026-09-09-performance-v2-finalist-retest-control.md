# Performance DB v2 — массовый ретест и контроль финалистов

**Статус:** Implemented; root verification passed
**Дата:** 2026-09-09
**Зависимости:**
[Performance DB v2 CHECK & RETEST](2026-09-03-performance-v2-retest-workflow.md),
[Selection review import](2026-09-02-performance-v2-selection-review-import.md),
[ADR-0022](../decisions/0022-performance-v2-selection-review-ledger.md)

## Цель

Добавить повторяемый контур, в котором оператор одним запуском ретестирует
текущих пользовательских финалистов всех пар, при необходимости добавляет
`RESERVE`, получает один общий XLSX для контроля и затем одним обратным
импортом уточняет текущие статусы и ранги. Portfolio Optimizer не получает
отдельного признака «окончательного набора»: он всегда читает актуальные
effective `User Status = FINALIST`.

## Термины

- **Effective status/rank** — актуальное пользовательское решение последнего
  принятого review, либо автоматическое решение при отсутствии review.
- **Retest cohort** — неизменяемый список Strategy ID, Result ID, status/rank и
  исходных typed-параметров, зафиксированный при запуске массового ретеста.
- **Control workbook** — один XLSX со всеми выбранными Pair + Direction,
  предназначенный для контроля, назначения `RETEST` и группового review.
- **Group** — логическая группа `(Pair, Direction)`. Отдельная таблица или
  отдельный пользовательский файл для группы не создаётся.

## Пользовательский поток

1. Оператор может экспортировать один control workbook с текущими effective
   `FINALIST` и `RESERVE` по всем парам и сторонам.
2. В workbook можно изменить `User Status`, назначить `User Rank` внутри своей
   `(Pair, Direction)`, добавить комментарий и выставить `RETEST` для отдельных
   строк. Обратный импорт выполняется одной атомарной операцией.
3. В блоке `CHECK & RETEST` есть кнопка **«Ретестировать всех текущих
   финалистов»** и расположенный рядом чекбокс **«Включая RESERVE»**.
4. Без чекбокса cohort содержит effective `FINALIST`. С чекбоксом cohort
   содержит effective `FINALIST + RESERVE`. Статусы определяются в одной
   read-only транзакции на момент запуска и затем не меняют cohort.
5. Отдельный существующий путь по тегу `RETEST` сохраняется для выборочного
   ретеста строк, отмеченных в control workbook.
6. После tester и `IMPORT & REPLACE` оператор формирует новый общий control
   workbook. Portfolio Optimizer при следующем Campaign читает текущие
   effective `FINALIST` обычным способом.

## Период и единые настройки

Массовый запуск использует один `test_end` для всех стратегий. Поле обязательно
и редактируемо; начальное значение — начало UTC-дня `сегодня - 2 дня`, чтобы
не включать неполные или ещё не доставленные последние сутки.

Поле общего `test_start` обязательно и редактируемо. Начальное значение — дата
листинга самой старой пары в cohort. Для каждого символа импорт сохраняет
существующее правило:

```text
effective_start = max(test_start, listing_date + LISTING_WARMUP_HOURS)
effective_end = test_end
```

Поэтому фактические начала разных символов могут отличаться, а конец всегда
одинаков. Отсутствующая или некорректная listing date исключает только
соответствующую стратегию с существующим reason code. `test_start < test_end`.

Все JSON строятся из `templates/strategies/retest-mrs3/base.json` и сохранённых
typed-параметров стратегии. Контракт запуска фиксирует:

- `InitialBalance = 1000 USDT`;
- `balance_percentage_long = balance_percentage_short = 100`;
- `risk_long = risk_short = 1`;
- исходные `lot_x`, MA, shifts, side и timeframe;
- одну версию tester config, комиссии и market data policy для всего cohort.

Manifest хранит scope (`FINALIST` либо `FINALIST_RESERVE`), Strategy/Result ID,
исходный effective status/rank, requested/effective period, hashes JSON и
tester-config digest. Повторное использование committed inbox разрешено только
при полном совпадении manifest digest, cohort и дат.

## Импорт результатов

Массовый ретест переиспользует существующий native `SINGLE_MODE` и
`IMPORT & REPLACE`. Успешный отчёт заменяет текущие result/action/equity/window
facts соответствующей стратегии. История отдельной копии retest result не
создаётся. Ошибка одной стратегии сохраняет её прежний current result и
попадает в failure report; успешные siblings импортируются по существующему
контракту.

Сохраняется действующая монотонность REPLACE: новый report end не может быть
раньше текущего, а новый effective period не может быть короче текущего.
Нарушение исключает соответствующую стратегию и не сокращает сохранённую
историю молча.

REPLACE не изменяет effective `User Status`, `User Rank`, комментарий или тег
`REJECTED`. Массовый scope не создаёт временные `RETEST` tags. Выборочный путь
по `RETEST` очищает только свои теги согласно действующему контракту.

## Автоматическое ранжирование после ретеста

Автоматическое ранжирование после массового ретеста является отдельным режимом
`RETEST_COHORT`. Запрос передаёт только идентификатор завершённого bulk-retest job;
сервер сам восстанавливает из его manifest точный список успешно импортированных
Strategy ID и проверяет их current Result ID. Произвольный список Strategy ID от
браузера не принимается.

Selection pipeline запускается независимо для каждой `(Pair, Direction)`, но его
входом служат только успешно импортированные участники этого cohort. Все фильтры,
percentile populations, analog groups, stage counters, `Auto Status`, `Auto Rank`
и Top N вычисляются внутри этой ограниченной совокупности. Другие `ACTIVE` записи
той же пары и стороны не могут влиять на результат, даже если их метрики лучше.
Участник с ошибкой импорта остаётся со старым current result, показывается в
`Retest Failures` и не попадает в post-retest ranking.

Существующий обычный selection endpoint сохраняет прежний режим: он ранжирует
все подходящие `ACTIVE` записи выбранной пары и стороны. Его нельзя использовать
для post-retest export без серверного ограничения cohort. Реализация переиспользует
существующий pipeline после ограничения входного DataFrame и передаёт те же exact
Strategy ID в подготовку window cache; новая система формул не создаётся.

Каждый созданный group selection run фиксирует `ranking_scope=RETEST_COHORT`,
bulk-retest job ID, cohort/manifest digest, список успешно импортированных
Strategy/Result ID и digest конфигурации фильтров. Его `Auto Status`, `Auto Rank`,
score и причины являются подсказкой и immutable audit facts, а не
пользовательским решением.

Ретест сам по себе не меняет `User Status` и не присваивает новый `User Rank`.
В новом control workbook:

- `User Status` заполняется текущим effective status до экспорта;
- control export, привязанный к завершённому массовому ретесту, оставляет
  `User Rank` пустым, поскольку старый rank относится к прежним
  Performance-фактам;
- обычный повторный control export без нового ретеста показывает текущий
  effective `User Rank` для контроля;
- `Auto Status/Auto Rank` показывают новый расчёт;
- импорт неизменённого workbook не повышает и не понижает стратегии по
  автоматической рекомендации.

## Один общий XLSX

Control workbook содержит все строки выбранного scope ровно один раз и минимум
следующие листы:

- `Candidates` — Pair, Direction, Strategy/Result ID, период, ключевые raw и
  window metrics, `Auto Status`, `Auto Rank`, immutable reason/score,
  редактируемые `User Status`, `User Rank`, `RETEST`, `Comment`;
- `Groups` — счётчики до/после фильтров и automatic status по каждой
  `(Pair, Direction)`;
- `Retest Failures` — отсутствующие/отклонённые отчёты и reason codes;
- very-hidden metadata — database instance, workbook schema, зафиксированные
  group run IDs, result IDs, config/manifest digests и export timestamp.

Один workbook может представлять несколько существующих immutable selection
runs: по одному run для каждой `(Pair, Direction)`. Новая параллельная система
статусов не создаётся. Экспорт сохраняет все group runs и возвращаемые bytes в
одной writer-транзакции. Обратный импорт валидирует и записывает reviews всех
group runs атомарно.

`User Rank` может повторяться между группами, но внутри одной
`(Pair, Direction)` непустые значения должны быть уникальными положительными
целыми. Пустой rank допустим. Все действующие проверки формул, identities,
current Result ID, stale/foreign workbook, status vocabulary, analog target,
comment length и ZIP limits сохраняются для каждой строки и группы.

## Panel

В существующем блоке `CHECK & RETEST` добавляются:

- кнопка `Ретестировать всех текущих финалистов`;
- чекбокс `Включая RESERVE`, по умолчанию выключен;
- счётчики выбранных FINALIST и RESERVE;
- общий `Test start` и `Test end` с описанными defaults;
- после импорта — кнопка `Скачать общий контрольный XLSX`;
- существующий выборочный RETEST остаётся доступен.

Panel показывает frozen cohort count, даты, прогресс, число успешно заменённых
и failure count. Нельзя молча переключить scope или переиспользовать inbox
другого cohort/периода.

## Non-goals

- отдельная база или сохранение копий каждого результата ретеста;
- автоматическое изменение `User Status/User Rank`;
- глобальный rank между разными Pair + Direction;
- автоматический запуск Portfolio Optimizer после review import;
- изменение формул существующего selection pipeline;
- удаление стратегий, результатов или review history;
- портфельная комбинаторика и joint tick-test в этом контуре.

## Acceptance evidence

- Scope без чекбокса фиксирует только текущие effective FINALIST; scope с
  чекбоксом фиксирует FINALIST и RESERVE, не включая ANALOG/FILTERED/REJECTED.
- Cohort и даты входят в manifest digest; иной scope или период не переиспользует
  старый committed inbox.
- JSON каждого члена содержит единые sizing/tester defaults и его исходные
  typed strategy parameters.
- Разные listing dates дают разные effective starts при одном exact test end.
- Успешный REPLACE меняет Performance facts и не меняет user decision; ошибка
  sibling сохраняет его прежний current result.
- Общий workbook содержит несколько Pair + Direction без дубликатов и допускает
  повтор rank 1 между группами, но отклоняет повтор rank внутри группы.
- `Auto Status/Auto Rank` нельзя изменить; `User Status` сохраняется, а
  `User Rank` после массового ретеста пуст.
- Post-retest ranking получает только успешно импортированные Strategy ID
  выбранного bulk-retest job. Дополнительная `ACTIVE` стратегия с той же Pair +
  Direction и заведомо лучшими метриками не меняет ranks, statuses, stage counts
  или содержимое workbook.
- Стратегия из cohort с ошибкой импорта находится в `Retest Failures`, не
  ранжируется по старому current result и не появляется в `Candidates`.
- Metadata каждого group run содержит `RETEST_COHORT`, job/manifest/config
  digests и точные Strategy/Result ID; подмена job или current result отклоняется.
- Один корректный обратный импорт атомарно обновляет все group reviews и RETEST
  tags; ошибка любой строки не пишет частичный review.
- Повторный control export отражает новые current results и effective statuses;
  Portfolio Optimizer читает тот же exact FINALIST set.
- Existing tag-driven CHECK & RETEST и одногрупповой XLSX round-trip продолжают
  проходить без изменения поведения.

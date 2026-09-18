# SCREENER 01 — план реализации экрана скринера пар

**Статус (обновлено 2026-09-18):** ✅ Этапы 0–3 закрыты. `errors.py`,
`registry.py`, `listing.py` (реестр → dates.xlsx → Bybit по-символьно с
троттлингом ≥250мс и распознаванием HTTP 403, восстановлено и обосновано
после изучения официальной документации Bybit), `evaluate.py` (вердикты по
CSV, обе стороны) — все реализованы, 72/72 теста `tests/screener` зелёные,
каждый файл прошёл несколько раундов независимого review до схождения
(0 замечаний в последнем раунде по каждому). ⏳ Следующий шаг — Этап 4
(панель: `panel_testing.py` kwargs + `panel.py` контроллер-методы +
роуты `/api/v2/testing/screener/*`). Всё без коммитов — по решению
пользователя, изменения остаются pending до полной реализации, один общий
коммит в конце. Контракт —
[2026-09-18-pair-screener.md](../../specs/2026-09-18-pair-screener.md)
(там же — актуальные разделы про SHORT (раздел 4/5) и реестр ликвидности
(раздел 6.6), которых не было на момент первой версии этого плана).

## Контекст

Спецификация описывает дешёвый прогон 304 точек/пару вместо полных 5472 для
предварительного отсева пар. На реальных данных
`D:\!Humster\tester\report\my_test` (16 пар, 4864 строки
`reports_history.csv`, полная 304-сетка, без пропусков и дублей) установлено:

- Раздел 6.1a закрыт: CSV уже содержит колонки параметров прогона
  (`settings[*].basic.symbol/time_frame/mrs2.ma_close_long.len/
  mrs2.ma_long.len/mrs2.ma_long.multiplier`). Спот-проверка 81 отчёта против
  `extract_html_strategy_settings` — 0 расхождений. Решение: CSV-only.
- Вердикты по 16 парам посчитаны (раздел 9 спецификации): GO — MSTRUSDT,
  KORUUSDT, SOXLUSDT; CHECK — 8 пар; STOP — 5 пар; INCOMPLETE — 0.
- Пользователь решил не выполнять раздел 9 п.3 (полный сбор по 2+ парам из
  каждого вердикта) и перейти сразу к реализации экрана — риск принят
  явно, зафиксирован в спецификации (разделы 9, 10).

Независимая проверка кода (три Explore-агента + прямое чтение) подтвердила:
CSV-ридера, рендерера, который правит `end` записи символа под число
выбранных пар, backend/UI для новой карты и публичного Bybit-launchTime-
хелпера в репозитории нет — пишутся заново по существующим паттернам
(`AlgorithmConfig`/`SourceV6ImportSettings` для конфигов; `eligibility.py`/
`selection.py`/`plateau.py` — вердикт как строки + `pandas.DataFrame`, без
Enum; `market_snapshot.py._pages` — публичный Bybit fetch; `panel_testing.py`/
`test_panel_testing.py` — сервис и тесты runner'а; `panel_web/index.html`+
`app.js` — карта RUNNER 01 и shortlist-таблица).

Независимый review диффа Этапа 0 (docs) нашёл и исправил: партиционные
`reports_history_p*.csv` не были учтены; раздел 8 требовал отказ всей
оценки на per-pair INCOMPLETE-условии (противоречило разделу 6.4); Bybit-
фолбэк не был ограничен по частоте (в проекте уже был реальный инцидент
бана); DD=0-спецкейс расходился с уже измеренной цифрой 99.4% совпадения
с `economic_pass`; раздел 4 противоречил новому acceptance-evidence о
побайтовом равенстве 4 статических записей шаблону. Все исправления внесены
в спецификацию — этот план уже отражает исправленную версию.

## Принятые решения

1. Этап оценки — CSV-only, включая партиционные `reports_history_p*.csv`
   (объединяются в один набор строк).
2. PnL-порог в economic-pass — `≥` (единственное осознанное отличие от
   `eligibility.py`); DD > 0 обязателен (как в `eligibility.py`, без
   спецкейса на DD=0).
3. USDT-суффикс — fail-closed: токен без `USDT` после uppercase-
   нормализации отклоняет весь ввод явной ошибкой, без автодополнения.
4. Даты листинга (дважды пересмотрено 2026-09-18 — см. Этап 3 ниже):
   сначала лист «Пары» реестра ликвидности (решение 11), затем
   `input/dates.xlsx` (`load_listing_dates`); для отсутствующих символов —
   публичный no-key Bybit `v5/market/instruments-info` `launchTime`, по
   одному запросу на отсутствующий символ (`category=linear&symbol=...`,
   без пагинации). Троттлинг ≥ 250 мс между запросами, действует и между
   вызовами (не только внутри одного) — восстановлено и обосновано
   изучением официальной документации Bybit после того, как было
   ошибочно снято. Сетевая ошибка/ненайденный символ/HTTP 403 — явная
   ошибка вызова, не тихий INCOMPLETE.
5. Сторона — LONG и SHORT в v1 (решение 10), с селектором в UI.
6. `ScreenerConfig` — отдельный dataclass с `load_screener_config` (секция
   `screener` в `config.local.json`: `stop_best_pnl30=8, go_min_good=8,
   big_min_good=5, good_pnl30=10, big_shift_bp=110`); экономические пороги
   берутся отдельно из существующего `AlgorithmConfig.from_json`.
7. Инвариант раздела 7: **единственный** global-fail — разные
   `(StartDate, EndDate)` в объединённом наборе строк (папка содержит
   отчёты минимум двух разных прогонов). Нехватка/лишние прогоны у
   конкретной пары (не 304 уникальных комбинации, нечитаемая строка) — это
   вердикт INCOMPLETE только для этой пары, не отказ всей оценки; остальные
   пары оцениваются нормально.
8. **Проверено чтением кода**: `render_strategy()` (`panel_testing.py:118`)
   жёстко требует `side in {"LONG", "SHORT"}`, иначе `PanelTestingError`; а
   `prepare()` вызывает `render_tester_config`/`render_strategy` напрямую, не
   через инъекцию. Изначально (до решения 10) это исключало третий ключ
   `"LONG_SCREEN"` в `_TEMPLATES`. После решения 10 это уже не нужно: `side`
   у скринера — реальный `"LONG"`/`"SHORT"`, как у RUNNER 01, меняется
   только КОНФИГ-шаблон (полная сетка → сеточная screen-версия), стратегия
   (`_TEMPLATES[side][1]`) остаётся той же, что у RUNNER 01 для той же
   стороны. Минимальный фикс остаётся тем же: `prepare()`/`fill()` получают
   два новых опциональных kwarg — `config_template_path: str | None = None`
   (переопределяет только `_TEMPLATES[side][0]`, не трогая `[1]`) и
   `render_config: Callable = render_tester_config`. По умолчанию оба дефолт
   → поведение RUNNER 01 не меняется байт-в-байт. Скринер зовёт, например,
   `fill(side="SHORT", ..., config_template_path="templates/tester/mrs2/
   config_tester_short_screen.json", render_config=render_screener_tester_config)`.
   `_TEMPLATES` и `render_strategy` не трогаются вообще.
9. Screener переиспользует тот же `LocalTestingService`-инстанс (и тот же
   `TesterTargetLock`, ключ по `bot_root`), что и RUNNER 01 — осознанная
   взаимная эксклюзивность на один бот.
10. **SHORT включён в v1** (решение пользователя 2026-09-18, после начала
    реализации): у SHORT свой базовый тестер-конфиг (`config_tester_short.json`
    — поля `ma_short.*`, множитель `>1` строкой с запятой как разделителем,
    например `"1,003"`) и своя уже существующая стратегия
    (`templates/strategies/source-v6-mrs2/short.json`, не создаётся заново).
    Новый screening-шаблон — `config_tester_short_screen.json`. Сторона
    определяется по заголовку CSV при оценке (`ma_long.multiplier` vs
    `ma_short.multiplier`), а не передаётся отдельно; наличие обеих колонок
    сразу — отказ всей оценки (смешение сторон в одной папке отчётов).
    `render_screener_tester_config` не зависит от стороны — работает с любым
    переданным текстом шаблона.
11. **Реестр ликвидности включён в v1** (решение пользователя 2026-09-18):
    книга `input/bybit_tradfi_liquidity.xlsx`
    (`screener.liquidity_registry_path` в `ScreenerConfig`) — источник
    списка пар и дат листинга (лист `Пары`, read-only) и место, куда
    скринер пишет свои находки (лист `Скрининг`, единственный, которым он
    владеет). Контракт листа `Скрининг`, инвариант «не трогать другие
    листы» и upsert-семантика с сохранением ручных полей — раздел 6.6
    спецификации. Реализовано в `src/mrs3/screener/registry.py`.
12. Только `basic.symbol` получает динамический `start=1, step=1,
    end=len(symbols)` при рендере; четыре статических параметра
    (`multiplier`, `ma_long.len`, `ma_close_long.len`, `time_frame`) уже
    корректны в самом файле шаблона и рендерером не трогаются (раздел 4/8
    спецификации).

## Этапы реализации

**Этап 0 (docs) — ✅ закрыт.** Спецификация обновлена и прошла независимый
review; `PRD.md`/`progress.md` обновлены.

**Этап 1 (feat) — ✅ закрыт.** `src/mrs3/screener/config.py`: `ScreenerConfig`
(`stop_best_pnl30, go_min_good, big_min_good, good_pnl30, big_shift_bp,
expected_combos_per_pair=304, liquidity_registry_path` — последнее поле
добавлено позже, вместе с решением 11) реализован; `load_screener_config(path)`
— секция `screener` тем же паттерном, что `load_source_v6_import_settings`
(`src/mrs3/config.py`): unknown-key rejection по `__dataclass_fields__`,
валидация в `__post_init__`. Прошёл независимый review (нашёл и
исправлено: `decimal.InvalidOperation` вместо `ValueError` на нечисловых
значениях; дублирующий `_local_config_object` заменён на импорт из
`mrs3.config`). Тесты `tests/screener/test_config.py` — 20/20 зелёных.

**Этап 2 (feat) — ✅ закрыт.** Оба шаблона —
`templates/tester/mrs2/config_tester_long_screen.json` и
`config_tester_short_screen.json` (добавлен вместе с решением 10): 5 записей
`parameter_mining`, у 4 статических — корректные `start/end/step` уже в
файле. Пятая (`basic.symbol`) — плейсхолдер.
`src/mrs3/screener/render.py::render_screener_tester_config(...)` — как
`render_tester_config`, но после подстановки `values` в запись символа
дополнительно выставляет ей `start=1, step=1, end=len(clean_symbols)`;
проверка символа — обязательный суффикс `USDT` вдобавок к текущему regex.
`render_tester_config` не меняется. Прошёл независимый review без
замечаний. Тесты `tests/screener/test_render.py` — рендер обоих реальных
шаблонов для N пар даёт `304*N` комбинаций, `end` записи символа равен `N`,
отказ без `USDT`/на дублях, остальные 4 записи побайтово равны шаблону.

**Этап 3 (feat) — ✅ закрыт**

✅ Готово и прошло независимый review: `src/mrs3/screener/errors.py::
ScreenerEvaluationError` — общее исключение "весь вызов должен упасть, а не
один вердикт". `src/mrs3/screener/registry.py` — `read_registry_
listing_dates(path, symbols)` (читает лист «Пары», валидирует/отклоняет
дубли только среди запрошенных символов — посторонняя битая строка в
реестре не блокирует вызов, это фикс 3-го раунда review) и
`write_screening_results` (atomic upsert в лист «Скрининг» по ключу
пара+сторона с сохранением ручных полей, round-trip остальных листов).
`src/mrs3/screener/listing.py::resolve_listing_dates` — приоритет: реестр
→ `load_listing_dates` (dates.xlsx) → Bybit по одному запросу на
отсутствующий символ (`category=linear&symbol=...`, без пагинации).
Ограничение «один проход на вызов» сначала снималось (2026-09-18, решение
пользователя), затем восстановлено в тот же день после явного
предметного изучения официальной документации Bybit
(https://bybit-exchange.github.io/docs/v5/rate-limit,
https://bybit-exchange.github.io/docs/v5/market/instrument): 600
запросов/5с с одного IP → `403 access too frequent`, автоматический бан
IP минимум на 10 минут; `instruments-info` — публичный, без ключа,
отдельного (более строгого) лимита для него в документации нет, значит
применяется общий IP-лимит. Защита с запасом: принудительный троттлинг
≥ 250 мс между последовательными Bybit-запросами (≤ 20 запросов/5с, ~30×
запас от порога), действует и внутри одного вызова, и между отдельными
вызовами (не сбрасывается — блокировка Bybit работает по скользящему
окну, не по «вызову»); явный HTTP 403 — немедленный отказ с текстом
«подождите ~10 минут», без дальнейших запросов по оставшимся символам.
Сетевая ошибка/символ не найден/невалидный `launchTime` — явная ошибка
всего вызова. Прошло несколько раундов review (пропущенный try/except
вокруг `load_listing_dates`; перегиб с cross-call кэшем результатов,
откачен — кэш результатов и троттлинг запросов разные вещи, троттлинг
не кэширует данные и намеренно переживает границу вызова; scoping-фикс
реестра).

✅ Готово и прошло независимый review: `src/mrs3/screener/evaluate.py`.
Читает `reports_history.csv` + `reports_history_p*.csv`, объединяет в один
набор; проверяет required-колонки и отсутствие пустых `basic.symbol`
(отдельные фиксы review); единственный global-инвариант — одна пара
`StartDate/EndDate` на весь набор (сравнение по распарсенным датам, не по
сырым строкам — фикс review), иначе отказ всего вызова; `_shift_bp()`
(`Decimal`, `ROUND_HALF_UP`, отдельные формулы для LONG/SHORT); economic-
pass — своя функция, DD>0 обязателен, PnL `≥` (единственное отличие от
`eligibility.py`); `absolute_trade_floor` из `eligibility.py`
переиспользуется напрямую; `_effective_days()` — точная Decimal-арифметика
через целые наносекунды `Timedelta.value` (не через float, фикс review),
явно отклоняет неположительные значения (дата листинга на/после конца
окна — иначе `pnl30` мог тихо поменять знак, фикс review); нечитаемая
строка ИЛИ число прогонов пары ≠ ожидаемому (раздельно отслеживаются — фикс
review, чтобы дубль-комбо не маскировал нечитаемую строку) → вердикт
INCOMPLETE для этой пары, остальные пары считаются нормально.
`evaluate_pairs(...)` — `pnl30`, `good_point`, агрегация по паре (verdict
INCOMPLETE/STOP/GO/CHECK, `big_shift`, лучшая точка), детерминированная
сортировка `mergesort`. `evaluate_and_record(...)` — тонкая обёртка,
дополнительно вызывает `write_screening_results` в реестр, если он
настроен. Прогон по спискам колонок вместо `iterrows()` (перф-фикс
review). Тесты `tests/screener/test_evaluate.py` (все вердикты, BIG_SHIFT,
обе стороны с разбором запятой у SHORT, per-pair INCOMPLETE во всех
вариантах, отказ всего вызова на разных датах/смешении сторон, объединение
партиционных CSV, запись в реестр). Итог по всему Этапу 3:
`tests/screener` — 72/72 зелёных.

**Этап 4 (feat) — ⏳ не начат (следующий шаг).** `panel_testing.py`: у `prepare()`/`fill()` — два новых
опциональных kwarg (`config_template_path`, `render_config`, решение 8);
дефолты сохраняют текущее поведение RUNNER 01. Отдельная функция
`expected_screener_runs(symbols)` — читает screener-шаблон и перемножает
длины `values` всех записей × число символов (не хардкод 304). `panel.py`:
контроллер-методы `local_screener_status/fill/start/stop/evaluate/export` по
образцу `local_testing_*`, со своим парсером символов (comma/space/newline,
uppercase, USDT fail-closed, дедуп), `side="LONG"`, `config_template_path`/
`render_config` из решения 8; `evaluate/export` — только чтение текущего
`report_dir`, без лока. Новые роуты `/api/v2/testing/screener/{status,fill,
start,stop,evaluate,export}` в диспетчере и allow-list. Тесты по образцу
`test_panel_testing.py` (в т.ч. регрессия: RUNNER 01 без новых kwarg
рендерит побайтово как раньше) + тест взаимной эксклюзивности с RUNNER 01
через общий `TesterTargetLock`.

**Этап 5 (feat, UI) — ⏳ не начат.** `index.html`: карта `#screener-local` (по образцу
`#runner-local`) — textarea пар, статичная пометка LONG, даты, чекбокс
очистки отчётов, кнопки (Проверить/Подготовить/Старт/Стоп/Оценить/Передать в
RUNNER 01), блок ожидаемого числа прогонов, таблица вердиктов
(`.table-wrap > table`, по образцу shortlist). `app.js`: парсер textarea с
живым превью `304×N`; вызовы новых endpoint'ов; рендер таблицы
(`valueCell`/`countCell`-паттерн); экспорт CSV/XLSX blob-download (как у
finalist-экспорта); кнопка "Передать в RUNNER 01" — чисто клиентская, пишет
`#local-pair.value` из отсортированных GO(+CHECK) без нового endpoint'а.
`app.css` — стили таблицы по образцу `.shortlist-table`. Проверка:
`node --check app.js`, затем ручной прогон панели: заполнить пары → оценить
уже существующую папку `D:\!Humster\tester\report\my_test` (read-only) →
сверить вердикты со значениями этой сессии → проверить перенос в RUNNER 01.
Реальный тестер (кнопка "Старт") не запускается без отдельной просьбы.

**Этап 6 (docs) — ⏳ не начат.** Обновить `PRD.md` (реестр фич, финальный
статус SCREENER 01) и `progress.md`.

## Порядок коммитов

Один scoped conventional commit на этап, каждый — полный цикл из
`CLAUDE.md`: `.venv\Scripts\python.exe -m pytest` на затронутую область,
`git diff --check`, независимый review, исправление замечаний, обновление
PRD/progress в том же коммите. Порядок строго 0→1→2→3→4→5→6, следующий этап
не начинается до коммита предыдущего.

## Проверка

- `.venv\Scripts\python.exe -m pytest tests/screener tests/test_panel_testing.py
  tests/runner -q` после каждого backend-этапа.
- `git diff --check` перед каждым коммитом.
- Ручная сверка вердиктов на реальных `D:\!Humster\tester\report\my_test\
  reports_history.csv` после этапа 3 и снова после этапа 5 (GO:
  MSTRUSDT/KORUUSDT/SOXLUSDT; CHECK: 8; STOP: 5).
- Ручной браузерный прогон экрана после этапа 5.

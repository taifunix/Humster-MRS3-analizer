# SCREENER 01 — план реализации экрана скринера пар

**Статус:** Этап 0 (спецификация) закрыт. Этап 1 в работе. Контракт —
[2026-09-18-pair-screener.md](../../specs/2026-09-18-pair-screener.md).

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
4. Даты листинга: сначала `input/dates.xlsx` (`load_listing_dates`); для
   отсутствующих символов — публичный no-key Bybit
   `v5/market/instruments-info` `launchTime`, **не более одного
   постраничного запроса на весь вызов оценки** (кэш в памяти на вызов, без
   повторов на символ и без фонового опроса — см. риск раздела 10
   спецификации про историю бана Bybit). Сетевая ошибка/ненайденный символ
   — явная ошибка вызова, не тихий INCOMPLETE.
5. Сторона — только LONG в v1, без селектора.
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
   через инъекцию. Третий ключ `"LONG_SCREEN"` в `_TEMPLATES` сломался бы на
   этой проверке. Стратегия скринера реально LONG (`use_long=true`),
   меняется только конфиг-шаблон (сетка), а не сторона. Минимальный фикс:
   `prepare()`/`fill()` получают два новых опциональных kwarg —
   `config_template_path: str | None = None` (переопределяет
   `_TEMPLATES[side][0]`) и `render_config: Callable = render_tester_config`
   (какой функцией рендерить конфиг). По умолчанию оба дефолт → поведение
   RUNNER 01 не меняется байт-в-байт. Скринер зовёт
   `fill(side="LONG", ..., config_template_path="templates/tester/mrs2/
   config_tester_long_screen.json", render_config=render_screener_tester_config)`.
   `_TEMPLATES` и `render_strategy` не трогаются вообще.
9. Screener переиспользует тот же `LocalTestingService`-инстанс (и тот же
   `TesterTargetLock`, ключ по `bot_root`), что и RUNNER 01 — осознанная
   взаимная эксклюзивность на один бот.
10. Только `basic.symbol` получает динамический `start=1, step=1,
    end=len(symbols)` при рендере; четыре статических параметра
    (`multiplier`, `ma_long.len`, `ma_close_long.len`, `time_frame`) уже
    корректны в самом файле шаблона и рендерером не трогаются (раздел 4/8
    спецификации).

## Этапы реализации

**Этап 0 (docs) — закрыт.** Спецификация обновлена и прошла независимый
review; `PRD.md`/`progress.md` обновлены.

**Этап 1 (feat, в работе)** — `src/mrs3/screener/config.py`: `ScreenerConfig`
(`stop_best_pnl30, go_min_good, big_min_good, good_pnl30, big_shift_bp,
expected_combos_per_pair=304`), `load_screener_config(path)` — секция
`screener` тем же паттерном, что `load_source_v6_import_settings`
(`src/mrs3/config.py`): unknown-key rejection по `__dataclass_fields__`,
валидация в `__post_init__`. Тесты `tests/screener/test_config.py`.

**Этап 2 (feat)** — новый шаблон
`templates/tester/mrs2/config_tester_long_screen.json`: 5 записей
`parameter_mining`, у 4 статических — корректные `start/end/step` уже в
файле. Пятая (`basic.symbol`) — плейсхолдер.
`src/mrs3/screener/render.py::render_screener_tester_config(...)` — как
`render_tester_config`, но после подстановки `values` в запись символа
дополнительно выставляет ей `start=1, step=1, end=len(clean_symbols)`;
проверка символа — обязательный суффикс `USDT` вдобавок к текущему regex.
`render_tester_config` не меняется. Тесты `tests/screener/test_render.py` —
рендер реального шаблона для N пар даёт `304*N` комбинаций, `end` записи
символа равен `N`, отказ без `USDT`/на дублях, остальные 4 записи побайтово
равны шаблону.

**Этап 3 (feat)** — `src/mrs3/screener/listing.py::resolve_listing_dates`
(сначала `load_listing_dates`, для отсутствующих символов — один
постраничный Bybit-запрос на весь вызов, in-memory кэш; сетевая
ошибка/символ не найден — ошибка всего вызова). `src/mrs3/screener/
evaluate.py`: чтение `reports_history.csv` + `reports_history_p*.csv`,
объединение в один набор; проверка единственного global-инварианта (одна
пара `StartDate/EndDate` на весь набор, иначе отказ всего вызова);
`shift_bp()` (`Decimal`, `ROUND_HALF_UP`); economic-pass — своя функция, DD>0
обязателен, PnL `≥` (единственное отличие от `eligibility.py`);
`absolute_trade_floor` из `eligibility.py` переиспользуется напрямую.
Нечитаемая/битая строка или число прогонов пары ≠ ожидаемому → вердикт
INCOMPLETE для этой пары (не отказ всего вызова, остальные пары считаются
нормально). `evaluate_pairs(...)` — `pnl30`, `good_point`, агрегация по паре
(verdict INCOMPLETE/STOP/GO/CHECK, `big_shift`, лучшая точка),
детерминированная сортировка `mergesort`. `export_verdicts()` (CSV; для
XLSX — проверить на месте существующий xlsx-writer в панели, иначе
`openpyxl` напрямую). Тесты `tests/screener/test_evaluate.py` (все
вердикты, BIG_SHIFT, per-pair INCOMPLETE при битой строке или нехватке
прогонов при нормальной оценке остальных пар, отказ всего вызова только на
разных датах, объединение партиционных CSV) и `tests/screener/
test_listing.py` (мок HTTP, без реальных сетевых вызовов, проверка ошибки
на недоступности/ненайденном символе).

**Этап 4 (feat)** — `panel_testing.py`: у `prepare()`/`fill()` — два новых
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

**Этап 5 (feat, UI)** — `index.html`: карта `#screener-local` (по образцу
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

**Этап 6 (docs)** — обновить `PRD.md` (реестр фич, финальный статус
SCREENER 01) и `progress.md`.

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

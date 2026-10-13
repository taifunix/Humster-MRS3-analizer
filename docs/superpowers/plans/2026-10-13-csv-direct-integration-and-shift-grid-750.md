# План: интеграция CSV Direct в main и расширение сетки шифтов MRS2 до 7,5%

**Дата:** 2026-10-13. **Назначение:** стартовый документ новой сессии (контекст
предыдущей исчерпан). **Порядок:** часть A (интеграция CSV Direct), затем
часть B (сетка до 750 bp) — B меняет `pipeline`/`refine`/`plateau`, через которые
идёт и CSV Direct, поэтому B делается уже на `main` после слияния A.

Старт сессии: `CLAUDE.md` → `PRD.md` → `progress.md` → этот план →
[CSV Direct spec (в ветке)](../../specs/2026-10-10-csv-direct-analysis.md) и
[P7 spec](../../specs/2026-10-13-panel-performance-db-selection.md).
Правила проекта не меняются: тесты только `.venv\Scripts\python.exe -m pytest`,
каждый коммит — независимое ревью, `progress.md`/PRD в том же коммите.

## 0. Состояние на 2026-10-13

- `main`: протокол оценки MRS3 v2 (P1–P6) и P7 (выбор PerformanceDB в Panel,
  шаблоны бирж `templates/exchanges/<биржа>/`, отказ Stage 1 для базы не Bybit).
  P7e (папки шаблонов) — коммит `1bf84b0` (`feat: pick MRS3 and optimizer
  templates from per-exchange folders`); A3 зависит от
  `templates/exchanges/*/exchange.json`.
- Ветка `feat/csv-direct-analysis` (worktree `.worktrees/csv-import-integration`):
  3 коммита от `1cc5abe` — `ae4449b` (pipeline: точная `Decimal` 30-дневная
  активность), `d6a5efe` (Analysis DuckDB из `reports_history.csv` + run
  snapshot), `b55ea6d` (Panel: `POST /api/v2/strategies/fresh/analyze-csv`,
  карточка «CSV Direct»). 22 файла, +2182 строки. Ревью ветки пройдено;
  последний шаг по её `progress.md` — приёмка пользователем полного канонического
  CSV-скана через Panel и слияние.
- Известные ограничения ветки: эндпоинт синхронный и однопроцессный; нет
  PRETEST A/B (в CSV нет поточечной истории); DD%/WinRate в CSV округлены до
  2 знаков; парсер Source v6 не читает новый HTML тестера (повторы метрик,
  объединённые ячейки); база анализа не хранит биржу.
- Полный прогон `main` 2026-10-13 (до P7e): 40 известных падений, все были и
  раньше (`test_portfolio_input` 14, `v5_read_compat` 6, `equity_regime_selection`
  3, benchmark equity selection 3, create benchmark slice 13,
  `test_panel.py::test_static_panel_shell_contains_only_navigation_contract`).
  В `progress.md` встречается более старая цифра 57 — перед A1 перемерить
  базовую линию полным прогоном и сравнивать с ней.

## Часть A. Интеграция CSV Direct в main

### A1. Перенос ветки на main

1. Новая ветка `feat/csv-direct-on-main` от текущего `main`; `git merge
   feat/csv-direct-analysis` (не rebase: сохраняет ревьюнутые коммиты).
2. Конфликты (`git merge-tree --write-tree main feat/csv-direct-analysis`
   против `1bf84b0`, после P7e): только **`progress.md`** и
   один фрагмент **`src/mrs3/panel.py`** — allowlist POST-маршрутов (~строка
   10975 слитого файла). Решение: набор из main (в нём есть
   `performance-databases/activate`, `/create`, `selection/aggregate-import`)
   плюс `/api/v2/strategies/fresh/analyze-csv`. `app.js`, `index.html`,
   `test_panel_static_ui.py`, PRD и порядок `except` сливаются автоматически;
   `pipeline.py` в main после `1cc5abe` не менялся.
3. Номер ADR (git-конфликта нет, но коллизия смысловая): в `main` ADR-0070 —
   протокол MRS3 v2. ADR ветки переименовать в **ADR-0071**
   (`0071-csv-direct-analysis-from-tester-csv.md`) и поправить все ссылки:
   заголовок `# ADR-0070` в самом файле, spec CSV Direct, `progress.md`, PRD,
   комментарий в `tests/test_panel_static_ui.py:350`.
4. Проверка после слияния: CSV Direct-наборы (`tests/test_csv_*`,
   `tests/test_panel_csv_direct_e2e.py`, `test_pipeline.py`) + все Panel-тесты +
   полный прогон; новые падения сверх известных 40 — исправить до коммита.

### A2. Совместимость с протоколом v2 и P7

- CSV Direct строит только Analysis DB (до READY). READY JSON из такой базы
  идёт через `_generate_fresh_strategies` → протокол v2 (цель оценки, шаблоны
  биржи активной базы). Проверить e2e: CSV → анализ → шортлист → READY JSON с
  `templates/exchanges/<биржа>/mrs3-strategy.json`.
- Run snapshot CSV-прогона содержит `basic.use_fix`/`exchange.use_upnl` MRS2-теста —
  к протоколу MRS3 v2 они не относятся (это MRS2-источник), не путать.

### A3. Ключ биржи в базе анализа (решение пользователя 2026-10-12)

- Записывать биржу из `exchange.name` run snapshot в метаданные Analysis DB
  (`analysis_runs`/manifest); один CSV-прогон — одна биржа, разные — отказ.
- Генерация READY: биржа базы анализа должна совпадать с биржей активной
  PerformanceDB (`EXCHANGE_MISMATCH`), иначе стратегии BingX уйдут в базу Bybit.
- Базы анализа без ключа (Source v6, старые CSV) — считать Bybit (как v1
  PerformanceDB без маркера).
- Даты листинга CSV-анализа: файл `panel_workflow.listing_dates_path`, лист —
  `listing_dates_sheet` из `templates/exchanges/<биржа>/exchange.json`.

### A4. Фоновое задание вместо синхронного эндпоинта

- Перевести `analyze-csv` на `_start_tracked_panel_job` (как Source v6 analysis):
  прогресс фаз (load / analysis / publish), отмена, восстановление после
  перезапуска Panel, ресурсная блокировка.
- Замер на полном каноническом скане (на 10 944 точках сейчас ~20 с: load 0,9,
  analysis 10, publish 9). Цель оптимизации — publish (9 с) и analysis; решать
  по профилю, не вслепую.

### A5. Приёмка и закрытие

- Приёмка пользователем: полный канонический CSV через Panel (время, READY).
- После слияния: удалить worktree `.worktrees/csv-import-integration` и ветку,
  обновить PRD (фича CSV Direct — implemented) и `progress.md`.
- Вне объёма A (записать в PRD): PRETEST A/B для CSV; парсер Source v6 для
  нового HTML; точность DD%/WinRate.

## Часть B. Сетка шифтов MRS2: 5,5% → 7,5%, шаг 0,4%

### B0. Определение

- Сейчас: `DEFAULT_CANONICAL_SHIFTS_BP` = 30, 40, 50, 60, 70, 90, 110, 140, 170,
  200, 230, 270, 310, 350, 390, 430, 470, 510, 550 (19 значений).
- Расширение: + **590, 630, 670, 710, 750** (24 значения).
- Множители MRS2: LONG `1 − bp/10000` → 0.941, 0.937, 0.933, 0.929, 0.925;
  SHORT `1 + bp/10000` → 1,059, 1,063, 1,067, 1,071, 1,075 (в SHORT-шаблонах
  десятичная запятая).

### B1. Принцип обратной совместимости

**Эффективная сетка = старая сетка (19, до 550) плюс самый длинный полностью
присутствующий в данных префикс расширения (590…750)**; пол — старая сетка
(данные короче 550 обрабатываются как сейчас). Область — по прогону или по
скоупу (решение B3.1). Данные до 550 → эффективная сетка = старая, и результаты
(Analysis DB, READY, шортлист, PerformanceDB, оптимизатор) равны прежним — это
главный golden-тест. **Оговорка про идентичность:** `algorithm_config_sha256`
(`pipeline._canonical(asdict(config))`, `_flatten_config`) входит в
`analysis_id` (`source_v6_analysis_fresh.py:200-204`, в ветке
`csv_direct_analysis.py:134`). Любая правка конфига — новые значения по
умолчанию (`shift_domain_max_bp`, `gap_rules`, `shift_factors`), новое поле
dataclass, удаление неиспользуемых полей, правка `config.local.json` — меняет
хэш и `analysis_id`. Golden-тест сравнивает кадры/READY, а идентичность — по
решению B3.5. Данные до 750 → 24 шифта. Нельзя просто заменить канонический
кортеж: соседство шифтов в `refine`/`plateau` определяется соседними элементами
кортежа, и у точки 550 появился бы сосед 590, которого нет в старых данных →
точки 550 стали бы «неполными» и старые результаты изменились бы.

### B2. Инвентаризация зависимостей (что проверить и поменять)

| # | Где | Сейчас | Что сделать |
| --- | --- | --- | --- |
| 1 | `templates/tester/mrs2/config_tester_{long,short}{,_screen}.json` — `parameter_mining` множителя | 19 значений, `end: 19` | +5 значений, `end: 24` (SHORT — с запятой) |
| 2 | `src/mrs3/config.py` `DEFAULT_CANONICAL_SHIFTS_BP` | 19 шифтов | не менять (legacy-хэши); добавить `EXTENDED_CANONICAL_SHIFTS_BP` (24) |
| 3 | `config.py` `AlgorithmConfig.__post_init__` | кортеж должен покрывать `shift_domain` 30..550 | разрешить эффективный префикс; `shift_domain.max_bp` 550 → 750 (меняет хэш конфига — B3.5) |
| 4 | `config.py` `shift_factors` (`eligibility.shift_factor`) | последняя полоса `max_bp 550`; шифт > 550 → `None` → `required_min_trades` = NaN → `standalone_sample_pass` = False (`eligibility.py:129-136`): точка **отклоняется** | продлить последнюю полосу до 750 (или новая полоса) — **решение B3.2**; `__post_init__` (`config.py:517-529`) не проверяет покрытие домена полосами — добавить проверку (сейчас `tests/test_config.py:822-848` принимает сетку до 700 с полосами до 550) |
| 5 | `config.py` `absolute_floor_*` | > 200 bp → 5 сделок | применяется и к 590–750; подтвердить |
| 6 | `config.py` `gap_rules` (`selection.required_gap_bp`) | последнее правило `300..551`; левый шифт ≥ 551 → **исключение** | продлить до `751` (или новое правило) — **решение B3.3** |
| 7 | `config.py` `refine`-поля (`fine_zone/boundary_zone/fine_step/*_radius`), `shift_domain_*`, `gap_mid_start_bp`, `gap_lower_*`, `deep_gap_boundary_bp` | вне `config.py` не используются (кроме валидации и хэша конфига) | не трогать без нужды: удаление меняет хэш конфига (B1); `ma_neighbor_radius` используется в `refine`/`plateau` |
| 8 | `src/mrs3/refine.py`, `plateau.py` | соседи из `config.canonical_shifts_bp` | передавать эффективную сетку скоупа |
| 9 | `src/mrs3/loader.py` `normalize_shift` | любой целый bp | без изменений |
| 10 | CSV Direct `csv_direct_input.py`; `refine.canonical_shift_neighbors` (`refine.py:19-22`) | ввод требует прямоугольную сетку скоупа; refine **падает** на неканоническом шифте — CSV с 590+ сейчас ломает анализ | после B4c работает; тест «скоуп до 750 и скоуп до 550 в одном CSV» — успех или отказ по ответу B3.1 |
| 11 | `src/mrs3/source_v6_coverage.py` `CANONICAL_READINESS_SHIFTS_BP`, `source_v6_surface.py:603` `required_grid`, `panel_surfaces.py:17,256,348,390` | готовность = все 19 шифтов × close | готовность = эффективный префикс ≥ legacy; лишние частичные шифты — исключить с причиной |
| 12 | `src/mrs3/duckdb_direct.py` `CANONICAL_GRID_VERSION = "mrs3_shift_grid_30_550_v1"`, `READINESS_MAX_SHIFT_BP = 550` | контракт готовности/материализации | версия `30_750_v1` для расширенной сетки; старая принимается |
| 13 | `src/mrs3/analysis_storage.py` проверки grid contract (~633, 894, 1100); witness-проверка ~1028–1043 | требуют `canonical_shifts_bp == DEFAULT`, `readiness_max_shift_bp == 550`; у ~1043 захардкожены «конец на 430» и шаги ≤ 10 bp ниже 150 — противоречит и текущей сетке (путь мёртвый или сломан) | принимать обе версии; путь ~1028–1043 разобрать отдельно (удалить или починить) |
| 14 | `src/mrs3/panel.py` `_DIRECT_POINT_CONFIG_HASH` (~365) и `panel.py:8648` (оба из `AlgorithmConfig().canonical_shifts_bp`) | хэш legacy-сетки | хэш по версии сетки базы, иначе старые базы перестанут открываться |
| 15 | Профиль анализа (UI `analysis-profile`, `analysis_profile.py`) | редактирует `shift_factors`, `gap_rules` | валидация: правила покрывают максимум сетки (до 750) |
| 16 | Шортлист `fresh_shortlist.py` `min_shift_pct` | ≤ 100 | без изменений; подсказка UI |
| 17 | Генерация MRS3 (`analysis_strategies`, `fresh_analysis_strategies`, шаблоны `templates/exchanges/*/mrs3-strategy.json`) | множитель из `shift_bp` | проверить отсутствие верхних границ и прототипов ордеров |
| 18 | PerformanceDB (`performance_v2_*`, `portfolio/input.py`) | `shift_bp >= 0`, без верхней границы | без изменений; тест импорта ордера 750 |
| 19 | Оптимизатор `portfolio/position_sizing.py` `lot_capacity` | `30 <= shift <= 550`, иначе `INVALID_ORDER_GEOMETRY`; премия `max(0, s−0.6)/4.9` (=1 при 5,5%) | граница → 750; премия выше 5,5% — **решение B3.4** |
| 20 | Локальный `config.local.json` пользователя | свои `canonical_shifts_bp`, `shift_domain`, `shift_factors` (все 1), `gap_rules` (70/90/110/130, до 551) | обновить вместе с кодом (gap_rules до 751!); меняет хэш конфига — B3.5 |
| 21 | `scripts/build-debian-import-bundle.py:33,160` | захардкожена сетка до 550 | обновить или пометить legacy |
| 22 | Скринер `src/mrs3/screener/config.py:27` `expected_combos_per_pair=304` (19 × 16), `screener/evaluate.py:287` | при +5 шифтах в `_screen`-шаблонах все пары станут «неполными» | решение B3.6: расширять ли скрин-шаблоны; если да — 24 × 16 = 384 |
| 23 | Тесты, закрепившие 19 шифтов | `test_panel_testing.py:108-112` (значения шаблонов, `end` 19) и счётчики 304 (246, 320, 384, 1090, 1096); `tests/screener/test_config.py:22`, `test_render.py:97`; `test_eligibility.py:78` (551 → None); `test_selection.py:1088-1094`; `test_config.py:595,608,852`; `test_refine.py:27-65,154` | обновлять осознанно: legacy-ожидания сохранить отдельными тестами |

### B3. Решения пользователя (нужны до реализации)

1. Эффективная сетка — по скоупу (symbol/side/timeframe) или по всему прогону
   (см. п. 7)? Допустимы только: старая сетка или старая + префикс расширения;
   «дырявые» сетки — отказ с причиной; данные короче 550 — как сейчас.
2. `shift_factors` для 590–750: продлить последнюю полосу (0,20 по умолчанию;
   у пользователя все 1) или отдельная полоса?
3. `gap_rules` для левого шифта ≥ 550: тот же минимальный геп (150 по умолчанию,
   130 у пользователя) или больше?
4. Премия лот-модели оптимизатора выше 5,5%: продолжать формулу (при 7,5%
   `((7,5−0,6)/4,9)² ≈ 1,98` → множитель до ≈ 3,2 при бонусе 1,1) или
   ограничить премию единицей (поведение как у 5,5%)?
5. Идентичность анализа: хэшировать конфиг, усечённый до эффективной сетки
   (старые данные → прежний `analysis_id`), или принять новый `analysis_id` при
   равных результатах (golden по кадрам/READY, не по id)?
6. Расширять ли скрин-шаблоны (`*_screen.json`) — скринер ждёт 304 комбинации на
   пару.
7. Эффективная сетка по скоупу требует передавать сетку скоупа в
   `plateau`/`refine` (сейчас один конфиг на прогон) — подтвердить п. 1 с учётом
   этой цены. Рекомендация: по прогону, если все скоупы прогона одной сетки,
   иначе отказ с перечнем скоупов.

### B4. Этапы и тесты

- **B4a** — спецификация + ADR (новая версия контракта сетки) с ответами B3.
- **B4b** — шаблоны MRS2 тестера (+5 множителей) и конфиг; golden: на старых
  данных кадры анализа и READY равны прежним; идентичность (`analysis_id`,
  хэш конфига) — по решению B3.5.
- **B4c** — эффективная сетка в `pipeline`/`refine`/`plateau`/`eligibility`/
  `selection`; тесты: скоуп до 750 (соседство 550↔590, гепы, веса), скоуп до 550
  без изменений; смешанный прогон (скоупы до 550 и до 750) — успех при сетке по
  скоупу или отказ с перечнем скоупов при сетке по прогону (по ответу B3.1).
- **B4d** — Source v6 coverage/surface, `duckdb_direct`, `analysis_storage`
  (версии контракта), Panel-хэш; тест: старая база анализа открывается.
- **B4e** — оптимизатор (граница и премия); тесты на 590–750 и неизменность
  старых результатов (golden SHA Stage 2).
- **B4f** — e2e: MRS2-прогон с расширенной сеткой → CSV Direct → шортлист →
  READY → RETEST → PerformanceDB → оптимизатор.

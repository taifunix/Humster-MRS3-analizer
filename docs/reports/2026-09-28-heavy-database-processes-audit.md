# Аудит тяжелых процессов Source DB и PerformanceDB

Дата: 2026-09-28. Проверенный код: `de7fea9`. Локальный DuckDB: `1.5.5`.
Результат: анализ и предложения; runtime не изменен. Рабочие БД и tester не запускались.
Проверки выполнялись на временных синтетических БД через `.venv`.

Implementation amendment: после аудита пользователь разрешил внедрение в отдельной
ветке. Получены новые Git commits; implementation baseline — `8f59c2c`.
Новые правила Panel import (atomic first-target initialization и trusted `Output`)
включены в active spec и R5 плана. Findings/замеры этого отчета относятся к исходному
audit baseline; утверждение выше об отсутствии runtime changes относится к аудиту.

## Вывод

Основной резерв — убрать повторную подготовку одних и тех же данных, SQL в циклах
и лишнее удержание больших объектов. Многопроцессная обработка уже используется
в импорте и материализации; простое увеличение числа workers не решает эти проблемы.

Лучшие первые задачи: пакетная публикация analysis DB, повторное использование
подготовки семи Performance-окон, пакетное чтение истории review и optimizer inputs.
Четыре воспроизведенных дефекта PerformanceDB заслуживают отдельных небольших исправлений.
Проблема Source `safe_to_delete=YES` **отложена по указанию пользователя** и не является
предварительным условием остальных работ.

План: [внедрение оптимизаций](../superpowers/plans/2026-09-28-heavy-database-optimization.md).
R4 получил независимый `PLAN_APPROVED` от Claude Opus 5/high; это review плана,
а не подтверждение уже внедренного ускорения.

## Как понимать оценки

Проценты означают **сокращение времени операции**: 20% — условные 100 секунд превращаются
в 80. Это инженерные ожидания относительно текущего кода, а не результат внедрения.
Диапазоны зависят от доли затронутого этапа, размера данных, диска и памяти. Они
не складываются между строками. Общий эффект считается по измеренным долям времени.
Например, сокращение на 80% этапа, занимающего 5% запуска, дает примерно 4% всему запуску.

Обозначения: **R** — воспроизведено; **S** — механизм виден в текущем коде, влияние на
большом корпусе не измерено; **H** — гипотеза, требующая профиля перед изменением.
Аудит не является доказательством отсутствия других дефектов.

| Модуль / операция | Ожидаемое сокращение времени | Основной источник эффекта | Уверенность |
| --- | ---: | --- | --- |
| Source v6 import, весь запуск | 5–15%; 15–30% только при доминировании последовательного завершения | Параллельная существующая проверка payload, пакетные overlap-решения, настройка размеров задач | S; исторически проверка занимала и всего 5% |
| Source v6 merge | 0–15%; 10–30% только для будущего доказанного сокращения повторных чтений | Streaming hash, overlap-решения; metadata reuse требует сохранения snapshot-контракта | S/H |
| Материализация, весь запуск | 10–25% | Общая подготовка A/B, чтение несколькими точками на соединение | S |
| Анализ материализованной поверхности, весь запуск | 5–30% | Публикация JSON пакетами; внутри самой записи возможно 40–90% | R для механизма записи, H для полного запуска |
| Performance v2 ADD/REPLACE import | 10–30% при значимой цене подготовки/записи | Пакетная запись metadata, подготовка вне длинной секции writer, ограничение накопления отчетов | S/H |
| Performance v4→v5 migration, однократно | 40–80% на backfill большого числа actions | Пакетный UPDATE вместо запроса на каждое действие | S/H; не влияет на уже обновленные БД |
| Полный пересчет Performance window cache | 15–35% | Один flat timeline на result, пакетные upserts | S |
| Отдельный cold A/B pair helper | 20–50% при преобладании чтения/подготовки | Общий source bundle для пары; seven-window selection уже читает source один раз | S/H |
| Только equity-quality backfill | 0–15% | Пакетная проверка revisions/запись, подбор workers | S; чтение уже ограничено |
| Selection: первая загрузка candidates при готовых window caches | 10–30% | Общая подготовка SQL-агрегатов actions без изменения предикатов | S |
| Selection: повторный preview с попаданием в candidate LRU | 5–20% | Меньше повторного чтения readiness/facts в одном snapshot | S/H |
| Чтение effective review decisions | 50–85% именно этапа history replay при десятках/сотнях runs | Удаление трех запросов на run | R для числа запросов |
| Catalog/export, если доминирует история review | 10–40% | Предыдущая строка; это не дополнительный независимый эффект | H |
| Optimizer prepared-input preparation/read | 15–40% для нескольких результатов | Пакетная повторная проверка и однократный digest в пределах snapshot | S |
| Global FINALIST RETEST: freeze/подготовка | 10–30% | Пакетные orders и effective decisions | S; время самого tester не затрагивается |
| Обычный CHECK & RETEST: подготовка | 0–5% | Уже пакетное чтение; небольшой резерв | S/H |
| XLSX: только writer, сверх уже выполненного | 0–10% | Локальные преобразования DataFrame; главный эффект уже получен | H |
| Selection review / control XLSX import | 0–10% | Пакетные обращения и уменьшение повторных разборов при доказанном совпадении | H, низкий приоритет |
| Prune | 0% целевого ускорения | Исправить FK-дефект, сохранить backup/restore | R |
| Legacy Source import / Performance v1 | 0–15% как предварительная гипотеза | Ограничение памяти, batch delete; сначала подтвердить актуальную нагрузку | H, не переносить оценки v6/v2 |

Для bounded parsing главным результатом может стать снижение памяти и исчезновение
провала скорости при paging. На небольшом корпусе дополнительный staging способен
не ускорить запуск вообще. Укорочение времени удержания writer — отдельная метрика
доступности БД, а не автоматически ускорение самого импорта.

## Что уже сделано и не является новым резервом

- Source v6: sealed segments, ограниченная очередь парсинга, отдельный лимит segment
  writers, один SQL-проход reduce, пакетные metadata inserts. Старое предложение
  «распараллелить дерево merge» не относится к текущему single-pass пути.
- Surface publication: SQL-копирование готовых payload без повторного encode,
  rowid-срезы проверки, `threads=1` и `memory_limit=1GB` у проверяющих workers.
  Ранее OOM-путь уже исправлен.
- Материализация: устранены повторная канонизация при decode и квадратичный cutoff.
  Сохраненный замер 60 реальных точек: медиана 24,41 → 9,43 с, то есть 2,59×,
  с одинаковым digest analysis rows. Это **уже текущая база сравнения**.
- Fresh analysis использует `point_analysis_input`; повторного декодирования всех
  fragments на обычном новом surface-пути уже нет.
- Performance: групповой child-count readback при импорте, prepared inputs из уже
  разобранного отчета, bounded window-worker batches, одна cache SELECT на result,
  candidate LRU, read-only equity preview, один save XLSX и reuse стилей.

Источники: [materialization speed](../specs/2026-09-21-source-v6-materialization-speed.md),
[surface throughput](../specs/2026-08-22-source-v6-surface-throughput.md),
[publication throughput](../specs/2026-08-21-source-v6-publication-throughput.md),
[M5 measurements](../superpowers/plans/2026-09-26-performance-v2-equity-quality-m5-slice-evidence.md),
[shortlist v2](../specs/2026-09-27-shortlist-filters-v2.md).

## Подтвержденные дефекты

### B1. Поврежденный optimizer cache пересчитывается, но не исправляется — R

`src/mrs3/performance_v2_optimizer.py:589–604, 631–645`.
При совпадающих version/digest, но поврежденном `prepared_json`, reader отправляет
result на пересборку. Затем writer сравнивает только version/digest/status/reason и
пропускает запись: JSON в этой проверке отсутствует.

Воспроизведение на `_typed_candidate_database` из существующего optimizer-теста:
подготовить result → заменить `prepared_json` на `{}` → повторить prepare.
Возвращается `availability.available=True`, но последующий strict read падает:
`prepared artifact schema version is invalid`.

Исправление: отличать реально переиспользованную строку от пересобранной после
невалидного payload. Не разрешать metadata-only skip оставлять такой payload.
Проверку актуальности source при записи сохранить. Новая схема/кеш не нужны.

### B2. Prune не учитывает FK от optimizer_prepared_inputs — R

`src/mrs3/performance_v2_prune.py:21–29, 80–172`;
FK: `src/mrs3/performance_v2_store.py:344–345`.
В preview/delete перечислены equity/window/action/result-таблицы, но отсутствует
`optimizer_prepared_inputs`. Удаление старого result с prepared-строкой падает
`ConstraintException: result_id ... is still referenced`.

На временной БД apply воспроизвел ошибку, существующий backup/restore восстановил
counts `(strategies, results, prepared) = (1, 1, 1)`. Потери данных в этом опыте нет,
но полезная очистка не выполняется и оплачиваются backup/restore.

Исправление: включить prepared-таблицу в counts и последовательное удаление детей
до родителей. **Не превращать каскад в одну транзакцию**: существующий
[prune-контракт](../specs/2026-09-11-performance-v2-prune.md) отдельно учитывает
ограничение DuckDB FK и требует checkpoint, backup и restore после ошибки.

### B3. XLSX `Trades` подменяется счетчиком отчета — R

`src/mrs3/panel_performance_v2.py:165–170` поверх
`src/mrs3/performance_v2_selection.py:1183–1185`.
`_export_xlsx` берет cached candidate, затем перезаписывает `total_trades` значением
`strategy_results.total_trades`. Общий writer выводит это поле в `Trades`.

Воспроизведение: cached completed round trips = **1**, raw report counter = **777**;
экспорт показывает **777**. Это расходится с семантикой общего selection workbook.
Исправление касается только exported `Trades`; raw report counter остается 777.
При отсутствии готовой метрики показывать отсутствие, а не подставлять report counter.
Export не должен рассчитывать окна или записывать БД.

### B4. Progress материализации ждет окончания всех успешных задач — R

`src/mrs3/source_v6_materializer.py:297–309`.
Используется `wait(..., return_when=FIRST_EXCEPTION)`. Когда ошибок нет, ожидание
возвращается после всех futures, поэтому callback не сообщает ранние завершения.
Проверка с подмененным executor и быстрой/медленной задачей дала пары
`(показано завершений, реально завершено): [(0,0), (1,2), (2,2)]`.

Исправление: получать завершенные задачи по мере готовности, сохранять обработку
исключений, ограничить число pending futures. Это улучшает progress и память
очереди; само по себе не дает гарантированного ускорения вычислений.
Перед cleanup на Windows дождаться закрытия запущенными workers всех файлов.

### B5. Final Source payload proof — R, отложено пользователем

`src/mrs3/source_v6_importer.py:532–559` и
`src/mrs3/source_v6_storage.py:1476`.
Import проверяет payload промежуточной БД, затем перепаковывает ее. На конечном файле
проверяет metadata/id set/digest, но не payload. Fault injection после compact,
заменивший `payload_blob` поврежденными bytes, дал `COMMITTED`, `safe_to_delete=YES`;
последующая реконструкция упала `compact checksum mismatch`.

Это не обнаружение порчи в пользовательской БД. Это воспроизведение известного
пробела, прямо записанного в [ADR-0015](../decisions/0015-source-v6-published-file-identity-readback.md)
как follow-up для import; merge уже проверяет конечный файл.
По указанию пользователя B5 не блокирует план. Если вернуться к нему позже,
перенести тяжелую проверку на конечный файл, а не добавлять второй полный проход.

### B6. HTML parser скрывает неправильный порядок actions — R

`src/mrs3/performance_v2_html.py:199–253` сортирует actions по timestamp, сохраняя
исходный `action_index`. Поэтому последующая проверка `ACTIONS_OUT_OF_ORDER`
(`performance_v2_import.py:679–685`) уже не видит нарушения в исходном HTML.

В существующем HTML fixture переставлены две целые action-строки. Parser вернул
индексы `[1, 0]` в порядке времени; `_warmup_report` принял результат: `failure=None`.
Существующий тест `test_warmup_rejects_out_of_order_source_rows` переставляет действия
уже после parser и поэтому не покрывает реальную входную границу.

Проверять исходную последовательность до нормализации либо сохранять ее до rejection.
Добавить regression через реальные HTML bytes → parser → import/warmup; отдельно
сохранить допустимый порядок действий с одинаковым timestamp. Это исправление
корректности, ожидаемое ускорение 0%.

### B7. Legacy empty import возвращает успешное evidence — R, низкий приоритет

`src/mrs3/duckdb_import.py:292–305, 724–733`. Импорт пустого каталога во временную БД
дал `COMMITTED`, discovered/imported = `0/0`, `safe_to_delete=YES`: `all([])` считается
истиной. Фактическое поведение воспроизведено; пользовательского удаления не было.
При возврате к legacy evidence нужно отдельно определить пустой импорт как no-op
либо ошибку и не выдавать вводящее в заблуждение доказательство переноса отчетов.
Работа с этим evidence отложена вместе с темой `safe_to_delete`; новый блокер не вводится.

## Возможности оптимизации по цепочкам

### S1. Source import / merge

В import тяжелый `_verify_published_identity` вызывается синхронно внутри
`_publish_segments_single_pass` (`source_v6_storage.py:1476`), хотя рядом уже есть
`verify_published_identity_parallel` (`:1888`), используемый merge (`source_v6_merge.py:556`).
Можно перенести существующую проверку **того же staging-файла** за закрытие writer и
использовать bounded process pool. Это самостоятельное ускорение без закрытия B5.
Прямой reducer должен по-прежнему возвращать только проверенный результат.
Нельзя открыть этот файл read-only из других процессов, пока writer держит его.

При многокусковых точках import последовательно делает decode/resolve/persist
(`source_v6_importer.py:519–530`), merge — аналогично (`source_v6_merge.py:481–494`).
Пакетная подготовка решений и запись через уже имеющийся
`persist_fragment_resolutions` может помочь overlap-корпусам. Для singleton-точек
`persist_batch_resolution` уже не открывает БД: список requests пуст.

`compact_v6_database` (`source_v6_storage.py:223`) целиком копирует БД. Сейчас это
часть контракта компактной публикации, а fresh import отвергает существующий target.
Предложение «пропустить compact при no-change» сюда неприменимо. Сначала измерить
цену копии и полученный размер; изменение политики упаковки — отдельное решение.

Merge дополнительно пять раз читает каждый входной файл для content identity:
два вызова `_content_identity` внутри `_read_input` при preflight, два при execute
и один перед publication (`source_v6_merge.py:90–104, 147, 154, 213, 422, 575`).
Внутри используется `read_bytes()`, поэтому размер одного Python-буфера сопоставим
с размером БД. Первый простой шаг — потоковый SHA с теми же именами артефактов,
length prefixes и absent markers, **с сохранением всех проверок**. Это уменьшает
пиковую память; ускорение диска не гарантируется. Metadata/header/origin parsing
между preflight/execute тоже повторяется. Его reuse или сокращение числа хеширований
требует отдельного доказательства immutable snapshot и защиты от изменения DB/WAL/TMP.
Нельзя заменить это сравнением только размера/mtime. Условные 10–30% для такого
варианта требуют сначала профиля; это не эффект безопасного streaming hash сам по себе.

Ownership rows строятся полными списками и затем pandas-frame
(`source_v6_merge.py:301–325`, `source_v6_storage.py:1074–1100, 1454–1478`).
Chunked/native generation потенциально сокращает peak RSS на 20–40% в metadata-heavy
merge, время обычно на 0–10%; это гипотеза, которую нельзя складывать с предыдущей.
`COUNT(*)` до/после compact — проверка сохранения таблиц. Не считать ее автоматически
полным сканированием: DuckDB может использовать metadata. Пока оставить и измерить.

### S2. Материализация

`measure_point_group` декодирует точку и вычисляет полное окно; затем
`_pretest_ab_evidence` повторно вызывает `calculate_metrics` для последних 14 дней
(`source_v6_materializer.py:75–100, 179–183`). Окна различаются, оба нужны. Повторяется
подготовка stitching/cycles/series, а B фактически использует PnL и round-trip count.
Выделить повторно используемую подготовку внутри одной точки, сохранив точные
границы, carry-in/open-tail, quiet-tail и все причины недоступности.
Просто взять B из усеченного готового A нельзя без доказательства эквивалентности.

Каждая точка открывает соединение в `decode_fragment_slice`
(`source_v6_storage.py:1803–1820`). Проверить небольшие batches точек на соединение
либо process-local read-only connection. Не передавать connection через multiprocessing
и не добавлять общий глобальный connection pool. Выгода выше на мелких точках.

### S3. Анализ поверхности и публикация результата

`source_v6_analysis_fresh.py:265–294`: каждый JSON-row записывается через
`executemany` без явной транзакции; весь `frames` сначала сериализуется для digest,
затем каждый row сериализуется для insert. Механизм строки-за-строкой воспроизведен
микрозамером ниже. Использовать существующий registered-frame/`INSERT SELECT` подход
порциями, одну транзакцию публикации и тот же atomic rename. Digest/canonical JSON
должны совпадать; порядок физических строк не заменяет явный logical order.

`run_multiscope_analysis` (`:231–260`) передает compact rows по scope в ProcessPool,
но все результаты накапливаются до записи. При очень больших JSON это память
родителя + передача результатов. Ограниченная по памяти publication/staging имеет смысл после
измерения RSS. Добавление workers при одном scope само по себе не ускорит анализ.

Вычислительный путь: `pipeline.py:559` → eligibility/refine → plateau → close profiles
→ BASE → structures. В `plateau.py:216–245` support для border/core вычисляется
повторно; в `selection.py:987–993` перечисляются комбинации 2–4ORD.
Это вторичные кандидаты для profiling. Не сокращать universe, не отбрасывать
diagnostic rows и не заменять точные predicates эвристикой ради скорости.

### P1. Performance import / migrations

`performance_v2_html.py:168–181, 311–329` отдельно декодирует HTML и вызывает
`_raw_markup` для current-header gate, затем `performance.py:434–482` снова делает
decode/inventory/DOM. Можно использовать результат общего разбора для проверки
current headers, сохранив все required/duplicate/limits проверки. Ожидание 5–20%
parser CPU включено в общий диапазон импорта, а не прибавляется к нему. Сначала B6.

`performance_v2_import.py:302–337` отправляет все parser futures и сохраняет все
ParsedPerformanceV2Report в родителе. Далее одновременно живут parsed/validated/
warmup results и writer buffers. Ограниченная очередь сама по себе не ограничит
память уже собранных отчетов: нужен ограниченный жизненный цикл данных, а при
больших пакетах — временные подготовленные chunks с сохранением атомарности publish.

Writer lock/connection открыты уже в `:1820–1836`, до inbox/staging/parser.
Перенос чистой CPU/IO-подготовки за пределы DB writer-секции может сократить ее
длительность; перед записью потребуется повторно сверить current IDs, typed-config
dedup и revisions. Это не разрешение ослаблять REPLACE или параллельно писать одну БД.

В `_publish` (`:1550–1640`) strategy/order/result metadata идут отдельными запросами,
а `_persist_phase8_prepared` последовательно вычисляет prepared input в транзакции
(`:1670–1680`). Начать с batch metadata и переиспользования вычислений; вынос CPU
делать только при сохранении привязки к окончательным IDs и Decimal(38,12).
`_append_rows` уже пакетный, но каждый раз перечитывает схему (`:282–297`), что можно
сделать один раз на текущем соединении после schema gate.

В `performance_v2_store.py:854–895` v4→v5 backfill загружает все action JSON через
`fetchall()` и выполняет UPDATE на каждую строку. Это отдельная крупная однократная
операция: chunked decode + set-based update внутри существующей migration transaction.
Не включать eager optimizer rebuild всей истории.

### P2. A/B, семь окон, equity-quality

`performance_v2_windows.py:379–460` каждый раз строит `_flat_samples(equity, actions)`,
потом заново выбирает серии и round trips. Selection вызывает вычисление до семи
окон на result (`performance_v2_selection.py:701–708`). Переиспользовать flat timeline
и индексы границ, не менять window-local peaks, fees и исключение действия на W0.

Отдельный pair API (`performance_v2_windows.py:477–527`) вызывает cold calculation
для каждого окна независимо, повторяя source read. Ему полезен общий immutable
source bundle для пары. Это не семь raw reads в selection: selection уже получает
source один раз на result, там повторяется именно подготовка/вычисление.

Cache write уже находится внутри batch transaction (`performance_v2_selection.py:1038–1055`),
но `_persist` вызывается для каждой метрики; equity publication отдельно делает
source-metadata SELECT и upsert на каждый result (`performance_v2_equity_cache.py:596–612`).
Уменьшить число statements, сохранив recheck всех revisions до первой записи.

В helper `_selection_window_job` (`performance_v2_selection.py:696–697`) полностью
готовый cache при `include_equity=False` возвращается как список для повторной записи.
Обычные Panel-вызовы часто уже передают только missing IDs; это не баг всех previews.
Для прямого fully-warm helper-вызова можно возвращать нулевой write set, сохранив progress.

Два чтения equity-only backfill — aggregate полной истории и bounded path с edge
sentinels — имеют разные назначения. Удалять одно как «дубликат» нельзя: оно участвует
в invalid/raw counts либо baseline. Новый алгоритм equity и 6h-grid уже реализованы.

### P3. Selection и Panel

`load_selection_candidates` (`performance_v2_selection.py:1108–1111`) выполняет три
action-агрегации: full holding, B holding, best trade. Первые две повторяют построение
интервалов. У третьей отличаются side/open predicates. Общий scan/интервалы возможны
только с независимым сохранением этих правил, включая partial close и side flip.

Перед проверкой LRU Panel читает readiness, current results, window facts token и
equity token (`panel.py:5093–5154`). При LRU hit можно уменьшить повторные преобразования
в одном snapshot. Нельзя заменять проверку revisions одним mtime/TTL: REPLACE сохраняет
result_id, а факты могут измениться. LRU уже зависит от содержимого/ревизий кешей.

Warm candidate LRU hit не требует повторных action scans. Первая загрузка candidates
при готовом window cache по-прежнему читает агрегаты actions — это текущий контракт.
Preview остается без записи; equity facts берутся только из готового кеша.

### P4. Review history, RETEST, XLSX

`effective_selection_decisions` (`performance_v2_selection_review.py:847–907`) читает
все runs, затем для каждого делает SELECT latest review, SELECT review rows и SELECT
selection rows. `latest_user_reviews_by_strategy` (`:819–844`) загружает всю историю
выбранных strategy IDs и оставляет по одной строке в Python.

Сначала пакетно загрузить необходимые таблицы и оставить текущий ordered replay.
Дальнейшее SQL-уплотнение истории допустимо только с теми же правилами ordinary
replacement, scoped overlay и сохранения последних пользовательских решений.
Нельзя просто брать последний run или его auto FINALIST.

Global freeze вызывает `_source_orders` для каждого выбранного участника
(`performance_v2_finalist_retest.py:1241–1250`): заменить одним чтением orders.
Обычный RETEST уже читает набор orders одним SQL (`performance_v2_retest.py:289–301`).

XLSX writer недавно ускорен: не повторять single-save/style-reuse работу.
Экспорт держит read-only connection до окончания `_export_xlsx`
(`panel_performance_v2.py:213–275`). После полного чтения можно закрывать DB раньше
создания workbook; ожидается уменьшение времени занятости, а не ускорение openpyxl.
Review/control import сохраняет bounds ZIP, запрет формул, identity/stale checks
и атомарные tags/ledger; массовое удаление проверок не оправдано.

### P5. Optimizer prepared inputs

`performance_v2_optimizer.py:574–605` читает raw sources даже для reuse, считает
digest; `:621–645` повторяет source SELECT/digest по одному result под writer;
затем strict consumer `:670–696` читает и хеширует source снова.
Campaign вызывает prepare и затем strict read (`panel_portfolio.py:1431–1448`).

Сначала исправить B1, затем пакетно читать writer recheck и не пересчитывать digest
одного immutable source-объекта несколько раз. Не убрать повторную проверку на новой
границе snapshot. Более глубокий metadata-only fast path потребовал бы отдельного
контракта trusted revision, сейчас его не предлагается вводить.

### L1. Legacy import и remote delivery

В `duckdb_import.py:880–931` родитель хранит все `_PreparedReport`, хотя submission
уже ограничен; в `performance_import.py:579–601` остаются все futures и reports.
Выгода ограничения жизненного цикла objects — прежде всего память и отсутствие
paging. Ориентир снижения RSS 15–40% для Source legacy и 30–80% для крупных Performance
пакетов — гипотеза, не проведенный замер. Просто ограничить futures недостаточно.

Legacy preflight хеширует HTML (`duckdb_import.py:192–198`), parser читает его снова
(`:308–315`); это разные границы проверки. Не удалять snapshot proof ради одного чтения.
При replacement-heavy импорте `:619–624` удаляет строки через `executemany`, где
set-based delete может уменьшить число statements. Append-only не получит этот эффект.

Прямые программы `programs/Обработчик HTML-DuckDB/` v3 (`:431–445, 513`) и v4
(`:104–121, 166`) пишут по одному отчету. Потенциал batch SQL — условные 20–60%
**этапа записи**, если он действительно доминирует. Это отдельный исторический runtime:
его перенос/переписывание требует своей спецификации и проверки на реальном пути
отчетов; общий план не дает на это разрешения. v4 по-прежнему зависит от соседнего v3 codec.

Remote delivery (`panel_remote_source_db.py:205–211, 260–267, 820–828`) хеширует файл
на обоих концах передачи. Это проверка целостности транспортировки; повторение
не является ненужным. Без профиля сети/диска и равноценного evidence оставить как есть.

## Замеры и проверка

1. ` .venv\Scripts\python.exe -m pytest tests/test_performance_v2_optimizer.py tests/test_performance_v2_prune.py -q`
   — **39 passed, 37,10 s**. Эти тесты не покрывают B1/B2; их зеленый статус не
   опровергает отдельные воспроизведения. Full suite в этой audit-сессии не запускался.
   Дополнительно ` .venv\Scripts\python.exe -m pytest tests/test_performance_v2_html.py -q`
   — **32 passed, 0,98 s**; raw HTML order regression B6 в текущем наборе отсутствует.
2. Временные fixture-БД: B1 — repair available, strict read error; B2 — FK failure и
   восстановление `(1,1,1)`; B3 — cached 1, XLSX 777; B5 — поврежденный final payload
   публикуется с YES. Продуктовый код не патчился; fault injection/monkeypatch были
   только в диагностическом процессе. B4 проверен с ThreadPool вместо ProcessPool,
   что изолирует семантику `wait`, но не является process-pool benchmark.
   B6 дополнительно проверен root через перестановку HTML rows и успешный warmup;
   B7 — отдельным read-only auditor на временном пустом каталоге/БД.
3. History replay, один диагностический запуск каждого размера: 2 runs → 8 SQL,
   0,0278 s; 102 runs → 308 SQL, 0,5066 s. Новые runs пустые, синтетические.
   Надежное наблюдение — число запросов; эти секунды не предсказывают real-data latency.
4. Disk-backed запись одинаковых 1000 пар `(scope_key, payload_json)`, три повтора,
   время включает insertion и checkpoint; setup таблицы вне таймера:

| Способ | Времена, s | Медиана, s |
| --- | --- | ---: |
| executemany, autocommit | 2,2469 / 2,3534 / 2,4473 | 2,3534 |
| executemany, явная transaction | 0,5257 / 0,5735 / 0,5167 | 0,5257 |
| registered DataFrame + INSERT SELECT | 0,0483 / 0,0473 / 0,0390 | 0,0473 |

У всех count = 1000, `bit_xor(hash(scope_key,payload_json)) = 7007025286146495211`.
Это контроль одинакового набора строк в опыте, не замена точному canonical SHA при
приемке реализации. Такой выигрыш нельзя переносить на вычисление plateau/structures.

Сохраненный M5-замер, 64 реальных result на frozen slice, новый runtime:

| Workers | Cold median, s | Cold peak RSS, MiB | Equity-only backfill median, s |
| ---: | ---: | ---: | ---: |
| 1 | 38,540 | 216,6 | 17,326 |
| 4 | 28,749 | 236,7 | 8,363 |
| 8 | 33,164 | 277,8 | 8,773 |
| 16 | 29,612 | 348,5 | 9,519 |

4 workers лучше 16 на этом срезе примерно на 2,9% cold и 12,1% backfill; cold RSS
меньше примерно на 32%. Это не доказательство универсального default=4.
Общий default 16 без нового сопоставимого профиля менять не следует.

## Параллелизм без избыточной нагрузки

- Source parse/normalize, materialization и тяжелая Python/Decimal подготовка:
  использовать существующий ProcessPool, bounded submissions, измерить 1/4/8/16.
  Увеличивать процессы только пока растет throughput и достаточно RAM.
- DuckDB set-based copy/aggregate: использовать внутренний parallel SQL, сначала
  уменьшить число запросов. Для множества внешних workers не умножать бездумно их
  число на большое значение DuckDB threads в каждом процессе.
- Cache workers уже используют ThreadPool и `threads=1`. Сравнить режимы на большом
  корпусе; перенос в процессы окупится только если CPU-выгода превышает spawn/IPC.
- Один writer для общей БД; workers не делят соединение. Чтение несколькими процессами
  — только когда writer закрыт. Сохранять RAM headroom под parent, decoded objects,
  DuckDB buffers и IPC; размер пакета ограничивать также bytes, а не только rows.

Эти ограничения согласуются с официальными рекомендациями
[DuckDB concurrency](https://duckdb.org/docs/current/connect/concurrency),
[tuning workloads](https://duckdb.org/docs/current/guides/performance/how_to_tune_workloads)
и [Python threads](https://duckdb.org/docs/current/guides/python/multiple_threads).
Рекомендация относится к существующему локальному in-process режиму; новый DB-сервис
или зависимость для этой задачи не нужны.

## CALC-02 measured selection-cache write stage (2026-09-29)

The selection writer now uses a private native bulk upsert within its existing
transaction. At most 896 unique rows and 18,816 parameters enter one statement;
larger direct-call batches use several statements under the same transaction.
Duplicate keys split into later statements, preserving every original row
and making the last valid values/timestamp win. Scalar
window callers, cache selection, arithmetic and source/equity rechecks keep
their existing path. Nonfinite Decimals are rejected before SQL so a
later valid duplicate cannot hide them. Global coalescing was rejected after
an actual-schema probe showed that it hid earlier Decimal and INTEGER overflow.

DuckDB 1.5.5 actual-schema preflight confirmed scalar/bulk insert and update,
mixed-scale Decimal and NULL parity, and matching nonfinite rejection. The
maximum 896-row statement executed successfully. New tests exercise SQL
identity, 896/897-row boundaries, timestamp/duplicate semantics, real second-
chunk failure rollback, and late equity failure with earlier-batch retention.
The directly affected suites passed 219 tests; the related equity/portfolio/
Panel/store contour passed 406 with four Windows symlink skips. Three existing
export tests changed only their writer mock target. Temporary negative injection
proved all three guards reject writes without changing file or catalog identity;
the normal export suite passed eight tests. The integrated
pre-change baseline passed 5,538 tests with nine Windows symlink skips in
1,505.40 seconds. The invalid-earlier-duplicate regression was RED before R9
and GREEN after it; Decimal/INTEGER conversion failures roll back and reopen
with zero rows. The 899-row fixture retains all inputs in groups of 896/2/1.
Final full-suite verification passed 5,557 tests with nine Windows symlink
skips and 30 warnings in 2,794.54 seconds. Collection contains 5,566 cases:
the baseline's 5,538 passing tests plus exactly 19 new scenarios, with unchanged
skips. Independent Opus 5/high returned `CODE_REVIEW_PASS`; CALC-02 is accepted.

The final R9 benchmark used seven paired, alternating runs on fresh temporary
file-backed actual-schema databases and persisted the same 896 unique rows:
224 unavailable rows with NULL metrics and 672 populated available rows.
It uses the existing test-schema seed and deterministic per-window keys.
Both arms used the same frozen per-input timestamps and caller transaction;
the timed region includes begin, writes and commit. Reopened, ordered readback
of all 21 columns, including timestamps, matched in every run.

| Arm | Window INSERTs | Seven durations, seconds | Median, seconds |
| --- | ---: | --- | ---: |
| Scalar | 896 | 13.135960, 15.502873, 16.307221, 12.616319, 13.386796, 14.963132, 15.772092 | 14.963132 |
| Bulk | 1 | 0.300894, 0.318896, 0.302521, 0.299999, 0.345486, 0.314956, 0.329397 | 0.314956 |

Bulk maximum 0.345486 s is below scalar minimum 12.616319 s. The median
reduction is 97.9% for this synthetic persistence stage only. It is not a
whole-selection or whole-import speed claim. The host reports 34 logical CPUs
and Intel64 Family 6 Model 85; post-run process RSS was 142,798,848 bytes,
not a peak-memory comparison. The write-stage probe uses no worker pool.
Input SHA-256: `d55f5b96d907c4d459a52cb67804cff24df3e4afe158972a7c4129974336ebd7`.
Seed database SHA-256: `cf4836cb925f15d3127e006a792861e2201dc2ab14fd67acef623a73d338631b`.
Stored all-column SHA-256: `f03eb788758791cb5ea9ee91475cd7ca2060d869483f6bf30071b9975d510c2f`.
This newly recorded fixture replaces the earlier deleted temporary harness;
its numbers are a fresh scalar/bulk comparison, not a comparison with the
previous fixture's timings.

The existing shared Panel/config value is `duckdb_import.workers` in
`config.local.json`; `config.performance.json` has no separate worker field.
The example sets 16; the existing absent-section fallback is 4. Performance
v2 config keeps its existing cap of 64. No setting changes are part of CALC-02.

Read-only worker-routing audit confirms that the main Panel HTML/Source v6
import, materialization, fresh analysis, direct materialization, Performance v2
import/cache and optimizer-preparation paths pass the same shared setting.
The tracked `config.example.json` still uses 4; `config.local.json.example`
uses 16. Direct API defaults are distinct from Panel configuration and should
not be mistaken for additional Panel settings.

Exceptions remain in the existing paths: `partial_performance_import.py` uses
the legacy importer default/cap of 16, `patch_merge_source_v6.py` has a CLI
default of 4, synchronous `LocalSurfacesService.surface_publish()` omits the
service worker value, and the older `run_source_v6_analysis()` route is serial.
Remote Source import reads the remote machine's shared config rather than
transmitting the local worker value. These observations introduce no runtime
change in CALC-02.

Outer pools and DuckDB query threads are separate mechanisms. Fresh surface
validation explicitly uses one DuckDB thread and a 1 GB connection limit;
Performance cache workers set SQL threads to one, while the final Panel
selection query sets them to the shared worker count. Most other connections
use plain `duckdb.connect` without a query-thread setting. The static audit
cannot establish their runtime default or prove excessive nested parallelism;
that remains a profiling question, not a confirmed defect.

## WIN-01 measured standalone cold-pair stage (2026-09-29)

The public pair now shares one complete `_load_source(result_id)` snapshot only
within that call. The scalar path, each window's lazy flat calculation, scalar
persistence and post-write readback retain their existing order. No source is
cached across calls. Four actual-schema cold valid/out-of-range combinations,
cache-hit permutations, autocommit versus caller rollback, and a real source
mutation between pair calls verify this boundary. The Windows module passed 43
tests; related selection/equity/Panel/portfolio tests passed 488 with four
existing Windows symlink skips. Twelve new collected nodes bring total
collection to 5,578. Fresh full-suite verification passed 5,569 tests with
nine existing Windows symlink skips and 30 warnings in 1,504.19 seconds.
Independent Opus 5/high returned `CODE_REVIEW_PASS`; WIN-01 is accepted.

A temporary DuckDB 1.5.5 actual-schema fixture contains 2,000 open/close
cycles, 4,000 actions and 4,001 equity samples. Untimed real function-forwarding
probes found source loads 2→1 and logical SQL operations 12→9 for two distinct
cold windows; flat preparations, calculations, scalar writes and cache reads
stayed at 2/2/2/4. Returned typed metric SHA-256 was
`1248558a6d09199a6eef1ba61713ad9d38f49fb748993b9e4bba2c473aa4d675`;
all 20 deterministic stored fields had SHA-256
`9972e81f25b2d095481406262bbfb72d5b96c504f0c23a4c7414599c58853b6c`
in every arm/run; calculation timestamps were separately typed and non-null.
The temporary seed SHA-256 was
`b61e6ab5172b7826bc0f0ba709aedb4ba8cf4e525c36f845f03f024b2fbfcfc3`.

One warm-up and seven alternating paired runs measured medians of 0.515420 s
for two scalar calls and 0.346342 s for the pair. Scalar range was
0.496094–0.596792 s; pair range was 0.315456–0.375011 s. The 32.8% lower
median describes this cold-pair fixture only; it does not measure whole
selection, Source analysis, Performance import or the reported import tail.

## CACHE-01 measured fully warm selection helper (2026-09-29)

The fully warm ordinary selection worker now returns no rows for publication.
The batch coordinator therefore skips its writer connection and transaction;
cached windows, readiness, candidate facts and progress callbacks remain the
same. Partial ordinary batches still republish their complete ordered metric
set. Ordinary preview was already read-only. Window `calculated_at_utc` is a
Panel candidate-LRU token input, so avoiding redundant timestamp updates also
avoids invalidating that token when facts have not changed.

An actual-schema DuckDB seed with three ACTIVE results and 21 cached windows
had SHA-256 `0f30a129e633e94149e3e0f79dcfce3024ead5efed803c201c81a8a9fc3c0888`.
The pinned pre-edit `67f1e99` baseline and candidate used the same ignored
harness, seed, cohort, one worker, and seven measured fresh-seed runs after one
warm-up each. These were separate sequential sweeps, not cross-commit
alternating samples. Every baseline run opened four readers and two writers,
began/committed two transactions, and offered 21 cached rows in two bulk
persistence calls. Every candidate run opened four readers and no writers,
transactions or persistence calls. Source loads/calculations stayed at zero;
callbacks stayed `[2, 1]`. The substantive stored-row SHA-256 was identical
across arms, `f2a4ae6a0397197234f50fd2dc5e0a98bf73c30b90665cf77e96a98b0dced02c`.

Median elapsed time was 0.325610 s before and 0.204175 s after, a 37.3%
reduction for this three-result warm-helper fixture. Baseline range was
0.300606–0.361737 s; candidate range was 0.178534–0.234249 s. These numbers
do not measure whole selection/preview, PerformanceDB import, or its reported
long completion tail. The changed-source-revision regression proves that the
ordinary warm path still makes no source-revision recheck; its previous writer
loop had no recheck to remove.

TDD RED caught the seven-row private write set and a public writer open. The
selection suite passed 181 tests, related windows/equity 253, and
Panel/export/portfolio 531 with five Windows symlink skips. Fresh full-suite
verification passed 5,570 tests with nine Windows symlink skips and 30 warnings
in 1,556.37 seconds. Independent Opus 5/high returned `CODE_REVIEW_PASS`;
CACHE-01 is accepted. The latest read-only local journal check still found the
409-report September 24 import without `phase_seconds`, so the next
representative import is needed to attribute its completion tail.

## CALC-01b seven-window boundary measurement (2026-09-29)

Selection now requests ordered boundary search only for source tuples loaded
with explicit timestamp/index ordering. Scalar, pair and portfolio calculations
keep the prior linear path; the portfolio source query does not guarantee row
order. The ordered path uses the existing shared flat timeline and preserves
the inclusive equity interval and W0-exclusive action interval. Empty,
missing, collapsed, no-trade, out-of-range and duplicate-boundary regressions
compare complete typed results; an actual-schema test also compares persisted
deterministic fields. This change adds no worker setting or schema version.

The fixed actual-schema DuckDB 1.5.5 seed SHA-256 is
`633d596aecf240ec0dcadd5fa37c892a056b2958bc99bfe43ffb6738a9d8c8cd`.
It has 4,000 hourly actions, 4,001 equity samples, 2,001 flat timestamps and
seven distinct AVAILABLE selection windows. The seven complete metrics had
identical canonical SHA-256
`9e82b18e45489af0d630fbdc286288c300deb4d57b4f4dbe7b26d8d056c2a289`
in the default and ordered modes.

One warm-up pair and 21 same-process pairs alternated execution order on the
same preloaded source and flat tuple. The timed region contained only seven
sequential calculator calls per arm. Median was 0.038658 s for linear versus
0.031973 s for ordered search, 17.3% lower on this stage and fixture.
Candidate nearest-rank p75 was 0.033668 s, below the baseline median;
candidate minimum 0.029805 s was below baseline minimum 0.035230 s. All
predeclared timing gates passed. The pinned pre-edit 8-sample baseline was
sanity evidence only; the paired in-process baseline was the comparator.
This is not a whole-selection, PerformanceDB import or production-tail speed
measurement.

## Карта покрытия и ограничения

| Контур | Просмотренные модули | Итог |
| --- | --- | --- |
| Source v6 import/merge | source_v6_importer, source_v6_storage, source_v6_merge, source_v6_stitch; Panel Source/CLI callers | S1, B5 deferred; повторные full-file hashes и metadata |
| Materialization/publication | source_v6_materializer, source_v6_surface_fresh, source_v6_coverage, source_v6_storage | S2, B4; старый OOM фикс учтен |
| Analysis | source_v6_analysis_fresh, pipeline, plateau, selection, fresh shortlist callers | S3; точность universe выше эвристического pruning |
| Performance input/parser | performance_v2_input, performance_v2_html, performance parser | P1, B6; hash/staging/limits — границы доверия |
| Import/schema | performance_v2_import, performance_v2_store | P1; batch/migration/память |
| Window/equity | performance_v2_windows, performance_v2_equity_quality, performance_v2_equity_cache | P2; формулы не менять |
| Selection/Panel | performance_v2_selection, panel, panel_performance_v2 | P3, B3 |
| Review/export | performance_v2_selection_review, performance_v2_finalist_retest, общий selection workbook writer | P4 |
| RETEST | performance_v2_retest, performance_v2_finalist_retest | P4; tester runtime вне анализа DB-ускорений |
| Optimizer DB adapter | performance_v2_optimizer, portfolio/input, panel_portfolio callers | P5, B1; solver/search не часть DB-аудита |
| Cleanup | performance_v2_prune, scripts/prune_performance_v2.py | B2; backup/restore сохранить |
| Legacy / remote | duckdb_import/source_schema/direct, source_v6_surface/analysis, analysis_storage, performance/import/store/metrics, programs v3/v4, panel_remote_source_db | L1, B7 deferred; обзор и bounded repro, без нового real-corpus профиля |

Тщательно проверялись текущие v6/v2 пути и их тяжелые зависимости; legacy-код не
получал того же уровня динамической проверки. Полный corpus, cold-disk, REPLACE
с реальным inbox, remote-host CPU/диск и исчерпывающая проверка всех редких branches
остаются задачами измерения при внедрении. Этот отчет не закрывает открытые M5 gates.

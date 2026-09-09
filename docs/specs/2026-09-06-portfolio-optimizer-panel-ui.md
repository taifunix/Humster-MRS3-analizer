# Portfolio Optimizer — минимальный интерфейс Panel

**Дата:** 2026-09-06
**Статус:** Specified / `PLAN_APPROVED`, U0 docs `CODE_REVIEW_PASS`;
U1 не реализован.
**Основная спецификация:** [Portfolio Optimizer](2026-09-05-portfolio-optimizer.md).
**Решение:** [ADR-0031](../decisions/0031-portfolio-optimizer-panel-ui-and-campaign-boundary.md).
**План:** [поэтапное внедрение](../superpowers/plans/2026-09-05-portfolio-optimizer.md).

## 1. Назначение и приоритет документов

Документ определяет только интерфейс Panel, запуск предварительного расчёта,
состояния задания, редактирование настроек и выгрузку XLSX. Правила допуска,
расчёта размера позиции, ликвидности, маржи, риска, перебора и ранжирования
остаются в основной спецификации.

При конфликте основная спецификация Portfolio Optimizer и
[ADR-0030](../decisions/0030-portfolio-optimizer-m2-admission-and-sizing-contract.md)
имеют приоритет над этой UI-спецификацией и ADR-0031 в вопросах `FINALIST`,
ликвидности, leverage, sizing и DD. Редакция D7 сохраняет смысл D6 в этих
вопросах. Интерфейс не повторяет алгоритмы оптимизатора.

## 2. Цели и границы

### 2.1. Цели

- превратить существующую вкладку `Portfolio` в рабочее место
  `Portfolio Optimizer`;
- дать пользователю выбрать пары, направления, профили и ограничения запуска;
- выполнять предварительный расчёт как сохраняемое серверное задание;
- показывать подробный журнал и прогресс после перезагрузки браузера или Panel;
- показывать сводку и выгружать успешный результат этапа 1 в XLSX;
- редактировать отдельный `portfolio_optimizer.local.json` в существующей
  вкладке настроек;
- заранее определить, но не включать, передачу выбранных вариантов тестеру.

### 2.2. Не входит

- алгоритмы отбора, расчёта размеров, риска и ранжирования в Panel;
- запись в PerformanceDB;
- запуск портфельного тестера на этапе 1;
- загрузка или обратный импорт XLSX;
- торговые команды, допуск к реальной торговле и управление средствами;
- удалённый доступ, новая авторизация или хранение секретов;
- автоматическая очистка старых заданий и результатов.

M2 и последующие серверные этапы оптимизатора не зависят от готовности UI.

## 3. Граница доверия

Интерфейс сохраняет существующую локальную модель Panel на `127.0.0.1`.
Новые учётные данные и удалённая авторизация не добавляются.

Только `GET/PUT /api/v2/portfolio/settings` могут передавать значения локальных
путей, чтобы пользователь мог видеть и редактировать их в локальной форме.
Остальные ответы API, журнал, ошибки и XLSX содержат только названия полей,
логические идентификаторы, символы, Strategy ID, числа, метрики, версии и
digests. Абсолютные пути, переменные окружения и секреты запрещены. При
диагностике значение заменяется на `<redacted-path>` или `<redacted-secret>`.
Это правило явно охватывает `readiness.*.blockers`,
`job.diagnostics[].message`, `Error.error.message`, все `field_errors`, значения
`Metadata` и `Excluded.Message`. Единственное исключение — `document` в Settings
GET/PUT локальной Panel.

## 4. Экран запуска

### 4.1. Исходная готовность

До запуска Panel показывает:

- состояние настроек, `schema_version`, `policy_version` и `config_digest`;
- доступность этапов 1 и 2 и точные причины блокировки;
- наличие активного задания;
- список доступных пар и количество текущих `FINALIST` по направлениям.

### 4.2. Выбор пар и направлений

Пользователь задаёт общие значения `Максимум LONG` и `Максимум SHORT`.
При выборе пары эти значения копируются в её строку. После копирования каждое
значение редактируется независимо. `0` отключает направление.

Входной набор — только текущее точное `User Status=FINALIST`. Если финалистов
меньше или столько же, сколько заданный максимум, берутся все. Если больше,
усечение выполняется только по текущему `User Rank` по возрастанию. `Auto Rank`
не участвует.

Область полноты и уникальности ранга — одна пара и одно направление. Порядок:

1. невыбранная пара получает `PAIR_UNSELECTED`;
2. максимум `0` получает `DIRECTION_DISABLED`;
3. если количество не больше максимума, принимаются все без требования ранга;
4. если количество больше максимума, требуются заполненные уникальные
   `User Rank`, затем принимаются первые N;
5. отсутствие или повтор ранга блокирует только эту пару и направление с
   `USER_RANK_MISSING` или `USER_RANK_DUPLICATE`;
6. валидные строки после N получают `USER_RANK_CUTOFF`.

Расчёт продолжается, если после локальной блокировки остались кандидаты. Если
не осталось ни одного, задание завершается ошибкой
`PORTFOLIO_JOB_NO_ELIGIBLE_CANDIDATES`, без XLSX.

### 4.3. Профили и поля запуска

`AGGRESSIVE`, `BALANCED` и `CONSERVATIVE` выбираются независимыми флажками;
изначально выбраны все. Для каждого выбранного профиля задаются:

| Поле | Правило |
| --- | --- |
| `equity_usdt` | число, совместимое с `DECIMAL(38,12)`, строго больше нуля |
| `max_balance_usdt` | пусто означает отсутствие переопределения; иначе `DECIMAL(38,12)` строго больше нуля; связи с equity нет |
| `max_candidates` | целое строго больше нуля и не больше текущего `search.total_test_budget` |

Если не выбран ни один профиль, кнопка расчёта заблокирована. Эти значения —
поля конкретного Campaign, а не новые ключи файла настроек.

## 5. Campaign и задание этапа 1

Кнопка `Рассчитать варианты` атомарно фиксирует проверенные поля запуска,
точные байты и digest текущих настроек, версии политик и алгоритмов, а также
порядок входных данных на сервере. После этого создаются новые неизменяемые
`campaign_id` и `job_id`, а поля этого запуска блокируются.

Каждое нажатие создаёт новый Campaign. `Новый расчёт` может заполнить форму
предыдущими значениями, но создаёт новые снимки и идентификаторы. Если текущий
digest настроек отличается от зафиксированного, Panel показывает
`SETTINGS_CHANGED_SINCE_FREEZE`; активный Campaign не меняется.

Одновременно разрешено одно активное задание оптимизатора. После проверки
`expected_config_digest`, но до создания идентичностей сервер рассчитывает
`input_digest` нового запроса:

- если `input_digest` и `config_digest` равны обоим digest активного задания,
  возвращается `409 PORTFOLIO_JOB_ACTIVE_DUPLICATE`;
- при любом другом активном задании возвращается `409 PORTFOLIO_JOB_BUSY`.

Проверка duplicate выполняется первой и имеет приоритет над busy. При
несовпадении `expected_config_digest` с точными текущими байтами настроек сервер
возвращает `409 CONFIG_CHANGED` и не создаёт Campaign или job.
Любой неуспешный ответ POST `/campaigns` также не создаёт ни одну из этих
идентичностей и не ставит задание в очередь.

Этап 1 не вызывает совместный портфельный тестер.

## 6. Состояния, прогресс и восстановление

Задание связано ровно с одним Campaign и сохраняется сервером через общий
реестр заданий Panel. Обновление страницы или перезапуск браузера не прерывают
работу. После перезапуска серверного процесса сохранённые `QUEUED`, `RUNNING`
и `CANCEL_REQUESTED` переводятся в `INTERRUPTED`; скрытого продолжения нет.

Состояния: `QUEUED`, `RUNNING`, `CANCEL_REQUESTED`, `CANCELLED`, `FAILED`,
`INTERRUPTED`, `SUCCEEDED`.

Фиксированный порядок отображаемых стадий:

1. `VALIDATE_SNAPSHOT`
2. `LOAD_FINALISTS`
3. `SELECT_CANDIDATES`
4. `GENERATE_VARIANTS`
5. `VALIDATE_VARIANTS`
6. `BUILD_WORKBOOK`
7. `PUBLISH_RESULTS`

Для стадии передаются `completed` и, только когда заранее известен знаменатель,
`total` и монотонный `percent`. При неизвестном знаменателе показываются
неопределённая шкала и прошедшее время. Общий прогресс равен
`floor(число завершённых стадий * 100 / 7)`, остаётся меньше 100 до успешного
завершения и равен 100 только при `SUCCEEDED`.

Запись журнала содержит время UTC, стадию, важность `INFO|WARNING|ERROR`,
стабильный код с префиксом `PORTFOLIO_JOB_`, текст и необязательные счётчики.
Отмена запрашивается идемпотентно и исполняется на безопасной границе. Частичные
данные отменённого, прерванного или упавшего задания доступны только как
очищенная диагностика, а не как результат или XLSX. Автоматического удаления
заданий, снимков, диагностики и успешных книг в U1 нет.

## 7. Контракт API Panel

Другие маршруты не входят в этот контракт.

| Метод и путь | Запрос | Успешный ответ | Определённые ошибки |
| --- | --- | --- | --- |
| `GET /api/v2/portfolio/readiness` | нет | `200 {stage1:{enabled,blockers},stage2:{enabled,blockers},settings_state}` | очищенный `Error` |
| `GET /api/v2/portfolio/settings` | нет | `{state,document,digest,schema_version,policy_version}`; `document` и `digest` равны `null`, если документ нельзя безопасно разобрать; состояния: `READY`, `MISSING`, `INVALID`, `UNSUPPORTED_SCHEMA` | очищенный `Error` |
| `PUT /api/v2/portfolio/settings` | `{expected_digest,document}` с полным документом поддерживаемой схемы | форма успешного Settings GET с новым digest | `409 CONFIG_CHANGED`, `422 CONFIG_INVALID`; read-only состояния не сохраняются |
| `POST /api/v2/portfolio/campaigns` | `{pairs:[{pair,max_finalist_long,max_finalist_short}],profiles:[{profile_id,equity_usdt,max_balance_usdt?,max_candidates}],expected_config_digest}` | `202 {campaign_id,job_id,status:"QUEUED",input_digest,config_digest}` | `409 CONFIG_CHANGED` без создания Campaign/job; `409 PORTFOLIO_JOB_ACTIVE_DUPLICATE`; `409 PORTFOLIO_JOB_BUSY`; `422 PORTFOLIO_CAMPAIGN_INVALID` |
| `GET /api/v2/portfolio/jobs/active` | нет | всегда `200`: `{job:null}`, если активного задания нет, иначе `{job:<Job>}` | очищенный `Error` |
| `GET /api/v2/portfolio/jobs/{job_id}` | нет | `200 {job:<Job>}` | `404 PORTFOLIO_JOB_NOT_FOUND` |
| `POST /api/v2/portfolio/jobs/{job_id}/cancel` | нет | `202 {job_id,status}`; повтор в `CANCEL_REQUESTED` возвращает то же текущее состояние без второго действия | `404 PORTFOLIO_JOB_NOT_FOUND`; любое уже терминальное состояние — `409 PORTFOLIO_JOB_TERMINAL` |
| `GET /api/v2/portfolio/campaigns/{campaign_id}/results` | нет | `200` только при `SUCCEEDED`: `{campaign_id,input_digest,config_digest,summary,blockers,workbook_available:true}` | неизвестный Campaign — `404 PORTFOLIO_CAMPAIGN_NOT_FOUND`; `QUEUED`, `RUNNING`, `CANCEL_REQUESTED`, `CANCELLED`, `FAILED` и `INTERRUPTED` — `409 PORTFOLIO_JOB_RESULTS_UNAVAILABLE` |
| `GET /api/v2/portfolio/campaigns/{campaign_id}/stage1.xlsx` | нет | `200` с байтами XLSX только при `SUCCEEDED` | неизвестный Campaign — `404 PORTFOLIO_CAMPAIGN_NOT_FOUND`; любое состояние кроме `SUCCEEDED` — `409 PORTFOLIO_JOB_WORKBOOK_UNAVAILABLE` |
| `POST /api/v2/portfolio/campaigns/{campaign_id}/tester-submissions` | `{confirmed:true,campaign_id}` | в будущем: `202 {campaign_id,job_id,status:"QUEUED"}` | сейчас маршрут объявлен, но не реализован и всегда возвращает `409 PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED`; несовпадение подтверждения отклоняется |

```text
Error = {
  error: {
    code: string,
    message: string,
    field_errors: [{field: string, code: string, message: string}]
  }
}
```

`field_errors` — пустой список, когда ошибок полей нет. Сообщения не повторяют
значения путей и секретов.

Канонический код закрытого этапа 2 — только
`PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED`; сокращённый `STAGE2_NOT_AUTHORIZED` не
используется.

```text
Job = {
  job_id,
  campaign_id,
  kind,
  status,
  stage: {index,name,status,completed,total?,percent?},
  overall_percent,
  counters,
  diagnostics: [{severity,code,message}],
  created_at,
  started_at?,
  finished_at?
}
```

Все даты — ISO-8601 UTC. В U1 `kind=STAGE1_CALCULATION`; значение
`TESTER_SUBMISSION` появляется только после разрешения этапа 2.

`config_digest` и CAS настроек используют SHA-256 точных загруженных байтов.
`input_digest` — SHA-256 детерминированного UTF-8 JSON проверенных полей запуска.
Зафиксированный серверный порядок не восстанавливается во frontend.

`settings_state` в readiness имеет ровно те же значения `READY`, `MISSING`,
`INVALID`, `UNSUPPORTED_SCHEMA`, что Settings GET. При любом значении кроме
`READY` `stage1.enabled=false`, а `stage1.blockers` содержит стабильную причину.
Readiness описывает серверные возможности и не получает ещё не отправленную
форму. Поэтому кнопка Calculate доступна только при одновременных
`stage1.enabled=true` и локально валидной форме; отсутствие выбранного профиля
или пары блокирует кнопку независимо от readiness. Сервер повторяет эту
проверку при POST. `stage2` не может быть enabled без успешного Campaign,
M5/M6 и явного разрешения тестера.

## 8. Настройки

В существующей вкладке Settings добавляется один сворачиваемый раздел
`Portfolio Optimizer`. Группы являются только представлением и не создают
ключи: пути/хранилище, поиск, ликвидность, маржа, риски, проверка/ранжирование.

Текущая схема v1 содержит:

- `schema_version`, `policy_version`, `algorithm_versions`;
- `inputs.performance_db`, `inputs.portfolio_db`, `inputs.collector_root`,
  `inputs.approved_templates`;
- для каждого элемента `scenarios`: `account`; денежные объекты `deposit`,
  `collateral`, `max_balance`, `sizing.upper_bound` с `amount`/`currency`;
  элементы `sizing.grid` с теми же полями;
- дескрипторы `search.universe`, `search.composition`, `search.sizing`,
  `search.limiter`, `search.priority` с `policy_id`/`parameters`, а также
  `search.seed`, `search.rounds`, `search.total_test_budget`;
- `research.development_window`, `research.validation_window`, `research.warmup`,
  `research.evidence_minimum` с `value`/`unit`, плюс `research.boundary`;
- дескрипторы `liquidity` и `margin` с `policy_id`/`parameters`;
- ровно три профиля с дескрипторами `pnl` и `liquidity`
  (`policy_id`/`parameters`), а также `ranking.id`,
  `ranking.parameters`, `ranking.top_n`;
- `runner.target`, `runner.root`, `runner.timeout.value`, `runner.timeout.unit`,
  `runner.retries`.

Неизвестные ключи отклоняются. `UNSUPPORTED_SCHEMA`, `MISSING` и `INVALID`
показываются явно в режиме только для чтения; Panel не создаёт, не исправляет и
не подменяет документ значениями по умолчанию. `Save` отключён.

Форма отмечает несохранённые изменения. `Reload` требует подтверждения перед их
отбрасыванием. `Save` отправляет полный поддерживаемый документ и
`expected_digest`. Сервер сначала проверяет документ, пишет временный файл в той
же папке, выполняет flush/fsync, атомарный `os.replace`, повторное чтение,
разбор и проверку digest. При неудаче повторного чтения прежние байты
восстанавливаются тем же атомарным способом. Успешный ответ возвращает новый
digest. Изменения влияют только на будущие Campaign.

В v1 `scenarios[].max_balance` обязателен и имеет тип `Money`. Возможная v2
может разрешить `null`/отсутствие и определить значения формы по умолчанию, но
это требует отдельного принятого изменения схемы, миграции и parser. Пока parser
не поддерживает v2, такие поля не показываются как редактируемые. Необязательный
`max_balance_usdt` в Campaign — отдельное переопределение запуска и не меняет v1.

## 9. Сводка результата этапа 1

При успехе Panel показывает:

- сколько финалистов прочитано и допущено;
- исключения, сгруппированные по причинам;
- число созданных вариантов по каждому профилю;
- число вариантов, подготовленных к будущей передаче тестеру;
- блокеры и незакрытые политики отдельно от обычных исключений.

Кнопка `Скачать XLSX` появляется только при `SUCCEEDED`.

## 10. XLSX этапа 1

Книга не содержит макросов, формул, внешних ссылок, локальных путей или
секретов. Порядок листов фиксирован: `Summary`, `Finalists`, `Portfolios`,
`Members`, `Excluded`, скрытый `Metadata`. Числа записываются числовыми ячейками
без зависимости от локали; время — строкой ISO-8601 UTC. Неизвестные значения
остаются пустыми, а не подменяются нулём.

Точные заголовки:

| Лист | Заголовки слева направо |
| --- | --- |
| `Summary` | `Key`, `Value` |
| `Finalists` | `Campaign ID`, `Strategy ID`, `Result ID`, `Pair`, `Direction`, `User Status`, `User Rank`, `Effective Maximum`, `Selection Status`, `Selection Reason` |
| `Portfolios` | `Campaign ID`, `Candidate ID`, `Profile`, `Scheduling Position`, `Scheduling Key ID`, `Scheduling Score (Individual/Margin; Not Portfolio PnL)`, `Member Count`, `Pair Count`, `Limiter`, `Maximum Individual DD %`, `Minimum Free Margin Reserve %`, `Maximum Account MM Load %`, `Gate Result`, `Blocking Reasons`, `User Decision`, `User Test Priority`, `User Comment` |
| `Members` | `Campaign ID`, `Candidate ID`, `Profile`, `Member Ordinal`, `Strategy ID`, `Result ID`, `Pair`, `Direction`, `User Rank`, `Scalar %`, `Quantity`, `Leverage`, `Notional USDT`, `Estimated Individual DD USDT`, `Estimated Individual DD %`, `Liquidity Scalar Ceiling %`, `Calculated Initial Margin USDT`, `Gate Result`, `Reasons` |
| `Excluded` | `Campaign ID`, `Scope`, `Object ID`, `Pair`, `Direction`, `Profile`, `Stage`, `Gate Result`, `Portfolio Reason`, `Message` |
| `Metadata` | `Key`, `Value` |

В `Members` текстовыми являются идентификаторы, профиль, пара, направление,
`Gate Result` и `Reasons`; `Member Ordinal`, `User Rank`, `Scalar %`, `Quantity`,
`Leverage`, суммы USDT и проценты — числовые ячейки. Суффикс `%` означает
процентные пункты, `USDT` — сумму в USDT, `Quantity` — количество контракта в
биржевой единице инструмента. `User Rank` может быть пустым только когда §4.2
разрешает отбор без ранга.

`Metadata` обязательно содержит строки `schema_version`, `campaign_id`,
`created_at_utc`, `input_digest`, `config_digest`, `policy_version`, версии
алгоритмов и договор стабильных идентификаторов. Причина в `Portfolio Reason`
берётся из перечисления §4.2 или из нормативных причин основной спецификации;
UI не определяет новый смысл этих причин.

Порядок строк стабилен для воспроизводимости, но не имеет делового смысла.
Этап 2 использует зафиксированный серверный порядок, а не порядок строк книги.
Три последние колонки `Portfolios` изначально пусты:

- `User Decision`: пусто, `TEST` или `SKIP`;
- `User Test Priority`: пусто или положительное целое;
- `User Comment`: произвольный текст.

Они зарезервированы для будущего безопасного импорта. В MVP импорта нет.
Будущий читатель должен игнорировать неизвестные дополнительные колонки и их
порядок, опираясь на стабильные имена листов и заголовков.

## 11. Этап 2 — передача тестеру

Отдельное подтверждение обязательно. Для каждого выбранного профиля берутся
первые N вариантов из зафиксированного серверного порядка, где N — сохранённый
`max_candidates`. XLSX не загружается обратно и не является входом.

Кнопка и маршрут остаются отключёнными с точной причиной до завершения M5/M6 и
отдельного явного разрешения на запуск тестера. Исследовательские пороги сами по
себе не разрешают тестирование, `RECOMMENDATION_READY`, допуск к торговле или
реальное использование.

## 12. Архитектурная граница

Поток вызовов: frontend Panel → API Panel → пакет `src/mrs3/portfolio`.
Выбор финалистов, порядок, генерация вариантов, причины исключения и все
расчёты выполняет пакет оптимизатора. Frontend и адаптер Panel не имеют своих
копий алгоритмов и не пишут в PerformanceDB. При реализации допускается один
небольшой специализированный адаптер Panel, только если существующий код Panel
не позволяет сохранить эту границу без смешивания обязанностей.

## 13. Приёмка U0 и будущего U1

U0 считается завершённым после согласованности этой спецификации, ADR-0031,
основной спецификации, плана, PRD, progress и AGENTS и независимого review.
Тесты к документационному изменению неприменимы.

U1 потребует проверяемых сценариев:

- изоляция ошибки `User Rank` одной пары/направления;
- CAS-конфликт настроек и запрет сохранения неподдерживаемой схемы;
- восстановление страницы и переход незавершённого задания в `INTERRUPTED`
  после рестарта сервера;
- монотонный известный прогресс и неопределённая шкала без знаменателя;
- идемпотентная отмена и отсутствие книги при любом состоянии кроме
  `SUCCEEDED`;
- отсутствие путей и секретов вне Settings GET/PUT;
- неизменяемость Campaign при последующем изменении настроек;
- постоянная блокировка этапа 2 до его двух разрешающих условий.

U0 не разрешает реализацию U1, запуск тестера или реальную торговлю. Открытые
политики основной спецификации — минимальный PnL, окончательные пределы
ликвидности/свежести, `each_strategy_max_dd_pct` и точное ранжирование профилей —
остаются блокерами соответствующих будущих функций.

## 14. Settings form contract (U0 implementation)

The Settings screen uses the same `portfolio_optimizer.local.json` document as
the only source of truth and keeps `GET/PUT /api/v2/portfolio/settings` with
`expected_digest` compare-and-swap. The raw JSON editor is replaced by a human
form. The form exposes only `search.total_test_budget` and, for each of the
three profiles `AGGRESSIVE`, `BALANCED`, and `CONSERVATIVE`, the amounts in
`scenarios.<PROFILE>.deposit`, `collateral`, `max_balance`,
`sizing.upper_bound`, every `sizing.grid` entry, and `profiles.<PROFILE>.ranking.top_n`.
Currencies are shown read-only; paths, IDs, versions, policy descriptors,
research and runner settings remain hidden technical fields in the cloned
document.

Money inputs accept only positive JSON integers or dot-decimal strings. An
unchanged value preserves its exact JSON type and string scale. A changed
digits-only value from a numeric amount is emitted as a JSON integer; a changed
dot-decimal value is a string. When the original amount is a JSON string,
edited values remain strings so their lexemes stay exact. Floating point JSON, comma decimals, zero
and negative values are invalid. Grid entries are one nonblank value per line
after trimming surrounding blank lines, numerically positive, strictly
increasing, and no greater than the edited upper bound. New grid entries use
the READY document's canonical currency. Budget and
`top_n` inputs accept only positive JSON integers matching
`^[1-9][0-9]*$`. Every inbound exposed value is validated before enabling the
form; any invalid value makes the whole form read-only and shows:
`Серверные настройки содержат недопустимые значения. Сохранение отключено.`
This same message applies to inbound JSON float amounts; the form does not
attempt to round or reinterpret binary floating point values.

Save deep-clones the fetched document and patches only exposed leaves. An
unchanged submit remains deep-equal. A successful PUT stores and renders the
returned authoritative document and digest. On `409 CONFIG_CHANGED`, the form
performs exactly one GET, discards local edits, renders the refreshed server
version and keeps the message `Настройки изменены в другой сессии. Загружена
серверная версия.` until the next manual save. Non-`READY` states disable all
form controls and Save; Reload remains available. This form does not change
the separate runtime variant-generation blocker or the paused
export-only-finalists path.

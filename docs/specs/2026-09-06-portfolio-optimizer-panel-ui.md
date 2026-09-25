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

User-visible Russian literals MUST be stored and rendered as UTF-8; mojibake is invalid.

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
| `bank_available_usdt` | необязательный максимум доступного банка в USDT; `null`/отсутствие означает отсутствие потолка; иначе конечное положительное `DECIMAL(38,12)` |
| `max_candidates` | целое строго больше нуля; сохраняемый лимит будущего выбора результатов после joint tests, который не ограничивает полный pre-test composition universe |

The launch form shows the fixed research-risk policy beside each selectable
profile in the same compact three-row table: DD ceiling / free-margin reserve
floor / MM-load ceiling are `20/20/50`, `10/40/35`, and `5/60/20` percent.
These values are policy labels, not editable Campaign fields.

Если не выбран ни один профиль, кнопка расчёта заблокирована. Эти значения —
поля конкретного Campaign, а не новые ключи файла настроек. `search.total_test_budget`
остаётся скрытым legacy/downstream техническим полем конфигурации и не является
полем формы или gate для создания Campaign Этапа 1; общего бюджета между профилями нет.

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
| `POST /api/v2/portfolio/campaigns` | `{pairs:[{pair,max_finalist_long,max_finalist_short}],profiles:[{profile_id,bank_available_usdt?,max_candidates}],expected_config_digest}` | `202 {campaign_id,job_id,status:"QUEUED",input_digest,config_digest}` | `409 CONFIG_CHANGED` без создания Campaign/job; `409 PORTFOLIO_JOB_ACTIVE_DUPLICATE`; `409 PORTFOLIO_JOB_BUSY`; `422 PORTFOLIO_CAMPAIGN_INVALID` |
| `GET /api/v2/portfolio/jobs/active` | нет | всегда `200`: `{job:<Job>}` для активного задания, иначе для последнего завершённого Portfolio job; `{job:null}`, только если Portfolio jobs ещё нет. Это восстанавливает ход и результат после F5 без повторного расчёта | очищенный `Error` |
| `GET /api/v2/portfolio/jobs/{job_id}` | нет | `200 {job:<Job>}` | `404 PORTFOLIO_JOB_NOT_FOUND` |
| `POST /api/v2/portfolio/jobs/{job_id}/cancel` | нет | `202 {job_id,status}`; повтор в `CANCEL_REQUESTED` возвращает то же текущее состояние без второго действия | `404 PORTFOLIO_JOB_NOT_FOUND`; любое уже терминальное состояние — `409 PORTFOLIO_JOB_TERMINAL` |
| `GET /api/v2/portfolio/campaigns/{campaign_id}/results` | нет | `200` только при `SUCCEEDED`: `{campaign_id,input_digest,config_digest,summary,blockers,workbook_available:true}` | неизвестный Campaign — `404 PORTFOLIO_CAMPAIGN_NOT_FOUND`; `QUEUED`, `RUNNING`, `CANCEL_REQUESTED`, `CANCELLED`, `FAILED` и `INTERRUPTED` — `409 PORTFOLIO_JOB_RESULTS_UNAVAILABLE` |
| `GET /api/v2/portfolio/campaigns/{campaign_id}/stage1.xlsx` | нет | `200` с байтами XLSX только при `SUCCEEDED` | неизвестный Campaign — `404 PORTFOLIO_CAMPAIGN_NOT_FOUND`; любое состояние кроме `SUCCEEDED` — `409 PORTFOLIO_JOB_WORKBOOK_UNAVAILABLE` |
| `POST /api/v2/portfolio/campaigns/{campaign_id}/tester-submissions` | `{confirmed:true,campaign_id}` | при подтверждённой committed Stage 1 Campaign и доступном общем `LocalTestingService`: `202 {campaign_id,job_id,status:"QUEUED"}` | без shared tester provider — `409 PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED`; несовпадение подтверждения и Campaign binding отклоняется |

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
  `search.seed`, `search.rounds`, `search.total_test_budget` (legacy/downstream,
  скрытое техническое поле, не gate Этапа 1);
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
Campaign не принимает прежние `equity_usdt` и `max_balance_usdt`: они отклоняются
с полевой ошибкой `LEGACY_PROFILE_FIELD_UNSUPPORTED`; compatibility fallback нет.

## 9. Сводка результата этапа 1

При успехе Panel показывает одну операторскую строку с количеством финалистов,
реально переданных в расчёт после ограничения по `User Rank`, количеством
ненулевых позиций первого варианта и количеством допущенных финалистов, которым
этот вариант назначил нулевую позицию. Размер полного
аудит-снимка и число отсечённых строк не дублируются на экране: полный снимок
может содержать больше строк, потому что фиксируется до усечения, но оптимизатор
получает только допущенные строки.

Для первого варианта Panel показывает состав фактически ненулевых позиций и
понятную декомпозицию банка:

- банк насыщения портфеля — размер счёта, для которого рассчитаны показанные
  полные размеры позиций и выше которого этот вариант больше не масштабируется;
- `Минимальный банк для DD ≤ <предел профиля>% на истории` — минимальный банк
  по исторической кривой при заданном пределе DD (для текущего AGGRESSIVE
  результата подпись содержит `20%`);
- `Банк для DD ≤ <предел профиля>% в 95% стресс-сценариев` как максимум P95
  по настроенным bootstrap-блокам;
- минимальный банк для соблюдения лимитов выбранного профиля;
- `MaxDD SUM` — сумму индивидуальных MaxDD фактических участников, пересчитанных
  пропорционально назначенным полным номиналам позиций;
- среднюю глубину худших 20% и 10% исторических просадок в USDT и две справочные
  доли через `/`: сумма относительно целевого банка / сумма относительно банка
  насыщения. Если целевой банк не задан, первая доля показывается прочерком;
- `IM` и `MM` в USDT с тем же порядком справочных долей: целевой банк / банк
  насыщения; отсутствие целевого банка не подменяется другим знаменателем;
- пару, сторону, Strategy/Result ID, полный номинал позиции, долю банка и плечо
  каждого фактического участника, рассчитанные `balance_percentage` и
  `max_balance`, а также распределение полного номинала между входными ордерами
  только в процентах. Номинал участника не превышает его лимит ликвидности, но
  не обязан ему равняться.

Карточки результата сгруппированы отдельными рядами: все виды банка; показатели
DD/CDaR; PnL и маржа. `bank_available_usdt` подписывается как `Целевой банк`.
Над долями один раз указывается порядок знаменателей `целевой / насыщение`.

Рабочие листы weighted XLSX используют короткие русские заголовки с переносом
не более чем на три визуальные строки и компактной шириной столбцов. Лист
`Состав` кроме назначенного размера содержит проверяемые frozen-данные
PerformanceDB (`Timeframe`, `User Rank`, source PnL/MaxDD, `ORD_N`, доли входных
ордеров X/Y/Z/W в процентах для поддерживаемых 1–4 ордеров) и операторские результаты sizing (`x`, `C`, `x/C`,
`balance_percentage`, `max_balance`, плечо и масштабированный индивидуальный
MaxDD). Технические digest и внутренние коды не добавляются на операторские
листы; для них остаётся скрытый `Metadata`.

`DD`, равный пределу профиля, не означает независимо измеренную просадку
фиксированного банка: исторический банк подбирается как минимальный банк,
удерживающий расчётный DD внутри этого предела. Блокеры и незакрытые политики
показываются отдельно от обычного рангового усечения.

Кнопка `Скачать XLSX` появляется только при `SUCCEEDED`.

Panel does not render the raw `summary` mapping as a key/value dump. The result
card shows the accepted-finalist count, counts by risk profile, the actual
positive-weight composition, and the operator-relevant metrics of variant 1
with display-only rounding. Duplicate `optimizer_*` mirrors, empty collections,
rank-cutoff counts, and
`UNKNOWN`/`NOT_TESTED` limiter fields are omitted. With `Limiter L=0`, limiter
details are not shown; a future positive `Limiter L` makes its known PnL,
reserve, and bottleneck metrics visible through the same formatter.
Campaign/candidate identifiers remain available in one collapsed
technical-details section. Dynamic result values are inserted as text, never
as HTML. The metrics and overview use two columns at 760 px and below, and long
technical identifiers wrap inside their local container.

## 10. XLSX этапа 1

Книга не содержит макросов, формул, внешних ссылок, локальных путей или
секретов. Для текущего `WEIGHTED_V1` порядок листов фиксирован: `Итог`,
`Варианты`, `Состав`, `Финалисты`, `Исключено`, скрытый `Metadata`. Точный
исполняемый JSON остаётся источником полных чисел; операторская книга округляет
денежные суммы и проценты до двух знаков. Неизвестные значения остаются пустыми,
а не подменяются нулём или строкой `UNKNOWN`.

`Итог` содержит только понятную сводку первого варианта. `Варианты` содержит по
одной строке на каждый вариант с банком, риском, PnL, IM/MM и списком пар.
`Состав` содержит по одной строке на каждую ненулевую позицию каждого варианта:
ранг варианта, профиль, пару/сторону, Strategy/Result ID, полный номинал позиции,
долю итогового банка, плечо и доступную ёмкость. `Финалисты` сохраняет аудит
исходного снимка и результата рангового отбора, `Исключено` — только отдельные
причины исключения, а `Metadata` — воспроизводимые привязки Campaign.

Legacy `PRETEST_PROXY` сохраняет прежний формат книги до отдельной миграции.

Legacy `PRETEST_PROXY` заголовки:

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

Отдельное подтверждение обязательно. Сначала для каждого выбранного профиля
строится полный зафиксированный pre-test composition universe; его объём
ограничивается только `search.max_enumerated_combinations`. После joint tests
будущий выбор первых N результатов из серверного порядка может использовать
сохранённый `max_candidates`. XLSX не загружается обратно и не является
входом.

Кнопка UI остаётся отключённой с точной причиной до завершения M5/M6 и
отдельного явного разрешения на запуск тестера. Backend-маршрут Phase 7
доступен только для подтверждённой committed Stage 1 Campaign через общий
`LocalTestingService`; без injected provider он fail-closed с
`PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED`. Исследовательские пороги сами по себе не
разрешают тестирование, `RECOMMENDATION_READY`, допуск к торговле или реальное
использование.

Для текущего Phase 7 разрешена подготовка и fake-verified local-only off-only
baseline через backend-маршрут. Общий `LocalTestingService` получает уже готовые JSON из
приватного Stage 1 artifact и не вызывает render/resizing. До изменения
`hb/settings_strategy` он использует существующие target lock, stop и snapshot;
затем устанавливает ровно всю пачку выбранного портфеля и tester config с
`single_mode=false`, `UpdateData=false`, `use_runs=false` и пустым
`parameter_mining`. Имена файлов обязаны совпадать с именами стратегий, а
readback hashes — с замороженными payloads. Старые reports/wizard logs не
удаляются. На success, error или timeout прежние config и strategies должны
быть восстановлены существующим restore path до снятия lock. Этот helper сам
не запускает tester и не меняет Stage 2 authorization.

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
form. The form exposes, for each of the three profiles `AGGRESSIVE`, `BALANCED`,
and `CONSERVATIVE`, the amounts in
`scenarios.<PROFILE>.deposit`, `collateral`, `max_balance`,
`sizing.upper_bound`, every `sizing.grid` entry, and `profiles.<PROFILE>.ranking.top_n`.
The legacy/downstream `search.total_test_budget` remains hidden and is not a
Stage-1 form field or creation gate.
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
the READY document's canonical currency. The
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

## D8 amendment: v2 settings and thin facade

The Stage 1 amendment below is normative for current PRETEST_PROXY Campaigns;
the settings details in this D8 section are retained as the compatibility
history for the earlier one-size adapter path.

The optimizer Settings contract now uses strict `schema_version = 2`. The
server accepts a v1 document only through the deterministic in-memory
migration defined by the main optimizer specification and ADR-0033. GET may
display the migrated v2 document while retaining the source byte digest for
CAS. A successful Save always writes v2; it removes every legacy monetary
`scenarios.<PROFILE>.sizing.grid` and preserves all other accepted document
fields. v2 unknown keys are rejected before write. A stale
`expected_digest` still returns `409 CONFIG_CHANGED`, and the server never
merges a stale partial document.

The current weighted Settings form exposes only controls consumed by the
production adapter. The main surface contains
`close_volume_participation_pct` (1..200, default 200), `round_down_usdt`
(default 10), `backfill_write_enabled`, and the default-off
`liquidity.spread_history_bypass_pretest` checkbox labelled
**Предварительный расчёт без истории стакана**. The checkbox is a temporary
pretest-only mode and warns that spread history is not read or evidence of
liquidity. A default-closed advanced section
contains `search.max_enumerated_combinations`, `history_step_minutes`,
`lp_solutions_per_profile`, `max_targets`, `bootstrap_scenarios_per_block`,
`bootstrap_diagnostic_scenarios`, `wall_time_seconds`, `solver_time_seconds`,
`minimum_coverage_pct`, market-reference `maximum_age_hours`, and archive lag
0..48 (default 6). The combinations field is a fail-closed Cartesian-product
guard, not a count of future tester runs. `lp_solutions_per_profile` is the
actual per-profile LP-call ceiling used by weighted search; it is not a display-
only plan field.

The weekend window, scenarios, profile compatibility fields, and weighted
parameters not consumed by the current adapter are not rendered and are
removed from the active `weighted_search` document rather than preserved as
misleading controls. The retained flat keys are ordered by meaning: history,
search breadth, bootstrap verification, runtime limits, then deferred Phase 13
controls. Limiter and priority controls are the only exception: their six
fields remain in a separate default-closed Phase 13 section with an explicit
notice that current Stage 1 forces `L=0` and priority 1, so editing them cannot
affect current calculation. Existing schema-v2 documents with retired weighted
keys are normalized in memory; unknown keys still fail closed. The form does
not render a sizing grid or partial-close size variants. Fixed descriptors,
algorithm versions, paths, and policy identifiers remain technical fields in
the cloned document. The Bybit minute root is
accepted as a server-side local input and is never supplied by an untrusted
browser field. The legacy/downstream `search.total_test_budget` remains in the
configuration schema but is hidden from Settings and does not gate Stage 1
Campaign creation.

The retired keys are `repair_attempts`, `additional_passes`, `base_vectors`,
`scenarios`, `cdar_pct`, `diagnostic_cdar_pct`,
`alternatives_per_profile`, `p30_tolerance_pct`, `bootstrap_block_days`,
`bootstrap_p95`, `bootstrap_low_block_common_days`,
`scale_warning_multiple`, `api_requests_per_second`, `api_concurrency`,
`api_retries`, `reference_max_age_hours`, and `csv_download_concurrency`.
They were not runtime inputs. Where their names resembled current algorithm
steps, the actual WS1.2 behavior was and remains fixed in code:
bootstrap block families are 1/3/7 days, bootstrap risk uses nearest-rank P95,
CDaR diagnostics are 80/90, and the optional repair/additional branch performs
at most one pass. `base_vectors`, `scenarios`, `alternatives_per_profile`,
`p30_tolerance_pct`, `bootstrap_low_block_common_days`, `scenarios`,
`scale_warning_multiple`, and the retired API/download/reference controls had
no Stage 1 config consumer. Scenario counts, wall time, solver time, target breadth,
and solver-call breadth remain explicit through the retained fields above.

Unknown adapter blocker and warning codes remain visible verbatim in the Panel
status, journal, summary, and exclusions; only codes with an explicit Russian
label are humanized. If weighted search unexpectedly escapes its result
contract, Panel receives a safe all-caps search code when one is available,
otherwise only `WEIGHTED_SEARCH_EXCEPTION_<CLASS>`; exception text, paths and
values are not exposed.

Panel remains a thin facade. It owns HTTP validation, CAS, immutable Campaign
capture, job lifecycle/progress, and artifact delivery. `src/mrs3/portfolio`
owns migration, candidate identity, the one frozen market-reference snapshot,
sizing, liquidity, gates, and ranking. No Panel code duplicates those
algorithms or writes PerformanceDB.

## Stage 1 PRETEST_PROXY amendment

The Settings form labels `max_candidates` as **Candidates for joint tick test**
and presents `search.max_enumerated_combinations` as the **Pretest evaluation
budget**. Individual DD and ranking `top_n` remain readable compatibility
fields and are not editable Stage 1 gates. Stage 1 output is preliminary
PRETEST_PROXY evidence, with joint metrics and recommendation fields shown as
`UNKNOWN` or `NOT_TESTED`; a PARTIAL profile result remains visible in the
workbook and Panel summary.

## Current weighted Stage 1: limiter disabled, off only

### Optional profile bank ceiling amendment

See [ADR-0041](../decisions/0041-portfolio-optimizer-optional-bank-ceiling.md).

Each selected risk profile carries the optional Campaign launch field
`bank_available_usdt`, denominated in USDT and constrained to finite positive
`DECIMAL(38,12)` when supplied. Omitted and explicit `null` values are
equivalent and pass `bank_available=None` to weighted search. The ceiling is
retained only in `campaign.launch.profiles` for audit and summary; it is never
copied into executable `facts.B` or tester `InitialBalance`.

Weighted search and the adapter use `metrics.required_bank_usdt` as the
authoritative candidate bank. A candidate over the supplied ceiling is
excluded; when the profile is exhausted the profile reports
`BANK_UNAVAILABLE`. Every wrapper in a candidate has that required bank as
`facts.B`. For example, a `5000` ceiling and `1800` required bank produce
`facts.B=1800` and `InitialBalance=1800`; an uncapped profile behaves the same
way using its required bank. Stage 2 reads only committed candidate metrics and
wrapper facts, and rejects missing, non-positive, non-finite, mixed, or
mismatched values. Legacy `equity_usdt` and `max_balance_usdt` fields fail
closed with `LEGACY_PROFILE_FIELD_UNSUPPORTED`.

For `search_mode=WEIGHTED_V1`, this section governs current Stage 1 limiter and
priority behavior; see [ADR-0040](../decisions/0040-portfolio-optimizer-phase7-off-only-local-stage2.md).

For the production weighted adapter, bot `open_positions_limiter` is not
operational. The adapter uses `LIMITER_DISABLED_OFF_ONLY`: it passes explicit
`L=0` and a `priorities` mapping of 1 for every strategy. `L>0`, limiter replay,
and `position_priority` do not participate in current candidate ranking or
admission. The lower-level limiter math and APIs are preserved for Phase 13.

The executable strategy JSON keeps `mrs.position_priority=1` for the existing
template contract. Each frozen internal candidate payload has
`account.open_positions_limiter=0`; this wrapper value contributes to candidate
identity and is not tester readback. Phase 7 covers only the off baseline;
its execution items remain open. Limiter implementation and all limiter
comparisons/release evidence are deferred to Phase 13. This amendment does not
grant blanket tester authorization; the Stage 2 route remains constrained to the
confirmed committed-campaign/provider boundary. Separately, the
user authorized a bounded local-only off-only tester baseline on 2026-09-21;
no run or result is complete, and execution remains gated by implementation,
focused tests, and review. Exchange actions, trading, and production database
writes are not authorized.

### Compact pair selector

The Panel shows Stage 1 readiness as one compact status strip. Technical schema,
policy and config-digest values are not operator controls and are not displayed.
The strip lists blockers only when Stage 1 is actually blocked; Stage 2 state is
not duplicated there.

The Stage 1 pair selector is one full-width table, not a card per pair. Each
row contains the selection checkbox and pair name followed by the editable
LONG and SHORT finalist limits. The available finalist summary stays inline
with the pair name. Direction headings appear once in the table header. The
table body may scroll vertically so a large current FINALIST universe does not
stretch the complete screen. Compact actions above the table select or clear
all pairs without rewriting their per-row LONG/SHORT limits; there are no
separate global directional-limit fields. A third action selects every pair
with available finalists and copies its reported LONG/SHORT availability into
the corresponding per-row limits. A pair is shown when either LONG or SHORT
has at least one finalist; only pairs with zero finalists on both sides are
omitted.

Every successful weighted Stage 1 publishes a private
`.portfolio-results/<campaign_id>/stage1-executables.json` beside `stage1.xlsx`.
The canonical, digest-bound artifact contains the server-ranked eligible
off-only candidates, their explicit symbol/side/strategy/result identities and
the exact strategy payload wrappers. Every executable candidate also contains
the exact common half-open UTC `pretest_period` frozen from the single
`PreparedWeightedInput` (`start_utc`, `end_utc` only). It is committed and
rolled back with the workbook. A later Stage 2 may load it after Panel restart
only when its campaign bindings, runtime path, digest and candidate payloads
still validate; it must configure the tester with `StartDate=start date` and
inclusive `EndDate=(exclusive end date - 1 day)`, without rereading finalists
or PerformanceDB and without rerunning preparation. Raw payloads remain absent
from the public Campaign summary, API response and XLSX.

## Phase 7 Stage 2 off-only local orchestration (current slice)

The backend Stage 2 orchestration is implemented and fake-verified for one
already-prepared off-only candidate. It remains local-only and does not grant
blanket tester authorization; the frontend submission control remains disabled
in this slice. Without the injected shared `LocalTestingService`, the route
stays fail-closed with `PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED`.

Submission prepares one private baseline from a committed Stage 1 executable
artifact, persists only compact bindings, and runs one asynchronous
`portfolio.stage2` job. The worker uses the in-memory prepared package and
committed Stage 1 bindings; it does not reread finalists/PerformanceDB or rerun
adapter/preparation. It stages exact payloads through the shared
`LocalTestingService.fill_prebuilt(..., delete_old_reports=false)`, starts the
tester, reads a fresh stable wizard result, and always calls the same service's
`stop()` after successful fill. Existing config/strategy restoration owns the
rollback; no real tester run has occurred in this verification slice.

The tester creates reports directly in the candidate folder through the exact
64-lowercase-hex `name_comment`; Panel does not copy, move, rename, or delete
report files/folders. Readback requires one portfolio result, the exact
prepared strategy-name set (at least two names), and every core results metric
as a finite number. The persisted/public projection includes only Campaign and
Stage 1 bindings, candidate identity, report folder/name comment, names, period,
report identity/link when available, and JSON-safe core metrics. A persisted
nonterminal Stage 2 job after restart is projected as `INTERRUPTED` and is never
resumed automatically.

Valid confirmed submissions return the persisted asynchronous job, while
malformed confirmation still follows the normal typed HTTP validation path.

Preparation selects the first candidate in the persisted eligible artifact
order, even when its saved `order` is greater than zero. The production
`candidate_id` must be exactly 64 lowercase hexadecimal characters and is used
verbatim as both the portfolio name and tester `name_comment`. The bank is
authoritative from the matching `campaign.launch.profiles[*].equity_usdt`;
every selected wrapper's finite positive `facts.B` must be Decimal-equal to
that value. The frozen half-open UTC `pretest_period` maps to tester
`StartDate=start` and inclusive `EndDate=end-1 day`. The canonical MRS3 tester
template preserves all fields except `name_comment`, `StartDate`, `EndDate`,
`InitialBalance`, `single_mode=false`, and `UpdateData=false`; `use_runs=false`
and an empty `parameter_mining` remain required. Only each wrapper's nested
`strategy` object is emitted as canonical deterministic UTF-8 JSON, keyed by
its exact unique name, with at least two strategies. Invalid bindings fail
closed with the single client-safe `PORTFOLIO_STAGE2_INPUT_INVALID` (HTTP 409).

P7-R4 adds the final pre-run controls. Stage 2 must revalidate the committed
artifact and exact first-candidate receipt immediately before staging and again
before result acceptance. `fill_prebuilt` must return byte/hash readback for the
effective tester config and exact strategy set; a mismatch prevents start. The
existing tester target lock remains held through start, readback, stop, exact
settings restoration and post-restore equality verification.

Before start the worker snapshots the shared wizard result and the direct
candidate report folder. A result is acceptable only when the wizard changed,
the referenced regular non-link report is new or changed, lies inside the
recorded tester time window and is stable. Every `CORE_METRICS` value is required
and finite, with drawdown nonnegative. Expected weights, sizing and
`max_balance` remain receipt-bound artifact facts, not invented tester metrics.
Failure, timeout, cancellation, mismatch or missing evidence never triggers an
automatic retry or parameter adjustment.

The first persisted eligible artifact candidate remains authoritative; Stage 2
does not rerank it. After fake-only implementation, tests and independent code
review, execution stops for fresh user confirmation before creating a new
Campaign or mutating tester state.

## Slice C startup migration amendment (2026-09-23)

The Panel constructs its shared job registry with restart recovery deferred.
Before recovery marks nonterminal jobs as interrupted, the Portfolio service
migrates legacy embedded Stage 1 Campaign inputs while holding the registry
lock. A journal-sized backup is written beside `.panel-jobs.json`; migration
checks free space for `2 * len(journal_bytes)` plus the sum of canonical
compressed snapshot sizes for every nonterminal or successful candidate and a
64 MiB reserve. Insufficient space causes no migration, backup, recovery, or
journal mutation. Migration writes and verifies external Campaign snapshots,
atomically saves the compact journal, reloads and validates every record, then
deletes the fixed adjacent backup only after that verification. A failed
individual migration leaves that job unchanged and remains retryable on the
next startup.

Legacy `COMMITTED` Campaigns retain a verified external descriptor. Legacy
`FAILED` and `CANCELLED` Campaigns retain only compact campaign ID/input/config
bindings and diagnostics; they have no rerun endpoint. Deferred or interrupted
Campaign snapshots are cleaned only after the terminal state is durable and no
nonterminal job shares the path. Successful snapshots are retained. A later
submit always creates a new Campaign and snapshot.

Operator sign-off dated 2026-09-23 permits permanent deletion of the full
embedded payload of an unrepairable legacy job after it is marked `FAILED` and
compacted. The retained `campaign_id`, `input_digest`, `config_digest`, and
compact diagnostics are sufficient to identify the non-executable payload;
no quarantine file is retained.
Terminal cleanup unlinks only the exact snapshot file after durable terminal
state; it never recursively removes the campaign directory. It then makes a
best-effort nonrecursive `rmdir` of that campaign directory, tolerating a
missing, nonempty, permission-denied, or sharing-conflicted directory.

The steady-state persistence budget is at most 360 journal writes per hour per
active job, plus bounded writes for substage transitions and completion. The
read-only projected total journal is 4,678,721 bytes because unrelated
tester/retest jobs occupy about 4.63 MiB; the portfolio contribution falls from
about 81 MiB to tens of KiB. This contract therefore makes no claim that the
total journal is below 2 MiB.

## Slice D progress telemetry amendment (2026-09-23)

Stage 1 variant generation may report a bounded event with `substage`, `unit`,
`completed`, an exact positive `total` or `null`, and a UTF-8 detail capped at
160 bytes. Profile enumeration has an exact denominator; adaptive solver work
and any work without an honest denominator remains indeterminate. Bootstrap
events are coordinator batch updates only and do not persist per-unit history.
Callback failures are advisory, rate-limited to four ordinary events per
second, and disabled after three exceptions without changing optimizer output.

The Panel keeps fixed-size live progress and a ten-second heartbeat in memory,
persisting only forced completion, eligible substage transitions no more often
than once per two seconds, ten-second heartbeats and terminal transitions. There
are no generic two-second unit writes. Elapsed time and ETA use monotonic anchors;
the API exposes elapsed, ETA and heartbeat age with the persisted fallback after
reload. The UI uses `performance.now()`, resets percentage at each substage,
shows a determinate accessible bar only for a reliable positive total, and marks
heartbeat age over 30 seconds as informationally stale. Backend ETA is `null`
unless the total is exact and positive, completed units are at least two, and
elapsed time is at least two seconds; a missing or inconsistent total, or a
completed count above total, is indeterminate. The UI suppresses numeric ETA for
stale heartbeats and terminal jobs and displays `stalled / ETA unknown`; the
determinate bar may remain visible as informational progress.

Progress locking has one direction: the progress-state lock may nest the
registry lock for a guarded persistence write; registry-locked paths never
acquire the progress-state lock.

## Slice D post-search failure diagnostics amendment (2026-09-23)

Once `weighted_search` returns, generic contract failures are persisted as
stable safe blocker codes so Campaign journal diagnostics remain actionable
after terminal snapshot cleanup. The adapter uses
`WEIGHTED_SEARCH_RESULT_INVALID` for malformed result status/mode/candidates/
warnings, `WEIGHTED_CANDIDATE_BANK_INVALID` for required-bank or bank-filter
contract failures, `WEIGHTED_CANDIDATE_SHAPE_INVALID` for candidate member or
profile/scenario shape failures, `WEIGHTED_PRETEST_PERIOD_INVALID` for the
post-search pretest period, `WEIGHTED_PAYLOAD_INVALID` for existing
payload generation failures, and `WEIGHTED_EXECUTABLE_IDENTITY_INVALID` for
executable identity/evidence failures. Pre-search configuration errors remain
`WEIGHTED_SEARCH_CONFIG_INVALID`; more specific existing snapshot and payload
codes are preserved. Blockers contain codes only: no raw values, exceptions,
paths, arrays, or full Campaign payload are retained.

This taxonomy is temporary diagnostic instrumentation. The next real run
identified executable-identity serialization as the failing post-search stage:
frozen `optimizer_source_metadata` is a nested `Mapping`, not necessarily a
built-in `dict`. Identity canonicalization must treat frozen and plain JSON
containers identically without stringifying or dropping their contents. The
instrumentation remains until one successful real Campaign is completed.

The post-search split also distinguishes
`WEIGHTED_CANDIDATE_SLOT_DUPLICATE` (duplicate normalized symbol+side slot) and
`WEIGHTED_CANDIDATE_LIMITER_INVALID` (malformed limiter); other candidate
structural failures remain `WEIGHTED_CANDIDATE_SHAPE_INVALID`. The single-search
path keeps `WEIGHTED_SEARCH_CONFIG_INVALID` only at pre-search validation; all
reachable validation after the weighted-search return is categorized by the
result, bank, candidate, payload, or executable-identity code above. The
single outer post-search contract-error guard remaps only a residual generic
`WEIGHTED_SEARCH_CONFIG_INVALID` to `WEIGHTED_POST_SEARCH_CONFIG_INVALID`;
specific downstream codes bubble unchanged. The
existing weighted-search wall-time bound remains 900 seconds; this diagnostic
change adds no timeout behavior.

A weighted-search result with status `budget_limited` is a valid incomplete
search result, not a malformed result. When it contains candidates, the adapter
applies the same bank/shape/payload validation as for `PASS`, publishes only
those validated candidates and adds the safe warning
`PROFILE:<profile>:WEIGHTED_SEARCH_BUDGET_LIMITED:<reason>`. When it contains no
candidates, the same code is a blocker. The reason must be a bounded uppercase
code; malformed status/reason/candidates remain `WEIGHTED_SEARCH_RESULT_INVALID`.
Only `WALL_TIME_LIMIT`, `SOLVER_CALL_LIMIT`, `SOLVER_TIME_LIMIT`,
`NEW_X_LIMIT`, and `BOOTSTRAP_INCOMPLETE` may publish validated partial
candidates; every other safe reason uses the budget-limited code as a blocker.
`BOOTSTRAP_INCOMPLETE` can have candidates only when an earlier complete base
bootstrap already produced them and a later optional bootstrap stopped; an
incomplete base bootstrap produces no candidate. If a target-bank ceiling
removes every valid partial candidate, `BANK_UNAVAILABLE` remains the blocker
and the budget-limited code remains visible as a warning.

## Stage 1 settings completeness amendment (2026-09-24)

The Settings form exposes the remaining operator-relevant Stage 1 inputs in a
separate, default-closed `Подготовка истории и воспроизводимость` block:

- `search.seed`, a non-negative integer controlling deterministic bootstrap
  sampling;
- `search.composition.parameters.minimum_common_days`, a positive integer
  controlling the minimum common history window admitted to weighted search.

Save deep-clones the READY document and patches only these leaves. Invalid
values are rejected in the browser and revealed inside the collapsed block.
The Settings GET returns the migrated canonical v2 document, so both leaves
are present before the form is enabled. The backend accepts unbounded Python
integers for both values; the browser intentionally limits edits to exact
JavaScript safe integers to prevent JSON precision loss. There is no additional
upper or cross-field bound for either value.
The diagnostic-only composition coverage/gap fields and the fixed weekend UTC
boundaries remain config-only; they are not presented as effective Stage 1
controls.

## Planned combination preflight amendment (2026-09-24)

The Campaign form must calculate the exact requested Cartesian-product size
live from the selected pair/direction rows. Each non-zero pair/direction limit
contributes `min(requested finalists, available finalists)` options; the shown
combination count is their product. Selecting all available finalists updates
this value immediately. The operator never calculates it manually.

The form shows `Комбинаций: N · технический лимит: M`. If `N > M`, Calculate
is disabled before Campaign creation and the status explains that the finalist
limits must be reduced or the technical guard deliberately raised. The server
repeats the same preflight before creating a job and returns the safe actual
count and limit on rejection. The guard remains a technical protection against
unbounded memory and runtime; it is not a target number of portfolios and must
not fail only after finalist series have been loaded.

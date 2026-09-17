# Portfolio Optimizer — контракт weighted search

Дата: 2026-09-14. Версия: WS1.1. Статус: PLAN_APPROVED от Opus,
заключение передано пользователем 2026-09-14. B1–B3 закрыты.
Алгоритмический план R4 и этот контракт приняты. Необязательная оговорка
CONFIRMED/UNKNOWN добавлена в §9 плана; runtime не изменён.

План реализации и чек-лист: [weighted search R4.2](../superpowers/plans/2026-09-12-portfolio-optimizer-weighted-search-discussion.md).
Архитектурное решение: [ADR-0036](../decisions/0036-portfolio-optimizer-weighted-search-contract.md).
Связанные контракты: [основной Optimizer](2026-09-05-portfolio-optimizer.md),
[Panel](2026-09-06-portfolio-optimizer-panel-ui.md),
[peak-DD](../decisions/0035-portfolio-optimizer-peak-equity-drawdown.md),
[исходный sizing](../superpowers/plans/2026-09-13-portfolio-optimizer-finalist-sizing-audit.md).

## 1. Цель, область применения и приоритет

Получить небольшой список портфелей: состав, полные номиналы позиций x,
доли от банка, требуемый капитал, limiter, приоритеты и basic.max_balance.
Предварительная модель сокращает число дорогих joint tick-tests, а не заменяет их.
Нет условия sum(x)=B и фиксированного ограничения числа пар.

Этот контракт применяется только к новому явному search_mode=WEIGHTED_V1.
После принятия он имеет приоритет над противоречащими правилами прежнего
PRETEST_PROXY: равными долями, uniform k, обязательным перебором одиночек/пар,
исторической базой «стартовый банк × sum(lot_x)» и повторным daily→minute sizing.

Campaign читается и исполняется только при
`campaign_contract_version=PORTFOLIO_WEIGHTED_CAMPAIGN_V1`,
`search_mode=WEIGHTED_V1` и `weighted_algo_version=WS1.1`. Отсутствующее,
неподдерживаемое или legacy-значение одного из этих полей отклоняется fail-closed;
legacy Campaign не читаются. Сравнения точные, чувствительные к регистру и без
нормализации пробелов. В `versions` должны быть как минимум те же три поля с
точно равными top-level значениями; дополнительные provenance-поля разрешены.
Наличие ключа `stage1_mode` top-level либо в `versions` запрещено независимо от
его значения.

До реализации `weighted_search.py` корректный новый Campaign сохраняется, но при
исполнении завершится единственным блокером `WEIGHTED_SEARCH_NOT_IMPLEMENTED`;
fallback на PRETEST_PROXY запрещён.

Стабильные validation-коды: `CAMPAIGN_LEGACY_STAGE1_MODE_UNSUPPORTED`,
`CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED`, `CAMPAIGN_SEARCH_MODE_REQUIRED`,
`CAMPAIGN_LEGACY_SEARCH_MODE_UNSUPPORTED`, `CAMPAIGN_SEARCH_MODE_UNSUPPORTED`,
`CAMPAIGN_WEIGHTED_ALGO_VERSION_REQUIRED`,
`CAMPAIGN_WEIGHTED_ALGO_VERSION_UNSUPPORTED`, `CAMPAIGN_VERSIONS_MISMATCH`.
`WEIGHTED_SEARCH_NOT_IMPLEMENTED` — adapter-only код, не код валидации.

Проверка short-circuit выполняется в порядке: наличие `stage1_mode`, contract
version, search mode, algorithm version, parity `versions`; возвращается только
первая причина. Не-строка или пустая строка `search_mode`/`weighted_algo_version`
соответствует `*_REQUIRED`; точный `PRETEST_PROXY` — legacy search-mode code;
другая непустая строка — соответствующий `*_UNSUPPORTED`. Не-строка или пустая
строка contract version также unsupported. При freeze/resume ошибка отображается
как тот же typed code без записи Campaign; adapter возвращает единственный
blocker с тем же кодом до загрузчиков, search и workers.
Campaign должен быть mapping: не-mapping отклоняется
`CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED` до field-order, поскольку у него нет
top-level или `versions` ключей.

MVP: один LONG FINALIST на symbol, ручной выбор имеет приоритет. Если выбора нет,
минимальный заданный User Rank, затем strategy_id; отсутствующий rank — после
заданных. Выбор фиксируется в Campaign, User Status/Rank/comments не изменяются.
Индивидуальные DD и знак PnL не являются hard filters. Ноль x исключает участника.

Non-goals: live trading, вывод капитала, новый runner, изменение MA/shifts/lot_x,
SHORT/BOTH, подбор нескольких стратегий на сторону, автоматическое управление
ликвидностью, сетка 50/75/100% C и гарантии риска будущего периода.

## 2. Входы и единицы

Все timestamps — UTC; денежные величины — USDT. Проценты UI делятся на 100
до вычислений. Неизвестные, NaN/Inf, Boolean вместо числа не становятся нулём.

| Вход | Содержимое / проверка |
| --- | --- |
| Campaign | идентификатор, WEIGHTED_V1, версии аналитики и margin policy, seed, снимок настроек |
| Источники | упорядоченные symbol/strategy_id/result_id/imported_at_utc/report hash/settings hash; LONG, текущий FINALIST |
| Ряды | факты equity/actions/cycles, эффективные периоды и известная база S исходного sizing |
| Reference | цены, minQty/qtyStep/tickSize/minNotional/maximum quantities, актуальный максимум leverage и risk tiers, комиссии/качество |
| CSV | проверенные завершённые сутки существующего minute_capacity/backfill, оборот USDT |
| Профиль | MaxDD m, резерв r, MM u, необязательный минимум P30, M=Max candidates |
| Ресурсы | единственное существующее duckdb_import.workers, параметры времени/LP/сценариев |

Для AGGRESSIVE/BALANCED/CONSERVATIVE соответственно:
m=0.20/0.10/0.05, r=0.20/0.40/0.60, u=0.50/0.35/0.20.

### Phase 5 benchmark prerequisite

The Phase 5 benchmark may consume only explicit imported User Status/User Rank
decisions. Automatic filtering and ranking populate Auto Status/Auto Rank only;
they never create or fill User Status/User Rank. An ordinary unreviewed run is
therefore absent from the effective user finalist universe until its edited
workbook is imported. This prerequisite is evidence for the benchmark and does
not mark Phase 5 complete.
Все профили максимизируют net PnL/30 при своих ограничениях. Допустимо
0<m,u<1 и 0<=r<1. B_available отсутствует либо конечный >0.

## 3. Подготовка данных и граница известной базы

Источник фиксируется один раз. Общий период — пересечение эффективных периодов
выбранного universe, обрезанное до полных UTC-суток существующим period helper.
Сохраняется существующий минимум 14 суток. Нулевые x не расширяют окно.
Шаг history_step_minutes — положительное целое, default 5.
В узле сетки берётся последнее известное состояние не позже узла; затем
разность соседних узлов. Equity внутри интервала не усредняется.
Внутрисеточные экстремумы могут теряться; это ограничение отчёта, а не гарантия.

Минимальные подготовленные данные на участника:
strategy/result revision; UTC boundaries; normalized_delta[T]; valid[T];
timestamps_utc содержит T+1 UTC-границ интервалов; normalized_delta, valid и reasons — T×N по участникам.
cycles с интервалами [first fill, final flat), базой S и атрибутируемыми
приращениями equity; индивидуальные hold/count/occupancy и source-scale diagnostics.
Матрица normalized_delta имеет форму T×N, порядок колонок — stable strategy_id.

Известный flat-интервал без событий даёт delta=0, valid=true. Отсутствие нового
события не означает потерю данных: состояние переносится вперёд. История до
листинга, потерянный участок или неатрибутируемое изменение equity — valid=false,
а не нулевой доход. Неизвестность общей матрицы блокирует вычисление риска.
Reason содержит symbol/strategy_id/result_id и UTC-границы невалидного интервала;
universe и окно автоматически не изменяются.
Приращения разных циклов нормализуются отдельно до суммирования в интервал.
Partial fills сохраняют одну плановую базу полного цикла, а не текущий объём fill.
Перекрывающиеся циклы одного LONG-участника требуют доказанной атрибуции;
при её отсутствии normalized return — UNKNOWN.

normalized_delta_i,t = delta_equity_i,t / S_i,k. Net equity включает расходы
и UPnL; realized-only не заменяет её. Price/Cost действий — actual fills,
не плановый полный S. Максимальная quantity также не является S в USDT.

Для WS1.1 источник S подтверждён фазой 1 буквально: use_fix=false,
use_upnl=true, use_frozen_balance=true, balance_percentage_long=100,
risk_long=1 и max_balance=0. Для каждого не carry-in цикла S берётся из
первого opening-from-flat как balance - pnl + fee; одна база сохраняется для
всего цикла, включая partial fills. Carry-in или неизвестная база дают
UNKNOWN для зависимого normalized return. Price/Cost, maximum quantity,
initial balance и fixed-basis calibration не являются заменой S.

Без округления контроль нормализации: delta=10,S=100 и delta=20,S=200 дают
normalized increments 0.1/0.1; при x=50 вклад равен 5/5 USDT.
При обратном умножении на исходные S восстановленные increments совпадают
с фактами с численным допуском; это проверка арифметики, не точности исполнения.
В fixture используется atol=1e-8, rtol=1e-8. Для денежных округлений JSON
допуск равен сумме qtyStep×reference price уровней, не этим float-допускам.

Кэш MVP — в памяти одного Campaign, без нового persistent store/lock framework.
Ключ: версии аналитики, упорядоченные source revisions/hashes, общее окно,
history step и digest настроек подготовки. Матрица источника независима от B/L.

Phase 8 разрешает Performance v2 сохранять только per-result подготовленную
основу, описанную в
[Performance v2 optimizer prepared inputs](2026-09-17-performance-v2-optimizer-prepared-inputs.md).
Финальная общая T×N сетка остаётся Campaign-кэшем с тем же ключом: persistent
артефакт не зависит от будущих B/L, не фиксирует состав портфеля и не заменяет
проверку source revision при чтении.
Маржинальные факты дополнительно ключуются reference digest и margin policy.
Другой imported_at или hash при том же result_id даёт другой ключ; старые
optional metadata при REPLACE не наследуются. Запись/сбор результатов — в parent.

## 4. Ликвидность и номиналы

C_i = floor_to_50_USDT(participation_pct/100 × mean_minute_turnover).
Default participation=30%, диапазон 1–200%. Default окно: 7 завершённых суток
перед frozen Campaign time, включая нулевые минуты без сделок.
mean = сумма оборота / (1440 × число проверенных полных суток).
При накоплении 1–6 проверенных суток допустимы с PRELIMINARY и их датами;
0 суток — UNKNOWN. Не загруженный день не считается нулевым; backfill проверяет
полноту до вычисления. Пять рабочих дней — аналитика, отдельного weekend-теста нет.

0<=x_i<=C_i. x — максимальная полная набранная позиция, включая все lot_x.
Для lot_x=[0.5,1,1.5] и x=600 уровни равны [100,200,300] до qtyStep.
Leverage влияет на маржу, а не увеличивает x. Проверяются все уровни и closing qty.
Ни один размер не округляется вверх до minQty скрытно. Неисполняемый участник
удаляется только из данного решения; не более 3 repair LP внутри общего бюджета.
После удаления/округления пересчитываются путь/DD/маржа/доход, без старого кэша x.
Spread screen даёт диагностическое предупреждение; геометрически доказанное
неисполнение обрабатывается существующим gate. Новый orderbook gate не добавляется.

## 5. Риск и капитал

g_0=0; g_t=cumsum(normalized_delta @ x); E_t=B+g_t;
h_t=max(0,g_1,...,g_t). Running equity peak равен B+h_t и не сбрасывается
локальной вершиной ниже прежнего HH. MaxDD=max((h_t-g_t)/(B+h_t)).
bank_for_path=max(0,max_t(((1-m)*h_t-g_t)/m)).
Положительный B>=bank_for_path эквивалентен peak-MaxDD<=m для данного пути.
Пример g=[0,1000,400], m=0.20: B=2000, peak=3000, DD=600/3000.

P30_common=sum(g increments)×30/common_days. Индивидуальный весь период
не подменяет общий период в цели портфеля. CDaR_peak80 — средняя глубина худших
20% временных наблюдений DD; CDaR90 — диагностика. Денежный CDaR в LP вторичен,
отдельного CDaR hard gate нет. Отрицательные вклады не отбрасываются.

Stationary bootstrap использует общие индексы всех колонок, горизонт T:
NumPy Generator(PCG64), seed фиксирован. Для mean block D дней вероятность
restart равна history_step_minutes/(1440*D); требуется 0<p<=1.
Первый индекс uniform[0,T); далее с вероятностью p новый uniform индекс,
иначе previous+1 modulo T. Seed sequence задаётся seed, block ordinal,
scenario index; номер worker не участвует. Версия NumPy сохраняется в manifest.
Индексы не зависят от x: один набор на batch применяется ко всем проверяемым
кандидатам Campaign. Не генерировать RNG заново для каждого x; хранить сразу
все сценарии не требуется. Общие индексы сохраняют совместные движения стратегий.
Default D=[1,3,7], 1000 сценариев на D, screening=первые 100 только если
нужно сократить исходные x перед полным bootstrap.
p95 — nearest rank ceil(0.95*count), не интерполяция.
B_risk=max(historical bank_for_path, три p95 scenario banks).
Перестановки короткой истории не являются новыми рыночными режимами.

## 6. Маржа и limiter

USDT-only отдельный Cross account, без посторонних позиций. Reference заморожен.
До LP фиксируются коэффициенты a_i/b_i — USDT IM/MM на один USDT выбранного
полного номинала x_i. Они ограничивают сверху ставки на всём диапазоне [0,C_i]
для reference и возможных состояний исходных уровней. В расчётах и LP строго:
I_i(x_i)=a_i*x_i; M_i(x_i)=b_i*x_i; I_i(0)=M_i(0)=0.
C_i определяет область выбора коэффициентов, а НЕ постоянную сумму маржи при C_i.
В простом случае одинаковой номинальной базы без order loss:
a_i=imr_i_max+open_fee_rate_i+close_fee_rate_i,
b_i=mmr_i_max+close_fee_rate_i. Для уровней с разными reference prices и order loss
коэффициенты должны также покрывать эти линейные вклады без двойного счёта;
не переносить пример ставок на неподтверждённую номинальную базу.
Envelope строится по существующему margin.py и проверяется на маленьком независимом
переборе состояний. Неизвестный bound блокирует sizing. MM deduction можно
опустить для верхней границы; нельзя добавлять постоянную I_i(C_i) к a_i*x_i.
Не перечислять подмножества стратегий для каждой оценки L.

L=0 означает off; ell=N при off, иначе min(L,N). Все priority в MVP — 1..5.
При достижении L снимаются opening orders участников без позиции.
При превышении бот закрывает 5→4→3→2→1, при равенстве случайно; очереди входов нет.
Это не очередь выбора лучших стратегий. Закрытия excess — market, обычная
торговля — limit/post-only. Подтверждение фактической механики — evidence фазы 4:
живые уровни, снятие opening orders, закрытие excess и освобождение их IM.
limiter_release_status=CONFIRMED только со ссылкой на это evidence в reference;
до подтверждения статус UNKNOWN и I_held:=I_all для каждого L. Это консервативная
граница того же WEIGHTED_V1, не переключение режима. Loss_extra сохраняется.
Кандидат явно помечается как не использующий освобождение маржи после реакции.
Это evidence-факт, не пользовательская настройка и не следствие успеха LP.

k_extra=N-ell. Loss_extra=0.015 × худшая сумма k_extra закрываемых полных x.
Для fixed x: обойти priority 5→1; взять всю группу, пока её размер <= остатка k;
в первой пограничной группе взять остаток крупнейших x. Equal x разрешается
stable strategy_id для воспроизводимости суммы. Это стресс, не симуляция
случайного выбора бота. Для off потери равны нулю.
I_all=sum(I_i(x_i)); M_all=sum(M_i(x_i)). При CONFIRMED
I_held=top_ell(I_i(x_i)) по ВСЕМ участникам; иначе I_held=I_all.
A=(1-m)B-Loss_extra>0.

Три обязательных условия: I_all<=A; M_all<=u*A; I_held<=(1-r)*A.
B_margin=[Loss_extra+max(I_all,M_all/u,I_held/(1-r))]/(1-m).
B_required=ceil(max(B_risk,B_margin,1)) USDT; после ceil проверки повторяются.
Полная IM/MM проверяется до реакции; свободный резерв r — после отмены/закрытия.
До реакции резерв может быть ниже r даже длительно при стоящих заявках.
Это явная политика модели, не гарантия биржи; оба резерва выводятся отдельно.
Loss_extra учитывается один раз, не повторяется через order_loss/fee.
Ни DD, ни полный M_all не получают скидку L/N. При r=0 или активном полном
IM/MM floor дальнейшее уменьшение L может не улучшать капитал.

Приоритеты: T_eff=mean_hold+beta*max(0,Hold90-mean_hold), beta=0.5;
SlotScore=mean net full-cycle PnL при выбранном x / T_eff, USDT/hour.
Неизвестный или неположительный T_eff — UNKNOWN score. Положительные score
сортируются DESC, tie strategy_id. Группа начинается при score<group_max/2,
предел 5 групп; достигнув предела, остаток входит в последнюю. Неизвестные/
неположительные score также в последней, при наличии свободной группы — новой.
Priority 1 — первая, 5 — последняя, 0 не генерируется.

Replay L — один deterministic counterfactual проход по известным циклам:
отклонённый цикл не занимает слот. Сначала releases в timestamp, затем starts
по strategy_id/source cycle ordinal; carry-in>L или неатрибутируемый вклад —
UNKNOWN limiter PnL. Принимается цикл при свободном слоте, без ожидания/очереди.
Результат MODEL; это не воспроизведение случайного bot close.
Boundary UPnL атрибутируется; off должен совпадать с P30_common по допуску §3.

## 7. Поиск, отсечение и бюджет

LP переменные B>=1, x∈[0,C], running peaks h с h_0=0:
h_t>=h_(t-1), h_t>=g_t, g_t>=(1-m)h_t-mB.
Исходный discovery LP имеет I_all<=(1-m)B, M_all<=u*(1-m)B,
БЕЗ off-резерва I_all/(1-r). Это relaxation, не acceptance.
При заданном B_available верхняя цель — max P30_common, B<=B_available.
Без bank cap верхняя цель sum(C_i*max(p_i30,0)), где p_i30 — общий
нормализованный доход одного USDT за 30 суток. Нет положительной цели —
явный пустой результат, а не портфель с B=0.
Для каждой положительной цели min B при P30>=target. До K=8 целей:
верхняя, верхняя/K, 0.5*верхняя (именно половина верхней цели);
дубликаты удаляются с сохранением первого появления. Далее уточнение по §7.1 плана.
Интервал требует уточнения, если L1-разность x/sum(x) его концов >1e-8
или различается набор активных C (abs(x-C)<=max(1e-8,1e-8*C)). Выбирать
максимальный (target_hi-target_lo)/upper_target, tie меньший target_lo;
новая цель — midpoint. Если таких интервалов нет, остановить уточнение.
Для K=1 только верхняя, K=2 верхняя и нижняя. Версия solver и residual tolerance
фиксируются; равнозначные LP optima не объявляются уникальным математическим ответом.
Infeasible пропускает только эту цель; time-limit incumbent допускается
только после независимой проверки residuals, со статусом budget_limited.
Любой реально начатый solver call, timeout/repair/alternative также расходует
один из 20 вызовов/профиль; повтор не бесплатен. Глобальный оптимум не обещается.

После округления проверить все L=1..N-1/off до отбраковки x по bank cap.
Для same x история/bootstrap риска вычисляются один раз. При fixed bank
MODEL задаёт порядок L; UNKNOWN вместо числа использует off, затем L DESC.
Если смешаны статусы, MODEL ранжируются между собой; UNKNOWN остаются
контрольными альтернативами в той же квоте, без сравнения с числом 0.
Без bank cap сохранить отдельно минимум B_required и лучший MODEL PnL,
если это разные L. Рейтинг по разным банкам не объявляется единым победителем.

Один proportional step для выбранного L:
lambda=min(B_target/max(B_risk,B_margin),min_i(C_i/x_i)), только x_i>0.
Если все L не проходят B_available — выбрать L минимального B_required и
попробовать lambda<1. Перепроверить qty/tier/полный bootstrap и сохранить
как отдельный кандидат. Нулевой denominator при положительном x — ошибка bound.

Одно дополнительное поколение LP при фиксированных B/L/составе/priorities/
масках: max P30_limiter_model, либо P30_common с явным UNKNOWN эффекта L.
Используются все три margin inequalities с верхней LE переменной; точный
top_k эпиграф: z свободен, v_j-z<=w_j, w_j>=0, k*z+sum(w_j)<=bound.
При CONFIRMED I_held uses all participants через top-ell; при UNKNOWN
используется линейный I_all без скидки. LE uses boundary closing group плюс полные группы.
После LP и округления заново состав/priorities/LE/replay/P30/DD/C/маржа/bootstrap.
Не прошедшая ограничения/заданную цель альтернатива отбрасывается; исходный
кандидат сохраняется. Второй LP для стабилизации не запускается.

Квоты: 20 solver calls/profile, до 2M базовых x, резерв до M новых x,
всего 3M полностью bootstrap-проверенных x; до 3 L на x, итог <=M joint.
M default 20, range 1–50, НЕ количество пар. Пропорциональный шаг и LP
делят один резерв, CDaR <=2/profile, repair<=3, всё внутри 20 calls.
Отбор семейств/контрольных L — точные пять шагов §10 плана.
До 60 joint при трёх default профилях, без второго скрытого общего лимита.

## 8. Результат, UNKNOWN и существующая интеграция

Существующий путь: panel_portfolio → adapter.run_portfolio_adapter →
candidate_search → новый weighted_search.py → position_sizing/margin/metrics.
Не переписывать старые search.py/integration.py и не вводить второй runner.
Данные источников хранятся один раз, raw series не копируются в Members/API.

| Отсутствующее обязательное данное | Последствие |
| --- | --- |
| S/нормализованная общая equity | риск и поиск зависимого universe не выполняются; явный reason |
| C/IM/MM/reference | конкретный input непригоден; universe нельзя сокращать скрытно, показать исключение до фиксации |
| Cycle attribution для limiter | P30_limiter=UNKNOWN; x проходит DD/маржу и может занять контрольное joint-место |
| Hold/SlotScore | UNKNOWN score в последней priority группе, warning; не нулевой доход |
| Подтверждение снятия/освобождения IM | limiter_release_status=UNKNOWN, I_held:=I_all; явная отметка отсутствия выгоды от освобождения маржи, Loss_extra сохраняется |
| Подтверждение export sizing | candidate не получает export PASS, исходный x не меняется |

Calculated/model status не подменяет существующий gate enum PASS/FAIL/UNKNOWN
или disposition RESEARCH_ONLY. Ни MODEL, ни успешный LP не означает
RECOMMENDATION_READY. Аддитивно расширяются существующие
mrs3.portfolio.candidate_search.PortfolioCandidate и
mrs3.portfolio.candidate_search.SearchResult. Новый public result format не создаётся;
mrs3.portfolio.search.SearchResult относится к legacy и здесь не используется.

Исполнимый payload: B/x/C, effective quantities/prices, q_i=x_i/B,
basic.max_balance_i=C_i/q_i, position_priority, open_positions_limiter.
Фактический путь этих двух последних полей и преобразование q в
balance_percentage/risk берутся из шаблона и подтверждаются обратным чтением,
а не выдумываются. use_upnl/use_frozen_balance сохраняются исходными.
Если start sizing base A0 != B, требуется доказанное обратное преобразование;
неизвестная семантика блокирует export. q не является lot каждого уровня.
B_sat_settings=max(C_i/q_i) для положительных x — аналитическая база насыщения,
не гарантированно доступный к выводу капитал. B_sat_frontier показывается отдельно.

Identity через существующий canonical.py: mode/algorithm/margin versions,
source revisions/hashes, common window/grid, reference/capacity/settings digests,
limiter_release_status и digest подтверждающего evidence/эффективной margin policy,
B, ordered member symbol/side/id, реальные rounded orders, q/max_balance,
L/priorities. workers отсутствует в identity; seed и параметры модели включены.
Числа сериализуются существующим Decimal/canonical contract, не сырым float JSON.
Изменённый executable payload меняет identity. Для golden fixture экспорт
и повторное чтение должны дать один digest; legacy fixture неизменен.

Настройки используют существующий Optimizer JSON; UI — подписанные поля
с единицами, редкие в раскрываемом блоке. Полный набор/ranges — §12 плана.
Summary показывает required/available/saturation B, P30/DD/CDaR, два резерва,
MM, ограничивающий фактор, MODEL/UNKNOWN/NOT_TESTED и budget_limited.
Members — finalist/side/x/C/q/priority/max_balance/source-scale/hold/warnings.

## 9. Ресурсы и API

Один вычислительный пул, ширина только duckdb_import.workers из панели;
worker pool/BLAS/HiGHS не перемножаются. Независимые LP/батчи параллельны,
адаптивные зависимости последовательны. Не создавать отдельные worker knobs.
36 cores/200GB — ресурс машины, не прогноз 36× ускорения и не разрешение
36 tester процессов. Возможности solver/память проверяются замером.
Экспериментальные K=8, wall=15 min, solver call=30 sec до первого замера;
partial result сохраняет manifest готовых/неисследованных задач.
Manifest: source/settings/reference/model versions, seed, counters, solver
statuses/residuals, stage wall/CPU/peak RSS, worker width, reasons/stopping point.

Reference загружается один раз на Campaign, общий throttling между jobs:
initial 2 requests/sec, concurrency=1, retry<=3 (каждый расходует бюджет),
age<=2h. Архив CSV — host concurrency=2, атомарные проверенные сутки.
429/10006/reset headers обрабатываются общим loader; 403 access-too-frequent
останавливает обращения минимум 10 min с persisted cooldown, в том числе
после restart. API-лимиты проверяются на дату реализации; бан не обходится.

## 10. Acceptance и границы выполнения

Проверяемые задачи — единственный чек-лист §13 плана. Формулы WS1 остаются
нормативными; plan approval не равно acceptance реализации.
Каждая фаза оставляет tests, scoped diff и независимый CODE_REVIEW_PASS.
Evidence — `docs/superpowers/plans/2026-09-14-portfolio-optimizer-weighted-search-phase-N-evidence.md`,
N соответствует фазе; содержит requirements, scoped paths, точные команды/
exit codes, reviewer disposition, нерешённые blockers; реальные local paths
и generated reports в repo не сохраняются.

Ключевые regressions: обратная нормализация/UPnL границ; 600 full position
split 100/200/300; peak DD bank=2000; arbitrary top-ell vs N<=6 enumeration;
I_i=a_i*x_i/M_i=b_i*x_i, zero/homogeneity и пример I_all=200/B_margin=371;
CONFIRMED: off2778/L10 1936/L9 1767/L8 1784, off-fail/L-pass сохранён;
UNKNOWN release: I_held=I_all, L10=2862 при сохранённых Loss_extra=75;
re-priority после LP без рекурсии; full bootstrap после изменения x;
budgets M/3M/20; no-lookahead; REPLACE сохраняет User Status/Rank/comments
и меняет revision; exports round-trip; workers full-result invariance;
fake clock API/cooldown. Карта test files — §13 плана.

Команды только `.venv\Scripts\python.exe -m pytest <файлы фазы> -q`, затем
relevant broader checks и `git diff --check`. Флажки закрывает root по evidence,
не исполнитель по собственному заявлению. Реальные calibration/joint tester
запуски, production REPLACE и API требуют отдельного назначения в соответствующей
фазе; fixture tests разрешены scope реализации. Этот пакет авторизует только docs.

# ADR-0036: Взвешенный поиск позиций и капитала

Дата: 2026-09-14. Статус: Accepted — PLAN_APPROVED от Opus вместе с WS1.1,
заключение передано пользователем 2026-09-14.

## Контекст

Равные доли и перебор составов прежнего PRETEST_PROXY недостаточны для
подбора индивидуальных размеров с ликвидностью, общей просадкой и limiter.
Пользователь согласовал план R4, Opus подтвердил его PLAN_APPROVED.
Спецификация WS1.1 и это решение получили отдельный PLAN_APPROVED; код не внедрён.

## Решение

Ввести единственный Campaign contract WEIGHTED_V1 через существующий adapter/candidate_search;
один weighted_search.py с SciPy/HiGHS вместо собственного solver.
Подбирать полные номиналы x∈[0,C] и B без sum(x)=B. Общий normalized equity
путь задаёт peak-DD согласно ADR-0035; CDaR — вторичная цель, shared stationary
bootstrap — чувствительность к доступной короткой истории.
Вся IM и полный профильный MM обеспечиваются до реакции limiter, профильный
свободный резерв — после отмены/закрытия. Excess loss=1.5% номиналов all-in,
без двойного счёта; full MM/DD не получают скидку L/N.
IM/MM — фиксированные верхние коэффициенты, умноженные на выбранный x,
а не постоянные суммы при C. До подтверждения освобождения IM в evidence
I_held=I_all с UNKNOWN release; только CONFIRMED разрешает top-ell и выгоду
от снятия заявок. Loss_extra сохраняется в обоих случаях.
Подмножества top-ell IM берутся по всем участникам, а закрываемые excess —
по priority. После перераспределения x priority и зависимые gates проверяются
заново без рекурсивной стабилизации.

Нормативный контракт: [WS1.1](../specs/2026-09-14-portfolio-optimizer-weighted-search.md).
Реализация/evidence: [план R4.2](../superpowers/plans/2026-09-12-portfolio-optimizer-weighted-search-discussion.md).

По явному указанию пользователя это решение supersedes ранее принятый
compatibility-пункт: legacy Campaign не читаются и не мигрируются. Campaign обязан
нести точные `campaign_contract_version=PORTFOLIO_WEIGHTED_CAMPAIGN_V1`,
`search_mode=WEIGHTED_V1` и `weighted_algo_version=WS1.1`, продублированные в
`versions`; `stage1_mode` запрещён. Отсутствие, legacy или неподдерживаемое
значение fail-closed. Восемь стабильных validation-кодов:
`CAMPAIGN_LEGACY_STAGE1_MODE_UNSUPPORTED`,
`CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED`, `CAMPAIGN_SEARCH_MODE_REQUIRED`,
`CAMPAIGN_LEGACY_SEARCH_MODE_UNSUPPORTED`, `CAMPAIGN_SEARCH_MODE_UNSUPPORTED`,
`CAMPAIGN_WEIGHTED_ALGO_VERSION_REQUIRED`,
`CAMPAIGN_WEIGHTED_ALGO_VERSION_UNSUPPORTED`, `CAMPAIGN_VERSIONS_MISMATCH`.
До следующей фазы adapter корректного Campaign возвращает только
`WEIGHTED_SEARCH_NOT_IMPLEMENTED`; это не validation-код и не разрешает fallback.
Campaign является mapping; не-mapping отклоняется
`CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED` до проверки `stage1_mode`, так как ключей
Campaign у него нет.

## Последствия

Старые Campaign/режимы не исполняются. WEIGHTED_V1 заменяет uniform k,
обязательные одиночки/пары, неподтверждённую базу source size и повторный
minute refinement. Fallback между режимами запрещён.
Квоты 20 LP/профиль, 3M x, M joint и единый duckdb_import.workers сохраняют
ограниченную стоимость. MVP LONG-only; phases SHORT/live/multi-size отложены.
Модельный минимум банка и насыщение не гарантируют будущий риск или вывод.
Неизвестный source sizing/маржинальный bound/export semantics блокирует
зависимый результат; missing limiter replay оставляет MODEL PnL неизвестным.
Приёмка реализации требует тестов и CODE_REVIEW_PASS. Старые ADR не переписываются.

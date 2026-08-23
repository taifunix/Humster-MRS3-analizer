# План: BASE 1ORD выбирается по одной штуке на TF вместо трёх

**Дата:** 2026-08-24
**Статус:** на утверждение
**Контракт:** [canonical phase 1](../../specs/2026-08-16-mrs3-v07-canonical-phase1.md), §15 и §17
**Смежный контракт:** [analysis shortlist / READY JSON](../../specs/2026-08-18-analysis-shortlist-ready-json-contract.md)
**Найдено:** при разборе колонки 1ORD на вкладке «Стратегии и DD5»

## Что говорит контракт

§17, дословно:

> For each exact scope, one frozen local BASE per Plateau becomes a BASE candidate.
> **Rank at most three** by:
> `PnL@DD5 theoretical DESC, raw PnL DESC, Trades DESC, DD ASC, PointID ASC`

и там же:

> A scope with BASE 1ORD but no multiorder candidate must remain selectable.
> Generation succeeds if either BASE output or selected READY multiorder output
> exists. It errors only when both are absent.

§15 задаёт пул: по одному замороженному BASE на **каждое READY-плато**, из
continuity-usable представителей, у которых исходная точка
`standalone_eligible == True`.

То есть: пул — это плато, а не scope; из пула берётся **до трёх** на точный
`Pair + Side + TF`.

## Что происходит на самом деле

Замер на реальной `CXMT_2026-07-29_2026-08-17.analysis-v6.duckdb`:

| TF | плато | READY с замороженным BASE | выбрано сейчас | по контракту |
| --- | --- | --- | --- | --- |
| 5m | 24 | 9 | **1** | 3 |
| 15m | 16 | 12 | **1** | 3 |
| 1h | 8 | 8 | **1** | 3 |
| 4h | 8 | 5 | **1** | 3 |
| 30m | 6 | 6 | **1** | 3 |
| 45m | 6 | 6 | **1** | 3 |
| 2h | 2 | 2 | **1** | 2 |
| 3h | 1 | 1 | **1** | 1 |

**Доступно 49 кандидатов, выбирается 8, по контракту должно быть 21.**

Кроме 3h — там всего одно плато — каждый TF имеет не меньше двух пригодных
кандидатов, то есть требование «минимум два 1ORD на TF» выполнимо везде, где
это вообще возможно по данным.

## Причина

[`selection.py:643-652`](../../../src/mrs3/selection.py#L643):

```python
for _, group in local.groupby(["symbol", "side"], sort=True):
    selected_rows.append(
        group.sort_values(
            ["pnl_dd5_theoretical", "pnl_pct", "trades", "dd_pct", "point_id"],
            ascending=[False, False, False, True, True],
            kind="mergesort",
        ).iloc[0]
    )
```

Два отклонения от §17:

1. **`.iloc[0]` — берётся один вместо «at most three».** Порядок сортировки при
   этом ровно контрактный, менять его не нужно; отсекается только хвост.
2. **`groupby(["symbol", "side"])` — без `timeframe`.** Контракт требует
   «exact scope» = `Pair + Side + TF`. Докстринг функции прямо закрепляет
   неверную формулировку: «at most one frozen BASE per pair+side».

Второе сейчас **замаскировано**: fresh-анализ выполняет `_analyze_points`
отдельно на каждый scope, поэтому внутри вызова всегда один `(symbol, side)`, и
результат совпадает с «один на TF». Но для любого вызывающего, который подаёт
несколько TF одним кадром — а это legacy-путь `pipeline.py:448` — из всех TF
останется один-единственный BASE. Это скрытая потеря данных, а не косметика.

## Последствия

- В READY JSON уходит один 1ORD на TF вместо двух-трёх. Восемь стратегий вместо
  двадцати одной на этом наборе.
- Колонка `1ORD` на вкладке показывает `1` в каждой строке и выглядит как
  «почти ничего не нашли», хотя пул из 49 кандидатов посчитан и заморожен.
- Сравнение TF между собой искажено: TF с 12 сильными плато и TF с одним дают
  одинаковую единицу.

## План работ

### Этап 1 — Тест, воспроизводящий отклонение

Узкий падающий тест на `select_base_one_order`:

1. Кадр из нескольких плато одного `Pair+Side+TF` с разными
   `pnl_dd5_theoretical` → ожидается **три** строки в контрактном порядке.
2. Пул из двух плато → ожидается две строки (не три и не одна).
3. Пул из одного плато → одна строка.
4. Кадр с **двумя TF** одного `Pair+Side` → BASE выбирается независимо для
   каждого TF, а не один на оба. Это тест на пункт 2 причины.
5. Порядок ранжирования не меняется: при равных `pnl_dd5_theoretical`
   решает `pnl_pct`, затем `trades`, затем `dd_pct` ASC, затем `point_id`.

### Этап 2 — Исправление отбора

```python
for _, group in local.groupby(["symbol", "side", "timeframe"], sort=True):
    selected_rows.extend(
        group.sort_values(...).head(config.max_base_one_order).to_dict("records")
    )
```

- добавить `timeframe` в группировку;
- `.iloc[0]` → `.head(N)`, где `N` — верхняя граница из §17;
- поправить докстринг: «per exact scope», а не «per pair+side».

**Открытый вопрос к заказчику:** сделать `N` константой `3` по §17 или
настройкой `AlgorithmConfig`. Настройка меняет `algorithm_config_sha256`, а с
ним `analysis_id` и всю lineage — то есть требует переанализа. Константа — нет.
**Рекомендую константу 3**: контракт задаёт её как фиксированную верхнюю
границу, а не как параметр эксперимента.

### Этап 3 — Проверка инвариантов ниже по потоку

1. `pipeline.py:356-359` — `BASE_1ORD variant count does not match selected
   baselines`. Проверка сравнивает число вариантов с числом выбранных baselines;
   при трёх вместо одного она должна продолжать сходиться. Тест обязателен.
2. `base_json_count` в манифесте (`pipeline.py:562`) и `base_1ord_count` по §17
   должны вырасти соответственно.
3. §17: `1ORD -> EQUAL only` — три BASE дают три JSON, не шесть.
4. Уникальность имён стратегий: три BASE одного scope не должны схлопнуться в
   одно имя. `validate_unique_names` обязан это ловить — добавить тест.

### Этап 4 — Панель

После исправления колонка `1ORD` начнёт показывать 1–3 вместо 1 без правок
фронтенда: она уже считает из `base_one_order`.

Отдельный вопрос — **сделать BASE-кандидатов выбираемыми**. Сейчас
`list_fresh_analysis_shortlist` считает их, но не предлагает, потому что
`base_one_order` хранит *точки*, а `generate_fresh_analysis_strategies` читает
только `structures`. §17 при этом прямо требует:

> Panel/controller must derive selectable scopes from the canonical published
> run/surface plus persisted Plateau BASE facts, not only from 2–4ORD candidates.
> A scope with BASE 1ORD but no multiorder candidate must remain selectable.

Сейчас это **не выполняется**: пять TF из восьми (1h, 2h, 3h, 30m, 45m) имеют
BASE, но ноль многоордерных структур, и в панели они невыбираемы — то есть их
1ORD-стратегии недостижимы вовсе.

**Решение:** fresh-анализ должен сохранять BASE не только как точку, но и как
структуру-кандидата `order_count = 1` — ровно так, как её строит
`pipeline._base_structure`. Тогда генератор примет её без изменений: он уже
содержит ветку `methods = (LotMethod.EQUAL,) if order_count == 1`.

Это отдельный этап с собственным решением по хранению: добавить BASE-структуры в
таблицу `structures` fresh-анализа либо ввести соседнюю таблицу. Первое проще и
не меняет DDL, но смешивает два происхождения в одной таблице — нужен явный
признак.

## Порядок

| Этап | Зависит | Объём |
| --- | --- | --- |
| 1. Тесты отклонения | — | малый |
| 2. Исправление отбора | 1 | малый |
| 3. Инварианты ниже по потоку | 2 | малый |
| 4. Выбираемость BASE в панели | 2, 3 | средний, нужен ADR по хранению |

Этапы 1–3 закрывают «минимум два 1ORD на TF». Этап 4 закрывает «scope с BASE, но
без многоордерных, остаётся выбираемым» — без него пять TF из восьми на текущем
наборе так и останутся без единой стратегии.

## Проверка

`.venv\Scripts\python.exe -m pytest -q`. Не коммитить `Input/`, `Output/`,
`Data/`, HTML, DuckDB и generated artifacts.

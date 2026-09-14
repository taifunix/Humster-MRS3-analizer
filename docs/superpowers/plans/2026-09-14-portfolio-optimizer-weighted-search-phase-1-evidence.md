# Weighted Portfolio Search — evidence фазы 1

Дата: 2026-09-14. Статус: accepted после независимого Opus `CODE_REVIEW_PASS`.

## Выбранная база исходного размера

Используется только исходный динамический режим тестера:
`use_fix=false`, `use_upnl=true`, `use_frozen_balance=true`.
Fixed-basis retest и override `use_fix=true` не используются.

Для текущих LONG-финалистов `balance_percentage_long=100` и `risk_long=1`.
Поэтому для каждого цикла, начавшегося из flat-состояния,
`S_i,k` равен sizing balance непосредственно перед первым открытием цикла.
Из строки opening-action он восстанавливается как
`Balance - PnL + Fee`; в проверенных opening-action `PnL=0`.
Следующий цикл получает уже новый баланс, поэтому `S_i,k` меняется вместе с
накопленным результатом. Цикл, открытый до доступной границы отчёта, не получает
придуманную базу: его normalized return остаётся `UNKNOWN`.

Price/Cost остаются фактами исполнения и не используются вместо планового `S`.
Текущий Performance v2 parser допускает только `use_upnl=true`; отчёт с
`use_upnl=false` отклоняется до импорта. `raw_action_json` этого импортёра
содержит только Price/Cost, их quality-маркеры и версию их семантики, поэтому
`NULL` для старой строки без этих полей не удаляет другой action payload.

## Контрольные финалисты

| Вид | strategy_id / result_id | Финалист | Source evidence |
| --- | --- | --- | --- |
| 1 уровень | 16618 / 16767 | `BABAUSDT_3h_LONG_1ORD_CMA3_BASE_c1db5db39996ce3d_EQUAL` | report SHA-256 `4fe5aa4be0603fecd937b30b1f8d1a3794d8008bf54c6a1a01bd2093c5bd5836`; первые базы циклов: 1000, 1024.411986736, 1046.395386937 USDT |
| 3 уровня | 19081 / 19230 | `PANWUSDT_3h_LONG_3ORD_CMA2_STR_8d2fd3c0c2526449_EQUAL` | report SHA-256 `f14dacfe9e32f10be7f76e07e5770078d3a97873f1b9b9333e9f660c2c7854e3`; первая база цикла 1000 USDT, `lot_x=[0.333333333333, 0.333333333333, 0.333333333334]` |

Оба отчёта содержат Price/Cost и allowlisted source settings. Настройки sizing
совпадают: `balance_percentage_long=100`, `risk_long=1`, `max_balance=0`,
`leverage=20`; тестовый режим остаётся динамическим.

## Воспроизводимая арифметика

Fixture `tests/fixtures/portfolio/source_sizing_arithmetic.json` содержит два
цикла с разными динамическими базами: `delta=10,S=100` и `delta=20,S=200`.
Оба дают `r=0.1`; при `x=50` оба вклада равны 5 USDT. Обратное умножение
восстанавливает исходные delta с `atol=rtol=1e-8`.

## REPLACE

После загрузки 13 сохранённых отчётов текущая БД содержит 13 ожидаемых
FINALIST-результатов, 6519 actions с `raw_action_json` и 65646 equity rows;
v4 REPLACE сохранил текущие result IDs. Regression на временной БД проверяет,
что REPLACE сохраняет User Status/Rank/comment и result ID, а старый отчёт без
Price/Cost и дополнительных settings записывает `NULL`, не наследуя значения
предыдущей ревизии. ADD/REPLACE старого формата остаются совместимыми.

## Проверки

- `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_finalist_retest.py tests/test_performance_v2_import.py tests/test_portfolio_input.py -q`
  — `145 passed`.
- `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_html.py::test_parser_requires_upnl -q`
  — `1 passed`.
- `git diff --check` — passed.
- Независимый Opus review — `CODE_REVIEW_PASS`.

Реальный tester run, массовый retest и обращения к Bybit не выполнялись.

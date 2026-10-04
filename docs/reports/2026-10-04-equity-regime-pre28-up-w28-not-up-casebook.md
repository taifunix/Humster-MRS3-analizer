# PRE28=UP, W28≠UP: решения по всем сочетаниям

**Дата:** 2026-10-04. **Статус:** согласованная проектная классификация; в действующий фильтр не внедрена. Источник чисел — неизменные факты M3 (`Output/EquityM3/2026-10-04/full/facts.jsonl`), направления при `epsilon=2`.

Для каждой строки сначала действует уже согласованный предел риска: `D14≥23%` либо `D7≥23%` → `DROP; User Status = REJECTED`. Таких отчётов 21; они включены в N и показаны в столбце DD23. Для остальных с D14,D7<23%:

- W28=DOWN при любом W14/W7 → `DROP; User Status = REJECTED`, причина `W28_DOWN`.
- W28=FLAT/MIXED и PRE28=UP: `RESUMED / PASS` требует **одновременно W14=UP, W7=UP, нового полного ATH на W7 и его удержания** (`close > HWM(T−7)`). При D14,D7<23% отсутствие хотя бы одного условия даёт `STALLED / PASS + RESERVED`.
- Это правило касается PRE28=UP. Если при W28=FLAT/MIXED PRE28 не UP или недоступен, продолжает действовать отдельное `DROP; User Status = REJECTED`, причина `PRE28_AND_W28_NOT_UP`.

`H` — число отчётов с обновлённым и удержанным ATH в W7; само по себе H не означает RESUMED. У H нет пересечения с DD23 в этой выборке. Все 48 сочетаний перечислены, одно сейчас не встречается в базе.

## A: W28=DOWN — N=284, DD23=10, H=0

| № | W14 | W7 | N | DD23 | H | Исход при DD<23% |
| --- | --- | --- | ---: | ---: | ---: | --- |
| A01 | DOWN | DOWN | 139 | 3 | 0 | DROP; User Status REJECTED / W28_DOWN |
| A02 | DOWN | FLAT | 18 | 0 | 0 | DROP; User Status REJECTED / W28_DOWN |
| A03 | DOWN | MIXED | 18 | 0 | 0 | DROP; User Status REJECTED / W28_DOWN |
| A04 | DOWN | UP | 9 | 0 | 0 | DROP; User Status REJECTED / W28_DOWN |
| A05 | FLAT | DOWN | 3 | 0 | 0 | DROP; User Status REJECTED / W28_DOWN |
| A06 | FLAT | FLAT | 26 | 0 | 0 | DROP; User Status REJECTED / W28_DOWN |
| A07 | FLAT | MIXED | 8 | 0 | 0 | DROP; User Status REJECTED / W28_DOWN |
| A08 | FLAT | UP | 5 | 0 | 0 | DROP; User Status REJECTED / W28_DOWN |
| A09 | MIXED | DOWN | 9 | 1 | 0 | DROP; User Status REJECTED / W28_DOWN |
| A10 | MIXED | FLAT | 7 | 0 | 0 | DROP; User Status REJECTED / W28_DOWN |
| A11 | MIXED | MIXED | 5 | 0 | 0 | DROP; User Status REJECTED / W28_DOWN |
| A12 | MIXED | UP | 10 | 0 | 0 | DROP; User Status REJECTED / W28_DOWN |
| A13 | UP | DOWN | 8 | 2 | 0 | DROP; User Status REJECTED / W28_DOWN |
| A14 | UP | FLAT | 2 | 0 | 0 | DROP; User Status REJECTED / W28_DOWN |
| A15 | UP | MIXED | 6 | 4 | 0 | DROP; User Status REJECTED / W28_DOWN |
| A16 | UP | UP | 11 | 0 | 0 | DROP; User Status REJECTED / W28_DOWN |

## B: W28=FLAT — N=627, DD23=0, H=127

| № | W14 | W7 | N | DD23 | H | Исход при DD<23% |
| --- | --- | --- | ---: | ---: | ---: | --- |
| B01 | DOWN | DOWN | 14 | 0 | 0 | STALLED 14 |
| B02 | DOWN | FLAT | 7 | 0 | 0 | STALLED 7 |
| B03 | DOWN | MIXED | 2 | 0 | 0 | STALLED 2 |
| B04 | DOWN | UP | 8 | 0 | 0 | STALLED 8 |
| B05 | FLAT | DOWN | 11 | 0 | 0 | STALLED 11 |
| B06 | FLAT | FLAT | 94 | 0 | 3 | STALLED 94 |
| B07 | FLAT | MIXED | 32 | 0 | 5 | STALLED 32 |
| B08 | FLAT | UP | 77 | 0 | 14 | STALLED 77 |
| B09 | MIXED | DOWN | 8 | 0 | 0 | STALLED 8 |
| B10 | MIXED | FLAT | 34 | 0 | 0 | STALLED 34 |
| B11 | MIXED | MIXED | 35 | 0 | 11 | STALLED 35 |
| B12 | MIXED | UP | 93 | 0 | 23 | STALLED 93 |
| B13 | UP | DOWN | 0 | 0 | 0 | Сейчас нет отчётов; при появлении STALLED |
| B14 | UP | FLAT | 21 | 0 | 2 | STALLED 21 |
| B15 | UP | MIXED | 22 | 0 | 5 | STALLED 22 |
| B16 | UP | UP | 169 | 0 | 64 | H: RESUMED 64; без H: STALLED 105 |

## C: W28=MIXED — N=2119, DD23=11, H=357

| № | W14 | W7 | N | DD23 | H | Исход при DD<23% |
| --- | --- | --- | ---: | ---: | ---: | --- |
| C01 | DOWN | DOWN | 108 | 0 | 0 | STALLED 108 |
| C02 | DOWN | FLAT | 13 | 0 | 0 | STALLED 13 |
| C03 | DOWN | MIXED | 18 | 0 | 0 | STALLED 18 |
| C04 | DOWN | UP | 11 | 0 | 0 | STALLED 11 |
| C05 | FLAT | DOWN | 24 | 0 | 0 | STALLED 24 |
| C06 | FLAT | FLAT | 19 | 0 | 0 | STALLED 19 |
| C07 | FLAT | MIXED | 19 | 0 | 1 | STALLED 19 |
| C08 | FLAT | UP | 37 | 0 | 10 | STALLED 37 |
| C09 | MIXED | DOWN | 48 | 0 | 0 | STALLED 48 |
| C10 | MIXED | FLAT | 39 | 0 | 0 | STALLED 39 |
| C11 | MIXED | MIXED | 32 | 0 | 4 | STALLED 32 |
| C12 | MIXED | UP | 78 | 0 | 32 | STALLED 78 |
| C13 | UP | DOWN | 165 | 1 | 0 | STALLED 164 |
| C14 | UP | FLAT | 173 | 0 | 5 | STALLED 173 |
| C15 | UP | MIXED | 439 | 6 | 8 | STALLED 433 |
| C16 | UP | UP | 896 | 4 | 297 | H: RESUMED 297; без H: STALLED 595 |

**Проверка инвариантов:** при W28=DOWN удержанный ATH невозможен: `close > HWM(T−7) ≥ equity(T−28)` делает итог W28 положительным, а DOWN требует отрицательного итога. Поэтому H=0 во всей группе A. Удержанный ATH с W7=DOWN также невозможен, но встречается при W7=FLAT (10) и MIXED (34): эти 44 отчёта получают STALLED, поскольку W7 не UP. Ещё 79 отчётов с H и W7=UP получают STALLED, поскольку W14 не UP.

**Итог по 3 030 отчётам:** 295 DROP / User Status REJECTED (284 W28=DOWN, ещё 11 W28=MIXED с DD≥23%); 361 RESUMED / PASS; 2 374 STALLED / PASS + RESERVED. Числа описывают проектное применение правил к сохранённым фактам, а не действующий Panel.

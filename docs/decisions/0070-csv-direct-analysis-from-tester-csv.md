# ADR-0070 — Analysis DuckDB строится из CSV тестера тем же движком, что и Source v6

- Дата: 2026-10-10
- Статус: Accepted (реализация в `feat/csv-direct-analysis`, приёмка пользователем ожидается)
- Контракт: [CSV Direct analysis](../specs/2026-10-10-csv-direct-analysis.md)

## Контекст

Тестер исправил `TotalTrades`: теперь это завершённые позиции. Тяжёлые Source v6 DB и surface
на практике удаляются сразу после получения Analysis DuckDB. HTML новой версии тестера
строгий парсер Source v6 уже не читает. Пользователь требует, чтобы Analysis DuckDB из CSV читалась
неизменёнными shortlist и генератором READY JSON и была совместима со старой.

Предложенный пакет поставки прогонял CSV-точки через движок в режиме `legacy_trades_proxy` и
дописывал поля `real_independent_events` после анализа. В этом режиме не применяются
`min_plateau_events_per_month` и многоордерный допуск, а BASE выбирается одной legacy-точкой
вместо ролевого отбора. Это нарушает и требование совместимости, и правило CLAUDE.md, запрещающее
смешивать legacy proxy с real events. На реальной базе MARA/ZM такой путь дал 19 READY против 36.

## Решение

1. Каждая строка CSV превращается в ту же analysis-row, которую Source v6 строит из surface
   (`_analysis_frame_row`). Анализ идёт штатным `_analyze_points()` в режиме
   `real_independent_events`; проекция и публикация — `source_v6_analysis_fresh._frames/_publish`.
2. `point_event_count = TotalTrades`. `_event_ids` — детерминированные count-slot метки внутри точки,
   только чтобы выполнить существующую схему точки; это не события рынка.
3. `events_last_30d` — точная частота `TotalTrades × 30 / max(EffectiveDays, 30)` (`Decimal`).
   `pipeline._apply_package_event_unions` принимает `Decimal` и берёт floor суммы по плато один раз.
   Для целых значений Source v6 поведение не меняется.
4. Метрики точки — по определениям Source v6: PnL% из `TotalPnL/InitialBalance`, win rate по
   `Win/(Win+Los)`, сделки в ноль допустимы, только фиксированный лот без uPnL.
5. Без PRETEST A/B артефакт использует уже поддерживаемую reader-пару v1
   (`analysis-v6-fresh-compact-v1` / `surface-v6-fresh-compact-v2`) с явной CSV-провенансой в
   manifest. Панель сообщает `pretest_ab_available=false`, UI блокирует галочку A/B.
6. Маршрут Source v6 остаётся без изменений для отката.

## Отклонённые альтернативы

- Legacy proxy + пост-фактум bridge (пакет поставки): другие правила отбора; доказано на MARA/ZM.
- Новый fingerprint `analysis-csv-*` с правками shortlist/генератора: нарушает требование не менять
  downstream.
- Fingerprint v2 с выдуманным PRETEST: ложные данные.
- Округлять активность каждой точки до целого: занижает сумму по плато (`Σ floor ≤ floor Σ`).

## Последствия

- Одна операция вместо трёх баз; HTML больше не нужен для анализа.
- Активность плато теперь — средняя частота, а не «последние 30 дней»; на MARA/ZM пороги далеко не
  пограничные (минимум 121–156 против 20).
- Точность DD% и WinRate ограничена двумя знаками CSV. На MARA/ZM READY не изменился; для полной
  точности нужен более подробный вывод тестера.
- Фильтр A/B для CSV-анализов недоступен.

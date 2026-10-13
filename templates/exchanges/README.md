# Шаблоны бирж

Одна папка на биржу; папку выбирает маркер биржи активной PerformanceDB
(Panel → Настройки → «PerformanceDB и биржа»). Чтобы добавить биржу, создайте
папку с её именем и файлами ниже; имя папки — значение `exchange.name` в
стратегиях бота и маркер биржи базы.

| Файл | Назначение |
| --- | --- |
| `exchange.json` | `exchange` (= имя папки) и `listing_dates_sheet` — лист дат листинга в файле `panel_workflow.listing_dates_path` (`null` — лист «Пары» Bybit) |
| `mrs3-strategy.json` | шаблон стратегии MRS3 для READY, RETEST и RETEST финалистов; один файл с прототипами обеих сторон |
| `mrs3-tester.json` | шаблон тестера MRS3; `InitialBalance` — база B протокола v2, комиссии и фандинг биржи |
| `portfolio-strategy.json` | шаблон стратегии оптимизатора (Stage 2); необязателен |
| `portfolio-tester.json` | шаблон тестера Stage 2 оптимизатора; необязателен |

Запуск проверяет, что стратегии и конфиги тестера совпадают с шаблонами биржи
по `exchange.account`, `MakerFee`, `TakerFee`, `FundingRate` и
`FundingIntervalHours`. Изменение шаблона требует проверки генерации обеих
сторон и отдельного review. Контракт:
[P7](../../docs/specs/2026-10-13-panel-performance-db-selection.md).

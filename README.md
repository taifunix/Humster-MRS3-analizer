# Humster MRS3 Analyzer

Инструменты для воспроизводимого отбора, генерации, тестирования и аудита кандидатов MRS3 mean-reversion strategy.

> Статус: идёт переход к v0.7. Код уже запускается и покрыт тестами, но production legacy-run ещё ожидает подтверждённые результаты v4 DuckDB-импорта.

## Что делает проект

```text
MRS2 reports → нормализованные точки → plateaus / structures → JSON кандидатов
           → Hamster Bot Tester → raw results → DD5 retest → individual comparison
```

Pipeline сохраняет traceability от каждого JSON до MRS2-точек и не выдаёт source-метрики за фактическую доходность MRS3 до реального теста.

## Быстрый старт

Нужен Python 3.11+ и локальный Hamster Bot Tester.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item config.local.json.example config.local.json
.\.venv\Scripts\python.exe -m pytest -q
```

В `config.local.json` задайте локальный `tester_runner.bot_root`. Файл игнорируется Git и не должен публиковаться.

## Основные команды

```powershell
# Справка по доступным командам
.\.venv\Scripts\python.exe -m mrs3.cli --help

# Локальная панель управления
.\scripts\start_panel.bat

# Построение кандидатов
.\.venv\Scripts\python.exe -m mrs3.cli select --help

# Проверка batch без изменения тестера / запуск batch
.\.venv\Scripts\python.exe -m mrs3.cli tester-plan --help
.\.venv\Scripts\python.exe -m mrs3.cli tester-run --help

# Post-test DD5 comparison
.\.venv\Scripts\python.exe -m mrs3.cli posttest --help
```

В панели источники MRS2 разделены на CSV и DuckDB. На вкладке «Кандидаты
стратегий» выберите ровно один вход: проверенный `source-pack` или
совместимый raw CSV. Портфельный раздел пока информационный: симулятор и
рекомендации недоступны до подтверждения входных контрактов.

Перед запуском на production данных прочитайте активную спецификацию: [v0.7 legacy selection](docs/specs/2026-08-10-v07-legacy-selection.md).

## Важные ограничения

- Не удаляйте HTML до успешного v4 import audit.
- Не смешивайте `legacy_trades_proxy` и real independent events.
- Сумма PnL исходных MRS2-ордеров не равна PnL готовой MRS3-стратегии.
- Реальное сравнение финалистов требует tick-test и DD5 retest.
- Портфельная симуляция пока вне scope: ей нужны временные ряды equity, drawdown, occupancy и margin.

## Документация и участие

- [PRD и реестр фич](PRD.md)
- [Текущая проверенная точка](progress.md)
- [Правила для агентов и Claude Code](AGENTS.md), [CLAUDE.md](CLAUDE.md)
- [Навигация по документации](docs/README.md)

Внутренние правила требуют отдельного agent-review перед каждым коммитом. Не добавляйте raw HTML, DuckDB, локальную конфигурацию или результаты тестера в Git.

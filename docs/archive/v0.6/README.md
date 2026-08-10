# MRS3 v0.6

Детерминированный селектор MRS3 и обвязка для пакетного запуска стратегий через веб-интерфейс Hamster Bot.

## Что уже реализовано

- нормализация CSV и дат листинга;
- фильтры истории, экономики и достаточности выборки;
- аудит недостающей детализации с шагом 0,1% без выдумывания отсутствующих точек;
- плато CORE 0,90 / envelope 0,75, CloseMA-профили, 1ORD и структуры MRS3 на 2–4 ордера;
- варианты лотов `EQUAL` и `INCOME` и готовые JSON стратегий;
- полный аудит в XLSX и CSV;
- безопасная пакетная установка стратегий в Hamster Bot;
- запуск каждой строки тестера через её HTMX wizard;
- контроль процентов и тройная проверка завершения;
- сверка `wizard_result.json` с HTML и атомарный итоговый CSV;
- локальная web-панель управления с живым статусом пакета.

## Установка на Windows

Требуется Python 3.11 или новее.

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -e .
```

После установки доступна команда `mrs3`. Без установки можно запускать `.venv\Scripts\python -m mrs3.cli ...`.

Для запуска панели двойным кликом используйте `start_panel.bat`. При первом запуске он сам создаст `.venv`, установит зависимости и откроет браузер.

## Настройка бота

Все машинно-зависимые пути находятся в секции `tester_runner` файла `config.example.json`. Сейчас в ней зафиксировано:

```json
{
  "bot_root": "C:\\Users\\sarpo\\OneDrive\\Документы\\!Humster\\!Tester",
  "executable": "hb_c.exe",
  "base_url": "http://127.0.0.1:80",
  "port": 80,
  "strategy_dir": "settings_strategy",
  "report_dir": "tester/report/my_test",
  "wizard_result": "tester/wizard_result.json",
  "wizard_progress": "tester/wizard_progress.json"
}
```

Все относительные пути вычисляются от `bot_root`. Если порт будет изменён, одинаково поменяйте `base_url` и `port`. Если `hb_c.exe` принимает аргумент порта, его можно добавить в `bot_args`.

## 1. Построение стратегий

Пример для LONG:

```powershell
mrs3 select `
  --input-csv reports_history_bybit_long_day2.csv `
  --dates dates.xlsx `
  --template ADM_3_LONG_SHORT.json `
  --side LONG `
  --config config.example.json `
  --output-dir output_long
```

Результаты:

- `output_long\strategies\*.json` — стратегии для тестера;
- `output_long\audit.xlsx` — полный аудит;
- `output_long\audit_csv\` — те же таблицы в CSV;
- `output_long\run_manifest.json` — хэши, количества и детерминированный digest.

Manifest отдельно показывает число 2ORD/3ORD/4ORD, BASE JSON и MRS3 JSON.
Для предоставленного полного LONG CSV проверенное разложение равно
`6 BASE_1ORD + 1 712 structures × 2 lot methods = 3 430 JSON`. Подробный
протокол независимой проверки находится в
`docs/algorithm-verification-2026-08-08.md`.

Текущий исходный CSV не содержит требуемой сетки 0,1% ниже 1,5%. Поэтому программа отмечает недостающие тесты в `03_Refine_Required`, но не создаёт фиктивные результаты. Когда появится подробный отчёт, его можно подать той же командой.

## 2. Безопасная проверка плана

Команда только читает конфиг, проверяет существование `bot_root`/`hb_c.exe`, безопасные точные пути и JSON стратегий, а также вычисляет SHA-256 каждого файла. Она не останавливает бот, не удаляет файлы и не обращается к HTTP:

```powershell
mrs3 tester-plan `
  --config config.example.json `
  --strategies output_long\strategies
```

Перед реальным запуском проверьте число стратегий. На предоставленном полном CSV текущий селектор создаёт 3 430 JSON, поэтому полный тест может быть очень долгим.

## 3. Пакетный запуск тестера

```powershell
mrs3 tester-run `
  --config config.example.json `
  --strategies output_long\strategies `
  --output-csv results\mrs3_long_results.csv
```

Последовательность строго фиксирована:

1. До любых изменений проверяются корень бота, точный `hb_c.exe`, защищённые пути и SHA-256 всего пакета стратегий.
2. На настроенном порту определяется PID и сверяется полный путь процесса с `hb_c.exe`.
3. Выполняется `POST /htmx/system/shutdown`; если бот не завершился, сигнал получает только ранее проверенный PID.
4. `settings_strategy` транзакционно заменяется точным набором повторно проверенных staged-копий; удаляются только `tester\report\my_test`, `wizard_result.json` и `wizard_progress.json`.
5. Бот запускается заново, после чего проверяется точный список стратегий в таблице тестера.
6. Для каждой стратегии вызываются её `GET /htmx/tester/wizard?single=...` и `POST /htmx/tester/wizard/run`. Глобальная кнопка «Запустить тестер» не используется.
7. Обвязка ждёт кнопку «Результат», соответствующую запись JSON и стабильный HTML.
8. Точные метрики JSON сверяются с округлёнными значениями HTML; HTML добавляет Profit Factor, gross profit/loss, funding, настройки и сделки.
9. Итоговый CSV записывается атомарно. Только после этого бот снова останавливается, затем отчёты и два wizard-лога удаляются.

После успешного пакета бот остаётся остановленным: это исключает гонку с файлами во время финальной очистки. При любой ошибке или `Ctrl+C` HTML и wizard-логи не удаляются. Рядом с CSV сохраняются `*.state.json` с последним этапом и причиной ошибки и компактный `*.progress.json` с живыми счётчиками и процентами.

## 4. DD5-сравнение после теста

```powershell
mrs3 posttest `
  --results-csv results\mrs3_long_results.csv `
  --audit-xlsx output_long\audit.xlsx `
  --strategies-dir output_long\strategies `
  --config config.example.json `
  --output-dir posttest_long
```

Команда создаёт таблицы `16_Raw_MRS3_Results`, `17_DD5_Normalized`, `18_Final_Comparison`, Pareto/near-tie ранжирование и каталог `scaled_strategies`. В масштабированных JSON нет искусственного ограничения `lot_x <= 1`; это аналитическая нормализация к DD5, поэтому все такие JSON обязательно нужно протестировать повторно.

## 5. Локальная панель управления

Самый простой запуск на Windows — двойной клик по `start_panel.bat`. Вручную:

```powershell
mrs3 panel --config config.example.json
```

Откроется `http://127.0.0.1:8765`. Панель работает отдельным Python-процессом и не выключается при перезапуске `hb_c.exe` на порту 80.

Из панели можно:

- проверить пакет командой `tester-plan` без изменений файлов и процессов;
- запустить все стратегии через `tester-run`;
- видеть этап workflow, количество отправленных, работающих и полностью проверенных стратегий, проценты и последние активные строки;
- запустить исходный селектор и DD5-анализ;
- скачать созданные CSV, XLSX, manifest и файлы состояния.

Пока задача выполняется, повторный запуск заблокирован. Панель слушает только loopback-адрес и не доступна другим компьютерам сети.

## Проверка проекта

```powershell
.venv\Scripts\python -m pytest -q
```

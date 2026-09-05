# Bybit market-data collector

Операционная папка для отдельного запуска сборщика. Реализация находится в
`src/mrs3/bybit_collector`; данные не должны храниться в Git.

## Быстрый локальный запуск

```powershell
New-Item -ItemType Directory -Force .tmp/bybit-market-data | Out-Null
Copy-Item bybit_market_data_collector/config.toml.example bybit_market_data_collector/config.toml
.\scripts\run_bybit_market_collector.ps1 -Config .\bybit_market_data_collector\config.toml
```

Перед запуском отредактируйте `config.toml`: укажите нужные символы. Для
проверки конфигурации без подключения используйте:

```powershell
.\.venv\Scripts\python.exe -m mrs3.bybit_collector.cli validate-config --config .\bybit_market_data_collector\config.toml
```

Проверка состояния и архива:

```powershell
.\.venv\Scripts\python.exe -m mrs3.bybit_collector.cli health --config .\bybit_market_data_collector\config.toml
.\.venv\Scripts\python.exe -m mrs3.bybit_collector.cli verify-archive --config .\bybit_market_data_collector\config.toml
```

Сборщик использует публичные Bybit endpoints и не требует API-ключей.

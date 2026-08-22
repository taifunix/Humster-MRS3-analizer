@echo off
setlocal
powershell.exe -NoProfile -Command "$ids=@(netstat -ano | Select-String ':8766\s+.*LISTENING' | ForEach-Object { ($_ -split '\s+')[-1] } | Select-Object -Unique); if (!$ids) { exit 1 }; $ids | ForEach-Object { Stop-Process -Id $_ -Force }; exit 0"
if not errorlevel 1 (
  echo New MRS3 panel stopped.
  exit /b 0
)
echo New MRS3 panel is not running on port 8766.
exit /b 0

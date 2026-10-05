@echo off
REM Supply-shock calendar: SEC offerings / secondary sales / IPO lockups + index rebalance days. Read-only, no orders.
REM Output: signals\supply_calendar.json ; log: logs\supply_calendar_last.log
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
echo [%DATE% %TIME%] start > "%~dp0logs\supply_calendar_last.log"
"%PY%" -X utf8 -u supply_calendar.py >> "%~dp0logs\supply_calendar_last.log" 2>&1
echo exit=%ERRORLEVEL% >> "%~dp0logs\supply_calendar_last.log"
del "%~dp0signals\job_supply_calendar.running" 2>nul

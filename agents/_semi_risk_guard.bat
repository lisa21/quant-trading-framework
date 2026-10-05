@echo off
REM Semiconductor risk guard: daily check of earnings revisions, 10Y/real yields and HY spread. Read-only, no orders.
REM Output: signals\semi_risk_guard.json ; log: logs\semi_risk_guard_last.log
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
echo [%DATE% %TIME%] start > "%~dp0logs\semi_risk_guard_last.log"
"%PY%" -X utf8 -u semi_risk_guard.py >> "%~dp0logs\semi_risk_guard_last.log" 2>&1
echo exit=%ERRORLEVEL% >> "%~dp0logs\semi_risk_guard_last.log"
del "%~dp0signals\job_semi_risk_guard.running" 2>nul

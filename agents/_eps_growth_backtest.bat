@echo off
REM Growth-stock EPS screen: download SEC facts + monthly prices, then backtest. Read-only, no orders.
REM Output: development\<date>\eps_growth\backtest_report.md ; log: logs\eps_growth_last.log
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
REM SEC contact header is read from agents\.sec_user_agent (local file, git-ignored)
if exist "%~dp0.sec_user_agent" set /p SEC_USER_AGENT=<"%~dp0.sec_user_agent"
set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
echo [%DATE% %TIME%] start > "%~dp0logs\eps_growth_last.log"
"%PY%" -X utf8 -u growth_eps_data.py >> "%~dp0logs\eps_growth_last.log" 2>&1
"%PY%" -X utf8 -u _backtest_eps_growth.py >> "%~dp0logs\eps_growth_last.log" 2>&1
echo [%DATE% %TIME%] done >> "%~dp0logs\eps_growth_last.log"
del "%~dp0signals\job_eps_growth_backtest.running" 2>nul

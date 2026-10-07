@echo off
REM Pre-registered test: do credit spreads / HYG-IEF warn of SPY drawdowns? Read-only.
REM Output: development\2026-10-07\credit_test\report.md ; log: logs\credit_warning_test.log
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe" -X utf8 -u _credit_warning_test.py
del "%~dp0signals\job_credit_warning_test.running" 2>nul

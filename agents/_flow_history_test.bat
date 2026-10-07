@echo off
REM Pre-registered test: do extreme put/call ratios or short volume predict SPY/QQQ drawdowns? Read-only.
REM Output: development\2026-10-07\flow_test\report.md ; log: logs\flow_history_test.log
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe" -X utf8 -u _flow_history_test.py
del "%~dp0signals\job_flow_history_test.running" 2>nul

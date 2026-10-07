@echo off
REM Read-only account snapshot (accinfo / positions / orders). Output: logs\account_snapshot_last.json
cd /d "%~dp0"
set PYTHONUTF8=1
"C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe" -X utf8 -u _account_snapshot.py > logs\account_snapshot_run.log 2>&1
del "%~dp0signals\job_account_snapshot.running" 2>nul

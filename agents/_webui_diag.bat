@echo off
REM Read-only WebUI diagnostics (local API responses). Output: logs\webui_diag_last.json
cd /d "%~dp0"
set PYTHONUTF8=1
"C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe" -X utf8 -u _webui_diag.py
del "%~dp0signals\job_webui_diag.running" 2>nul

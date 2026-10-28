@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run install_windows.bat first & exit /b 1)
set PYTHONPATH=%CD%\backend
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1

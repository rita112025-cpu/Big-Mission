@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul || (echo Install Python 3.12 first & exit /b 1)
py -3.12 -m venv .venv || exit /b 1
.venv\Scripts\python.exe -m pip install --require-hashes -r backend\requirements-lock.txt
if errorlevel 1 (
 echo Hash locked installation failed. See README for dependency setup.
 exit /b 1
)
where npm >nul 2>nul || (echo Install Node.js LTS first & exit /b 1)
cd frontend
call npm ci || exit /b 1
call npm run build || exit /b 1
echo Installed.

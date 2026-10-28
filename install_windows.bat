@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul || (echo Python 3.12 required & exit /b 1)
py -3.12 -m venv .venv || exit /b 1
.venv\Scripts\python.exe -m pip install -r backend\requirements-lock.txt || exit /b 1
where npm >nul 2>nul || (echo Node.js 22 required & exit /b 1)
pushd frontend
call npm ci --no-audit --no-fund || (popd & exit /b 1)
call npm run build || (popd & exit /b 1)
popd
echo Install completed. Run start_windows.bat

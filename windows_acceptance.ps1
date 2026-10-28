$ErrorActionPreference="Stop"
$root=Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$py=Join-Path $root ".venv\Scripts\python.exe"
if (!(Test-Path $py)) { throw "Run install_windows.bat first" }
$env:PYTHONPATH=Join-Path $root "backend"
& $py -m pytest tests -v
if ($LASTEXITCODE -ne 0) { throw "Core tests failed" }
Write-Host "Core tests passed. Checking Windows BAT and PowerShell shell availability..."
if (!(Get-Command powershell.exe -ErrorAction SilentlyContinue)) { throw "PowerShell not found" }
& cmd.exe /d /c "echo Windows BAT smoke test"
if ($LASTEXITCODE -ne 0) { throw "BAT smoke test failed" }
Write-Host "Open http://127.0.0.1:8000 after start_windows.bat and create tasks from examples."

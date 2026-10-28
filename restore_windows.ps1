param([Parameter(Mandatory=$true)][string]$Backup)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $root "data"
if (!(Test-Path (Join-Path $Backup "mission.sqlite3"))) { throw "Backup must contain mission.sqlite3" }
Write-Host "WARNING: This overwrites all current runs, tasks and logs. Stop the server first."
$answer = Read-Host "Enter RESTORE to continue"
if ($answer -cne "RESTORE") { throw "Restore cancelled" }
if (Test-Path $target) { Move-Item -LiteralPath $target -Destination (Join-Path $root ("data.before-restore-" + (Get-Date -Format "yyyyMMddHHmmss"))) }
Copy-Item -LiteralPath $Backup -Destination $target -Recurse
Write-Host "Restore complete. Restart the service."

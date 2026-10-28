param([Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $root "data"
if (!(Test-Path $source)) { throw "Data directory not found: $source" }
if (Test-Path $Destination) { throw "Backup destination already exists; choose a fresh folder" }
Write-Host "Stop Big-Mission before continuing. Press Ctrl+C to abort."
$answer = Read-Host "Have you stopped the service? Enter YES"
if ($answer -cne "YES") { throw "Backup cancelled" }
Copy-Item -LiteralPath $source -Destination $Destination -Recurse
Write-Host "Backup complete: $Destination"

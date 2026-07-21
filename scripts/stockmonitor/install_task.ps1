param(
    [string]$TaskName = "StockMonitor_Fetch",
    [string]$OutputFile = "",
    [switch]$SkipInitialFetch
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Runner = Join-Path $PSScriptRoot "run_fetch.ps1"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not $OutputFile) {
    $OutputFile = Join-Path $ProjectRoot "data\stockmonitor\spot.json"
}
$OutputFile = [System.IO.Path]::GetFullPath($OutputFile)

if (-not (Test-Path $Python)) {
    throw "Run .\run_web.bat once first so the project virtual environment is installed."
}

New-Item -ItemType Directory -Path ([System.IO.Path]::GetDirectoryName($OutputFile)) -Force | Out-Null

$PowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`" -OutputFile `"$OutputFile`""
$Action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $Arguments -WorkingDirectory $ProjectRoot

$TradingTimes = @()
for ($minutes = 9 * 60 + 30; $minutes -le 11 * 60 + 30; $minutes += 15) {
    $TradingTimes += $minutes
}
for ($minutes = 13 * 60; $minutes -le 15 * 60; $minutes += 15) {
    $TradingTimes += $minutes
}

$Triggers = foreach ($minutes in $TradingTimes) {
    $hour = [Math]::Floor($minutes / 60)
    $minute = $minutes % 60
    $at = (Get-Date).Date.AddHours($hour).AddMinutes($minute)
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $at
}

$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
$Task = New-ScheduledTask -Action $Action -Trigger $Triggers -Settings $Settings -Principal $Principal
Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Schedule: weekdays 09:30-11:30 and 13:00-15:00, every 15 minutes"
Write-Host "Snapshot: $OutputFile"

if (-not $SkipInitialFetch) {
    Write-Host "Running the first collection now..."
    & $Runner -OutputFile $OutputFile
    if ($LASTEXITCODE -ne 0) {
        throw "Initial StockMonitor collection failed. Check the log next to spot.json."
    }
}

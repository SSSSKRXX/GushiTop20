param(
    [string]$OutputFile = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Collector = Join-Path $PSScriptRoot "fetch_spot.py"

if (-not $OutputFile) {
    $OutputFile = Join-Path $ProjectRoot "data\stockmonitor\spot.json"
}

if (-not (Test-Path $Python)) {
    throw "Project virtual environment not found: $Python"
}

$OutputFile = [System.IO.Path]::GetFullPath($OutputFile)
$LogFile = Join-Path ([System.IO.Path]::GetDirectoryName($OutputFile)) "stockmonitor.log"
New-Item -ItemType Directory -Path ([System.IO.Path]::GetDirectoryName($OutputFile)) -Force | Out-Null

& $Python $Collector --output $OutputFile 2>&1 | Out-File -FilePath $LogFile -Append -Encoding utf8
exit $LASTEXITCODE

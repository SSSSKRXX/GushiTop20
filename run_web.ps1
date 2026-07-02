$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$App = Join-Path $ProjectRoot "web_app.py"
$Url = "http://127.0.0.1:8765"

if (-not (Test-Path $Python)) {
    python -m venv (Join-Path $ProjectRoot ".venv")
}

& $Python -m pip install -r (Join-Path $ProjectRoot "requirements.txt")

$isRunning = $false
try {
    $response = Invoke-WebRequest -UseBasicParsing $Url -TimeoutSec 2
    $isRunning = $response.StatusCode -eq 200
} catch {
    $isRunning = $false
}

if (-not $isRunning) {
    Start-Process -FilePath $Python -ArgumentList @($App) -WorkingDirectory $ProjectRoot -WindowStyle Hidden

    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            $response = Invoke-WebRequest -UseBasicParsing $Url -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
            $ready = $false
        }
    }

    if (-not $ready) {
        throw "Web server startup timed out. Run .\.venv\Scripts\python web_app.py in a terminal to view the error."
    }
}

Start-Process $Url
Write-Host "Web dashboard opened: $Url"

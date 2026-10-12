$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    $pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pythonLauncher) {
        & py -3.11 -m venv (Join-Path $PSScriptRoot ".venv")
    } else {
        $python = Get-Command python -ErrorAction SilentlyContinue
        if (-not $python) {
            throw "Python 3.11 is required. Install Python from python.org, then rerun this script."
        }
        & python -m venv (Join-Path $PSScriptRoot ".venv")
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
}

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Could not upgrade pip." }
& $venvPython -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

Write-Host ""
Write-Host "Starting Multi-Chain Paper Agent on http://127.0.0.1:8000"
Write-Host "Paper trading only. Automated trading is OFF by default."
Write-Host "Existing paper_agent.db will be preserved; do not use Reset unless intended."
Write-Host "Press Ctrl+C to stop the app."
Write-Host ""

& $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000
exit $LASTEXITCODE

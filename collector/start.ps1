# Spusteni sberace na Windows. Dvojklik na start.bat, nebo: powershell -ExecutionPolicy Bypass -File start.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Vytvoren soubor .env - dopln SUPABASE_SECRET_KEY, uloz a spust znovu." -ForegroundColor Yellow
    notepad .env
    exit 1
}

if (-not (Test-Path .venv\Scripts\python.exe)) {
    Write-Host "Zakladam virtualni prostredi (.venv)..."
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv } else { python -m venv .venv }
}

& .\.venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -U -r requirements.txt
Write-Host "Sberac bezi. Ukonceni: Ctrl+C" -ForegroundColor Green
& .\.venv\Scripts\python.exe tracker.py

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

$py = ".\.venv\Scripts\python.exe"
# Omezeni verzi (viz constraints.txt) - plati i pro docasna build prostredi pipu
$env:PIP_CONSTRAINT = Join-Path $PSScriptRoot "constraints.txt"
$pipArgs = @("-m", "pip", "install", "-q", "--disable-pip-version-check")

# 1) aktualni pip/setuptools (pip pribaleny k Pythonu byva starsi)
& $py @pipArgs -U pip setuptools wheel
if ($LASTEXITCODE -ne 0) { Write-Host "Aktualizace pip selhala - viz chyba vyse." -ForegroundColor Red; exit 1 }

# 2) protobuf3-to-dict nema hotovy balicek pro Windows a stavi se ze zdrojaku;
#    bez izolace se pouzije setuptools z .venv (obchazi chybu 'Invalid version: 0.dev0')
& $py @pipArgs --no-build-isolation protobuf3-to-dict
if ($LASTEXITCODE -ne 0) { Write-Host "Instalace protobuf3-to-dict selhala - viz chyba vyse." -ForegroundColor Red; exit 1 }

# 3) ostatni zavislosti
& $py @pipArgs -U -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Host "Instalace zavislosti selhala - viz chyba vyse." -ForegroundColor Red; exit 1 }

Write-Host "Sberac bezi. Ukonceni: Ctrl+C" -ForegroundColor Green
& $py tracker.py

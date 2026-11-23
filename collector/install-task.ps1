# Zaregistruje sběrač v Plánovači úloh Windows: spustí se na pozadí po přihlášení
# a při pádu se sám restartuje. Spuštění: powershell -ExecutionPolicy Bypass -File install-task.ps1
# Odebrání: powershell -ExecutionPolicy Bypass -File install-task.ps1 -Remove
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$TaskName = "TikTok Gift Collector"
$dir = $PSScriptRoot

if ($Remove) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Úloha '$TaskName' odebrána." -ForegroundColor Yellow
    exit 0
}

$pythonw = Join-Path $dir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Chybí $pythonw – nejdřív jednou spusť start.bat." }
if (-not (Select-String -Path (Join-Path $dir ".env") -Pattern '^LOG_FILE=.+' -Quiet)) {
    Add-Content -Path (Join-Path $dir ".env") -Value "LOG_FILE=collector.log"
    Write-Host "Do .env doplněno LOG_FILE=collector.log"
}

$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "tracker.py" -WorkingDirectory $dir
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Sbírá dary z TikTok LIVE do Supabase (tiktok_giftboard)" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Úloha '$TaskName' zaregistrována a spuštěna. Log: $dir\collector.log" -ForegroundColor Green

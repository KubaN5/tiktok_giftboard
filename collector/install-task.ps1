# Zaregistruje sberac v Planovaci uloh Windows: spusti se na pozadi po prihlaseni
# a pri padu se sam restartuje. Spusteni: powershell -ExecutionPolicy Bypass -File install-task.ps1
# Odebrani: powershell -ExecutionPolicy Bypass -File install-task.ps1 -Remove
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$TaskName = "TikTok Gift Collector"
$dir = $PSScriptRoot

if ($Remove) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Uloha '$TaskName' odebrana." -ForegroundColor Yellow
    exit 0
}

$pythonw = Join-Path $dir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Chybi $pythonw - nejdriv jednou spust start.bat." }
if (-not (Select-String -Path (Join-Path $dir ".env") -Pattern '^LOG_FILE=.+' -Quiet)) {
    Add-Content -Path (Join-Path $dir ".env") -Value "LOG_FILE=collector.log"
    Write-Host "Do .env doplneno LOG_FILE=collector.log"
}

$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "tracker.py" -WorkingDirectory $dir
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Sbira dary z TikTok LIVE do Supabase (tiktok_giftboard)" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Uloha '$TaskName' zaregistrovana a spustena. Log: $dir\collector.log" -ForegroundColor Green

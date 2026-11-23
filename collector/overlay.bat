@echo off
rem Pretahni MP4 zaznam mysi na tento soubor - vznikne *_overlay.mp4 s dary, komentari a bitvami.
rem Dalsi volby (napr. --delay 2, --start 600 --duration 60, --from-db) jde pripsat za nazev videa v PowerShellu.
cd /d "%~dp0"
if "%~1"=="" (
    echo Pretahni MP4 soubor mysi na overlay.bat
    pause
    exit /b 1
)
".venv\Scripts\python.exe" overlay.py %*
pause

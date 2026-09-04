@echo off
rem Klei heartbeat + auto gift claim for Oxygen Not Included (run by double-click)
chcp 65001 >nul
title ONI Klei Heartbeat Auto
cd /d "%~dp0"

where python >nul 2>nul
if not errorlevel 1 (set "PY=python") else (set "PY=py -3")

echo ============================================================
echo   ONI Klei Heartbeat + Auto Gift Claim
echo   - heartbeat (Tick) every 360s
echo   - auto claim gift when received
echo     (GetAllItems + SetItemOpened, no VerifyGiftingReceipt)
echo   - first run needs Steam client logon
echo   - Ctrl+C to exit
echo ============================================================

%PY% -X utf8 klei_heartbeat_auto.py %*
set RC=%errorlevel%

if not "%RC%"=="0" (
  echo.
  echo [!] script exit code %RC%, see credentials\hb_auto_console.log
)
echo.
pause
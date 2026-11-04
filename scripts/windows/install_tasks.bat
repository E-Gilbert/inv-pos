@echo off
REM ===== Run ONCE as administrator (right-click > Run as administrator) on the POS server PC. =====
REM  - starts INV-POS automatically when Windows starts (before anyone logs in)
REM  - nightly verified database backup (23:30) and end-of-day report pack (23:45)
REM  - watchdog: every 2 minutes, restarts INV-POS automatically if it is frozen or stopped
REM  - opens port 8000 on PRIVATE networks only, so tills/tablets on the restaurant Wi-Fi can connect
cd /d %~dp0\..\..
set APP=%CD%
net session >nul 2>nul
if errorlevel 1 (
  echo  Please right-click this file and choose "Run as administrator".
  pause
  exit /b 1
)
schtasks /create /f /tn "INV-POS Server" /sc onstart /ru SYSTEM /rl HIGHEST /tr "cmd /c cd /d \"%APP%\" && set CFC_LICENCE_MODE=development && .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-server-header >> data\server.log 2>&1"
schtasks /create /f /tn "INV-POS Backup" /sc daily /st 23:30 /ru SYSTEM /tr "cmd /c cd /d \"%APP%\" && .venv\Scripts\python.exe scripts\backup.py --key >> data\backup.log 2>&1"
schtasks /create /f /tn "INV-POS End-of-day reports" /sc daily /st 23:45 /ru SYSTEM /tr "cmd /c cd /d \"%APP%\" && .venv\Scripts\python.exe scripts\eod_reports.py >> data\reports.log 2>&1"
schtasks /create /f /tn "INV-POS Watchdog" /sc minute /mo 2 /ru SYSTEM /tr "\"%APP%\scripts\windows\watchdog.bat\""
netsh advfirewall firewall delete rule name="INV-POS" >nul 2>nul
netsh advfirewall firewall add rule name="INV-POS" dir=in action=allow protocol=TCP localport=8000 profile=private
echo.
echo  Done. INV-POS will start with Windows. Start it now with:  schtasks /run /tn "INV-POS Server"
echo  (If run.bat is open, close it first: only one copy may run.)
echo  If INV-POS freezes it restarts itself within about 3 minutes (watchdog).
echo  To restart it straight away: scripts\windows\restart_server.bat (as administrator).
echo  Remove everything again with: scripts\windows\uninstall_tasks.bat
pause

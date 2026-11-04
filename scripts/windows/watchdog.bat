@echo off
REM ===== Automatic "reset": checks every 2 minutes that INV-POS answers, and restarts it if it is frozen or stopped. =====
REM Registered by install_tasks.bat (runs as SYSTEM, nobody needs to do anything).
REM Paused while data\maintenance.flag exists (create it before updating, delete it afterwards).
REM Restarting is safe: paid sales are saved the moment they are paid and open checks stay open.
cd /d %~dp0\..\..
if exist data\maintenance.flag exit /b 0
call :check && exit /b 0
REM One failed check can be a busy moment or the server still starting: look again before acting.
timeout /t 20 /nobreak >nul
call :check && exit /b 0
echo %date% %time% INV-POS did not answer twice: restarting>> data\watchdog.log
schtasks /end /tn "INV-POS Server" >nul 2>nul
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":8000 .*LISTENING"') do taskkill /f /pid %%p >nul 2>nul
timeout /t 3 /nobreak >nul
schtasks /run /tn "INV-POS Server" >nul 2>nul
timeout /t 15 /nobreak >nul
call :check && (echo %date% %time% restarted OK>> data\watchdog.log) || (echo %date% %time% STILL NOT ANSWERING - check data\server.log>> data\watchdog.log)
exit /b 0

:check
powershell -NoProfile -Command "try { if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 8 http://127.0.0.1:8000/api/health).StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>nul
exit /b %errorlevel%

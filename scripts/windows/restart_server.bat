@echo off
REM ===== "Reset" button: restart INV-POS when tills can't connect or the screen is stuck. =====
REM Safe at any time: every sale is saved the moment it is paid, open checks stay open, nothing is cleared.
REM Run as administrator (right-click > Run as administrator). Put a shortcut to this file on the server desktop.
cd /d %~dp0\..\..
net session >nul 2>nul
if errorlevel 1 (
  echo  Please right-click this file and choose "Run as administrator".
  pause
  exit /b 1
)
echo  Stopping INV-POS...
schtasks /end /tn "INV-POS Server" >nul 2>nul
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":8000 .*LISTENING"') do taskkill /f /pid %%p >nul 2>nul
timeout /t 3 /nobreak >nul
echo  Starting INV-POS...
schtasks /run /tn "INV-POS Server" >nul 2>nul
if errorlevel 1 (
  echo  The INV-POS Server task is not installed. Run scripts\windows\install_tasks.bat first, or start run.bat.
  pause
  exit /b 1
)
set OK=
for /l %%i in (1,1,20) do (
  if not defined OK (
    timeout /t 2 /nobreak >nul
    powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8000/api/health).StatusCode } catch { exit 1 }" >nul 2>nul && set OK=1
  )
)
if defined OK (
  echo  INV-POS is running again. Tills and tablets can reload the page.
) else (
  echo  INV-POS did not answer within 40 seconds. Look at the end of data\server.log and call support.
)
pause

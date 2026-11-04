@echo off
REM Start INV-POS on Windows. First time: double-click setup.bat
cd /d %~dp0
if not exist .venv\Scripts\activate.bat (
  echo  Run setup.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
REM Licence enforcement stays OFF until the owner approves production licensing.
if "%CFC_LICENCE_MODE%"=="" set CFC_LICENCE_MODE=development
echo.
echo  INV-POS is running. Keep this window open (minimise it). Close it to stop the POS.
echo  On this PC open:            http://localhost:8000
echo  On tills and tablets open:  http://THIS-PC-IP:8000   (this PC's IPv4 address is shown below)
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do echo     IPv4:%%a
echo.
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-server-header

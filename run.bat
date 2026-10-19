@echo off
REM Start CFC INV-POS on Windows. First time: double-click setup.bat
cd /d %~dp0
call .venv\Scripts\activate.bat
REM Licence enforcement stays OFF until the owner approves production licensing.
if "%CFC_LICENCE_MODE%"=="" set CFC_LICENCE_MODE=development
echo.
echo  CFC INV-POS is running. On other devices open:  http://THIS-PC-IP:8000
echo  (find this PC's IP with: ipconfig)   Close this window to stop.
echo.
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-server-header

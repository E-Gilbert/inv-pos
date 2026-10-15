@echo off
REM Start CFC POS on Windows. First time: double-click setup.bat
cd /d %~dp0
call .venv\Scripts\activate.bat
echo.
echo  CFC POS is running. On other devices open:  http://THIS-PC-IP:8000
echo  (find this PC's IP with: ipconfig)   Close this window to stop.
echo.
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

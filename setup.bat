@echo off
REM ===== INV-POS one-time setup (Windows). Safe to run again after an update. =====
cd /d %~dp0
echo.
echo  INV-POS setup
echo  -------------
where python >nul 2>nul
if errorlevel 1 (
  echo  Python is not installed. Install Python 3.10 or newer from https://www.python.org/downloads/
  echo  and TICK "Add python.exe to PATH" during installation. Then run setup.bat again.
  pause
  exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo  Python 3.10 or newer is required. Installed version:
  python --version
  pause
  exit /b 1
)
if not exist .venv (
  echo  Creating the private Python environment...
  python -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat
echo  Installing / updating components (needs internet this one time)...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt || goto :fail
echo  Preparing the database (existing data is kept and backed up before any upgrade)...
python -c "from app.db import init_db; init_db()" || goto :fail
echo.
set /p LOADMENU= Load or update the City Food Court menu now? (Y/N): 
if /i "%LOADMENU%"=="Y" python scripts\load_menu.py menus\cfc_menu.csv --hide-missing --cfc
echo.
echo  Setup done.
echo   - Start the POS:            run.bat
echo   - Auto-start + backups:     scripts\windows\install_tasks.bat  (right-click, Run as administrator)
echo   - First login PIN:          1234 (you will be asked to change it)
echo.
pause
exit /b 0
:fail
echo.
echo  Setup FAILED. Nothing was changed in your data. Check the internet connection and run setup.bat again.
pause
exit /b 1

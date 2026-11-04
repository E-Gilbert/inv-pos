@echo off
REM Load or update the City Food Court menu (menus\cfc_menu.csv) and shop settings. Safe to run again.
cd /d %~dp0\..\..
call .venv\Scripts\activate.bat
python scripts\load_menu.py menus\cfc_menu.csv --hide-missing --cfc
pause

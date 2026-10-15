@echo off
REM One-time setup on Windows (needs Python 3.10+ from python.org, "Add to PATH" ticked)
cd /d %~dp0
python -m venv .venv
call .venv\Scripts\activate.bat
pip install -r requirements.txt
echo Setup done. Run run.bat to start. Optional demo data: python -m app.seed
pause

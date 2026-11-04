@echo off
REM Emergency: an admin forgot their PIN. Prints a temporary PIN that must be changed at the next login.
REM Only works on the server PC itself, and is recorded in the audit log.
cd /d %~dp0\..\..
.venv\Scripts\python.exe scripts\reset_admin_pin.py %*
pause

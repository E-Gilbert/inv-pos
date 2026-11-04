@echo off
REM Removes the INV-POS scheduled tasks and firewall rule (data and files are not touched). Run as administrator.
schtasks /delete /f /tn "INV-POS Watchdog"
schtasks /end /tn "INV-POS Server" >nul 2>nul
schtasks /delete /f /tn "INV-POS Server"
schtasks /delete /f /tn "INV-POS Backup"
schtasks /delete /f /tn "INV-POS End-of-day reports"
netsh advfirewall firewall delete rule name="INV-POS"
pause

@echo off
rem Pull new Gmail and Google Calendar into the CRM. Run every 15 minutes by the "ACMI CRM mail sync" scheduled task.
cd /d "%~dp0.."
if not exist logs mkdir logs
rem Keep the log under ~5 MB.
for %%A in (logs\sync_mail.log) do if %%~zA GTR 5000000 move /y logs\sync_mail.log logs\sync_mail.old.log >nul
echo ==== %date% %time% >> logs\sync_mail.log
.venv\Scripts\python.exe manage.py sync_mail >> logs\sync_mail.log 2>&1
rem Google Calendar for everyone who connected it (Meetings page).
echo ---- calendar >> logs\sync_mail.log
.venv\Scripts\python.exe manage.py sync_calendar >> logs\sync_mail.log 2>&1

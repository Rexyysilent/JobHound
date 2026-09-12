@echo off
rem jobhound daily run — registered in Windows Task Scheduler as "jobhound-daily".
rem Full run: fetch -> rank -> digest file -> telegram push (degrades gracefully
rem if telegram is unreachable). Output appends to data\cron.log.
cd /d "%~dp0"
if not exist "data" mkdir "data"
if not exist "data" exit /b 1
".venv\Scripts\python.exe" run.py >> "data\cron.log" 2>&1

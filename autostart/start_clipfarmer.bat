@echo off
REM Launch the clipfarmer scheduler in the background.
REM This is what Task Scheduler runs at user login.

cd /d "C:\Users\chris\clipfarmer"

REM Make sure logs/ exists
if not exist logs mkdir logs

REM Launch the scheduler. pythonw.exe = no console window.
REM Output is redirected to logs/scheduler-autostart.log so we can debug.
start "" /B ".venv\Scripts\pythonw.exe" -m scheduler >> "logs\scheduler-autostart.log" 2>&1

REM exit immediately — scheduler keeps running.
exit /b 0

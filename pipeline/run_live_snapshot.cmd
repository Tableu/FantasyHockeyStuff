@echo off
rem One live snapshot for Task Scheduler (schedule_live_tasks.ps1): %1 = injuries | lines | goalies.
rem Output is appended to logs\live\<kind>.log next to this script.
cd /d "%~dp0"
if not exist logs\live mkdir logs\live
set PYTHONIOENCODING=utf-8
python snapshot_live.py --kind %1 >> "logs\live\%1.log" 2>&1

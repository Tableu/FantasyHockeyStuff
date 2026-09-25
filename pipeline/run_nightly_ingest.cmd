@echo off
rem Nightly job for Task Scheduler (schedule_live_tasks.ps1), output appended to logs\live\nightly.log:
rem   1. pipeline: yesterday's games, lineups, season totals, then the live injury spells
rem   2. ModelFeatures: the season's variant-A lineup rows and goalie starts, which tonight's
rem      P(start) reads as its in-season history (Projections/project_tonight.py)
cd /d "%~dp0"
if not exist logs\live mkdir logs\live
set PYTHONIOENCODING=utf-8
set LOG="%~dp0logs\live\nightly.log"
python run_daily.py >> %LOG% 2>&1
cd /d "%~dp0..\Fantasy Hockey AI\ModelFeatures"
python build_lineup_features.py --season 2026-27 --variant A >> %LOG% 2>&1
python build_goalie_starts.py --season 2026-27 >> %LOG% 2>&1

@echo off
rem Since 2026-10-15 (plan container-own-db) the plan server runs this job on the container's database (Server/scheduled.py) and the task is disabled; run by hand it writes the PC's NHLStats, which the nightly copy-back overwrites.
rem Daily 05:00 job for Task Scheduler (schedule_live_tasks.ps1), output appended to
rem logs\live\player_teams.log:
rem   1. pipeline: import_player_teams.py -- new signings from the unresolved names (NHL search),
rem      then each projected or league-pool player's NHL team into Reference.PlayerTeamHistory
rem   2. ModelFeatures: build_players.py, so player_teams.parquet (the draft board's Team) follows
cd /d "%~dp0"
if not exist logs\live mkdir logs\live
set PYTHONIOENCODING=utf-8
set LOG="%~dp0logs\live\player_teams.log"
python import_player_teams.py >> %LOG% 2>&1
cd /d "%~dp0..\Fantasy Hockey AI\ModelFeatures"
python build_players.py >> %LOG% 2>&1

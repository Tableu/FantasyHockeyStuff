"""The PC's daily jobs, run by the plan server instead of Windows Task Scheduler (plan
container-own-db; the plan doc's Phase 1.6). The same steps as pipeline/run_nightly_ingest.cmd,
run_player_teams.cmd and run_live_snapshot.cmd:

    nightly        04:00            run_daily, then the season's lineup rows, goalie starts, season
                                    stats, season history and feature table, then every skater's
                                    rest-of-season projection; on the container's own database, then
                                    the copy-back of it into the PC's (sync_back.py)
    player_teams   05:00            import_player_teams, build_players
    injuries       10:00, 15:00     the full injury listing (snapshot_live --kind injuries), build_players

A job is due once its latest scheduled time has passed and it has not run for that time, so a
sleep or a restart runs what was missed when the server is back (what catch_up.ps1 did on Windows):
the most recent missed time only, nightly first. The times run for are kept in STATE, so a restart
does not run a job twice. Output is appended to pipeline/logs/live/<job>.log as the .cmd files did --
or, on another database (the trial's --jobs-db, while the Windows tasks still run), to
<job>.container.log: one log file open on both sides stops the Windows task (2026-10-13).
"""

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIBLINGS = HERE.parent
PIPELINE = SIBLINGS.parent / "pipeline"
MODEL_FEATURES = SIBLINGS / "ModelFeatures"
PROJECTIONS = SIBLINGS / "Projections"
LOGS = PIPELINE / "logs" / "live"
STATE = HERE / "state" / "scheduled_jobs.json"      # job -> the scheduled time it last ran for
PC_DB = "host.docker.internal"                     # the PC's SQL Server, as the container reaches it

# In the order a catch-up runs them: the database jobs never overlap (one worker), nightly first.
JOBS = {
    "nightly": {"label": "Nightly import", "times": ["04:00"], "log": "nightly.log"},
    "player_teams": {"label": "Player teams", "times": ["05:00"], "log": "player_teams.log"},
    "injuries": {"label": "Injury listing", "times": ["10:00", "15:00"], "log": "injuries.log"},
}


def season_of(day: dt.date) -> str:
    start = day.year if day.month >= 7 else day.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def steps(name: str, day: dt.date, copy_from: str | None = None) -> list:
    """[(folder, command)] for the job, as its .cmd file runs them. `copy_from`: the database the
    jobs write when it is not the PC's (the container's, db,1433) -- the nightly ends by copying it
    back into the PC's NHLStats, which model training reads."""
    season = season_of(day)
    if name == "nightly":
        copy_back = [(HERE, ["sync_back.py", "--source", copy_from, "--target", PC_DB])] if copy_from else []
        return [(PIPELINE, ["run_daily.py"]),
                (MODEL_FEATURES, ["build_lineup_features.py", "--season", season, "--variant", "A"]),
                (MODEL_FEATURES, ["build_goalie_starts.py", "--season", season]),
                (MODEL_FEATURES, ["build_season_stats.py", "--season", season]),
                (MODEL_FEATURES, ["build_season_history.py"]),
                (MODEL_FEATURES, ["build_feature_table.py", "--season", season]),
                (PROJECTIONS, ["ros_predict.py", "--season", season, "--min-games-played", "0",
                               "--weights", "points-league"])] + copy_back
    if name == "player_teams":
        return [(PIPELINE, ["import_player_teams.py"]), (MODEL_FEATURES, ["build_players.py"])]
    if name == "injuries":
        return [(PIPELINE, ["snapshot_live.py", "--kind", "injuries"]), (MODEL_FEATURES, ["build_players.py"])]
    raise KeyError(name)


def scheduled_times(name: str, now: dt.datetime, shift: dt.timedelta) -> list:
    """The job's times today and yesterday (local, naive), shifted, oldest first."""
    times = []
    for day in (now.date() - dt.timedelta(days=1), now.date()):
        for hhmm in JOBS[name]["times"]:
            hour, minute = map(int, hhmm.split(":"))
            times.append(dt.datetime.combine(day, dt.time(hour, minute)) + shift)
    return sorted(times)


def due(name: str, now: dt.datetime, last: dict, shift=dt.timedelta(0)):
    """The latest scheduled time that has passed and the job has not run for, or None."""
    passed = [t for t in scheduled_times(name, now, shift) if t <= now]
    if not passed:
        return None
    latest = passed[-1]
    ran = last.get(name)
    return latest if ran is None or dt.datetime.fromisoformat(ran) < latest else None


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(STATE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


def run(name: str, day: dt.date, echo, db_server: str | None = None) -> None:
    """The job's steps in order, each a child process; its output appended to the job's log, its
    last useful line echoed. A failing step stops the job and raises with its last lines.
    `db_server` points the steps at a database (NHLSTATS_DB_SERVER) other than the server's own: the
    trial's, whose ids differ from the PC's, so it is never copied back. The server's own database,
    when not the PC's, is (the cutover, 2026-10-15)."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    own = os.environ.get("NHLSTATS_DB_SERVER")
    copy_from = own if not db_server and own and own != PC_DB else None
    if db_server:
        env["NHLSTATS_DB_SERVER"] = db_server
    LOGS.mkdir(parents=True, exist_ok=True)
    log_name = JOBS[name]["log"].replace(".log", ".container.log") if db_server else JOBS[name]["log"]
    with open(LOGS / log_name, "a", encoding="utf-8") as log:
        log.write(f"\n===== {dt.datetime.now():%Y-%m-%d %H:%M:%S} {name} (plan server"
                  f"{', database ' + db_server if db_server else ''}) =====\n")
        for folder, command in steps(name, day, copy_from):
            done = subprocess.run([sys.executable, *command], cwd=folder, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=env)
            output = done.stdout + done.stderr
            log.write(output)
            log.flush()
            lines = [line for line in output.splitlines() if line.strip()]
            if done.returncode != 0:
                raise RuntimeError(f"{command[0]} failed:\n" + "\n".join(lines[-8:]))
            useful = [line for line in lines if " INFO " in line or " WARNING " in line]
            last = (useful or lines or ["done"])[-1].split(" INFO ")[-1].split(" WARNING ")[-1]
            echo(f"{command[0]}: {last[:200]}")

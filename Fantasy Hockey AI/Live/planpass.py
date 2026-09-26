"""One plan pass, as steps both front ends call: run_live.py (the terminal) and plan_gui.py (the
window). Nothing here is scheduled -- a pass runs when one of them asks.

    snapshots(kinds)   fresh injury / line-chart / starting-goalie reports into the Live schema
                       (pipeline/snapshot_live.py -- the same job Task Scheduler runs)
    tonight(day)       tonight's rows and projections (ModelFeatures/build_tonight.py, then
                       Projections/project_tonight.py)
    read_league(...)   the league as its platform shows it now (or a snapshot file)
    plan(...)          the shipped manager's plan (live.LiveRunner)
    save(...)          reports/<league>/plans/plan_{date}_{stem}.md + .json + plan_latest.md

Each step reports progress through `echo` (print by default; the window passes its log pane).
"""

import datetime as dt
import json
import os
import shutil
import subprocess
import sys

import pandas as pd

import seasonlayer  # noqa: F401 -- puts Season/ on sys.path; see seasonlayer.py
import live
import livepaths
import paths
import platforms

WINDOW_LEAD = dt.timedelta(minutes=30)
MODEL_FEATURES = paths.SIBLINGS / "ModelFeatures"
PIPELINE = paths.SIBLINGS.parent / "pipeline"
SNAPSHOT_KINDS = ("injuries", "lines", "goalies")


def _run(command, cwd, echo, label):
    """A sibling folder's script, its last line echoed; a failure raises with its stderr tail."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    done = subprocess.run([sys.executable, *command], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    lines = [l for l in (done.stdout + done.stderr).splitlines() if l.strip()]
    if done.returncode != 0:
        raise RuntimeError(f"{label} failed:\n" + "\n".join(lines[-8:]))
    useful = [l for l in lines if " INFO " in l or " WARNING " in l]
    echo(f"{label}: {(useful or lines or ['done'])[-1].split(' INFO ')[-1].split(' WARNING ')[-1]}")


def snapshots(kinds=SNAPSHOT_KINDS, echo=print, force_goalies=False) -> None:
    for kind in kinds:
        command = ["snapshot_live.py", "--kind", kind] + (["--force"] if kind == "goalies" and force_goalies else [])
        _run(command, PIPELINE, echo, f"snapshot {kind}")


class NoGames(Exception):
    """No regular-season NHL games on the day: nothing to plan."""


def tonight(day: dt.date, echo=print) -> None:
    _run(["build_tonight.py", "--date", day.isoformat()], MODEL_FEATURES, echo, "tonight's rows")
    if not (live.STATUS_DIR / f"tonight_{day.isoformat()}_skaters.parquet").exists():
        raise NoGames(f"no regular-season NHL games on {day.isoformat()} -- nothing to plan")
    _run(["project_tonight.py", "--date", day.isoformat()], paths.PROJECTIONS_DIR, echo, "projections")


def puck_times(day: dt.date) -> list:
    """Tonight's distinct puck times (UTC), from the schedule the tonight build read."""
    context = live.STATUS_DIR / f"tonight_{day.isoformat()}_context.parquet"
    if not context.exists():
        return []
    return sorted(set(pd.to_datetime(pd.read_parquet(context)["start_time_utc"]).dropna()))


def next_window(day: dt.date, now: dt.datetime):
    """The next puck time today if it is within WINDOW_LEAD of `now`, else None."""
    upcoming = [t for t in puck_times(day) if t > pd.Timestamp(now)]
    if not upcoming or upcoming[0] - pd.Timestamp(now) > WINDOW_LEAD:
        return None
    return upcoming[0]


def read_league(league, day: dt.date, league_file=None, platform_season=None, echo=print):
    if league_file:
        snapshot = live.LeagueSnapshot.load(league_file)
    else:
        adapter = platforms.for_league(league, season=platform_season)
        if adapter is None:
            raise SystemExit(f"{league.name} has no readable platform: pass --league-file")
        snapshot = live.LeagueSnapshot.from_platform(adapter, league.team_id, day)
    echo(f"league: {snapshot.source}, {len(snapshot.teams)} teams, "
         f"{len(snapshot.teams[snapshot.me]['roster'])} on my roster")
    return snapshot


def plan(runner, snapshot, now: dt.datetime, echo=print) -> dict:
    result = runner.plan(snapshot, now)
    echo(f"plan: {len(result['moves'])} move(s), {sum(1 for s in result['lineup'] if s['player'])} "
         f"in tonight's lineup, P(win) {result['p_win']:.0%}")
    return result


def save(league, result: dict, day: dt.date, stem: str, echo=print):
    plans_dir = livepaths.ensure(livepaths.league_reports(league.name) / "plans")
    path = plans_dir / f"plan_{day.isoformat()}_{stem}.md"
    path.write_text(live.render(result), encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    shutil.copyfile(path, plans_dir / "plan_latest.md")
    echo(f"saved {path.name}")
    return path

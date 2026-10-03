#!/usr/bin/env python
"""Times each step the server runs and its peak memory (Fantasy Hockey Phone App Plan, Phase 0's
table, run again where the server runs: the container, then the Azure VM). Linux only (resource).

    python measure_steps.py                  # the plan steps, then the nightly ingest
    python measure_steps.py --skip-nightly
    python measure_steps.py --game-id 2026020016    # also re-ingest one game (an upsert) to time it

Each step runs in its own process, as in a refresh, and reports its own peak resident memory
(itself and anything it starts). The snapshots run with --dry-run (fetched, rolled back); the
other steps write what a refresh or the nightly writes, plus build_tonight for 2026-03-14 (the
late-season replay Phase 0 timed). The league read and the plan run in one process: the board and
sampler build, the read, the plan -- nothing saved.
"""

import argparse
import datetime as dt
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "pipeline"
AI = ROOT / "Fantasy Hockey AI"
FEATURES, PROJECTIONS, LIVE = AI / "ModelFeatures", AI / "Projections", AI / "Live"
LATE_SEASON = "2026-03-14"
SEASON = "2026-27"

# Runs a script as __main__ and prints its time and peak memory on the last line.
WRAP = """
import resource, runpy, sys, time
start = time.perf_counter()
sys.argv = sys.argv[1:]
code = 0
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit as exit:
    code = exit.code if isinstance(exit.code, int) else (0 if exit.code is None else 1)
finally:
    peak = max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
               resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    print(f"@@measure {time.perf_counter() - start:.2f} {peak}", flush=True)
sys.exit(code)
"""

# The league read and the plan, timed part by part (Live/ on the path, as plan steps run).
PLAN = """
import datetime as dt, sys, time
sys.path.insert(0, "")
import seasonlayer, leagues, live, planpass
league, day = leagues.load(leagues.DEFAULT_LEAGUE), dt.date.today()
now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
t = time.perf_counter(); runner = live.LiveRunner(day, league)
print(f"@@part board+sampler {time.perf_counter() - t:.2f}")
t = time.perf_counter(); snapshot = planpass.read_league(league, day, now=now)
print(f"@@part league read {time.perf_counter() - t:.2f}")
t = time.perf_counter(); planpass.plan(runner, snapshot, now)
print(f"@@part plan {time.perf_counter() - t:.2f}")
"""


def steps(today: str, game_id, nightly: bool):
    yield "injury snapshot (targeted)", PIPELINE, ["snapshot_live.py", "--kind", "injuries", "--targeted", "--dry-run"]
    yield "line charts", PIPELINE, ["snapshot_live.py", "--kind", "lines", "--dry-run"]
    yield "goalie snapshot", PIPELINE, ["snapshot_live.py", "--kind", "goalies", "--dry-run"]
    yield "build_players", FEATURES, ["build_players.py"]
    yield "build_tonight, today", FEATURES, ["build_tonight.py", "--date", today]
    yield f"build_tonight, late season ({LATE_SEASON})", FEATURES, ["build_tonight.py", "--date", LATE_SEASON]
    yield "goalie_workload", PROJECTIONS, ["goalie_workload.py", "--build", "--season", SEASON, "--date", today]
    yield "project_tonight", PROJECTIONS, ["project_tonight.py", "--date", today]
    yield "league read + plan", LIVE, None
    if game_id:
        yield f"re-ingest game {game_id}", PIPELINE, ["run_daily.py", "--game-id", str(game_id)]
    if nightly:
        yield "nightly: run_daily (incremental)", PIPELINE, ["run_daily.py"]
        yield "nightly: build_lineup_features", FEATURES, ["build_lineup_features.py", "--season", SEASON, "--variant", "A"]
        yield "nightly: build_goalie_starts", FEATURES, ["build_goalie_starts.py", "--season", SEASON]
        yield "nightly: build_season_stats", FEATURES, ["build_season_stats.py", "--season", SEASON]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-nightly", action="store_true", help="Only the plan steps")
    parser.add_argument("--game-id", type=int, default=None, help="Also re-ingest this game (an upsert)")
    args = parser.parse_args()
    today = dt.date.today().isoformat()
    rows, failed = [], []
    for label, cwd, command in steps(today, args.game_id, not args.skip_nightly):
        if command is None:                 # the plan has no script of its own: a temporary one
            snippet = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
            snippet.write(PLAN)
            snippet.close()
            command = [snippet.name]
        start = time.perf_counter()
        done = subprocess.run([sys.executable, "-c", WRAP] + command, cwd=cwd, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        wall = time.perf_counter() - start
        lines = (done.stdout + done.stderr).splitlines()
        marks = [l.split() for l in lines if l.startswith("@@measure")]
        peak_mb = int(marks[-1][2]) / 1024 if marks else float("nan")
        rows.append((label, wall, peak_mb, done.returncode))
        print(f"{label:<44} {wall:7.1f} s  {peak_mb:6.0f} MB  exit {done.returncode}", flush=True)
        for line in lines:
            if line.startswith("@@part"):
                print(f"    {line[7:]} s")
        if done.returncode != 0:
            failed.append(label)
            print("    " + "\n    ".join(l for l in lines[-8:] if not l.startswith("@@")))
    print(f"\npeak of any one step: {max(r[2] for r in rows):.0f} MB; "
          f"{len(failed)} failed{': ' + ', '.join(failed) if failed else ''}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

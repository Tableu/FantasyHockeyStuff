#!/usr/bin/env python
"""Writes today's plan for my team: IR moves, adds and drops, claims, and tonight's lineup
(live.py). Recommend-only -- the plan is a file; make the moves on Fleaflicker.

    reports/live/plan_{date}_{HHMM}.md    the plan (and .json beside it)
    reports/live/plan_latest.md            a copy of the newest one

Before the draft there are no rosters, so `--make-fake` writes a made-up league (every seat
drafting the consensus board) to exercise the runner against:

    python run_live.py --make-fake                       # -> live/fake_league.json
    python run_live.py --date 2026-09-29 --league-file live/fake_league.json --refresh
    python run_live.py --date 2026-09-29 --league-file live/fake_league.json --now "2026-09-29 23:30"

`--refresh` rebuilds tonight's rows and projections first (ModelFeatures/build_tonight.py, then
Projections/project_tonight.py). `--now` (UTC) sets the moment the per-game lock is judged at.

`--window` is the scheduled mode for the per-game lock: it plans only when a group of games starts
within WINDOW_LEAD of now and that group has no plan yet (plan_{date}_w{HHMM}.md, the puck time),
so Task Scheduler can call it every 15 minutes and each window is planned once, fresh:

    python run_live.py --window --refresh --league-file live/league.json
"""

import argparse
import datetime as dt
import json
import logging
import shutil
import subprocess
import sys

import paths
import live

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("run_live")

FAKE_LEAGUE = paths.PROJECT_ROOT / "live" / "fake_league.json"
WINDOW_LEAD = dt.timedelta(minutes=30)
MODEL_FEATURES = paths.SIBLINGS / "ModelFeatures"


def refresh(day: dt.date) -> None:
    for folder, script in ((MODEL_FEATURES, "build_tonight.py"), (paths.PROJECTIONS_DIR, "project_tonight.py")):
        subprocess.run([sys.executable, script, "--date", day.isoformat()], cwd=folder, check=True)


def next_window(day: dt.date, now: dt.datetime):
    """The next puck time today if it is within WINDOW_LEAD of `now`, else None. Puck times come
    from the schedule the tonight build read (Reference.Schedule, via its context file)."""
    import pandas as pd
    context = live.STATUS_DIR / f"tonight_{day.isoformat()}_context.parquet"
    if not context.exists():
        refresh(day)
    if not context.exists():
        return None
    starts = sorted(set(pd.to_datetime(pd.read_parquet(context)["start_time_utc"]).dropna()))
    upcoming = [t for t in starts if t > pd.Timestamp(now)]
    if not upcoming or upcoming[0] - pd.Timestamp(now) > WINDOW_LEAD:
        return None
    return upcoming[0]


def main():
    parser = argparse.ArgumentParser(description="Write today's plan for my team")
    parser.add_argument("--date", default=None, help="game date (default: today on this PC)")
    parser.add_argument("--league-file", default=None, help="a league snapshot JSON (before the draft: the fake one)")
    parser.add_argument("--make-fake", action="store_true", help="write a made-up league to live/fake_league.json")
    parser.add_argument("--me", type=int, default=9, help="--make-fake: my seat (0-based; draft slot 10 = 9)")
    parser.add_argument("--opponent", type=int, default=0, help="--make-fake: this week's opponent seat")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--refresh", action="store_true", help="rebuild tonight's rows and projections first")
    parser.add_argument("--now", default=None, help="UTC moment for the per-game lock (default: now)")
    parser.add_argument("--window", action="store_true", help="plan only if a puck window opens within 30 min")
    args = parser.parse_args()
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    now = (dt.datetime.fromisoformat(args.now) if args.now
           else dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0))

    stem = f"plan_{day.isoformat()}_{now:%H%M}"
    if args.window:
        window = next_window(day, now)
        if window is None:
            log.info("no game starts within %s of %s UTC -- nothing to plan", WINDOW_LEAD, f"{now:%H:%M}")
            return
        stem = f"plan_{day.isoformat()}_w{window:%H%M}"
        if (live.LIVE_DIR / f"{stem}.md").exists():
            log.info("the %s UTC window is already planned (%s.md)", f"{window:%H:%M}", stem)
            return
    if args.refresh:
        refresh(day)
    runner = live.LiveRunner(day)

    if args.make_fake:
        fake = live.make_fake_league(runner.board, runner.config, runner.eligibility, args.me, args.opponent, args.seed)
        paths.ensure(FAKE_LEAGUE.parent)
        FAKE_LEAGUE.write_text(json.dumps(fake, indent=1), encoding="utf-8")
        log.info("wrote %s (%d teams x %d players)", FAKE_LEAGUE, len(fake["teams"]), len(fake["teams"][0]["roster"]))
        if not args.league_file:
            return

    if not args.league_file:
        raise SystemExit("no league snapshot: pass --league-file (Fleaflicker rosters are read once the league has drafted)")
    snapshot = live.LeagueSnapshot.load(args.league_file)
    plan = live.LiveRunner.plan(runner, snapshot, now)

    paths.ensure(live.LIVE_DIR)
    markdown = live.render(plan)
    (live.LIVE_DIR / f"{stem}.md").write_text(markdown, encoding="utf-8")
    (live.LIVE_DIR / f"{stem}.json").write_text(json.dumps(plan, indent=1, default=str), encoding="utf-8")
    shutil.copyfile(live.LIVE_DIR / f"{stem}.md", live.LIVE_DIR / "plan_latest.md")
    log.info("plan -> %s", live.LIVE_DIR / f"{stem}.md")


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Writes today's plan for my team: IR moves, adds and drops, claims, and tonight's lineup
(live.py) for a registry league (`--league`, Settings/leagues/). Recommend-only -- the plan is a file; make the moves on Fleaflicker.

    reports/plans/<league>/plan_{date}_{HHMM}.md    the plan (and .json beside it)
    reports/plans/<league>/plan_latest.md            a copy of the newest one

Before the draft there are no rosters, so `--make-fake` writes a made-up league (every seat
drafting the consensus board) to exercise the runner against:

    python run_live.py --make-fake                       # -> fixtures/beagles/fake_league.json
    python run_live.py --date 2026-09-29 --league-file fixtures/beagles/fake_league.json --refresh
    python run_live.py --date 2026-09-29 --league-file fixtures/beagles/fake_league.json --now "2026-09-29 23:30"

`--refresh` rebuilds tonight's rows and projections first (ModelFeatures/build_tonight.py, then
Projections/project_tonight.py). `--now` (UTC) sets the moment the per-game lock is judged at.

`--window` is the scheduled mode for the per-game lock: it plans only when a group of games starts
within WINDOW_LEAD of now and that group has no plan yet (plan_{date}_w{HHMM}.md, the puck time),
so Task Scheduler can call it every 15 minutes and each window is planned once, fresh:

    python run_live.py --league beagles --window --refresh      # reads the league from Fleaflicker
"""

import argparse
import datetime as dt
import json
import logging
import shutil
import subprocess
import sys

import seasonlayer  # noqa: F401 -- puts Season/ on sys.path; see seasonlayer.py
import leagues
import livepaths
import platforms
import paths
import live

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("run_live")

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
    parser.add_argument("--league", default=leagues.DEFAULT_LEAGUE,
                        help="A league in Settings/leagues/ (default %(default)s)")
    parser.add_argument("--date", default=None, help="game date (default: today on this PC)")
    parser.add_argument("--league-file", default=None, help="a league snapshot JSON (before the draft: the fake one)")
    parser.add_argument("--make-fake", action="store_true", help="write a made-up league to fixtures/<league>/fake_league.json")
    parser.add_argument("--me", type=int, default=9, help="--make-fake: my seat (0-based; draft slot 10 = 9)")
    parser.add_argument("--opponent", type=int, default=0, help="--make-fake: this week's opponent seat")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--refresh", action="store_true", help="rebuild tonight's rows and projections first")
    parser.add_argument("--now", default=None, help="UTC moment for the per-game lock (default: now)")
    parser.add_argument("--window", action="store_true", help="plan only if a puck window opens within 30 min")
    parser.add_argument("--platform-season", type=int, default=None,
                        help="read the league as it stood in a past season (e.g. 2025) -- a rehearsal")
    args = parser.parse_args()
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    now = (dt.datetime.fromisoformat(args.now) if args.now
           else dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0))

    league = leagues.load(args.league)
    plans_dir = live.PLANS_DIR / league.name
    fake_league = livepaths.FIXTURES_DIR / league.name / "fake_league.json"
    stem = f"plan_{day.isoformat()}_{now:%H%M}"
    if args.window:
        window = next_window(day, now)
        if window is None:
            log.info("no game starts within %s of %s UTC -- nothing to plan", WINDOW_LEAD, f"{now:%H:%M}")
            return
        stem = f"plan_{day.isoformat()}_w{window:%H%M}"
        if (plans_dir / f"{stem}.md").exists():
            log.info("the %s UTC window is already planned (%s.md)", f"{window:%H:%M}", stem)
            return
    if args.refresh:
        refresh(day)
    runner = live.LiveRunner(day, league)

    if args.make_fake:
        fake = live.make_fake_league(runner.board, runner.config, runner.eligibility, args.me, args.opponent,
                                     args.seed, my_name=league.team_name or "My team")
        paths.ensure(fake_league.parent)
        fake_league.write_text(json.dumps(fake, indent=1), encoding="utf-8")
        log.info("wrote %s (%d teams x %d players)", fake_league, len(fake["teams"]), len(fake["teams"][0]["roster"]))
        if not args.league_file:
            return

    if args.league_file:
        snapshot = live.LeagueSnapshot.load(args.league_file)
    else:
        adapter = platforms.for_league(league, season=args.platform_season)
        if adapter is None:
            raise SystemExit(f"{league.name} has no readable platform: pass --league-file")
        snapshot = live.LeagueSnapshot.from_platform(adapter, league.team_id, day)
    plan = live.LiveRunner.plan(runner, snapshot, now)

    paths.ensure(plans_dir)
    markdown = live.render(plan)
    (plans_dir / f"{stem}.md").write_text(markdown, encoding="utf-8")
    (plans_dir / f"{stem}.json").write_text(json.dumps(plan, indent=1, default=str), encoding="utf-8")
    shutil.copyfile(plans_dir / f"{stem}.md", plans_dir / "plan_latest.md")
    log.info("plan -> %s", plans_dir / f"{stem}.md")


if __name__ == "__main__":
    main()

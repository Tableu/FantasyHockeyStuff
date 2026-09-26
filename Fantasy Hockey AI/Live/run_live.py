#!/usr/bin/env python
"""Writes today's plan for my team: IR moves, adds and drops, claims, and tonight's lineup
(live.py) for a registry league (`--league`, Settings/leagues/). Recommend-only -- the plan is a file; make the moves on Fleaflicker.

    reports/<league>/plans/plan_{date}_{HHMM}.md    the plan (and .json beside it)
    reports/<league>/plans/plan_latest.md            a copy of the newest one

Before the draft there are no rosters, so `--make-fake` writes a made-up league (every seat
drafting the consensus board) to exercise the runner against:

    python run_live.py --make-fake                       # -> fixtures/beagles/fake_league.json
    python run_live.py --date 2026-09-29 --league-file fixtures/beagles/fake_league.json --refresh
    python run_live.py --date 2026-09-29 --league-file fixtures/beagles/fake_league.json --now "2026-09-29 23:30"

`--refresh` rebuilds tonight's rows and projections first (ModelFeatures/build_tonight.py, then
Projections/project_tonight.py). `--now` (UTC) sets the moment the per-game lock is judged at.

`--window` plans only when a group of games starts within 30 minutes of now and that group has no
plan yet (plan_{date}_w{HHMM}.md, the puck time). The window version of all this is plan_gui.py,
which runs these same steps (planpass.py) when it is opened and while it stays open.
"""

import argparse
import datetime as dt
import json
import logging

import seasonlayer  # noqa: F401 -- puts Season/ on sys.path; see seasonlayer.py
import leagues
import livepaths
import paths
import live
import planpass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("run_live")


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
    echo = log.info

    league = leagues.load(args.league)
    plans_dir = livepaths.league_reports(league.name) / "plans"
    fake_league = livepaths.FIXTURES_DIR / league.name / "fake_league.json"
    stem = f"{now:%H%M}"
    if args.window:
        if not planpass.puck_times(day):
            planpass.tonight(day, echo)
        window = planpass.next_window(day, now)
        if window is None:
            log.info("no game starts within %s of %s UTC -- nothing to plan", planpass.WINDOW_LEAD, f"{now:%H:%M}")
            return
        stem = f"w{window:%H%M}"
        if (plans_dir / f"plan_{day.isoformat()}_{stem}.md").exists():
            log.info("the %s UTC window is already planned", f"{window:%H:%M}")
            return
    if args.refresh:
        planpass.tonight(day, echo)
    runner = live.LiveRunner(day, league)

    if args.make_fake:
        fake = live.make_fake_league(runner.board, runner.config, runner.eligibility, args.me, args.opponent,
                                     args.seed, my_name=league.team_name or "My team")
        paths.ensure(fake_league.parent)
        fake_league.write_text(json.dumps(fake, indent=1), encoding="utf-8")
        log.info("wrote %s (%d teams x %d players)", fake_league, len(fake["teams"]), len(fake["teams"][0]["roster"]))
        if not args.league_file:
            return

    snapshot = planpass.read_league(league, day, args.league_file, args.platform_season, echo)
    result = planpass.plan(runner, snapshot, now, echo)
    planpass.save(league, result, day, stem, echo)


if __name__ == "__main__":
    try:
        main()
    except planpass.NoGames as error:
        log.info("%s", error)

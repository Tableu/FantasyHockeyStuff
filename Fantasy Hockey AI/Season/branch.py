#!/usr/bin/env python
"""Short paired runs: the candidate plays one week at a time from the shipped league's own state.

    python branch.py --strategy strategy-beagles --set 'streaming.min_gain=0.5' --replications 14
    python branch.py --strategy strategy-beagles --verify --replications 2   # zero in every week

**Why.** A season-long A/B (oneseat.py) lets one different move change the wire, and every later
week drift apart: about 6 points a week of noise per draft, so 56 drafts resolve about +/-0.8.
Most rules worth testing act within a week -- the bar a rental clears, how the week is planned --
and for those the drift is noise. Here the shipped league plays its season as oneseat's shipped
half does; at the start of every regular-season week a copy of the league plays that week with the
candidate's blocks in the test seat (rung 17's), and the test seat's points that week are paired
with the shipped run's. Game outcomes are the real season's box scores, the waiver order is seeded
by (replication, day), and the decision draws come from the copied simulator's own stream, so the
two sides see the same games; what differs is the candidate's decisions, for one week.

**What it cannot see.** Effects beyond the week: permanent upgrades priced over weeks, the roster
a candidate leaves behind, the playoffs, opponents adapting over a season -- and every week starts
from a state the shipped rule made. Screen week-local rules here; confirm a winner with oneseat.py
and the two-season rule.

**The error bar** is computed over drafts (each draft's mean weekly gap), never over weeks: weeks
of one draft share a roster and a wire. --verify branches the shipped blocks themselves and must
score exactly zero in every week -- the check that the copy carries every piece of state.
"""
import argparse
import copy
import dataclasses
import json
import logging
import math
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

import paths
import ladder
import oneseat
import tune
from decisionlayer import managers as managers_module

log = logging.getLogger("branch")

# Read-only for the whole season: shared by the copy, never deep-copied (the season's inputs).
SHARED = ("config", "calendar", "data", "eligibility", "scoreset", "proj_by_day", "nhl_team_by_day",
          "goalies_by_day", "unavailable_by_day", "status_by_day", "ros_by_day", "board_ros",
          "outcomes", "goalie_fit")


def fork(season):
    """A copy of the league as it stands: everything that changes (state, managers, rates, the
    simulator's random stream, the week's points) deep-copied, the season's inputs shared."""
    memo = {id(getattr(season, name)): getattr(season, name) for name in SHARED
            if getattr(season, name, None) is not None}
    twin = copy.deepcopy(season, memo)
    twin.branch = None
    return twin


def with_blocks(manager, candidate):
    """The seat's manager on the candidate's blocks (managers.TUNABLE), everything else its own."""
    manager.strategy = dataclasses.replace(
        manager.strategy, **{k: getattr(candidate, k) for k in managers_module.TUNABLE})
    if hasattr(manager, "plan"):
        manager.plan.stream_params = manager.stream_params
    return manager


def make_hook(seat, candidate, out):
    """The engine's per-week hook: play the week on a copy with the candidate in `seat`."""
    def hook(season, week, schedule, history):
        out["season"] = season
        twin = fork(season)
        twin.field[seat] = with_blocks(twin.field[seat], candidate)
        for day in season.calendar.days_in(week):
            twin._play_day(day, week, schedule, history)
        out["branch"][week] = twin.weekly[week][seat]
    return hook


_WORKER = {}


def _init(payload):
    _WORKER.update(payload)


def play(replication):
    """One draft: the shipped season with a branch every regular-season week. Returns
    [(week, shipped points, candidate points)] for the test seat."""
    w = _WORKER
    layout = w["layout"]
    seat = layout.test_seat(w["config"], replication)
    out = {"branch": {}}
    ladder.run_one(w["config"], w["calendar"], w["data"], w["eligibility"], w["scoreset"],
                   layout, replication, False, w["decision_sims"], w["shipped"], None,
                   branch=make_hook(seat, w["candidate"], out))
    weekly = out["season"].weekly
    return [(week, weekly[week][seat], points) for week, points in sorted(out["branch"].items())]


def stats(values):
    v = np.asarray(values, dtype=float)
    return float(v.mean()), (float(v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 1 else math.nan)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--season", default="2024-25")
    p.add_argument("--prior-season", default=None)
    p.add_argument("--league", default="league")
    p.add_argument("--weights", default="points-league")
    p.add_argument("--strategy", required=True)
    p.add_argument("--candidate", default=None)
    p.add_argument("--set", action="append", default=[], metavar="BLOCK.KEY=VALUE")
    p.add_argument("--opponents", default="8")
    p.add_argument("--opponent-sd", type=float, default=None)
    p.add_argument("--ros-tag", default=None)
    p.add_argument("--replications", type=int, default=14)
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--verify", action="store_true",
                   help="Branch the shipped blocks themselves: every week must score exactly zero")
    p.add_argument("--out", default=None)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stderr)
    for noisy in ("inputs", "draft", "draftroom", "engine", "simlayer", "ladder", "schedule"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    setup_args = argparse.Namespace(**{**vars(args), "out": None, "verify": False})
    ctx, shipped, candidate, base_layout, _, _ = oneseat.setup(setup_args)
    if args.verify:
        candidate = shipped
    elif tune.params_of(candidate) == tune.params_of(shipped):
        raise SystemExit("the candidate equals the shipped system; use --verify for that check")
    if args.replications % ctx.config.teams:
        log.warning("%d drafts is not a multiple of the league's %d teams", args.replications,
                    ctx.config.teams)
    log.info("shipped: %s", tune.label(shipped))
    log.info("candidate: %s", tune.label(candidate))
    payload = {"config": ctx.config, "calendar": ctx.calendar, "data": ctx.data,
               "eligibility": ctx.eligibility, "scoreset": ctx.scoreset, "layout": base_layout,
               "decision_sims": 200, "shipped": shipped, "candidate": candidate}
    workers = args.workers or ladder.default_workers(args.replications)
    if sys.path[0] != str(paths.PROJECT_ROOT):
        sys.path.insert(0, str(paths.PROJECT_ROOT))
    with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(payload,)) as pool:
        drafts = list(pool.map(play, range(args.replications)))

    per_draft = [float(np.mean([b - s for _, s, b in d])) for d in drafts]
    weeks = [b - s for d in drafts for _, s, b in d]
    if args.verify:
        nonzero = sum(1 for x in weeks if x != 0.0)
        print(f"verify: {len(weeks)} branched weeks over {len(drafts)} drafts, {nonzero} not exactly "
              f"zero -> {'OK' if nonzero == 0 else 'THE COPY MISSES STATE'}")
        return
    mean, se = stats(per_draft)
    week_sd = float(np.std(weeks, ddof=1))
    print(f"candidate - shipped, test seat, one week at a time: {mean:+.2f} +/- {se:.2f} pts/wk "
          f"({len(drafts)} drafts, {len(weeks)} weeks)")
    print(f"weeks changed: {sum(1 for x in weeks if x != 0.0)} of {len(weeks)}; per-week sd "
          f"{week_sd:.2f}, per-draft sd {float(np.std(per_draft, ddof=1)):.2f}")
    if len(per_draft) >= 4:
        half = len(per_draft) // 2
        a, ase = stats(per_draft[:half])
        b, bse = stats(per_draft[half:])
        print(f"halves: drafts 1-{half} {a:+.2f} +/- {ase:.2f}, {half + 1}-{len(per_draft)} "
              f"{b:+.2f} +/- {bse:.2f}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({"label": tune.label(candidate), "drafts": drafts, "per_draft": per_draft}, handle)


if __name__ == "__main__":
    main()

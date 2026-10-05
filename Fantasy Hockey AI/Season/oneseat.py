#!/usr/bin/env python
"""Realistic league mode, part 1 (T1): one test seat among thirteen opponents.

    python oneseat.py --strategy strategy-beagles --set streaming.spots=2 --replications 32
    python oneseat.py --strategy strategy --candidate strategy-beagles --replications 32
    python oneseat.py --verify                 # the pairing checks, at 2 drafts
    python oneseat.py --opponents 2,5,6 ...    # the old passive field instead of rung 8

**Why.** The ladder and `tune.py` seat our system in three or four of the fourteen seats and fill
the rest with other rungs of our own code, so copies of us contest the same free agents: the
rung 4 vs 3 diagnosis showed one rung mimicking another costing that other 7.5 points a week. In
12090 we are one team. Here exactly one seat runs the system under test.

**The design.** Every draft is played twice with the same thirteen opponents: once with the
shipped system (rung 17) in the test seat, once with the candidate (rung 27: rung 17 on the
candidate's add/drop and streaming blocks). The draft does not depend on in-season parameters,
so both runs have the same rosters, and the candidate's score is its seat's points a week minus
the shipped seat's, per draft. Opponents are identical draw for draw -- same rungs, same seats,
same draft boards; what they do in-season may differ only through the shared wire (the "spill").

**The test seat rotates through draft positions**: each block of `teams` drafts gives it every
draft position once, in a seeded order, on that replication's draft lottery.

**Opponents** default to rung 8 (T2): our orchestrator on a real 12088 manager's weekly move
budget, calibrated to that league's strength (`opponents.py`, `Decisions/managers.Opponent`). Any
rungs may be given instead (`--opponents 2,5,6` is tune.py's field without rung 17), interleaved
over the thirteen seats and rotated by replication.

The two-season rule applies: decide on 2024-25, confirm once on 2025-26.
"""

import argparse
import dataclasses
import json
import logging
import math
import random
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

import draftroom
import paths
import tune
from decisionlayer import load_strategy
from decisionlayer import managers as managers_module

log = logging.getLogger("oneseat")
SHIPPED, CAND = tune.SHIPPED, tune.CAND
DEFAULT_OPPONENTS = (8,)
POSITION_SEED = 5150


@dataclass(frozen=True)
class OneSeat:
    """A seat layout for `managers.build_field`: `test` in one seat, `opponents` in the rest."""

    test: int
    opponents: tuple

    def test_seat(self, config, replication) -> int:
        # The same lottery the engine draws: its block is the number of distinct rungs seated.
        order = draftroom.seat_order(config, replication, block=len({self.test, *self.opponents}))
        positions = list(range(config.teams))
        random.Random(POSITION_SEED + replication // config.teams).shuffle(positions)
        return order[positions[replication % config.teams]]

    def labels(self, config, replication) -> list:
        mine = self.test_seat(config, replication)
        others = [s for s in range(config.teams) if s != mine]
        labels = [None] * config.teams
        labels[mine] = self.test
        for i, seat in enumerate(others):
            labels[seat] = self.opponents[(i + replication) % len(self.opponents)]
        return labels


def layouts(opponents):
    """The shipped and candidate layouts. The same number of distinct rungs, so the same lottery."""
    import oneseat as module         # not __main__'s copy: worker processes unpickle it by name

    if SHIPPED in opponents or CAND in opponents:
        raise SystemExit(f"opponents may not include rung {SHIPPED} or {CAND}: one seat is ours")
    return module.OneSeat(SHIPPED, tuple(opponents)), module.OneSeat(CAND, tuple(opponents))


def check_layouts(config, base, cand, replications):
    """The pair differ in the test seat's rung and nothing else, and it moves around the draft."""
    positions = []
    for r in range(replications):
        a, b = base.labels(config, r), cand.labels(config, r)
        diff = [s for s in range(config.teams) if a[s] != b[s]]
        seat = base.test_seat(config, r)
        if diff != [seat] or a[seat] != SHIPPED or b[seat] != CAND:
            raise AssertionError(f"replication {r}: layouts differ at {diff}, test seat {seat}")
        if a.count(SHIPPED) != 1:
            raise AssertionError(f"replication {r}: {a.count(SHIPPED)} shipped seats")
        order = draftroom.seat_order(config, r, block=len(set(a)))
        positions.append(order.index(seat))
    return positions


def seat_table(ctx, layout, candidate, replications):
    table = ctx._seats(layout, candidate, replications)
    seats = {r: layout.test_seat(ctx.config, r) for r in range(replications)}
    table["test"] = [seats[r] == s for r, s in table.index]
    return table


def compare(base, alt, replications):
    mine = base.index[base["test"]]
    if not (alt.loc[mine, "test"]).all():
        raise AssertionError("the candidate's seat is not the shipped seat")
    if not ((base.loc[mine, "rung"] == SHIPPED).all() and (alt.loc[mine, "rung"] == CAND).all()):
        raise AssertionError("test seats are not rung 17 / rung 27")
    rest = base.index[~base["test"]]
    if not (base.loc[rest, "rung"] == alt.loc[rest, "rung"]).all():
        raise AssertionError("opponents differ between the pair")
    gap = {m: (alt.loc[mine, m] - base.loc[mine, m]).groupby(level="replication").mean()
           for m in ("pts", "win", "playoff")}
    spill = (alt.loc[rest, "pts"] - base.loc[rest, "pts"]).groupby(level="replication").mean()
    return {"gap": {m: v.tolist() for m, v in gap.items()}, "spill": spill.tolist(),
            "shipped_pts": base.loc[mine, "pts"].tolist(),
            "shipped_moves": float(base.loc[mine, "moves_spent"].mean()),
            "candidate_moves": float(alt.loc[mine, "moves_spent"].mean()),
            "opponent_moves": float(base.loc[rest, "moves_spent"].mean()),
            "replications": replications}


def stats(values):
    v = np.asarray(values, dtype=float)
    return float(v.mean()), (float(v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 1 else math.nan)


def summary(result) -> str:
    n = result["replications"]
    pts, se = stats(result["gap"]["pts"])
    win, wse = stats(result["gap"]["win"])
    spill, sse = stats(result["spill"])
    lines = [f"candidate - shipped, test seat: {pts:+.2f} ± {se:.2f} pts/wk, "
             f"win rate {win:+.3f} ± {wse:.3f} ({n} drafts)",
             f"opponents' points moved {spill:+.2f} ± {sse:.2f} pts/wk each (spill)"]
    if n >= 4:
        half = n // 2
        a, ase = stats(result["gap"]["pts"][:half])
        b, bse = stats(result["gap"]["pts"][half:])
        lines.append(f"halves: drafts 1-{half} {a:+.2f} ± {ase:.2f}, "
                     f"{half + 1}-{n} {b:+.2f} ± {bse:.2f}")
    sd = float(np.std(result["gap"]["pts"], ddof=1)) if n > 1 else math.nan
    if sd == sd and sd > 0:
        need = {t: math.ceil((sd / t) ** 2) for t in (1.0, 0.5)}
        lines.append(f"per-draft sd {sd:.2f}: about {need[1.0]} drafts for ±1.0, "
                     f"{need[0.5]} for ±0.5 (one standard error)")
    lines.append(f"moves a season: shipped {result['shipped_moves']:.0f}, candidate "
                 f"{result['candidate_moves']:.0f}, opponents {result['opponent_moves']:.0f}")
    return "\n".join(lines)


def with_settings(base, settings):
    """`block.key=value` overrides of the add/drop and streaming blocks, and the roster block
    (repair_wait_days, activation_drop, fill_check)."""
    changes = {"adddrop": {}, "streaming": {}, "roster": {}}
    for item in settings:
        key, _, raw = item.partition("=")
        block, _, name = key.partition(".")
        if block not in changes or not name or not raw:
            raise SystemExit(f"--set {item!r}: use adddrop.KEY, streaming.KEY or roster.KEY =VALUE")
        current = getattr(base, name) if block == "roster" else getattr(getattr(base, block), name)
        value = json.loads(raw) if raw not in ("inf", "none") else (
            math.inf if raw == "inf" else None)
        if isinstance(current, float) and isinstance(value, int):
            value = float(value)
        changes[block][name] = value
    return tune.with_(base, **changes)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--season", default="2024-25")
    p.add_argument("--prior-season", default=None)
    p.add_argument("--league", default="league")
    p.add_argument("--weights", default="points-league")
    p.add_argument("--strategy", default=None, help="The shipped system's strategy file")
    p.add_argument("--candidate", default=None,
                   help="A strategy file for the candidate (differing only in add/drop and "
                        "streaming)")
    p.add_argument("--set", action="append", default=[], metavar="BLOCK.KEY=VALUE",
                   help="Candidate override on top of --candidate or --strategy (repeatable); "
                        "roster.repair_wait_days too")
    p.add_argument("--opponents", default=",".join(map(str, DEFAULT_OPPONENTS)),
                   help="Opponent rungs, comma-separated")
    p.add_argument("--opponent-sd", type=float, default=None,
                   help="Rung 8's error on the projections it reads (default opponents.SD)")
    p.add_argument("--replications", type=int, default=32)
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--verify", action="store_true",
                   help="Check the pairing: layouts, and a candidate equal to the shipped system "
                        "scoring exactly zero in every seat")
    p.add_argument("--ros-tag", default=None,
                   help="Price `ros` on an alternative rest-of-season build (ros_train.py --tag)")
    p.add_argument("--out", default=None, help="JSON path for the per-draft result")
    return p.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        stream=sys.stderr)
    for noisy in ("inputs", "draft", "draftroom", "engine", "simlayer", "ladder", "schedule"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.season == tune.FINAL_SEASON:
        log.warning("%s is the confirmation season: decide on another one first", args.season)
    shipped = load_strategy(args.strategy)
    candidate = load_strategy(args.candidate) if args.candidate else shipped
    candidate = with_settings(candidate, args.set)
    opponents = tuple(int(r) for r in args.opponents.split(","))
    base_layout, cand_layout = layouts(opponents)

    ctx = tune.Context(args.season, args.prior_season or tune.previous(args.season), args.league,
                       args.weights, shipped, args.workers, ros_tag=args.ros_tag)
    if 8 in opponents:
        import opponents as opponents_module

        ctx.data["opponent_field"] = opponents_module.field(
            ctx.config, opponents_module.SD if args.opponent_sd is None else args.opponent_sd)
    replications = 2 if args.verify else args.replications
    positions = check_layouts(ctx.config, base_layout, cand_layout, max(replications,
                                                                         ctx.config.teams))
    log.info("test seat's draft positions: %s", positions[:replications])

    if args.verify:
        base = seat_table(ctx, base_layout, None, replications)
        same = seat_table(ctx, cand_layout, shipped, replications)
        diff = (same[["pts", "win", "moves_spent"]] - base[["pts", "win", "moves_spent"]]).abs()
        if float(diff.to_numpy().max()) != 0.0:
            raise AssertionError(f"a candidate equal to the shipped system changed the league:\n"
                                 f"{diff[diff.max(axis=1) > 0]}")
        print(f"verify: layouts pair on {max(replications, ctx.config.teams)} drafts (test seat "
              f"covers every draft position once per {ctx.config.teams}); identity candidate "
              f"changes nothing in any of {len(base)} seats over {replications} drafts")
        return

    if tune.params_of(candidate) == tune.params_of(shipped):
        raise SystemExit("the candidate equals the shipped system; use --verify for that check")
    log.info("shipped: %s", tune.label(shipped))
    log.info("candidate: %s", tune.label(candidate))
    # The shipped run is the same for every candidate: cached by the shipped parameters, the
    # opponents and their noise, the season, the format and the code (tune.Context.key).
    cache = paths.ensure(paths.REPORTS_DIR / "oneseat") / (
        f"base_{ctx.key(shipped, replications)}_{'-'.join(map(str, opponents))}"
        f"_{(ctx.data['opponent_field'].describe() if ctx.data.get('opponent_field') else 'na')}.parquet")
    if cache.exists():
        base = pd.read_parquet(cache)
    else:
        base = seat_table(ctx, base_layout, None, replications)
        base.to_parquet(cache)
    alt = seat_table(ctx, cand_layout, candidate, replications)
    result = compare(base, alt, replications)
    result.update({"season": args.season, "league": args.league, "weights": args.weights,
                   "opponents": list(opponents), "shipped": tune.label(shipped),
                   "candidate": tune.label(candidate), "code": ctx.code})
    out = args.out or (paths.ensure(paths.REPORTS_DIR / "oneseat")
                       / f"{args.season}_{args.league}_{ctx.key(candidate, replications)}.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=1)
    print(summary(result))
    print(f"-> {out}")


if __name__ == "__main__":
    main()

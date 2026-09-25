#!/usr/bin/env python
"""How much could perfect information buy the shipped system? Ceilings, seat-paired.

    python ceilings.py --season 2024-25 --replications 8

Each is measured against the shipped system (rung 17: the orchestrator drafting by the consensus
VOR board), in exactly its seats -- the same design as `tune.py`, so seat luck cancels:

    draft        the shipped manager, drafting from a VOR board built on the season's ACTUAL
                 fantasy points instead of the consensus. The in-season manager is unchanged, so
                 the gap is what a perfect draft board is worth.
    transactions `headroom.OracleStreamer` in rung 17's seats, drafting by the same consensus
                 board: the same rosters on opening night, then add/drop and streaming by what
                 players really scored over the forward window. The gap is what perfect
                 in-season information is worth -- including scoring luck no model can know.
    availability the shipped league, unchanged except that at every lock each manager knows who
                 really plays tonight: tonight's P(plays) is the realized 0/1 and tonight's
                 goalie P(start) the realized starter -- what a perfect lockout feed (lines,
                 scratches, injuries, starting goalies) would tell it. Only tonight: later nights
                 and the carried rates a player is valued on are left as projected, so a healthy
                 scratch is not read as worthless. Every seat gets it (the view is shared), so the
                 report gives the other seats' gain beside rung 17's.
    no_flag      the reverse: the shipped league with the lockout injury flag switched off for
                 every seat -- what the live snapshot job has to replace in real play. A lower
                 bound on its worth: the projections' own injury features stay in.
    oracle_proj / oracle_avail / oracle_role -- the transaction ceiling split. The same greedy
                 oracle, ranking by what it may know about each game:
                     proj   P(plays) x projected points          (no foresight: the mechanics)
                     avail  realized played x projected points   (+ who plays)
                     role   ... x realized / projected ice time  (+ how much he plays)
                 and `transactions` itself ranks by actual points (+ scoring luck). Goalies: P(start)
                 or the realized start, times the league line from earlier seasons; only the full
                 oracle sees a goalie's actual points.

None is a strategy: each reads outcomes, and none is ever seated by `ladder.py`. The oracle
streamer is greedy (best realized window first, over the same move budget and roster rules), so it
is a LOWER bound on the true transaction ceiling. Run on the tuning season, never 2025-26 -- the
numbers choose where effort goes, which is a decision. Writes docs/ceilings-<season>-<which>.md.
"""

import argparse
import logging
import math
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd

import engine as engine_module
import headroom
import inputs
import ladder
import paths
import tune
from decisionlayer import draft as draft_module
from decisionlayer import load_strategy
from decisionlayer import managers as managers_module

log = logging.getLogger("ceilings")
ORACLE, ORACLE_VOR = 99, 99 + managers_module.VOR_TWIN
ORACLE_FIELD = (2, 5, 6, ORACLE_VOR)       # the oracle in exactly rung 17's seats


class LockoutOracleSeason(engine_module.Season):
    """NOT A STRATEGY: tonight's view (lineup slate and decision draws) reads who really plays;
    `_carry_rates`, the future frames and everything else read the projections as built."""

    def _index_inputs(self):
        super()._index_inputs()
        played = self.data["actuals"][["game_id", "player_id", "target_played"]]
        realized = {(int(g), int(p)): float(bool(x)) for g, p, x in played.itertuples(index=False)}
        self.oracle_by_day = {}
        for day, frame in self.proj_by_day.items():
            known = [realized.get((int(g), int(p))) for g, p in zip(frame["game_id"], frame["player_id"])]
            p_plays = [k if k is not None else v for k, v in zip(known, frame["p_plays"])]
            self.oracle_by_day[day] = frame.assign(p_plays=p_plays)
        starts = self.data["goalie_starts"]
        for day, frame in starts.groupby("game_date"):
            self.p_start_model[day] = {int(p): float(bool(s)) for p, s in
                                       zip(frame["player_id"], frame["is_starter"])}

    def _tonight(self, method, *args):
        real = self.proj_by_day
        self.proj_by_day = self.oracle_by_day
        try:
            return method(*args)
        finally:
            self.proj_by_day = real

    def _slate_for(self, day):
        return self._tonight(super()._slate_for, day)

    def decision_draws(self, day, goalie_projections=None):
        return self._tonight(super().decision_draws, day, goalie_projections)


def paired(base, alt, rung_alt):
    mine = alt.index[alt["rung"] == rung_alt]
    if not (base.loc[mine, "rung"] == tune.SHIPPED).all():
        raise AssertionError("the seats did not line up with rung 17's")
    out = {}
    for m in ("pts", "win", "playoff", "moves_spent"):
        d = (alt.loc[mine, m] - base.loc[mine, m]).groupby(level="replication").mean()
        out[m] = (float(d.mean()), float(d.std(ddof=1) / math.sqrt(len(d))) if len(d) > 1 else 0.0)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--season", default="2024-25")
    p.add_argument("--prior-season", default=None)
    p.add_argument("--league", default="league")
    p.add_argument("--weights", default="points-league")
    p.add_argument("--replications", type=int, default=8)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--which", default="draft,transactions,availability")
    args = p.parse_args()
    which = set(args.which.split(","))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        stream=sys.stderr)
    for noisy in ("inputs", "draft", "engine", "simlayer", "ladder", "schedule"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.season == tune.FINAL_SEASON:
        raise SystemExit(f"{tune.FINAL_SEASON} is the final holdout; measure ceilings on another season")

    shipped = load_strategy(None)
    ctx = tune.Context(args.season, args.prior_season or tune.previous(args.season), args.league,
                       args.weights, shipped, args.workers)
    base = ctx.base(args.replications)
    results = {}

    # 1. Draft ceiling: rung 17's board from the season's actual points.
    if "draft" in which:
        results["draft"] = draft_ceiling(ctx, args, base)
    if "transactions" in which:
        results["transactions"] = transaction_ceiling(ctx, args, base, shipped)
    if "availability" in which:
        results["availability"] = availability_ceiling(ctx, args, base, shipped)
    if "no_flag" in which:
        results["no_flag"] = no_flag_floor(ctx, args, base)
    for level in ("proj", "avail", "role"):
        if f"oracle_{level}" in which:
            results[f"oracle_{level}"] = transaction_ceiling(ctx, args, base, shipped, level)
    write(args, ctx, base, results)


def no_flag_floor(ctx, args, base):
    """The shipped league with no lockout injury flag, for every seat."""
    real = ctx.data["availability"]
    ctx.data["availability"] = real.assign(injured_at_lockout=False)
    try:
        log.info("no injury flag: %d drafts", args.replications)
        blind = ctx._seats(tune.BASE_FIELD, None, args.replications)
    finally:
        ctx.data["availability"] = real
    result = paired(base, blind, tune.SHIPPED)
    log.info("no injury flag: %+.2f ± %.2f", *result["pts"])
    return result


def draft_ceiling(ctx, args, base):
    actual = draft_module.prior_season_board(ctx.data["actuals"], ctx.data["goalie_starts"],
                                             ctx.scoreset)
    actual.index = actual.index.astype(int)
    oracle_board = draft_module.vor_board(actual[[p in ctx.eligibility for p in actual.index]],
                                          ctx.config, ctx.eligibility)
    consensus_board = ctx.data["vor"][ctx.scoreset.name]
    ctx.data["vor"][ctx.scoreset.name] = oracle_board
    try:
        log.info("draft ceiling: %d drafts", args.replications)
        drafted = ctx._seats(tune.BASE_FIELD, None, args.replications)
    finally:
        ctx.data["vor"][ctx.scoreset.name] = consensus_board
    result = paired(base, drafted, tune.SHIPPED)
    log.info("draft ceiling: %+.2f ± %.2f", *result["pts"])
    return result


def oracle_values(ctx, level) -> dict:
    """{(date, player_id): value} the oracle ranks by, at a given level of foresight."""
    if level == "full":
        return engine_module.Outcomes(ctx.data["actuals"], ctx.data["goalie_starts"],
                                      ctx.scoreset).points
    import inputs as inputs_module

    proj = ctx.data["projections"]
    base = pd.read_parquet(paths.base_table(ctx.season),
                           columns=["game_id", "player_id", "target_played", "target_toi"])
    rows = proj.merge(base, on=["game_id", "player_id"], how="left")
    per_game = ctx.scoreset.score_columns(rows, prefix="lambda_")
    # PP and SH points: the share of the player's projected goals and assists.
    weights = ctx.scoreset.weights("skaters")
    scoring = rows["lambda_goals"] + rows["lambda_assists"]
    per_game = (per_game + weights.get("ppp", 0.0) * rows["pp_point_share"] * scoring
                + weights.get("shp", 0.0) * rows["sh_point_share"] * scoring)
    played = rows["target_played"].fillna(0).astype(bool).astype(float)
    if level == "proj":
        value = rows["p_plays"] * per_game
    elif level == "avail":
        value = played * per_game
    else:
        ice = (rows["target_toi"] / rows["toi"].where(rows["toi"] > 0)).clip(0, 3).fillna(1.0)
        value = played * per_game * ice
    out = {(pd.Timestamp(d), int(q)): float(v)
           for d, q, v in zip(rows["game_date"], rows["player_id"], value)}

    history = ctx.data["goalie_history"]
    line = float(pd.Series(ctx.scoreset.score_columns(history[history["is_starter"].astype(bool)],
                                                      side="goalies")).mean())
    starts = ctx.data["goalie_starts"]
    if level == "proj":
        pstart = ctx.data.get("p_start")
        known = {(pd.Timestamp(d), int(q)): float(v) for d, q, v in
                 zip(pstart["game_date"], pstart["player_id"], pstart["p_start"])}
        for d, q in zip(starts["game_date"], starts["player_id"]):
            out[(pd.Timestamp(d), int(q))] = known.get((pd.Timestamp(d), int(q)), 0.5) * line
    else:
        for d, q, st in zip(starts["game_date"], starts["player_id"], starts["is_starter"]):
            out[(pd.Timestamp(d), int(q))] = float(bool(st)) * line
    return out


def transaction_ceiling(ctx, args, base, shipped, level="full"):
    # 2. Transaction ceiling: the oracle streamer in rung 17's seats, same draft. One process: the
    # oracle class is registered here, and a spawned worker would not see it.
    headroom.OracleStreamer.realized = oracle_values(ctx, level)
    headroom.OracleStreamer.calendar = ctx.calendar
    managers_module.LADDER[ORACLE] = headroom.OracleStreamer
    log.info("transaction ceiling (%s): %d drafts, one process", level, args.replications)
    ns = SimpleNamespace(replications=args.replications, workers=1, verbose_weeks=False,
                         decision_sims=200)
    runs = ladder.run_replications(ns, ctx.config, ctx.calendar, ctx.data, ctx.eligibility,
                                   ctx.scoreset, ORACLE_FIELD, shipped)
    _, teams = ladder.summarize(runs, ctx.scoreset.name)
    oracle = (teams.assign(pts=teams["points"] / teams["weeks"],
                           win=teams["matchup_wins"] / teams["weeks"],
                           playoff=teams["made_playoffs"].astype(float))
              .set_index(["replication", "seat"]))
    result = paired(base, oracle, ORACLE_VOR)
    log.info("transaction ceiling (%s): %+.2f ± %.2f", level, *result["pts"])
    return result


def availability_ceiling(ctx, args, base, shipped):
    """The shipped league with a perfect lockout feed, one process (the engine class is swapped
    here, and a spawned worker would not see it)."""
    log.info("availability ceiling: %d drafts, one process", args.replications)
    real = engine_module.Season
    engine_module.Season = LockoutOracleSeason
    try:
        ns = SimpleNamespace(replications=args.replications, workers=1, verbose_weeks=False,
                             decision_sims=200)
        runs = ladder.run_replications(ns, ctx.config, ctx.calendar, ctx.data, ctx.eligibility,
                                       ctx.scoreset, tune.BASE_FIELD, shipped)
    finally:
        engine_module.Season = real
    _, teams = ladder.summarize(runs, ctx.scoreset.name)
    fed = (teams.assign(pts=teams["points"] / teams["weeks"],
                        win=teams["matchup_wins"] / teams["weeks"],
                        playoff=teams["made_playoffs"].astype(float))
           .set_index(["replication", "seat"]))
    result = paired(base, fed, tune.SHIPPED)
    others = {}
    for rung in (2, 5, 6):
        seats = fed.index[fed["rung"] == rung]
        d = (fed.loc[seats, "pts"] - base.loc[seats, "pts"]).groupby(level="replication").mean()
        others[rung] = (float(d.mean()), float(d.std(ddof=1) / math.sqrt(len(d))))
    result["others"] = others
    log.info("availability ceiling: rung 17 %+.2f ± %.2f; others %s", *result["pts"], others)
    return result


def write(args, ctx, base, results):
    shipped_pts = float(base.loc[base["rung"] == tune.SHIPPED, "pts"].mean())
    lines = [f"# Perfect-information ceilings: {args.season}, {args.league}, {args.weights}\n",
             f"Generated by `ceilings.py`; see its docstring. Seat-paired against the shipped "
             f"system (rung 17, {shipped_pts:.1f} points a week), {args.replications} drafts. "
             f"Neither ceiling is a strategy; the transaction one is a lower bound (a greedy "
             f"oracle over rung 4's one-week window).\n",
             "| perfect information in | points a week | win rate | playoff rate | moves |",
             "|---|---|---|---|---|"]
    for name, label in (("draft", "the draft board"), ("transactions", "add/drop and streaming"),
                        ("availability", "tonight's lineup at each lock (a perfect feed)"),
                        ("no_flag", "NO lockout injury flag (the floor without a feed)"),
                        ("oracle_proj", "oracle mechanics, projected values (no foresight)"),
                        ("oracle_avail", "oracle knowing who plays"),
                        ("oracle_role", "oracle knowing who plays and his ice time")):
        if name not in results:
            continue
        r = results[name]
        lines.append(f"| {label} | {r['pts'][0]:+.1f} ± {r['pts'][1]:.1f} | "
                     f"{r['win'][0]:+.3f} | {r['playoff'][0]:+.2f} | {r['moves_spent'][0]:+.0f} |")
    if "availability" in results:
        o = results["availability"]["others"]
        lines += ["", "With the feed, every seat gains (the view is shared): rungs 2 / 5 / 6 "
                  + " / ".join(f"{o[r][0]:+.1f} ± {o[r][1]:.1f}" for r in (2, 5, 6))
                  + " points a week."]
    out = paths.ensure(paths.DOCS_DIR) / f"ceilings-{args.season}-{'-'.join(sorted(results))}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[2:]))
    print(f"-> {out}")


if __name__ == "__main__":
    main()

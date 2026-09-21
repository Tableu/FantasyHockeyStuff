#!/usr/bin/env python
"""Roster totals and head-to-head matchups -- what the distributions are actually for.

A per-player distribution is not the decision. The decision is "start this one or that one",
"is this ceiling worth a roster spot", "am I ahead in this matchup", and every one of those
is a question about a **total**, which is where correlation stops being an academic point:
independent sampling understates a ten-skater stack's spread by a quarter of its standard
deviation (see `copula.py`), and understating spread systematically favours the favourite.

Everything here works on the same draws, so one simulation answers all of them at once:

    roster_points   totals per draw for a set of player-games
    summarize       mean, floor, ceiling and the chance of clearing a number
    matchup         P(win) against another roster, drawn *in the same simulation* so that
                    two managers rostering opposite sides of one game stay correlated
    start_sit       the swing in P(win) from starting each candidate, which is the actual
                    lineup decision and is not the same ranking as expected points

Used as a script it takes rosters as CSV files of `player_id` (with an optional `game_date`
to pin a player to one night) and reports the matchup:

    python rosters.py --season 2025-26 --from-date 2026-03-09 --to-date 2026-03-15 \
        --roster mine.csv --opponent theirs.csv --weights points-league
"""

import argparse
import logging
from pathlib import Path

import numpy as np
import pandas as pd

import paths
import scoring as scoring_module
import simulate as simulate_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rosters")


def roster_points(draws, scoreset, rows=None):
    """Points per draw for a roster: [sims], summed over the player-games it holds."""
    points = scoreset.score_draws(draws)
    if rows is not None:
        points = points[np.asarray(rows)]
    return points.sum(axis=0)


def summarize(totals, thresholds=()):
    """The shape of a total, in the terms a manager actually asks about."""
    totals = np.asarray(totals, dtype="float64")
    out = {
        "mean": float(totals.mean()),
        "sd": float(totals.std()),
        "floor_p10": float(np.quantile(totals, 0.10)),
        "median": float(np.quantile(totals, 0.50)),
        "ceiling_p90": float(np.quantile(totals, 0.90)),
    }
    for threshold in thresholds:
        out[f"p_over_{threshold:g}"] = float((totals > threshold).mean())
    return out


def matchup(mine, theirs):
    """P(win) head to head, plus the margin's shape. Ties are split, as most leagues do."""
    mine = np.asarray(mine, dtype="float64")
    theirs = np.asarray(theirs, dtype="float64")
    margin = mine - theirs
    wins = float((margin > 0).mean())
    ties = float((margin == 0).mean())
    return {
        "p_win": wins + ties / 2.0,
        "p_tie": ties,
        "margin_mean": float(margin.mean()),
        "margin_sd": float(margin.std()),
        "margin_p10": float(np.quantile(margin, 0.10)),
        "margin_p90": float(np.quantile(margin, 0.90)),
    }


def start_sit(draws, scoreset, locked_rows, candidate_rows, opponent_totals):
    """P(win) with each candidate added to a locked roster.

    This is the lineup decision itself, and it is *not* a ranking by expected points. A
    manager who is behind should want variance and a manager who is ahead should want a
    floor, which only shows up when the candidate is scored against the opponent's own
    distribution rather than on its own.
    """
    base = roster_points(draws, scoreset, locked_rows)
    ranked = []
    for row in candidate_rows:
        totals = base + roster_points(draws, scoreset, [row])
        result = matchup(totals, opponent_totals)
        ranked.append({
            "row": int(row),
            "player_id": int(draws.keys["player_id"].iloc[row]),
            "expected_points": float(scoreset.score_draws(draws)[row].mean()),
            "p_win": result["p_win"],
            "margin_sd": result["margin_sd"],
        })
    return sorted(ranked, key=lambda entry: entry["p_win"], reverse=True)


def load_roster(path, table):
    """Match a CSV of player ids to the rows of a lambda table."""
    roster = pd.read_csv(path)
    if "player_id" not in roster.columns:
        raise ValueError(f"{path} needs a player_id column")
    keys = table.reset_index(drop=True)
    merged = keys.reset_index().merge(roster, on="player_id", how="inner",
                                      suffixes=("", "_roster"))
    if "game_date_roster" in merged.columns:
        merged = merged[merged["game_date"] == pd.to_datetime(merged["game_date_roster"])]
    missing = set(roster["player_id"]) - set(merged["player_id"])
    if missing:
        log.warning("%d rostered player(s) have no game in this window: %s",
                    len(missing), sorted(missing)[:5])
    return merged["index"].to_numpy()


def parse_args():
    parser = argparse.ArgumentParser(description="Roster totals and head-to-head matchups")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--season")
    source.add_argument("--lambdas", type=Path)
    parser.add_argument("--variant", choices=("A", "B"), default="A")
    parser.add_argument("--date")
    parser.add_argument("--from-date", dest="from_date")
    parser.add_argument("--to-date", dest="to_date")
    parser.add_argument("--sims", type=int, default=5000)
    parser.add_argument("--weights", required=True, metavar="FILE",
                        help="Scoring file: a matchup is a question about points")
    parser.add_argument("--roster", type=Path, required=True)
    parser.add_argument("--opponent", type=Path, default=None)
    parser.add_argument("--independent", action="store_true")
    parser.add_argument("--chunk-rows", type=int, default=10 ** 9)
    parser.add_argument("--seed", type=int, default=17)
    return parser.parse_args()


def main():
    args = parse_args()
    table = simulate_module.load_table(args)
    scoreset = scoring_module.load(args.weights)
    simulator = simulate_module.build_simulator(args.independent, args.seed)

    # One draw over the whole window, so both rosters -- and any two players who share an
    # NHL game -- are sampled inside the same simulation.
    draws = simulator.draw(table, args.sims)
    mine_rows = load_roster(args.roster, table)
    mine = roster_points(draws, scoreset, mine_rows)
    log.info("roster: %d player-games over %d draws", len(mine_rows), args.sims)

    print(f"\n{args.roster.stem} under {scoreset.name}")
    for key, value in summarize(mine).items():
        print(f"  {key:12s} {value:8.2f}")

    if args.opponent:
        theirs_rows = load_roster(args.opponent, table)
        theirs = roster_points(draws, scoreset, theirs_rows)
        print(f"\n{args.opponent.stem}")
        for key, value in summarize(theirs).items():
            print(f"  {key:12s} {value:8.2f}")
        print("\nmatchup")
        for key, value in matchup(mine, theirs).items():
            print(f"  {key:12s} {value:8.3f}")


if __name__ == "__main__":
    main()

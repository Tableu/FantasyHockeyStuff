#!/usr/bin/env python
"""Fit the handful of numbers the goalie sampler needs, from the seasons BEFORE the one replayed.

    python goalie_fit.py --season 2025-26          # fits on 2023-24 + 2024-25

A goalie's line is built from the game the skaters already drew (`goalies.py`): shots against are
the opposing skaters' shots, goals against their goals. What that structure cannot supply is fitted
here, and nothing else is:

    empty_net_by_margin  P(each of the winner's goals past the first-goal margin went into an empty
                         net), by final skater-goal margin 2, 3, 4+. Empty-net goals are skater goals
                         but no goalie's goals against, and they only exist once the loser trails
                         and pulls his goalie -- a one-goal game has none by construction
    p_ot_one_goal        P(the game went to overtime | the skater goals differ by one); the loser
                         then takes an OT loss. A tie in skater goals is a shootout by definition,
                         because shootout goals are not skater goals
    pull_by_ga           P(the starter is pulled | his team's goals against), by count, 7+ pooled
    pulled_share_ga      of a pulled start's team goals against, the share charged to the starter
    pulled_share_saves   likewise for saves
    pulled_decision      P(the pulled starter keeps the decision), for a team win and a team loss

Keyed by the season it is FOR, like the dispersion and correlation fits, and refused if a training
season is that season: these are numbers a manager may know about goalies in general, never the
replayed season's own.
"""

import argparse
import json
import logging

import numpy as np
import pandas as pd

import paths

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("goalie_fit")

PULL_BUCKETS = 8                       # goals against 0..6, then 7+


def team_games(season: str) -> pd.DataFrame:
    """One row per team-game: the team's skater goals and shots (candidates who played), the
    opponent's, and the goalie side of the same game."""
    skaters = pd.read_parquet(paths.FEATURES_DIR / f"skaters_A_{season}.parquet",
                              columns=["game_id", "team_id", "target_goals", "target_shots"])
    own = (skaters.groupby(["game_id", "team_id"])[["target_goals", "target_shots"]].sum()
           .rename(columns={"target_goals": "goals", "target_shots": "shots"}).reset_index())
    goalies = pd.read_parquet(paths.FEATURES_DIR / f"goalie_starts_{season}.parquet")
    side = (goalies.groupby(["game_id", "team_id"])
            .agg(opp_team_id=("opp_team_id", "first"), last_period=("last_period_type", "first"),
                 ga=("goals_against", "sum"), saves=("saves", "sum")).reset_index())
    table = side.merge(own, on=["game_id", "team_id"])
    opp = own.rename(columns={"team_id": "opp_team_id", "goals": "opp_goals", "shots": "opp_shots"})
    return table.merge(opp, on=["game_id", "opp_team_id"]), goalies


def fit(train_seasons) -> dict:
    frames, starts = zip(*(team_games(s) for s in train_seasons))
    games = pd.concat(frames, ignore_index=True)
    goalies = pd.concat(starts, ignore_index=True)

    # Empty-net goals: the opponent's skater goals that no goalie of ours was charged with, scored
    # by the winning side while the loser's net was empty.
    won_by_opp = games["opp_goals"] > games["goals"]
    lost = games[won_by_opp]
    en = (lost["opp_goals"] - lost["ga"]).clip(lower=0)
    final_margin = (lost["opp_goals"] - lost["goals"])
    # Per-goal probability over the margin-1 goals that could have gone in empty, by margin.
    empty_net_by_margin = {}
    for m in (2, 3, 4):
        rows = final_margin >= m if m == 4 else final_margin == m
        empty_net_by_margin[str(m)] = float(en[rows].sum() / (final_margin[rows] - 1).sum())
    empty_net_share = float(en.sum() / lost["opp_goals"].sum())
    en_on_one = int(en[final_margin == 1].sum())

    margin = (games["goals"] - games["opp_goals"]).abs()
    one = games[margin == 1]
    p_ot = float((one["last_period"] == "OT").mean())
    tied = games[margin == 0]
    shootout_on_tie = float((tied["last_period"] == "SO").mean()) if len(tied) else float("nan")

    started = goalies[goalies["is_starter"].astype(bool)].merge(
        games[["game_id", "team_id", "ga", "saves", "goals", "opp_goals"]]
        .rename(columns={"ga": "ga_team", "saves": "saves_team"}),
        on=["game_id", "team_id"])
    bucket = started["ga_team"].clip(upper=PULL_BUCKETS - 1)
    pull = started.groupby(bucket)["pulled"].agg(["mean", "size"])
    pull_by_ga = [float(pull["mean"].get(k, 1.0)) for k in range(PULL_BUCKETS)]

    pulled = started[started["pulled"].astype(bool)]
    won = pulled["goals"] > pulled["opp_goals"]
    share_ga = float(pulled["goals_against"].sum() / max(pulled["ga_team"].sum(), 1))
    share_saves = float(pulled["saves"].sum() / max(pulled["saves_team"].sum(), 1))
    keeps_win = float((pulled.loc[won, "decision"] == "W").mean()) if won.any() else 0.0
    keeps_loss = float(pulled.loc[~won, "decision"].isin(["L", "O"]).mean()) if (~won).any() else 1.0

    report = {
        "trained_on": list(train_seasons),
        "team_games": int(len(games)),
        "starts": int(len(started)),
        "empty_net_by_margin": {k: round(v, 5) for k, v in empty_net_by_margin.items()},
        "empty_net_share": round(empty_net_share, 5),
        "empty_net_goals_in_one_goal_games": en_on_one,
        "p_ot_one_goal": round(p_ot, 5),
        "shootout_on_tied_skater_goals": round(shootout_on_tie, 5),
        "pull_by_ga": [round(v, 5) for v in pull_by_ga],
        "pull_bucket_sizes": [int(pull["size"].get(k, 0)) for k in range(PULL_BUCKETS)],
        "pull_rate": round(float(started["pulled"].mean()), 5),
        "pulled_share_ga": round(share_ga, 5),
        "pulled_share_saves": round(share_saves, 5),
        "pulled_decision": {"team_win": round(keeps_win, 5), "team_loss": round(keeps_loss, 5)},
        "league_save_pct": round(float(started["saves"].sum()
                                       / (started["saves"] + started["goals_against"]).sum()), 5),
        "shots_check": {"skater_shots_per_team_game": round(float(games["opp_shots"].mean()), 3),
                        "shots_against_per_team_game": round(float((games["ga"] + games["saves"]).mean()), 3)},
    }
    return report


def parse_args():
    parser = argparse.ArgumentParser(description="Fit the goalie sampler's numbers")
    parser.add_argument("--season", default="2025-26", help="The season the fit is FOR (held out)")
    parser.add_argument("--train", nargs="+", default=None,
                        help="Training seasons (default: every earlier season with data)")
    return parser.parse_args()


def main():
    args = parse_args()
    first = int(args.season[:4])
    train = args.train or [s for s in (f"{y}-{str(y + 1)[2:]}" for y in range(first - 10, first))
                           if (paths.FEATURES_DIR / f"goalie_starts_{s}.parquet").exists()]
    if args.season in train:
        raise SystemExit(f"{args.season} is both the season the fit is for and a training season")
    if not train:
        raise SystemExit(f"no training seasons before {args.season}")
    report = fit(train)
    out = paths.ensure(paths.REPORTS_DIR) / paths.goalie_fit_path(args.season).name
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log.info("fit on %s -> %s", ", ".join(train), out)
    print(json.dumps({k: v for k, v in report.items() if k != "pull_bucket_sizes"}, indent=2))


if __name__ == "__main__":
    main()

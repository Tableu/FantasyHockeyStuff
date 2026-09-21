#!/usr/bin/env python
"""Rest-of-season projections for a given date, for whatever asks for them.

This is the layer's output: one row per player, as of a date, carrying what he is expected to
produce over the games his team has left. Draft logic wants it for the whole season, trade
logic wants it for the stretch that remains, and waiver logic wants it next to a per-game
projection to tell a streaming play from a stash.

**As of a date, not as of a game.** Not every team plays every night, so a consumer asking
"what does this player project for" on a Tuesday needs each player's most recent state,
whenever his last game was. Every player's latest feature row at or before the date is taken,
and its age is reported in `state_age_days` -- a player whose team last played a week ago is
being projected from a week-old view, and that is worth seeing rather than hiding.

**The schedule is the multiplier.** The factors -- availability, ice time, rate per 60 -- do
not depend on the horizon; the number of games left does. So the same models serve a
projection made in October and one made in March, and `games_remaining` is simply read off
the schedule.

No scoring system is applied unless one is asked for. `--weights` adds a points column per
scoring file, and the stat line is emitted either way.

Usage:
    python ros_predict.py --season 2025-26 --as-of 2026-01-15 --weights points-league
    python ros_predict.py --season 2025-26 --as-of 2026-01-15 --horizon 42
"""

import argparse
import json
import logging

import numpy as np
import pandas as pd

import paths
import ros
import ros_baselines as baselines
import ros_train
import weights as weights_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ros_predict")


def parse_args():
    parser = argparse.ArgumentParser(description="Project rest-of-season production")
    parser.add_argument("--season", required=True)
    parser.add_argument("--as-of", dest="as_of", default=None,
                        help="Project from this date (default: the last date in the season)")
    parser.add_argument("--horizon", default="season",
                        help="Days to project over, or 'season' to run to the season's end "
                             "(default season)")
    parser.add_argument("--weights", action="append", default=None, metavar="FILE",
                        help="Scoring file to add a points column for; repeat for several")
    parser.add_argument("--min-games-played", type=int, default=1,
                        help="Skip players with fewer games than this behind them, whose "
                             "projection would be the positional prior and nothing else")
    parser.add_argument("--baseline", action="store_true",
                        help="Use the shrinkage baseline instead of the trained models")
    parser.add_argument("--features-dir", default=None)
    parser.add_argument("--out", default=None)
    return parser.parse_args()


def load_fit():
    """The boosters and the shrinkage they were trained alongside."""
    sidecar = paths.MODELS_DIR / "ros_fit.json"
    if not sidecar.exists():
        raise FileNotFoundError(f"{sidecar} is missing -- run ros_train.py --save")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    fitted = {factor: {"k": entry["k"], "recency": entry["recency"],
                       "prior": entry["prior"]}
              for factor, entry in payload["shrinkage"].items()}
    return payload, fitted


def load_boosters():
    import lightgbm as lgb
    boosters = {}
    for factor in baselines.FACTORS:
        path = paths.MODELS_DIR / f"ros_{factor}.txt"
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing -- run ros_train.py --save")
        boosters[factor] = lgb.Booster(model_file=str(path))
    return boosters


def state_as_of(table, as_of):
    """Each player's most recent row at or before the date, one row per player."""
    rows = table[table["game_date"] <= as_of]
    if rows.empty:
        raise ValueError(f"no games on or before {as_of.date()} in this season")
    latest = rows.sort_values("game_date").groupby("player_id", as_index=False).tail(1)
    latest = latest.copy()
    latest["state_age_days"] = (as_of - latest["game_date"]).dt.days
    return latest


def games_remaining(table, as_of, horizon_days):
    """Team games left in the window, per team, counted off the schedule."""
    schedule = ros.team_schedule(table)
    future = schedule[schedule["game_date"] > as_of]
    if horizon_days is not None:
        future = future[future["game_date"] <= as_of + pd.Timedelta(days=horizon_days)]
    counts = future.groupby("team_id").size().rename("games_remaining")
    return counts


def run(args):
    features_dir = paths.FEATURES_DIR if args.features_dir is None else args.features_dir
    table = ros.base_table(args.season, features_dir)
    as_of = (pd.Timestamp(args.as_of) if args.as_of
             else table["game_date"].max())
    horizon_days = ros.parse_horizon(args.horizon)

    payload, fitted = load_fit()
    if payload.get("horizon") and str(payload["horizon"]) != str(args.horizon):
        log.warning("the saved models were trained on a %s horizon and this is a %s one; "
                    "the factors are horizon-independent so this is usually fine, but the "
                    "label noise they were fitted against was not the same",
                    payload["horizon"], args.horizon)

    # The as-of derivations have to be computed over the whole season and only then sliced:
    # `team_games_to_date` counts a team's games before tonight, and counting it on a frame
    # already reduced to one row per player would count one game per player instead.
    table["season"] = args.season
    table = baselines.add_asof(table)
    state = state_as_of(table, as_of)
    state = ros_train.add_shrunk(state, fitted)

    remaining = games_remaining(table, as_of, horizon_days)
    state["games_remaining"] = state["team_id"].map(remaining).fillna(0.0)
    state["window_team_games"] = state["games_remaining"]
    state = state[state["gp_std"].fillna(0) >= args.min_games_played]
    log.info("as of %s: %d players, median %d games remaining, state age median %d days",
             as_of.date(), len(state), int(state["games_remaining"].median()),
             int(state["state_age_days"].median()))

    if args.baseline:
        factors = baselines.predict(state, fitted, "shrunk")
    else:
        boosters = load_boosters()
        columns = payload["feature_columns"]
        missing = [c for c in columns if c not in state.columns]
        for column in missing:
            state[column] = np.nan
        if missing:
            log.warning("%d training column(s) absent here, filled with NaN: %s",
                        len(missing), missing[:5])
        design = ros_train.matrix(state, columns)
        factors = pd.DataFrame(index=state.index)
        for factor, booster in boosters.items():
            factors[factor] = np.clip(booster.predict(design), 0, None)
        factors["availability"] = factors["availability"].clip(0, 1)

    totals = baselines.to_totals(state, factors)
    out = state[[c for c in ("season_id", "player_id", "team_id", "position", "game_date",
                             "state_age_days", "gp_std", "games_remaining")
                 if c in state.columns]].copy()
    out = out.rename(columns={"game_date": "state_from_game"})
    out["as_of"] = as_of
    for factor in baselines.FACTORS:
        out[f"proj_{factor}"] = factors[factor].to_numpy()
    for category in ["games"] + baselines.CATEGORIES:
        out[f"proj_{category}"] = totals[category].to_numpy()
    for scoreset in [weights_module.load(w) for w in (args.weights or [])]:
        out[f"{scoreset.name}_points"] = scoreset.score(totals).to_numpy()

    destination = (args.out or paths.ensure(paths.REPORTS_DIR)
                   / f"ros_projections_{args.season}_{as_of.date()}.parquet")
    out.to_parquet(destination, index=False)
    log.info("wrote %s: %d players x %d columns", destination, len(out), out.shape[1])
    print_preview(out, args)
    return out


def print_preview(out, args):
    scoresets = [weights_module.load(w) for w in (args.weights or [])]
    sort_column = (f"{scoresets[0].name}_points" if scoresets else "proj_goals")
    top = out.nlargest(15, sort_column)
    columns = ["player_id", "position", "games_remaining", "proj_availability",
               "proj_toi_per_game", "proj_goals", "proj_assists", "proj_shots"]
    columns += [f"{s.name}_points" for s in scoresets]
    view = top[[c for c in columns if c in top.columns]].copy()
    view["proj_toi_per_game"] = (view["proj_toi_per_game"] / 60).round(1)
    view = view.rename(columns={"proj_toi_per_game": "toi_min"})
    print(f"\ntop 15 by {sort_column}, as of {out['as_of'].iloc[0].date()}")
    print(view.round(2).to_string(index=False))


def main():
    run(parse_args())


if __name__ == "__main__":
    main()

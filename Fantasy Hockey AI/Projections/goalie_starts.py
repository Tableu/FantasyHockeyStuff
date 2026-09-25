#!/usr/bin/env python
"""P(this goalie starts tonight) -- the one goalie output that survived measurement.

The goalie branch was built, measured and deleted. What it established, and what this module is
therefore careful to respect:

    per-start fantasy points   R2 -0.8%, worse than a constant      -> do not model
    save percentage            r = +0.026 season-to-date to next    -> do not model
    the W/L/O decision         AUC 0.559 vs 0.522 for home ice      -> do not model
    P(start)                   AUC ~0.86                            -> THIS

So the standing treatment for a goalie's night is `P(start) x the league-average line`, and the
only thing worth fitting is the first factor. Nothing here predicts how well he would play.

**The trap this module is built around:** "who started the last game" carries almost no signal,
and among the goalies a decision is actually between it carries the *wrong* signal. Measured on
2025-26: over all candidates P(start | he started his team's last game) = 0.405 against 0.366 for
a goalie who did not -- AUC 0.520, i.e. nothing. Restricted to healthy candidates, which is the
population a lineup decision ranges over, it inverts: 0.412 against 0.436. Tandems alternate. The
column is included, but the features that carry the signal are start *share* and rest, and above
all the two relative ones below -- `start_share_edge` and `start_share_rank` take 43% of the
fitted gain between them, because a start is a contest inside a team-game rather than a property
of a goalie.

Every feature is as-of: cumulative counts are shifted so the current game is never inside its own
history. Train on past seasons, hold the simulated season out -- the season simulator reads this
model's output, so an in-sample P(start) would flatter rung 4 for nothing.

    python goalie_starts.py --train
    python goalie_starts.py --train --save
    python goalie_starts.py --predict --season 2025-26
"""

import argparse
import json
import logging
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

import paths

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("goalie-starts")

TRAIN_SEASONS = ["2023-24", "2024-25"]
HOLDOUT_SEASON = "2025-26"

FEATURES = [
    "start_share_std", "start_share_l10", "starts_l5", "appearances_l5",
    "started_prev_game", "games_since_start", "days_since_start",
    "dressed_share_std", "games_dressed_lookback",
    "injured_at_lockout", "partner_injured", "n_healthy_candidates",
    "partner_start_share", "start_share_edge", "start_share_rank",
    "days_rest", "is_back_to_back", "games_last_4d", "is_home",
]

PARAMS = {
    "objective": "binary",
    "metric": ["auc", "binary_logloss"],
    "learning_rate": 0.05,
    "num_leaves": 15,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 10.0,
    "num_threads": 0,
    "verbosity": -1,
    "seed": 17,
}


def parse_args():
    parser = argparse.ArgumentParser(description="Fit or apply the P(start) model")
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--predict", action="store_true")
    parser.add_argument("--season", default=HOLDOUT_SEASON,
                        help="The held-out season: scored by --train, predicted by --predict")
    parser.add_argument("--train-seasons", default=",".join(TRAIN_SEASONS))
    parser.add_argument("--models-dir", type=Path, default=None,
                        help="Default models/<season>/goalie_start/, for the season held out")
    parser.add_argument("--save", action="store_true", help="Write the booster and sidecar")
    parser.add_argument("--no-holdout", action="store_true",
                        help="Train on every --train-seasons season with nothing held back and "
                             "save to models/<next season>/goalie_start/ -- the deployment build. "
                             "No metrics come out of it; the last scored build supplies them")
    parser.add_argument("--rounds", type=int, default=400)
    return parser.parse_args()


def _candidates(season: str) -> pd.DataFrame:
    """The goalie candidate universe for a season, with the realized starter as the label."""
    lineup = pd.read_parquet(
        paths.PROJECT_ROOT.parent / "ModelFeatures" / "data" / "lineups"
        / f"features_A_{season}.parquet",
        columns=["season_id", "game_id", "game_date", "team_id", "player_id", "position",
                 "injured_at_lockout", "games_dressed_lookback", "feat_starting_goalie",
                 "label_dressed", "label_starting_goalie"])
    lineup = lineup[lineup["position"] == "G"].copy()
    lineup["game_date"] = pd.to_datetime(lineup["game_date"])
    for column in ("injured_at_lockout", "feat_starting_goalie", "label_dressed",
                   "label_starting_goalie"):
        lineup[column] = lineup[column].fillna(False).astype(bool)
    # Only goalies knowable before the game: dressed in the lookback, or on the injury report. The
    # lineup table also carries anyone who actually dressed (so its labels cover him) -- a call-up
    # appearing from nowhere, whose mere presence told the model he played (620 on 2026-01-15:
    # no lookback, and he started). Live, such a goalie reaches P(start) only through a pre-game
    # chart, which is fair; history cannot tell those apart, so they are left out of training.
    knowable = (lineup["games_dressed_lookback"] > 0) | lineup["injured_at_lockout"]
    log.info("%s: %d goalie candidates, %d not knowable before the game (dropped)",
             season, len(lineup), int((~knowable).sum()))
    lineup = lineup[knowable]

    starts = pd.read_parquet(paths.FEATURES_DIR / f"goalie_starts_{season}.parquet",
                             columns=["game_id", "team_id", "player_id", "is_starter",
                                      "appeared", "is_home"])
    table = lineup.merge(starts, on=["game_id", "team_id", "player_id"], how="left")
    # A candidate with no boxscore row was not dressed, so he neither started nor appeared.
    table["is_starter"] = table["is_starter"].fillna(False).astype(bool)
    table["appeared"] = table["appeared"].fillna(False).astype(bool)
    # is_home comes from the game, so it is safe to fill from the team's other candidates.
    table["is_home"] = (table.groupby(["game_id", "team_id"])["is_home"]
                        .transform(lambda s: s.ffill().bfill())).fillna(False).astype(bool)
    table["season"] = season
    return table


def _team_context(season: str) -> pd.DataFrame:
    """Rest and schedule density per team-game, from the lineup-independent base table."""
    base = pd.read_parquet(paths.feature_table(season, "A").parent / f"base_{season}.parquet",
                           columns=["game_id", "team_id", "days_rest", "is_back_to_back",
                                    "games_last_4d"])
    return base.drop_duplicates(["game_id", "team_id"])


def _as_of(table: pd.DataFrame) -> pd.DataFrame:
    """Per-goalie history, strictly before the game in question.

    Denominators are that goalie's *candidate appearances on his team's slate*, which is the
    closest available stand-in for team games -- and counted this way round on purpose: the
    rest-of-season work found that dividing by games played rather than by chances makes a player
    who was passed over look like a player with no evidence.
    """
    table = table.sort_values(["team_id", "player_id", "game_date"]).copy()
    group = table.groupby(["team_id", "player_id"], sort=False)

    chances = group.cumcount()                                   # already excludes this game
    prior_starts = group["is_starter"].cumsum() - table["is_starter"].astype(int)
    prior_dressed = group["label_dressed"].cumsum() - table["label_dressed"].astype(int)
    prior_appear = group["appeared"].cumsum() - table["appeared"].astype(int)

    table["chances"] = chances
    table["start_share_std"] = np.where(chances > 0, prior_starts / chances.clip(lower=1), np.nan)
    table["dressed_share_std"] = np.where(chances > 0, prior_dressed / chances.clip(lower=1),
                                          np.nan)

    starter_int = table["is_starter"].astype(int)
    appear_int = table["appeared"].astype(int)
    shifted_start = group["is_starter"].shift(1)
    table["started_prev_game"] = shifted_start.fillna(False).astype(bool)

    def rolling_prior(series, window):
        return (series.groupby([table["team_id"], table["player_id"]], sort=False)
                .apply(lambda s: s.shift(1).rolling(window, min_periods=1).sum())
                .reset_index(level=[0, 1], drop=True))

    table["start_share_l10"] = rolling_prior(starter_int, 10) / 10.0
    table["starts_l5"] = rolling_prior(starter_int, 5)
    table["appearances_l5"] = rolling_prior(appear_int, 5)

    # How long since he last started, in his team's games and in days. A backup who has not
    # started in three weeks is a different proposition from one who started two nights ago.
    last_start_date = table["game_date"].where(table["is_starter"])
    prev_start_date = (last_start_date.groupby([table["team_id"], table["player_id"]], sort=False)
                       .apply(lambda s: s.shift(1).ffill())
                       .reset_index(level=[0, 1], drop=True))
    table["days_since_start"] = (table["game_date"] - prev_start_date).dt.days
    chance_of_start = pd.Series(np.where(table["is_starter"], chances, np.nan),
                                index=table.index)
    prev_start_chance = (chance_of_start
                         .groupby([table["team_id"], table["player_id"]], sort=False)
                         .apply(lambda s: s.shift(1).ffill())
                         .reset_index(level=[0, 1], drop=True))
    table["games_since_start"] = chances - prev_start_chance
    return table


def _within_team_game(table: pd.DataFrame) -> pd.DataFrame:
    """Features about the competition, which is what a start actually is.

    P(start) is a contest between the goalies a club dressed, so the informative quantities are
    relative: how many healthy candidates there are, whether the partner is hurt, and how this
    goalie's start share compares with the best of the others. A club whose starter is injured has
    a backup starting with probability near one, and no amount of his own history says that.
    """
    table = table.copy()
    healthy = ~table["injured_at_lockout"]
    by_game = table.groupby(["game_id", "team_id"], sort=False)

    table["n_healthy_candidates"] = by_game["injured_at_lockout"].transform(
        lambda s: int((~s).sum()))
    table["partner_injured"] = by_game["injured_at_lockout"].transform("sum") - \
        table["injured_at_lockout"].astype(int)

    share = table["start_share_std"].fillna(0.0).where(healthy, -1.0)
    group_max = share.groupby([table["game_id"], table["team_id"]]).transform("max")
    group_sum = share.clip(lower=0).groupby([table["game_id"], table["team_id"]]).transform("sum")
    # The best share among the OTHERS: if this goalie is the max, the partner's is the second best.
    is_max = share >= group_max - 1e-12
    second = (share.where(~is_max)
              .groupby([table["game_id"], table["team_id"]]).transform("max").fillna(0.0))
    table["partner_start_share"] = np.where(is_max, second, group_max)
    table["start_share_edge"] = share - table["partner_start_share"]
    table["start_share_rank"] = (share.groupby([table["game_id"], table["team_id"]])
                                 .rank(ascending=False, method="min"))
    table["share_of_share"] = np.where(group_sum > 0, share.clip(lower=0) / group_sum, np.nan)
    return table


def build(seasons) -> pd.DataFrame:
    frames = []
    for season in seasons:
        table = _candidates(season)
        table = table.merge(_team_context(season), on=["game_id", "team_id"], how="left")
        table = _within_team_game(_as_of(table))
        frames.append(table)
    out = pd.concat(frames, ignore_index=True)
    log.info("features: %d candidate rows over %d seasons, %.1f%% started",
             len(out), len(seasons), 100 * out["is_starter"].mean())
    return out


def _matrix(table: pd.DataFrame) -> pd.DataFrame:
    frame = table[FEATURES].copy()
    for column in ("started_prev_game", "injured_at_lockout", "is_back_to_back", "is_home"):
        frame[column] = frame[column].astype(float)
    return frame


def auc(labels, scores) -> float:
    """Rank-based AUC, so a baseline needs no fitting to be scored."""
    labels = np.asarray(labels, dtype=bool)
    scores = np.asarray(scores, dtype="float64")
    keep = ~np.isnan(scores)
    labels, scores = labels[keep], scores[keep]
    positives, negatives = labels.sum(), (~labels).sum()
    if not positives or not negatives:
        return float("nan")
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(len(scores), dtype="float64")
    ranks[order] = np.arange(1, len(scores) + 1)
    # Average ranks within ties, or a constant baseline scores 1.0 instead of 0.5.
    frame = pd.DataFrame({"s": scores, "r": ranks})
    ranks = frame.groupby("s")["r"].transform("mean").to_numpy()
    return float((ranks[labels].sum() - positives * (positives + 1) / 2)
                 / (positives * negatives))


def normalize(table: pd.DataFrame, column="p_start_raw", out="p_start") -> pd.DataFrame:
    """Exactly one goalie starts per team-game, so the predictions must sum to one.

    Without this the model's outputs are independent probabilities that happen to be about right
    on average and wrong on any particular night -- two goalies at 0.6, or both at 0.3.
    """
    table = table.copy()
    total = table.groupby(["game_id", "team_id"])[column].transform("sum")
    table[out] = np.where(total > 0, table[column] / total,
                          1.0 / table.groupby(["game_id", "team_id"])[column].transform("size"))
    return table


def models_dir_for(season, override=None) -> Path:
    """Where a P(start) booster held out of `season` lives. It used to be models/ whatever the
    seasons, so a second build silently replaced the first."""
    return override or paths.models_dir(season, "goalie_start")


def train(rounds=400, save=False, train_seasons=None, season=HOLDOUT_SEASON,
          models_dir=None) -> dict:
    train_seasons = list(train_seasons or TRAIN_SEASONS)
    if season in train_seasons:
        raise SystemExit(f"{season} is both trained on and held out")
    custom_dir = models_dir is not None
    models_dir = models_dir_for(season, models_dir)
    fit = build(train_seasons)
    holdout = build([season])

    booster = lgb.train(PARAMS, lgb.Dataset(_matrix(fit), label=fit["is_starter"].astype(int)),
                        num_boost_round=rounds)
    holdout = holdout.assign(p_start_raw=booster.predict(_matrix(holdout)))
    holdout = normalize(holdout)

    labels = holdout["is_starter"].to_numpy(dtype=bool)
    scores = {
        "model": holdout["p_start"].to_numpy(),
        "model_unnormalized": holdout["p_start_raw"].to_numpy(),
        "baseline_start_share": holdout["start_share_std"].to_numpy(),
        "baseline_started_prev_game": holdout["started_prev_game"].astype(float).to_numpy(),
        "baseline_dressed_share": holdout["dressed_share_std"].to_numpy(),
    }
    metrics = {name: auc(labels, value) for name, value in scores.items()}

    # Calibration: does a predicted 0.7 start 70% of the time? A normalized probability that only
    # ranks well is not enough here -- rung 4 multiplies it by a points line.
    bins = pd.cut(holdout["p_start"], np.linspace(0, 1, 11), include_lowest=True)
    calibration = (holdout.groupby(bins, observed=True)
                   .agg(n=("is_starter", "size"), predicted=("p_start", "mean"),
                        actual=("is_starter", "mean")).reset_index(drop=True))

    gain = booster.feature_importance("gain")
    top = sorted(zip(FEATURES, gain / max(gain.sum(), 1e-9)), key=lambda kv: -kv[1])[:8]

    log.info("holdout AUC -- model %.4f (unnormalized %.4f)", metrics["model"],
             metrics["model_unnormalized"])
    log.info("baselines   -- season start share %.4f | dressed share %.4f | "
             "started previous game %.4f (an anti-predictor by construction)",
             metrics["baseline_start_share"], metrics["baseline_dressed_share"],
             metrics["baseline_started_prev_game"])
    log.info("top gain: %s", ", ".join(f"{k} {v:.3f}" for k, v in top))
    print("\ncalibration on the holdout")
    print(calibration.to_string(index=False, float_format=lambda v: f"{v:.3f}"))

    sidecar = {
        "target": "is_starter",
        "trained_on": train_seasons,
        "scored_on": season,
        "rows": int(len(fit)),
        "rounds": rounds,
        "auc": metrics,
        "top_features": [{"feature": k, "gain_share": round(v, 4)} for k, v in top],
        "calibration": calibration.to_dict("records"),
        "note": ("P(start) only. Per-start goalie quality is not modelled -- points R2 -0.8%, "
                 "save% r=+0.026, decision AUC 0.559 -- so the line is league average and this "
                 "is the only factor that varies. Predictions are normalized within team-game "
                 "because exactly one goalie starts."),
        "params": PARAMS,
        "feature_columns": FEATURES,
    }
    if save:
        paths.ensure(models_dir)
        booster.save_model(str(models_dir / "goalie_start.txt"))
        (models_dir / "goalie_start.json").write_text(
            json.dumps(sidecar, indent=2, default=str), encoding="utf-8")
        log.info("saved -> %s", models_dir / "goalie_start.txt")
    # A build aimed at its own directory (an experiment, verify.py's no-clobber check) keeps its
    # metrics there too, so it cannot overwrite the reported ones either.
    metrics_dir = models_dir if custom_dir else paths.REPORTS_DIR
    paths.ensure(metrics_dir)
    (metrics_dir / f"goalie_start_metrics_{season}.json").write_text(
        json.dumps(sidecar, indent=2, default=str), encoding="utf-8")
    return sidecar


def train_deployment(rounds=400, train_seasons=None, models_dir=None) -> dict:
    """The build to ship: every season in the fit, nothing scored, filed under the season after
    the last one trained on (`paths.target_season`). Same features and parameters as `train`, so
    the scored build's metrics describe it."""
    train_seasons = list(train_seasons or TRAIN_SEASONS)
    models_dir = models_dir or paths.models_dir(paths.target_season(train_seasons), "goalie_start")
    fit = build(train_seasons)
    booster = lgb.train(PARAMS, lgb.Dataset(_matrix(fit), label=fit["is_starter"].astype(int)),
                        num_boost_round=rounds)
    gain = booster.feature_importance("gain")
    top = sorted(zip(FEATURES, gain / max(gain.sum(), 1e-9)), key=lambda kv: -kv[1])[:8]
    scored = max(train_seasons)
    sidecar = {
        "target": "is_starter",
        "trained_on": train_seasons,
        "deployment_build": True,
        # A deployment build has no metrics of its own; quote the last scored build's, and say so.
        "scored_on": None,
        "metrics_from": f"goalie_start_metrics_{scored}.json",
        "rows": int(len(fit)),
        "rounds": rounds,
        "top_features": [{"feature": k, "gain_share": round(v, 4)} for k, v in top],
        "params": PARAMS,
        "feature_columns": FEATURES,
    }
    paths.ensure(models_dir)
    booster.save_model(str(models_dir / "goalie_start.txt"))
    (models_dir / "goalie_start.json").write_text(json.dumps(sidecar, indent=2, default=str),
                                                  encoding="utf-8")
    log.info("deployment build on %s saved -> %s", ", ".join(train_seasons),
             models_dir / "goalie_start.txt")
    return sidecar


def predict(season: str, models_dir=None) -> pd.DataFrame:
    models_dir = models_dir_for(season, models_dir)
    booster = lgb.Booster(model_file=str(models_dir / "goalie_start.txt"))
    sidecar = json.loads((models_dir / "goalie_start.json").read_text(encoding="utf-8"))
    if season in sidecar.get("trained_on", []):
        log.warning("%s is in this model's training seasons %s -- an in-sample P(start) would "
                    "flatter anything that consumes it", season, sidecar["trained_on"])
    table = build([season]).assign(p_start_raw=lambda t: booster.predict(_matrix(t)))
    table = normalize(table)
    out = table[["season_id", "game_id", "game_date", "team_id", "player_id", "p_start",
                 "p_start_raw", "injured_at_lockout"]].copy()
    path = paths.REPORTS_DIR / f"goalie_pstart_{season}.parquet"
    paths.ensure(paths.REPORTS_DIR)
    out.to_parquet(path, index=False)
    log.info("%s: %d rows, mean p_start %.4f -> %s", season, len(out), out["p_start"].mean(), path)
    return out


def main():
    args = parse_args()
    train_seasons = [s.strip() for s in args.train_seasons.split(",") if s.strip()]
    if args.no_holdout:
        if args.train or args.predict:
            raise SystemExit("--no-holdout is its own build: it scores nothing and there is no "
                             "unseen season to predict")
        train_deployment(rounds=args.rounds, train_seasons=train_seasons,
                         models_dir=args.models_dir)
        return
    if args.train:
        train(rounds=args.rounds, save=args.save, train_seasons=train_seasons,
              season=args.season, models_dir=args.models_dir)
    if args.predict:
        predict(args.season, args.models_dir)
    if not args.train and not args.predict:
        raise SystemExit("nothing to do: pass --train and/or --predict, or --no-holdout")


if __name__ == "__main__":
    main()

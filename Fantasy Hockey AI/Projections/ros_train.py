"""The gradient-boosted rung of the rest-of-season ladder.

`ros_baselines.py` establishes what has to be beaten: empirical-Bayes shrinkage on each
factor. This asks whether the 440-odd features in the base table add anything on top of a
player's own history, and it is built so that the answer can be *no* without the work being
wasted -- the shrunk estimate is handed to each model as a feature, so the trees start from
the baseline and only have to learn a correction to it.

Three choices that follow from what the baselines already taught:

**L1 objectives.** The constants in `ros_baselines.py` were first fitted to squared error and
reported on absolute error, which put a bimodal availability estimate in the middle of a gap
nobody lives in. Same discipline here: the reported metric is MAE, so the models optimize it.

**Rows weighted by how much window they had.** A forward rate measured over eighteen games is
a far better label than one measured over two, and weighting by the games behind the target
stops the models chasing the noise in short windows. Availability is weighted by the team
games in its window, the rest by the player's games in it.

**Per-factor models, not one points model.** The same decomposition the baselines use --
`team games x availability x ice time x rate per 60` -- so the comparison is like for like,
and so the output stays scoring-agnostic.

**Early stopping cannot be trusted here, so the round budget does the work.** A rest-of-season
label looks *forward*, so a fit row from November carries a window covering the same games the
validation rows' windows cover: the two share outcomes, and a validation loss computed on them
keeps improving long past the point where the model has stopped generalizing. It never fired
in practice -- every factor ran to whatever cap it was given. The per-game stack in `train.py`
has no such problem, because its labels are single games.

So the cap was swept against a genuinely unseen season instead, and it matters more than it
looks. Window fantasy points on 2025-26, points-league: 100 rounds gives MAE 26.41 and
Spearman 0.854, 250 gives 26.48 and 0.851, 500 gives 26.71 and 0.849, and 2000 gives 27.09
and 0.845 -- monotone, with more boosting steadily *worse* on accuracy and ranking while
slowly improving level bias (+1.8% at 100 against +0.6% at 2000). The default is 250, which
sits within 0.3% of the best MAE while keeping the bias meaningfully lower.

**Deployment builds carry no metrics, and say so.** `--no-holdout` trains on every season
given and skips scoring entirely, which is what to ship before a season starts; the numbers
quoted anywhere come from the last build that *was* scored, against a season it never saw.
The sidecar records which, and `ros_predict.py` prints it on every run, because a projection
whose provenance is unclear is worse than no projection.

Saved models are namespaced by horizon. A season-length window and a six-week window are
different labels with different noise, and loading one where the other is meant is the sort
of mistake that produces plausible numbers.

**A scored build can also hand its test-season projections to a consumer.** `--predictions-out`
writes one row per player per date for every test row -- not only the thinned rows the metrics
are computed on -- with the projected stat line over the window and the realized one as
`target_*`. That is the rest-of-season input a backtest may use: trained on seasons before the
one it projects, and carrying its outcomes so the consumer can prove as much. It never touches
`models/`, which keeps the deployment build there intact.

Usage:
    python ros_train.py --train 2023-24 2024-25 --test 2025-26 --weights points-league
    python ros_train.py --train 2023-24 2024-25 --test 2025-26 --horizon season --predictions-out
    python ros_train.py --train 2023-24 2024-25 2025-26 --horizon season --no-holdout --save
"""

import argparse
import json
import logging

import lightgbm as lgb
import numpy as np
import pandas as pd

import data
import paths
import ros
import ros_baselines as baselines
import weights as weights_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ros_train")

PARAMS = {
    "objective": "regression_l1",
    "metric": ["l1"],
    "learning_rate": 0.04,
    "num_leaves": 31,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.7,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 5.0,
    "num_threads": 0,
    "verbosity": -1,
    "seed": 17,
}

# Columns that describe the future, or identify a row rather than describing it.
LEAKY_PREFIXES = ("window_", "ros_", "target_", "label_")
DROP = set(data.DROP_FROM_FEATURES) | {
    "season", "window_end", "season_end", "window_complete", "source_game_id",
    "source_game_date", "source_team_id",
}
# The schedule is published in advance, so how many games his team plays in the window is
# knowable at the time and belongs in the features despite the window_ prefix.
KEEP = {"window_team_games"}


def parse_args():
    parser = argparse.ArgumentParser(description="Train the ROS model rung")
    parser.add_argument("--train", nargs="+", default=["2023-24", "2024-25"])
    parser.add_argument("--test", default="2025-26")
    parser.add_argument("--horizon", default=str(ros.DEFAULT_HORIZON_DAYS),
                        help="Days, or 'season' for windows running to the season's end")
    parser.add_argument("--save", action="store_true",
                        help="Write the boosters and the fitted shrinkage to models/, so "
                             "ros_predict.py can use them")
    parser.add_argument("--no-holdout", action="store_true",
                        help="Train on every season given and score nothing -- the build to "
                             "ship. Implies --save; the last scored build supplies the "
                             "metrics, and the sidecar records that it did")
    parser.add_argument("--weights", action="append", default=None, metavar="FILE")
    parser.add_argument("--train-thin-days", type=int, default=3)
    parser.add_argument("--thin-days", type=int, default=7)
    parser.add_argument("--loss", choices=("mae", "mse"), default="mae",
                        help="Loss the shrinkage constants are fitted to")
    parser.add_argument("--objective", choices=("l1", "l2"), default="l1",
                        help="LightGBM objective. l1 fits the conditional MEDIAN, which "
                             "ranks well but over-projects a left-skewed factor like "
                             "availability and compounds into every total; l2 fits the "
                             "mean, which is what a total wants")
    parser.add_argument("--rounds", type=int, default=250,
                        help="Boosting rounds, and the real regularizer here -- see the "
                             "note on early stopping in the module docstring")
    parser.add_argument("--early-stopping", type=int, default=100)
    parser.add_argument("--exclude-feature", action="append", default=[], metavar="COLUMN",
                        help="Drop this feature column before training (repeatable) -- for an "
                             "ablation. Needs --tag, so the build does not replace the real one")
    parser.add_argument("--tag", default=None,
                        help="Suffix for this build's models, predictions and summaries, e.g. "
                             "no-age, so an experiment sits beside the real build")
    parser.add_argument("--out", default="ros_model.json")
    parser.add_argument("--predictions-out", action="store_true",
                        help="Also write every test row's projection, with its realized window, "
                             "to reports/ros_predictions_<horizon>_<test>.parquet")
    return parser.parse_args()


def model_prefix(horizon):
    """Saved models are namespaced by horizon: the labels are not interchangeable."""
    return f"ros_{ros.suffix(horizon)}"


def feature_columns(frame):
    columns = [c for c in frame.columns
               if (not c.startswith(LEAKY_PREFIXES) or c in KEEP) and c not in DROP]
    leaked = [c for c in columns
              if c.startswith(("ros_",)) or (c.startswith("window_") and c not in KEEP)]
    assert not leaked, f"future information reached the feature matrix: {leaked}"
    return columns


def matrix(frame, columns):
    out = frame.reindex(columns=columns).copy()
    for column in out.columns:
        if out[column].dtype == bool:
            out[column] = out[column].astype("float32")
        elif (pd.api.types.is_object_dtype(out[column])
              or pd.api.types.is_string_dtype(out[column])):
            # pandas 2 hands these back as its own string dtype rather than object, which
            # LightGBM refuses; categories are what it wants either way.
            out[column] = out[column].astype("category")
    return out


def weight_column(frame, factor):
    """How much window stood behind this label."""
    if factor == "availability":
        return frame["window_team_games"].to_numpy("float64")
    return frame["window_played"].to_numpy("float64")


def add_shrunk(frame, fitted):
    """The baseline's answer, handed to the model as a starting point."""
    shrunk = baselines.predict(frame, fitted, "shrunk")
    for factor in baselines.FACTORS:
        frame[f"shrunk_{factor}"] = shrunk[factor].to_numpy()
    return frame


def train_factor(train, valid, factor, columns, rounds, early_stopping):
    spec = baselines.FACTORS[factor]
    target = spec["target"]

    def prepare(frame):
        rows = frame[np.isfinite(frame[target]) & (weight_column(frame, factor) > 0)]
        return (matrix(rows, columns), rows[target].to_numpy("float64"),
                weight_column(rows, factor))

    x_train, y_train, w_train = prepare(train)
    x_valid, y_valid, w_valid = prepare(valid)
    booster = lgb.train(
        dict(PARAMS), lgb.Dataset(x_train, y_train, weight=w_train),
        num_boost_round=rounds,
        valid_sets=[lgb.Dataset(x_valid, y_valid, weight=w_valid)],
        callbacks=[lgb.early_stopping(early_stopping, verbose=False)])
    scores = booster.best_score["valid_0"]
    metric, value = next(iter(scores.items()))
    log.info("%-16s %6d fit rows, %5d valid, best iteration %4d, valid %s %.4f",
             factor, len(y_train), len(y_valid), booster.best_iteration, metric, value)
    return booster


def top_features(booster, columns, count=8):
    gains = booster.feature_importance("gain")
    total = gains.sum() or 1.0
    order = np.argsort(gains)[::-1][:count]
    return [{"feature": columns[i], "gain_share": round(float(gains[i] / total), 4)}
            for i in order]


def predictions_path(horizon, season, tag=None):
    suffix = f"_{tag}" if tag else ""
    return paths.REPORTS_DIR / f"ros_predictions_{ros.suffix(horizon)}_{season}{suffix}.parquet"


def write_predictions(test, boosters, columns, args):
    """Every test row's projected window, beside what actually happened in it."""
    factors = pd.DataFrame(index=test.index)
    x = matrix(test, columns)
    for factor, booster in boosters.items():
        factors[factor] = np.clip(booster.predict(x, num_iteration=booster.best_iteration),
                                  0, None)
    factors["availability"] = factors["availability"].clip(0, 1)
    projected = baselines.to_totals(test, factors)
    realized = baselines.actual_totals(test)

    out = test[["player_id", "team_id", "game_date", "position", "window_team_games"]].copy()
    out["season"] = args.test
    out["trained_on"] = ",".join(args.train)
    for factor in factors.columns:
        out[f"pred_{factor}"] = factors[factor].to_numpy("float64")
    for column in projected.columns:
        out[f"proj_{column}"] = projected[column].to_numpy("float64")
    for column in realized.columns:
        out[f"target_{column}"] = realized[column].to_numpy("float64")
    destination = (paths.ensure(paths.REPORTS_DIR)
                   / predictions_path(args.horizon, args.test, args.tag).name)
    out.to_parquet(destination, index=False)
    log.info("wrote %d projected windows for %s (%d players, trained on %s) to %s",
             len(out), args.test, out["player_id"].nunique(), out["trained_on"].iloc[0],
             destination)


def run(args):
    if args.predictions_out and args.no_holdout:
        raise SystemExit("--predictions-out needs a scored build: a deployment build has no "
                         "unseen season to project")
    if args.exclude_feature and not args.tag:
        raise SystemExit("--exclude-feature needs --tag, or the ablation replaces the real build")
    frames = [baselines.load(s, args.horizon).drop(columns=args.exclude_feature)
              for s in args.train]
    train_all = baselines.add_asof(pd.concat(frames, ignore_index=True))
    deployment = args.no_holdout
    if deployment:
        if args.test in args.train:
            log.info("deployment build: training on %s, scoring nothing",
                     ", ".join(args.train))
        test = None
    else:
        test = baselines.add_asof(baselines.load(args.test, args.horizon)
                                  .drop(columns=args.exclude_feature))

    # Fit the shrinkage on the *training* rows only, thinned the same way the ladder was.
    fitted = {}
    ladder_train = baselines.thin(train_all, args.thin_days)
    for factor in baselines.FACTORS:
        prior = baselines.fit_prior(ladder_train, factor)
        k = baselines.fit_k(ladder_train, factor, prior, args.loss)
        fitted[factor] = {"prior": prior, "k": k}
        fitted[factor]["recency"] = baselines.fit_recency(ladder_train, fitted, factor,
                                                          args.loss)
    train_all = add_shrunk(train_all, fitted)
    if test is not None:
        test = add_shrunk(test, fitted)

    train_rows = baselines.thin(train_all, args.train_thin_days)
    cutoff = train_rows["game_date"].quantile(0.75)
    fit_rows = train_rows[train_rows["game_date"] <= cutoff]
    valid_rows = train_rows[train_rows["game_date"] > cutoff]
    test_rows = baselines.thin(test, args.thin_days) if test is not None else None
    log.info("fit %d rows to %s, early-stop on %d after it, test %s",
             len(fit_rows), cutoff.date(), len(valid_rows),
             f"{len(test_rows)} rows" if test_rows is not None else "none (deployment build)")

    columns = feature_columns(train_rows)
    log.info("%d features, %s objective", len(columns), args.objective)
    if args.objective == "l2":
        PARAMS["objective"], PARAMS["metric"] = "regression", ["l2"]

    predictions = (pd.DataFrame(index=test_rows.index) if test_rows is not None else None)
    importances = {}
    boosters = {}
    # Filed by the season the build predicts: the test season, or for a deployment build the
    # season after the last one trained on.
    prefix = model_prefix(args.horizon)
    models = (paths.ensure(paths.models_dir(
        paths.target_season(args.train, None if deployment else args.test),
        f"{prefix}_{args.tag}" if args.tag else prefix))
        if (args.save or deployment) else None)
    for factor in baselines.FACTORS:
        booster = train_factor(fit_rows, valid_rows, factor, columns, args.rounds,
                               args.early_stopping)
        if predictions is not None:
            predictions[factor] = np.clip(
                booster.predict(matrix(test_rows, columns),
                                num_iteration=booster.best_iteration), 0, None)
        importances[factor] = top_features(booster, columns)
        boosters[factor] = booster
        if models is not None:
            booster.save_model(str(models / f"{prefix}_{factor}.txt"),
                               num_iteration=booster.best_iteration)
    if predictions is not None:
        predictions["availability"] = predictions["availability"].clip(0, 1)
    if models is not None:
        # The shrinkage fit travels with the boosters: the models were trained with the
        # shrunk estimate as a feature, so a consumer that cannot rebuild it cannot use them.
        sidecar = {
            "horizon": args.horizon,
            "trained_on": args.train,
            "deployment_build": deployment,
            # A deployment build has no metrics of its own. Whatever is quoted for it comes
            # from the last build that was scored against a season it never trained on, and
            # naming that here is what stops the two being confused later.
            "scored_on": None if deployment else args.test,
            "metrics_from": (None if deployment else args.out),
            "early_stop_cutoff": str(cutoff.date()),
            "fit_rows": len(fit_rows),
            "early_stop_rows": len(valid_rows),
            "objective": args.objective,
            "feature_columns": columns,
            "loss": args.loss,
            "shrinkage": {factor: {"k": entry["k"], "recency": entry["recency"],
                                   "prior": entry["prior"]}
                          for factor, entry in fitted.items()},
        }
        (models / f"{prefix}_fit.json").write_text(json.dumps(sidecar, indent=2),
                                                   encoding="utf-8")
        log.info("saved %d boosters and the shrinkage fit to %s as %s_*",
                 len(baselines.FACTORS), models, prefix)
        if deployment:
            log.warning("this is a deployment build: it was scored against nothing. The "
                        "numbers to quote come from the last scored build.")

    if test is not None and args.predictions_out:
        write_predictions(test, boosters, columns, args)

    if test_rows is None:
        return {"deployment_build": True, "trained_on": args.train,
                "horizon": args.horizon, "features": len(columns),
                "importances": importances}

    scoresets = [weights_module.load(w) for w in (args.weights or [])]
    truth = baselines.actual_totals(test_rows)
    report = {"train": args.train, "test": args.test, "horizon_days": args.horizon,
              "features": len(columns), "rows": {"fit": len(fit_rows),
                                                 "valid": len(valid_rows),
                                                 "test": len(test_rows)},
              "importances": importances, "rungs": {}}

    for rung in ("season", "shrunk", "blended", "model"):
        factors = (predictions if rung == "model"
                   else baselines.predict(test_rows, fitted, rung))
        totals = baselines.to_totals(test_rows, factors)
        entry = {"factors": {}, "composite": {}}
        for factor, spec in baselines.FACTORS.items():
            target = test_rows[spec["target"]].to_numpy("float64")
            good = np.isfinite(target)
            error = factors[factor].to_numpy("float64")[good] - target[good]
            # Bias as well as MAE, because an L1 objective fits the median and a median
            # estimate of a skewed factor is biased as a mean -- which is invisible in MAE
            # and compounds into every total built by multiplying the factors together.
            entry["factors"][factor] = {
                "mae": round(float(np.mean(np.abs(error))), 4),
                "bias_pct": round(float(100 * error.mean()
                                        / max(abs(target[good].mean()), 1e-9)), 2)}
        for scoreset in scoresets:
            predicted_points = scoreset.score(totals)
            actual_points = scoreset.score(truth)
            error = predicted_points - actual_points
            entry["composite"][scoreset.name] = {
                "mae": round(float(error.abs().mean()), 3),
                "rmse": round(float(np.sqrt((error ** 2).mean())), 3),
                "bias_pct": round(float(100 * error.mean() / actual_points.mean()), 2),
                "spearman": round(float(predicted_points.corr(actual_points,
                                                              method="spearman")), 4)}
        report["rungs"][rung] = entry

    destination = paths.ensure(paths.REPORTS_DIR) / args.out
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print_report(report, scoresets)
    log.info("wrote %s", destination)
    return report


def print_report(report, scoresets):
    for scoreset in scoresets:
        print(f"\n=== window fantasy points under {scoreset.name} ===")
        rows = []
        for rung, entry in report["rungs"].items():
            composite = entry["composite"][scoreset.name]
            rows.append([rung, composite["mae"], composite["rmse"],
                         f'{composite["bias_pct"]:+.1f}%', composite["spearman"]])
        print(pd.DataFrame(rows, columns=["rung", "MAE", "RMSE", "bias",
                                          "Spearman"]).to_string(index=False))

    print("\n=== per-factor MAE (bias %) ===")
    factors = list(report["rungs"]["shrunk"]["factors"])
    rows = [[rung] + ["%.4f (%+.1f)" % (entry["factors"][f]["mae"],
                                        entry["factors"][f]["bias_pct"]) for f in factors]
            for rung, entry in report["rungs"].items()]
    print(pd.DataFrame(rows, columns=["rung"] + factors).to_string(index=False))

    print("\n=== what the models leaned on ===")
    for factor in ("goals_p60", "shots_p60", "toi_per_game", "availability"):
        if factor in report["importances"]:
            top = report["importances"][factor][:5]
            print("  %-14s %s" % (factor, ", ".join(
                f'{e["feature"]} {e["gain_share"]:.3f}' for e in top)))


def main():
    run(parse_args())


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Fits the skater projection models.

Runs the stack in `targets.PIPELINE` order, because the later models offset on the earlier
ones. Two kinds of prediction come out of each offset-supplying model:

    out-of-fold   for the training rows, from K-fold models that never saw them -- this is
                  what downstream models offset against, so they meet the same upstream
                  error at training time as they will at the lock.
    final         from the model fit on the fitting slice with early stopping on its tail,
                  used for the holdout rows (already genuinely out of sample).

Counting-stat models are *fit* on rows where the player played, but *predict* on every
candidate: the projection means "what he does if he plays", and `plays` supplies the
probability that he does.

Read-only with respect to NHLStats -- this folder never opens a database connection.

Usage:
    python train.py --all
    python train.py --target shots --variant A
    python train.py --all --walk-forward
"""

import argparse
import json
import logging
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

import data
import paths
import targets as targets_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("train")

TRAIN_SEASONS = ["2023-24", "2024-25"]
HOLDOUT_SEASON = "2025-26"
NUM_ROUNDS = 3000
EARLY_STOPPING_ROUNDS = 100
OOF_FOLDS = 4


def parse_args():
    parser = argparse.ArgumentParser(description="Fit the skater projection models")
    parser.add_argument("--target", action="append", help="Target to fit; repeat, or use --all")
    parser.add_argument("--all", action="store_true", help="Fit the whole pipeline in order")
    parser.add_argument("--variant", choices=("A", "B"), default="B",
                        help="Lineup variant to train on (default B: actual lineup + noise)")
    parser.add_argument("--train-seasons", default=",".join(TRAIN_SEASONS))
    parser.add_argument("--holdout-season", default=HOLDOUT_SEASON)
    parser.add_argument("--no-holdout", action="store_true",
                        help="Train on every --train-seasons season with nothing held back. "
                             "The deployment build: no metrics come out of it, so docs/ keeps "
                             "describing the last model that was actually scored.")
    parser.add_argument("--features-dir", type=Path, default=None)
    parser.add_argument("--exclude-feature", action="append", default=[], metavar="COLUMN",
                        help="Drop this feature column before training (repeatable) -- for an "
                             "ablation. Needs --tag, so the build does not replace the real one")
    parser.add_argument("--tag", default=None,
                        help="Suffix for this build's models, predictions and summaries, e.g. "
                             "no-age, so an experiment sits beside the real build")
    parser.add_argument("--models-dir", type=Path, default=None,
                        help="Where the boosters go. Default models/<season>/skaters/<variant>/, where "
                             "<season> is the one the build predicts: the holdout, or for a "
                             "deployment build (--no-holdout) the season after the last trained")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Refit monthly across the holdout season instead of once")
    parser.add_argument("--folds", type=int, default=OOF_FOLDS,
                        help="Out-of-fold splits for the offset-supplying models")
    return parser.parse_args()


def fit_booster(target, matrix, labels, weights, offset, fit_rows, early_rows, categorical):
    """One LightGBM fit, early-stopped on `early_rows`."""
    def dataset(rows, reference=None):
        kwargs = {}
        if offset is not None:
            kwargs["init_score"] = offset[rows]
        return lgb.Dataset(matrix.iloc[rows], label=labels[rows], weight=weights[rows],
                           categorical_feature=categorical, reference=reference,
                           free_raw_data=False, **kwargs)

    train_set = dataset(fit_rows)
    valid_set = dataset(early_rows, reference=train_set)
    evals = {}
    booster = lgb.train(
        target.lgb_params(), train_set, num_boost_round=NUM_ROUNDS,
        valid_sets=[valid_set], valid_names=["early"],
        callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False),
                   lgb.record_evaluation(evals),
                   lgb.log_evaluation(period=250)],
    )
    return booster, evals


def predict(booster, target, matrix, offset):
    """Predictions on the natural scale, with the offset added back in link space."""
    raw = booster.predict(matrix, num_iteration=booster.best_iteration, raw_score=True)
    if offset is not None:
        raw = raw + offset
    objective = target.objective
    if objective in ("poisson", "tweedie"):
        return np.exp(raw)
    if objective in ("binary", "cross_entropy"):
        return 1.0 / (1.0 + np.exp(-raw))
    return raw


def out_of_fold(target, matrix, labels, weights, offset, rows, categorical, folds):
    """K-fold predictions for `rows`, grouped by game so a game never straddles a fold.

    Grouping by game (not by row) also keeps variant B's perturbed copies of one candidate
    inside a single fold -- they differ only in their lineup, so splitting them would leak.
    """
    predictions = np.full(len(matrix), np.nan)
    assignment = out_of_fold.game_of[rows] % folds

    for fold in range(folds):
        holdin = rows[assignment != fold]
        holdout = rows[assignment == fold]
        if len(holdout) == 0:
            continue
        cut = int(len(holdin) * 0.9)
        booster, _ = fit_booster(target, matrix, labels, weights, offset,
                                 holdin[:cut], holdin[cut:], categorical)
        fold_offset = None if offset is None else offset[holdout]
        predictions[holdout] = predict(booster, target, matrix.iloc[holdout], fold_offset)
        log.info("  oof fold %d/%d: %d rows, %d trees", fold + 1, folds,
                 len(holdout), booster.best_iteration)
    return predictions


def train_one(target, table, matrix, columns, split, chain, weights):
    """Fit one target and return its predictions for every row, plus a metrics record."""
    categorical = data.categorical_in(columns)
    labels = targets_module.label_values(target, table).to_numpy(dtype="float64")
    mask = targets_module.row_mask(table, target.rows)

    row_weights = weights.copy()
    if target.weight_column:
        row_weights = row_weights * table[target.weight_column].fillna(0).to_numpy("float64")

    offset = targets_module.offset_values(target, chain)
    usable = mask & np.isfinite(labels) & (row_weights > 0)

    positions = pd.Series(np.arange(len(table)), index=table.index)
    fit_rows = positions.loc[split.fit].to_numpy()
    early_rows = positions.loc[split.early].to_numpy()
    holdout_rows = positions.loc[split.holdout].to_numpy()
    fit_rows = fit_rows[usable[fit_rows]]
    early_rows = early_rows[usable[early_rows]]

    # Start the model mean-matched rather than at the offset's implied rate of 1.0 per 60.
    intercept = 0.0
    if offset is not None:
        intercept = targets_module.mean_matching_intercept(labels, row_weights, offset, fit_rows)
        offset = offset + intercept
        log.info("%s: offset intercept %+.4f (zero-tree mean %.4f)", target.name, intercept,
                 float(np.mean(np.exp(offset[fit_rows]))))

    started = time.time()
    log.info("%s: fitting on %d rows (early-stop %d), objective %s, offset %s",
             target.name, len(fit_rows), len(early_rows), target.objective, target.offset or "-")
    booster, evals = fit_booster(target, matrix, labels, row_weights, offset,
                                 fit_rows, early_rows, categorical)

    predictions = predict(booster, target, matrix, offset)

    # p_plays multiplies every other projection, so a miscalibrated probability biases the
    # whole stack -- and the raw booster runs 5-9 points hot exactly in the uncertain middle
    # (a predicted 0.35 dresses 0.27 of the time), which is where the marginal players a
    # waiver decision is about live. An isotonic fit on the early-stop slice -- never the
    # holdout -- flattens that without touching the confident tails.
    calibration = None
    if target.name == "plays" and len(early_rows) > 1000:
        isotonic = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        isotonic.fit(predictions[early_rows], labels[early_rows],
                     sample_weight=row_weights[early_rows])
        knots_x = np.asarray(isotonic.X_thresholds_, dtype="float64")
        knots_y = np.asarray(isotonic.y_thresholds_, dtype="float64")
        before = predictions.copy()
        predictions = np.interp(predictions, knots_x, knots_y)
        calibration = {"x": knots_x.tolist(), "y": knots_y.tolist()}
        log.info("%s: isotonic calibration over %d knots (mean %.4f -> %.4f)",
                 target.name, len(knots_x), before.mean(), predictions.mean())

    # Offset-supplying models also need out-of-fold predictions over the training rows, so
    # the models that chain off them never see a suspiciously accurate upstream value.
    if target.name in ("toi", "shots"):
        training_rows = np.concatenate([fit_rows, early_rows])
        oof = out_of_fold(target, matrix, labels, row_weights, offset,
                          training_rows, categorical, out_of_fold.folds)
        replace = np.isfinite(oof)
        predictions = np.where(replace, oof, predictions)

    record = {
        "target": target.name,
        "objective": target.objective,
        "offset": target.offset,
        "offset_intercept": round(intercept, 6),
        "rows": target.rows,
        "fit_rows": int(len(fit_rows)),
        "early_rows": int(len(early_rows)),
        "holdout_rows": int(len(holdout_rows)),
        "features": len(columns),
        "best_iteration": int(booster.best_iteration),
        "early_stop_metric": {name: float(values[booster.best_iteration - 1])
                              for name, values in evals.get("early", {}).items()},
        "seconds": round(time.time() - started, 1),
        "note": target.note,
        "top_features": top_features(booster, columns, 20),
    }
    if calibration:
        record["calibration"] = calibration
    log.info("%s: %d trees, %.0fs, early-stop %s", target.name, booster.best_iteration,
             record["seconds"], record["early_stop_metric"])
    return booster, predictions, record


def top_features(booster, columns, count):
    gains = booster.feature_importance(importance_type="gain")
    order = np.argsort(gains)[::-1][:count]
    total = gains.sum() or 1.0
    return [{"feature": columns[i], "gain_share": round(float(gains[i] / total), 4)}
            for i in order]


def run(args):
    train_seasons = [s.strip() for s in args.train_seasons.split(",") if s.strip()]
    holdout_season = None if args.no_holdout else args.holdout_season
    seasons = train_seasons + ([holdout_season] if holdout_season else [])
    table = data.load_seasons(seasons, args.variant, args.features_dir)
    if args.exclude_feature and not args.tag:
        raise SystemExit("--exclude-feature needs --tag, or the ablation replaces the real build")
    absent = [c for c in args.exclude_feature if c not in table.columns]
    if absent:
        raise SystemExit(f"--exclude-feature names column(s) the table does not have: {absent}")
    table = table.drop(columns=args.exclude_feature)
    matrix, columns = data.build_feature_matrix(table)
    split = data.chronological_split(table, train_seasons, holdout_season)
    if holdout_season is None:
        log.warning("no holdout: this is a deployment build, and it produces no metrics")
    log.info("%s over %d features, variant %s", split, len(columns), args.variant)

    weights = data.copy_weights(table)
    out_of_fold.game_of = table["game_id"].to_numpy(dtype="int64")
    out_of_fold.folds = args.folds

    wanted = ([t.name for t in targets_module.PIPELINE] if args.all or not args.target
              else args.target)
    unknown = [name for name in wanted if name not in targets_module.BY_NAME]
    if unknown:
        raise SystemExit(f"unknown target(s): {unknown}")

    # The chain always runs from the start: a later model cannot be fit without the offsets
    # its predecessors supply, so requesting one target still computes what it depends on.
    needed = required_targets(wanted)
    chain = pd.DataFrame(index=table.index)
    records, boosters = [], {}

    # Every run used to save the same eleven files into models/, whatever its variant or holdout,
    # so a variant-A build, a variant-B build and the deployment build silently replaced one
    # another. Each build now has its own folder, named for the season it predicts.
    models_dir = args.models_dir or paths.models_dir(
        paths.target_season(train_seasons, holdout_season), "skaters",
        f"{args.variant}-{args.tag}" if args.tag else args.variant)
    paths.ensure(models_dir)
    paths.ensure(paths.REPORTS_DIR)
    log.info("boosters -> %s", models_dir)
    provenance = {"variant": args.variant, "train_seasons": train_seasons,
                  "holdout_season": holdout_season}

    for target in targets_module.PIPELINE:
        if target.name not in needed:
            continue
        booster, predictions, record = train_one(
            target, table, matrix, columns, split, chain, weights)
        chain[target.name] = predictions
        boosters[target.name] = booster
        if target.name in wanted:
            records.append(record)
            booster.save_model(str(models_dir / f"{target.name}.txt"),
                               num_iteration=booster.best_iteration)
            (models_dir / f"{target.name}.json").write_text(
                json.dumps({**record, **provenance, "params": target.lgb_params(),
                            "feature_columns": columns}, indent=2), encoding="utf-8")

    if len(split.holdout):
        save_predictions(table, split, chain, args.variant, holdout_season, args.tag)

    # training_<variant>.json describes what is in models/, so only a deployment build writes it.
    stem = (f"training_{args.variant}" if holdout_season is None
            else f"training_{args.variant}_{holdout_season}")
    suffix = f"_{args.tag}" if args.tag else ""
    summary_path = paths.REPORTS_DIR / f"{stem}{suffix}.json"
    summary_path.write_text(json.dumps({
        "models_dir": str(models_dir),
        "excluded_features": args.exclude_feature,
        "tag": args.tag,
        "variant": args.variant,
        "train_seasons": train_seasons,
        "holdout_season": holdout_season,
        "early_stop_cutoff": split.cutoff.strftime("%Y-%m-%d"),
        "features": len(columns),
        "models": records,
    }, indent=2), encoding="utf-8")
    log.info("wrote %s", summary_path.name)
    return chain, table, split


def required_targets(wanted):
    """`wanted` plus everything they offset on, transitively."""
    needed = set()
    pending = list(wanted)
    while pending:
        name = pending.pop()
        if name in needed:
            continue
        needed.add(name)
        offset = targets_module.BY_NAME[name].offset
        if offset:
            pending.append(offset)
    return needed


def save_predictions(table, split, chain, variant, season, tag=None):
    """Holdout-season predictions beside the actuals, for evaluate.py and calibrate.py."""
    keep = [c for c in data.KEY_COLUMNS if c in table.columns] + ["season"]
    frame = table.loc[split.holdout, keep].copy()
    for name in chain.columns:
        frame[f"pred_{name}"] = chain.loc[split.holdout, name].to_numpy()
    for column in table.columns:
        if column.startswith("target_"):
            frame[column] = table.loc[split.holdout, column].to_numpy()
    for column in data.BASELINE_COLUMNS:
        if column in table.columns:
            frame[column] = table.loc[split.holdout, column].to_numpy()
    path = paths.predictions(variant, season, tag=tag)
    frame.to_parquet(path, index=False)
    log.info("wrote %s: %d rows", path.name, len(frame))


def main():
    args = parse_args()
    if args.walk_forward:
        import walkforward
        walkforward.run(args)
        return
    run(args)


if __name__ == "__main__":
    main()

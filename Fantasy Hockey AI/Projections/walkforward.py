"""The honest headline number: refit monthly across the holdout season.

The single fit in `train.py` trains once on two seasons and scores a third. That is a clean
test, but it is not how the season simulator will use these models -- live, the model is
refit as the season goes and always knows everything up to yesterday. Walk-forward mirrors
that: for each month of the holdout season, fit on every prior season *plus* that season to
the start of the month, then predict the month.

More expensive (one full stack per month) and the number it produces is usually a little
better than the single-fit number, because the model is never two seasons stale. Quote this
one.

Entered through `train.py --walk-forward`.
"""

import json
import logging

import numpy as np
import pandas as pd

import data
import paths
import targets as targets_module
import train as train_module

log = logging.getLogger("walkforward")


def blocks(dates: pd.Series):
    """Calendar months of the holdout season, in order."""
    periods = dates.dt.to_period("M")
    return sorted(periods.unique())


def run(args):
    train_seasons = [s.strip() for s in args.train_seasons.split(",") if s.strip()]
    seasons = train_seasons + [args.holdout_season]
    table = data.load_seasons(seasons, args.variant, args.features_dir)
    matrix, columns = data.build_feature_matrix(table)
    weights = data.copy_weights(table)
    train_module.out_of_fold.game_of = table["game_id"].to_numpy(dtype="int64")
    train_module.out_of_fold.folds = args.folds

    holdout = table["season"] == args.holdout_season
    months = blocks(table.loc[holdout, "game_date"])
    log.info("walk-forward over %d month(s) of %s", len(months), args.holdout_season)

    collected = []
    for month in months:
        month_start = month.to_timestamp()
        predicting = holdout & (table["game_date"].dt.to_period("M") == month)
        history = table["game_date"] < month_start
        if predicting.sum() == 0 or history.sum() < 10_000:
            log.info("%s: skipped (%d rows to predict, %d of history)",
                     month, int(predicting.sum()), int(history.sum()))
            continue

        # Early-stop on the last 10% of history by date; fit on the rest.
        history_dates = table.loc[history, "game_date"]
        cutoff = history_dates.quantile(0.9)
        split = data.Split(
            table,
            table.index[history & (table["game_date"] < cutoff)],
            table.index[history & (table["game_date"] >= cutoff)],
            table.index[predicting],
            cutoff,
        )
        log.info("=== %s === %s", month, split)

        chain = pd.DataFrame(index=table.index)
        for target in targets_module.PIPELINE:
            _, predictions, _ = train_module.train_one(
                target, table, matrix, columns, split, chain, weights)
            chain[target.name] = predictions

        frame = table.loc[split.holdout, [c for c in data.KEY_COLUMNS if c in table.columns]].copy()
        frame["month"] = str(month)
        for name in chain.columns:
            frame[f"pred_{name}"] = chain.loc[split.holdout, name].to_numpy()
        for column in table.columns:
            if column.startswith("target_"):
                frame[column] = table.loc[split.holdout, column].to_numpy()
        for column in data.BASELINE_COLUMNS:
            if column in table.columns:
                frame[column] = table.loc[split.holdout, column].to_numpy()
        collected.append(frame)

    if not collected:
        raise SystemExit("walk-forward produced no months -- check the holdout season")

    out = pd.concat(collected, ignore_index=True)
    paths.ensure(paths.REPORTS_DIR)
    path = paths.REPORTS_DIR / f"predictions_walkforward_{args.variant}.parquet"
    out.to_parquet(path, index=False)
    log.info("wrote %s: %d rows over %d month(s)", path.name, len(out), out["month"].nunique())

    summary = paths.REPORTS_DIR / f"walkforward_{args.variant}.json"
    summary.write_text(json.dumps({
        "variant": args.variant,
        "train_seasons": train_seasons,
        "holdout_season": args.holdout_season,
        "months": sorted(out["month"].unique().tolist()),
        "rows": int(len(out)),
    }, indent=2), encoding="utf-8")
    return out

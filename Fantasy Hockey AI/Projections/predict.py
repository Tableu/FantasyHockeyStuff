#!/usr/bin/env python
"""Turns a feature table into the lambda table the Monte Carlo layer consumes.

**No fantasy scoring is applied here, by design.** These are stat projections; what a goal is
worth is not this layer's business. A consumer applies its own scoring -- `weights.py` loads a
scoring file and scores a stat line, and the same lambda table serves any number of leagues.

One row per player-game, carrying:

    p_plays                        probability he is in the lineup at all
    toi, ev_toi, pp_toi            seconds, conditional on playing
    lambda_{shots,hits,blocks,
            assists,goals,pim}     expected counts, conditional on playing
    pp_point_share, sh_point_share P(a given point is a power-play / short-handed point)

Every count is *conditional on playing*; `p_plays` is kept as its own column rather than
folded in, because Section 5 samples the two separately -- a 40% chance of a big night is not
the same distribution as a certain average one, and a head-to-head format cares about the
difference.

The chain runs in the same order training did, each model offsetting on the previous one's
prediction: plays -> toi -> {ev,pp}_toi -> shots -> {hits, blocks, assists, pim} -> goals.

Usage:
    python predict.py --season 2025-26 --variant A
    python predict.py --features path/to/table.parquet --out lambdas.parquet
"""

import argparse
import json
import logging
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

import data
import paths
import targets as targets_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("predict")

COUNT_CATEGORIES = ["shots", "hits", "blocks", "assists", "goals", "pim"]


def parse_args():
    parser = argparse.ArgumentParser(description="Project a feature table")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--season", help="Season whose feature table to project")
    source.add_argument("--features", type=Path, help="A feature parquet to project directly")
    parser.add_argument("--variant", choices=("A", "B"), default="A",
                        help="Which lineup variant's table to read (default A: live-shaped)")
    parser.add_argument("--features-dir", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    return parser.parse_args()


def load_model(name):
    path = paths.MODELS_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing -- run train.py --all")
    return lgb.Booster(model_file=str(path))


def sidecar(name) -> dict:
    """The record train.py saved beside a booster: feature list and offset intercept."""
    path = paths.MODELS_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing -- run train.py --all")
    return json.loads(path.read_text(encoding="utf-8"))


def training_columns():
    """The exact feature list the models were fit on, from any model's sidecar."""
    for target in targets_module.PIPELINE:
        if (paths.MODELS_DIR / f"{target.name}.json").exists():
            return sidecar(target.name)["feature_columns"]
    raise FileNotFoundError("no trained model sidecar found -- run train.py --all")


def project(table: pd.DataFrame) -> pd.DataFrame:
    """Run the whole chain over a feature table."""
    columns = training_columns()
    missing = [c for c in columns if c not in table.columns]
    if missing:
        log.warning("%d training column(s) absent from this table; filling with NaN, e.g. %s",
                    len(missing), missing[:5])
        for column in missing:
            table[column] = np.nan
    matrix, _ = data.build_feature_matrix(table, columns)

    chain = pd.DataFrame(index=table.index)
    for target in targets_module.PIPELINE:
        booster = load_model(target.name)
        offset = targets_module.offset_values(target, chain)
        raw = booster.predict(matrix, raw_score=True)
        if offset is not None:
            # The same mean-matching intercept train.py folded into the offset; without it
            # every offset model would predict a rate of 1.0 per 60 too low.
            raw = raw + offset + sidecar(target.name)["offset_intercept"]
        if target.objective in ("poisson", "tweedie"):
            values = np.exp(raw)
        elif target.objective in ("binary", "cross_entropy"):
            values = 1.0 / (1.0 + np.exp(-raw))
        else:
            values = raw

        calibration = sidecar(target.name).get("calibration")
        if calibration:
            values = np.interp(values, calibration["x"], calibration["y"])
        chain[target.name] = values
        log.info("%-15s mean %.4f", target.name, chain[target.name].mean())

    keep = [c for c in data.KEY_COLUMNS if c in table.columns]
    out = table[keep].copy()
    out["p_plays"] = chain["plays"]
    for name in ("toi", "ev_toi", "pp_toi"):
        out[name] = chain[name].clip(lower=0)
    for category in COUNT_CATEGORIES:
        out[f"lambda_{category}"] = chain[category].clip(lower=0)
    # The two strength shares are fit independently and can, rarely, sum above 1. A point is
    # power-play, short-handed or even-strength, so clamp the pair rather than let a sampler
    # draw an impossible split.
    pp = chain["pp_point_share"].clip(0, 1)
    sh = chain["sh_point_share"].clip(0, 1)
    out["pp_point_share"] = pp
    out["sh_point_share"] = np.minimum(sh, 1.0 - pp)
    return out


def main():
    args = parse_args()
    if args.features:
        table = pd.read_parquet(args.features)
        label = args.features.stem
    else:
        table = pd.read_parquet(paths.feature_table(args.season, args.variant, args.features_dir))
        label = f"{args.season}_{args.variant}"
    table["game_date"] = pd.to_datetime(table["game_date"])
    log.info("projecting %d rows", len(table))

    out = project(table)
    destination = args.out or paths.ensure(paths.REPORTS_DIR) / f"lambdas_{label}.parquet"
    out.to_parquet(destination, index=False)
    log.info("wrote %s: %d rows x %d columns", destination.name, len(out), out.shape[1])
    log.info("stat projections only -- apply a scoring system with weights.py if you want "
             "points (by name from LeagueSettings/scoring/, or a path)")


if __name__ == "__main__":
    main()

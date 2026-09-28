#!/usr/bin/env python
"""How much of a skater's fantasy scoring comes from peripherals, and does it make him steadier?

Scoring (goals, assists and their PP/SH bonuses) is lumpy -- a few events swing a week -- while
peripherals (hits, blocks, shots, PIM) accumulate. If peripheral-heavy players really are steadier,
a manager ahead in a matchup should prefer them and one behind should prefer scorers
(plans/peripheral-variance.md). Step 1: measure it before using it.

    python peripherals.py --measure --season 2024-25
    python peripherals.py --measure --season 2025-26     # the holdout, once

Per player-season (games played >= MIN_GAMES), from the holdout build's per-game projections
(predictions_A_{season}.parquet) on the games he played:

    peripheral share   projected points from hits, blocks, shots, PIM / all projected points
    model cv           the per-game sd of his fantasy points / its mean, each stat's variance
                       mu + theta mu^2 (dispersion_{season}.json) weighted by the scoring, stats
                       independent; a goal/assist carries its expected PP/SH bonus
    realized cv        the sd of his actual per-game points around the projection / his mean
                       projection -- what a manager lives with

Reported: Spearman of each predictor with the realized cv, and partial on the points level (a
low scorer's cv is high whatever his mix), and the realized cv by peripheral-share quintile.

**Measured (2026-09-27):**

                                  2024-25 (651 skaters)     2025-26 holdout (660)
    peripheral share vs cv        -0.351 (partial -0.620)   -0.290 (partial -0.581)
    model cv vs cv                +0.667 (partial +0.679)   +0.650 (partial +0.634)
    realized cv, most peripheral  0.719 vs 0.797-0.917      0.755 vs 0.878-0.935
    fifth vs the other four

Peripheral-heavy players ARE steadier at a given points level, and the model sd captures it (and a
little more): decisions use the model sd; peripheral share is what the window shows.
"""

import argparse
import json
import logging
import sys

import numpy as np
import pandas as pd

import paths

log = logging.getLogger("peripherals")

MIN_GAMES = 30
SCORING = ("goals", "assists")
PERIPHERALS = ("hits", "blocks", "shots", "pim")


def weights(scoring: str = "points-league") -> dict:
    return json.loads((paths.SCORESETS_DIR / f"{scoring}.json").read_text(encoding="utf-8"))["skaters"]


def player_table(season: str, scoring: str = "points-league") -> pd.DataFrame:
    w = weights(scoring)
    theta = {k: v["theta"] for k, v in json.loads(
        (paths.REPORTS_DIR / f"dispersion_{season}.json").read_text(encoding="utf-8"))["categories"].items()}
    g = pd.read_parquet(paths.REPORTS_DIR / f"predictions_A_{season}.parquet")
    g = g[g["target_played"].fillna(0).astype(float) > 0].copy()
    pp, sh = g["pred_pp_point_share"].fillna(0.0), g["pred_sh_point_share"].fillna(0.0)
    # A point's weight carries its expected PP / SH bonus.
    bonus = pp * w.get("ppp", 0.0) + sh * w.get("shp", 0.0)
    eff = {"goals": w["goals"] + bonus, "assists": w["assists"] + bonus}
    for k in PERIPHERALS:
        eff[k] = pd.Series(w.get(k, 0.0), index=g.index)
    g["exp_scoring"] = sum(eff[k] * g[f"pred_{k}"] for k in SCORING)
    g["exp_periph"] = sum(eff[k] * g[f"pred_{k}"] for k in PERIPHERALS)
    g["exp_points"] = g["exp_scoring"] + g["exp_periph"]
    g["var_points"] = sum(eff[k] ** 2 * (g[f"pred_{k}"] + theta.get(k, 0.0) * g[f"pred_{k}"] ** 2)
                          for k in SCORING + PERIPHERALS)
    actual = (w["goals"] * g["target_goals"] + w["assists"] * g["target_assists"]
              + w.get("ppp", 0.0) * g["target_ppp"] + w.get("shp", 0.0) * g["target_shp"]
              + sum(w.get(k, 0.0) * g[f"target_{k}"] for k in PERIPHERALS))
    g["residual"] = actual - g["exp_points"]
    t = g.groupby("player_id").agg(games=("residual", "size"), exp_points=("exp_points", "mean"),
                                   exp_periph=("exp_periph", "sum"), exp_all=("exp_points", "sum"),
                                   var_points=("var_points", "mean"), resid_sd=("residual", "std"),
                                   position=("position", "first"))
    t = t[t["games"] >= MIN_GAMES]
    t["peripheral_share"] = t["exp_periph"] / t["exp_all"]
    t["model_cv"] = np.sqrt(t["var_points"]) / t["exp_points"]
    t["realized_cv"] = t["resid_sd"] / t["exp_points"]
    return t


def spearman(a, b) -> float:
    return float(pd.Series(a).rank().corr(pd.Series(b).rank()))


def partial(x, y, z) -> float:
    """Spearman of x and y with z's (rank-linear) part taken out of both."""
    rx, ry, rz = (pd.Series(v).rank().to_numpy() for v in (x, y, z))
    fit = lambda a: a - np.polyval(np.polyfit(rz, a, 1), rz)
    return float(np.corrcoef(fit(rx), fit(ry))[0, 1])


def measure(season: str) -> None:
    t = player_table(season)
    print(f"\n{season}: {len(t)} skaters with {MIN_GAMES}+ games; peripheral share median "
          f"{t['peripheral_share'].median():.0%} (IQR {t['peripheral_share'].quantile(0.25):.0%}-"
          f"{t['peripheral_share'].quantile(0.75):.0%})")
    for name, col in (("peripheral share", "peripheral_share"), ("model cv", "model_cv"),
                      ("points level (exp pts/game)", "exp_points")):
        print(f"  {name:28} vs realized cv: Spearman {spearman(t[col], t['realized_cv']):+.3f}"
              + ("" if col == "exp_points" else
                 f" | partial on points level {partial(t[col], t['realized_cv'], t['exp_points']):+.3f}"))
    t["quintile"] = pd.qcut(t["peripheral_share"], 5, labels=["most scoring", "2", "3", "4", "most peripheral"])
    print("\nrealized cv by peripheral-share quintile (and the points level in each):")
    print(t.groupby("quintile", observed=True).agg(
        peripheral_share=("peripheral_share", "median"), exp_pts_per_game=("exp_points", "median"),
        realized_cv=("realized_cv", "median"), model_cv=("model_cv", "median"),
        skaters=("games", "size")).round(3).to_string())


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--season", default="2024-25")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    if args.measure:
        measure(args.season)
    else:
        parser.error("nothing to do: --measure")


if __name__ == "__main__":
    main()

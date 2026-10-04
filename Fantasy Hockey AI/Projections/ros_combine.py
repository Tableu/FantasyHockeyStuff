#!/usr/bin/env python
"""A scored season's rest-of-season predictions as the two builds make them together: the main
build's (ros_predictions_season_<season>.parquet) with the rookie build's rows
(..._rookie.parquet, ros_train.py --prospects --tag rookie) in place for players with little NHL
history (ros_baselines.is_rookie) -- what ros_predict.py does live, so the backtests (Season/,
which read the untagged file) see the same model.

Rewrites the untagged file, keeping the main build's own as ..._main.parquet the first time (a
fresh ros_train.py build replaces it), so a rerun -- after either build is rebuilt, or the rookie
rule changes -- always starts from the main build's predictions.

    python ros_combine.py --season 2024-25
"""

import argparse
import logging

import numpy as np
import pandas as pd

import ros_baselines as baselines
import ros_train

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ros_combine")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--season", action="append", required=True)
    parser.add_argument("--horizon", default="season")
    args = parser.parse_args()
    for season in args.season:
        main_path = ros_train.predictions_path(args.horizon, season)
        pristine = ros_train.predictions_path(args.horizon, season, "main")
        rookie_path = ros_train.predictions_path(args.horizon, season, baselines.ROOKIE_TAG)
        main = pd.read_parquet(main_path)
        if "build" not in main.columns:          # fresh from ros_train.py: keep it as the source
            main.to_parquet(pristine, index=False)
        main = pd.read_parquet(pristine)
        rookie = pd.read_parquet(rookie_path)
        key = ["player_id", "game_date"]
        if len(main) != len(rookie) or not main[key].reset_index(drop=True).equals(rookie[key].reset_index(drop=True)):
            raise SystemExit(f"{season}: the two builds' rows differ -- rebuild both with ros_train.py")
        history = baselines.add_history(main[["player_id"]].assign(season=season))
        rows = baselines.is_rookie(history).to_numpy()
        columns = [c for c in main.columns if c.startswith(("pred_", "proj_"))]
        combined = main.copy()
        combined.loc[rows, columns] = rookie.loc[rows, columns].to_numpy()
        combined["build"] = np.where(rows, "rookie", "main")
        combined.to_parquet(main_path, index=False)
        log.info("%s: %d of %d rows from the rookie build -> %s", season, int(rows.sum()),
                 len(combined), main_path.name)


if __name__ == "__main__":
    main()

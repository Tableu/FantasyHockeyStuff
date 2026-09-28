#!/usr/bin/env python
"""How much longer an injured player will be out, from the injury history alone.

Every pricing window (valuation.RosterNights, the week planner) leaves an injured player out of
TONIGHT only and counts his later games in full: a manager knows he is out today, not for how
long. This estimates how long -- his REMAINING team games missed, from what a manager knows at a
lock: the injury's type group and how many games he has already missed. Its end, which the
history holds, is never an input. (plans/injury-absence.md, step 1.)

    python build_injury_absence.py --measure --season 2024-25   # candidates, fit before 2024-25
    python build_injury_absence.py --measure --season 2025-26   # the holdout, once
    python build_injury_absence.py --build --season 2024-25     # the table a season is played with

**Measured (2026-09-27)**, all points; "long" = more than 7 more games (an upgrade window's middle):

                     2024-25 MAE / long accuracy     2025-26 (holdout)
    a  everyone         9.16 / 0.610                   9.03 / 0.578
    b  by type          8.27 / 0.711                   8.40 / 0.666
    c  type x missed    8.24 / 0.690                   8.29 / 0.655

b is built: as good as c on MAE, better at calling a long absence, and simpler. Modest -- injury
lengths are heavy-tailed -- but it separates a week from a month better than nothing does.

Source: Injuries.Spells (NHL Injury Viz, 2000-01 ..), read-only. For a spell of GamesMissed games,
at each point e games in (e < GamesMissed) the outcome is GamesMissed - e. Candidates, each fit
on the seasons BEFORE the one scored:

    a  everyone     the median remaining absence, all injuries and points pooled
    b  by type      the median by injury type group
    c  type x in    the median by type group and games already missed (bucketed), falling back
                    to b where a cell has too few spells -- the longer he has been out, the
                    longer he tends to stay out

Scored on MAE of remaining games missed, and on telling a long absence from a short one at the
middle of the windows that use it: 7 team games (an upgrade's four weeks) and 2 (a rental's
rest of the week).
"""

import argparse
import logging
import sys

import numpy as np
import pandas as pd

import nhlstats_db
import paths

log = logging.getLogger("injury_absence")

POINTS = (0, 1, 2, 3, 5, 8, 12, 20, 30)         # games already missed, the checkpoints scored
BUCKETS = (0, 1, 2, 3, 5, 8, 12, 20, 30)        # the same edges bucket "games already missed"
MIN_CELL = 30                                    # spells a type x missed cell needs before use
LONG = (7, 2)                                    # the middle of an upgrade's window, a rental's
CANDIDATES = ("a_everyone", "b_by_type", "c_type_missed")


def load_spells() -> pd.DataFrame:
    cursor = nhlstats_db.connect().cursor()
    cursor.execute(
        "SELECT x.DisplayName AS season, s.PlayerID, s.InjuryTypeGroup, s.InjuryType, "
        "s.GamesMissed, s.StartDate FROM Injuries.Spells s "
        "JOIN Reference.Seasons x ON x.SeasonID = s.SeasonID "
        "WHERE s.GamesMissed IS NOT NULL AND s.GamesMissed > 0")
    columns = [c[0] for c in cursor.description]
    spells = pd.DataFrame.from_records(cursor.fetchall(), columns=columns)
    spells["GamesMissed"] = spells["GamesMissed"].astype(int)
    spells["InjuryTypeGroup"] = spells["InjuryTypeGroup"].fillna(-1).astype(int)
    log.info("%d spells, %s .. %s", len(spells), spells["season"].min(), spells["season"].max())
    return spells


def bucket(missed) -> np.ndarray:
    """Games already missed -> the lower edge of its bucket."""
    edges = np.array(BUCKETS)
    return edges[np.searchsorted(edges, np.asarray(missed), side="right") - 1]


def points(spells: pd.DataFrame, every_game=False) -> pd.DataFrame:
    """(spell, e) rows for every point e a player is still out at: e games in, remaining."""
    rows = []
    for e in (range(0, int(spells["GamesMissed"].max())) if every_game else POINTS):
        out = spells[spells["GamesMissed"] > e]
        rows.append(out.assign(missed=e, remaining=out["GamesMissed"] - e))
    table = pd.concat(rows, ignore_index=True)
    table["bucket"] = bucket(table["missed"])
    return table


def fit(train: pd.DataFrame) -> dict:
    """The three candidates' medians, from every point of every training spell."""
    rows = points(train, every_game=True)
    by_type = rows.groupby("InjuryTypeGroup")["remaining"].median()
    cells = rows.groupby(["InjuryTypeGroup", "bucket"])["remaining"].agg(["median", "size"])
    return {"everyone": float(rows["remaining"].median()), "by_type": by_type,
            "cells": cells[cells["size"] >= MIN_CELL]["median"]}


def predict(model: dict, table: pd.DataFrame) -> pd.DataFrame:
    table = table.copy()
    table["a_everyone"] = model["everyone"]
    table["b_by_type"] = table["InjuryTypeGroup"].map(model["by_type"]).fillna(model["everyone"])
    keys = list(zip(table["InjuryTypeGroup"], table["bucket"]))
    table["c_type_missed"] = [model["cells"].get(k, np.nan) for k in keys]
    table["c_type_missed"] = table["c_type_missed"].fillna(table["b_by_type"])
    return table


def measure(season: str) -> None:
    spells = load_spells()
    train, test = spells[spells["season"] < season], spells[spells["season"] == season]
    model = fit(train)
    scored = predict(model, points(test))
    print(f"\n{season}: {len(test)} spells, fit on {train['season'].nunique()} seasons before it "
          f"({len(train)} spells)")
    print("\nMAE of remaining games missed, by games already missed:")
    mae = pd.DataFrame({c: (scored[c] - scored["remaining"]).abs().groupby(scored["missed"]).mean()
                        for c in CANDIDATES})
    mae["spells still out"] = scored.groupby("missed").size()
    print(mae.round(2).to_string())
    print("\nall points:", {c: round(float((scored[c] - scored["remaining"]).abs().mean()), 2)
                            for c in CANDIDATES})
    for threshold in LONG:
        actual = scored["remaining"] > threshold
        print(f"\nlong = more than {threshold} more games: {actual.mean():.0%} of points are long")
        for c in CANDIDATES:
            said = scored[c] > threshold
            hit = (said & actual).sum()
            precision = hit / said.sum() if said.sum() else float("nan")
            recall = hit / actual.sum() if actual.sum() else float("nan")
            accuracy = (said == actual).mean()
            print(f"  {c:14} accuracy {accuracy:.3f}  precision {precision:.3f}  recall {recall:.3f}")


def build(season: str) -> None:
    """What a season is played with (plans/injury-absence.md step 2): candidate b -- each injury
    type group's median remaining absence -- fit on the seasons BEFORE `season`, the everyone
    median as its fallback, the injury type names that map a report's body part to a group, and
    the season's own spells (player, start, group: the type a manager sees at a lock -- never
    the end) for the backtest engine."""
    spells = load_spells()
    train = spells[spells["season"] < season]
    model = fit(train)
    table = model["by_type"].rename("remaining").reset_index()
    table = pd.concat([table, pd.DataFrame([{"InjuryTypeGroup": -1, "remaining": model["everyone"]}])],
                      ignore_index=True)
    table["fit_seasons"] = f"{train['season'].min()}..{train['season'].max()}"
    types = (train.groupby(["InjuryType", "InjuryTypeGroup"]).size().rename("n").reset_index()
             .sort_values("n", ascending=False).drop_duplicates("InjuryType"))
    own = spells[spells["season"] == season][["PlayerID", "StartDate", "InjuryTypeGroup"]]
    out = paths.ensure(paths.FEATURES_DIR)
    table.to_parquet(out / f"injury_absence_{season}.parquet", index=False)
    types[["InjuryType", "InjuryTypeGroup"]].to_parquet(out / f"injury_types_{season}.parquet", index=False)
    own.rename(columns={"PlayerID": "player_id", "StartDate": "start_date",
                        "InjuryTypeGroup": "type_group"}).to_parquet(
        out / f"injury_spells_{season}.parquet", index=False)
    log.info("%s: %d type groups (+ everyone %.1f games), fit on %s; %d of its own spells",
             season, len(table) - 1, model["everyone"], table["fit_seasons"].iloc[0], len(own))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--season", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    if args.measure:
        measure(args.season)
    elif args.build:
        build(args.season)
    else:
        parser.error("nothing to do: --measure or --build")


if __name__ == "__main__":
    main()

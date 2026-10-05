#!/usr/bin/env python
"""A season's skater stat lines so far, for the plan window's Roster, Free agents and Matchup tabs:

    data/features/season_stats_{season}.parquet   one row per skater: gp, goals, assists, ppp, shp,
                                                  shots, hits, blocks, pim (summed across teams)

From `Stats.PlayerSeasonStats`, which the nightly ingest recomputes from the games it loaded
(pipeline/run_daily.py). Goalies are not here: their season lines are summed from
goalie_starts_{season}.parquet (build_goalie_starts.py), which the same nightly job rebuilds.

    python build_season_stats.py --season 2026-27
"""

import argparse
import logging

import pandas as pd

import nhlstats_db
import paths
from features import extract

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_season_stats")


def fetch(cursor, season_id: int) -> pd.DataFrame:
    cursor.execute("""
        SELECT PlayerID AS player_id, SUM(GamesPlayed) AS gp, SUM(Goals) AS goals,
               SUM(Assists) AS assists,
               SUM(PowerPlayGoals) + SUM(PowerPlayAssists) AS ppp,
               SUM(ShortHandedGoals) + SUM(ShortHandedAssists) AS shp,
               SUM(Shots) AS shots, SUM(Hits) AS hits, SUM(Blocks) AS blocks,
               SUM(PenaltyMinutes) AS pim
        FROM Stats.PlayerSeasonStats WHERE SeasonID = ?
        GROUP BY PlayerID""", season_id)
    columns = [c[0] for c in cursor.description]
    return pd.DataFrame.from_records([tuple(r) for r in cursor.fetchall()], columns=columns)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--season", required=True, help="Season display name, e.g. 2026-27")
    args = parser.parse_args()
    cursor = nhlstats_db.connect().cursor()
    season_id = extract.season_ids_for(cursor, [args.season])[args.season]
    table = fetch(cursor, season_id)
    out = paths.ensure(paths.FEATURES_DIR) / f"season_stats_{args.season}.parquet"
    paths.write_parquet(table, out)
    log.info("%s: %d skaters -> %s", args.season, len(table), out)


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""A season's NHL regular-season schedule, exported to parquet -- one row per game.

`Reference.Schedule` has every game, played or not, as soon as the NHL publishes the season, so
this works before opening night. The draft board counts each team's off-night and fantasy-playoff
games from it (`Live/draft_board.schedule_counts`), the way the aggregate workbook's Schedule
Info sheet does.

    python build_schedule.py --season 2026-27
"""

import argparse
import logging

import pandas as pd

import nhlstats_db
import paths

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("schedule")

SQL = """
    SELECT s.GameDate AS game_date, s.HomeTeamID AS home_team_id, s.AwayTeamID AS away_team_id
    FROM Reference.Schedule s
    JOIN Reference.Seasons se ON se.SeasonID = s.SeasonID
    WHERE se.DisplayName = ? AND s.GameType = '2'
    ORDER BY s.GameDate
"""


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--season", required=True, help="e.g. 2026-27")
    args = p.parse_args()

    cursor = nhlstats_db.connect().cursor()
    cursor.execute(SQL, args.season)
    games = pd.DataFrame.from_records(cursor.fetchall(), columns=[d[0] for d in cursor.description])
    if games.empty:
        raise SystemExit(f"Reference.Schedule has no regular-season games for {args.season}")
    games["game_date"] = pd.to_datetime(games["game_date"])
    per_team = pd.concat([games["home_team_id"], games["away_team_id"]]).value_counts()
    paths.ensure(paths.FEATURES_DIR)
    out = paths.FEATURES_DIR / f"schedule_{args.season}.parquet"
    games.to_parquet(out, index=False)
    log.info("%s: %d games, %s to %s, %d teams (%d-%d games each) -> %s", args.season, len(games),
             games["game_date"].min().date(), games["game_date"].max().date(), per_team.size,
             per_team.min(), per_team.max(), out)


if __name__ == "__main__":
    main()

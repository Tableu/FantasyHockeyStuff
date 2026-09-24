#!/usr/bin/env python
"""Player and team names, exported to parquet -- for anything a person reads.

Every other export is keyed by id and carries no names, which is right for the models and useless
for a draft board a person picks from. This writes `Reference.Players` (id, name, NHL position,
birth date) and `Reference.Teams` (id, abbreviation) as two small files. Nothing downstream joins
on a name; they are labels only.

    python build_players.py
"""

import logging

import pandas as pd

import nhlstats_db
import paths

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("players")


def fetch(cursor, sql) -> pd.DataFrame:
    cursor.execute(sql)
    return pd.DataFrame.from_records(cursor.fetchall(), columns=[d[0] for d in cursor.description])


def main():
    cursor = nhlstats_db.connect().cursor()
    players = fetch(cursor, "SELECT PlayerID AS player_id, FullName AS name, PositionCode AS "
                            "position, BirthDate AS birth_date FROM Reference.Players")
    teams = fetch(cursor, "SELECT TeamID AS team_id, Abbreviation AS team FROM Reference.Teams")
    for frame, key in ((players, "player_id"), (teams, "team_id")):
        if frame[key].duplicated().any():
            raise SystemExit(f"duplicate {key} in the reference table")
    paths.ensure(paths.FEATURES_DIR)
    players.to_parquet(paths.FEATURES_DIR / "players.parquet", index=False)
    teams.to_parquet(paths.FEATURES_DIR / "teams.parquet", index=False)
    log.info("%d players, %d teams -> %s", len(players), len(teams), paths.FEATURES_DIR)


if __name__ == "__main__":
    main()

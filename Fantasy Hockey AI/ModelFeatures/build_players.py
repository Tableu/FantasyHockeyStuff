#!/usr/bin/env python
"""Player and team names, exported to parquet -- for anything a person reads.

Every other export is keyed by id and carries no names, which is right for the models and useless
for a draft board a person picks from. This writes `Reference.Players` (id, name, NHL position,
birth date) and `Reference.Teams` (id, abbreviation) as two small files. Nothing downstream joins
on a name; they are labels only.

It also writes `Fantasy.PlatformPlayerIDs` -- a platform's own player id -> PlayerID, per season --
as `platform_ids.parquet`, which is how the draft assistant names a Fleaflicker pick exactly, and
`Injuries.RiskLists` -- a published injury-risk list per season (Dobber's Band-Aid Boys: tier
Certified, Trainee or Goalie) -- as `injury_risk.parquet`, which the draft window shows as 🩹.

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
    ids = fetch(cursor, """
        SELECT p.PlatformName AS platform, s.DisplayName AS season, x.ExternalPlayerID AS external_id,
               x.PlayerID AS player_id
        FROM Fantasy.PlatformPlayerIDs x
        JOIN Fantasy.Platforms p ON p.FantasyPlatformID = x.FantasyPlatformID
        JOIN Reference.Seasons s ON s.SeasonID = x.SeasonID""")
    risk = fetch(cursor, """
        SELECT src.SourceName AS source, s.DisplayName AS season, r.PlayerID AS player_id,
               r.RawPlayerName AS listed_as, r.Tier AS tier
        FROM Injuries.RiskLists r
        JOIN Injuries.Sources src ON src.SourceID = r.SourceID
        JOIN Reference.Seasons s ON s.SeasonID = r.SeasonID
        WHERE r.PlayerID IS NOT NULL""")
    if risk.duplicated(["source", "season", "player_id"]).any():
        raise SystemExit("a player listed twice on one injury-risk list")
    if ids.duplicated(["platform", "season", "external_id"]).any():
        raise SystemExit("duplicate platform ids")
    for frame, key in ((players, "player_id"), (teams, "team_id")):
        if frame[key].duplicated().any():
            raise SystemExit(f"duplicate {key} in the reference table")
    paths.ensure(paths.FEATURES_DIR)
    players.to_parquet(paths.FEATURES_DIR / "players.parquet", index=False)
    teams.to_parquet(paths.FEATURES_DIR / "teams.parquet", index=False)
    ids.to_parquet(paths.FEATURES_DIR / "platform_ids.parquet", index=False)
    risk.to_parquet(paths.FEATURES_DIR / "injury_risk.parquet", index=False)
    log.info("%d players, %d teams, %d platform ids, %d injury-risk rows -> %s", len(players),
             len(teams), len(ids), len(risk), paths.FEATURES_DIR)


if __name__ == "__main__":
    main()

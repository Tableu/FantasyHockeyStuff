#!/usr/bin/env python
"""Player and team names, exported to parquet -- for anything a person reads.

Every other export is keyed by id and carries no names, which is right for the models and useless
for a draft board a person picks from. This writes `Reference.Players` (id, name, NHL position,
birth date) and `Reference.Teams` (id, abbreviation) as two small files. Nothing downstream joins
on a name; they are labels only.

It also writes `Fantasy.PlatformPlayerIDs` -- a platform's own player id -> PlayerID, per season --
as `platform_ids.parquet`, which is how the draft assistant names a Fleaflicker pick exactly, and
`Injuries.RiskLists` -- a published injury-risk list per season (Dobber's Band-Aid Boys: tier
Certified, Trainee or Goalie) -- as `injury_risk.parquet`, which the draft window shows as 🩹, and
each player's latest `Live.PlayerStatus` row -- the pipeline's merge of the Fleaflicker, ESPN and
Daily Faceoff injury reports (OUT, SUSP, DTD, ACTIVE; a game-time decision; IR-eligible) -- as
`injury_status.parquet`, the draft window's Status column. The plan window's snapshot step
(Live/planpass.py) reruns this after each injury and line-chart snapshot, so that file follows the
reports.

`player_teams.parquet` is the team each player is signed with, per season: his open
`Reference.PlayerTeamHistory` stint (pipeline/import_player_teams.py, from his NHL page, daily at
05:00 via run_player_teams.cmd, which reruns this). The draft board's Team column reads it.

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
    # `active`: his NHL page's isActive, refreshed daily for every projected or league-pool
    # player by pipeline/import_player_teams.py (false = unsigned, retired or abroad).
    players = fetch(cursor, "SELECT PlayerID AS player_id, FullName AS name, PositionCode AS "
                            "position, BirthDate AS birth_date, CAST(Active AS BIT) AS active "
                            "FROM Reference.Players")
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
    status = fetch(cursor, """
        SELECT PlayerID AS player_id, Status AS status, CAST(GameTimeDecision AS BIT) AS gtd,
               CAST(IREligible AS BIT) AS ir_eligible, Sources AS sources, ChangedAt AS changed_at
        FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY PlayerID
                                           ORDER BY ChangedAt DESC, PlayerStatusID DESC) AS rn
              FROM Live.PlayerStatus) x
        WHERE rn = 1""")
    teams_now = fetch(cursor, """
        SELECT h.PlayerID AS player_id, s.DisplayName AS season, h.TeamID AS team_id,
               h.StartDate AS start_date
        FROM Reference.PlayerTeamHistory h
        JOIN Reference.Seasons s ON s.SeasonID = h.SeasonID
        WHERE h.EndDate IS NULL""")
    if teams_now.duplicated(["player_id", "season"]).any():
        raise SystemExit("a player with two open team stints in one season")
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
    status.to_parquet(paths.FEATURES_DIR / "injury_status.parquet", index=False)
    teams_now.to_parquet(paths.FEATURES_DIR / "player_teams.parquet", index=False)
    log.info("%d players, %d teams, %d platform ids, %d injury-risk rows, %d injury statuses "
             "(%d not active), %d current player teams -> %s", len(players), len(teams), len(ids),
             len(risk), len(status), (status["status"] != "ACTIVE").sum(), len(teams_now),
             paths.FEATURES_DIR)


if __name__ == "__main__":
    main()

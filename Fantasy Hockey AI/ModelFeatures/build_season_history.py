#!/usr/bin/env python
"""Season-level history, exported to parquet -- what the season spreads are fitted from.
Plan: ~/.claude/plans/boom-bust-odds.md (steps 1-2).

Three files under data/features/:

- `season_totals.parquet`: `Stats.SeasonTotals`, the NHL's own season line per player per season
  (2000-01 on, summed across teams; pipeline/import_season_totals.py). Hits and blocks are NaN
  before 2005-06 (not tracked).
- `injury_seasons.parquet`: per skater per season, the team games he lost to injury spells
  (`Injuries.Spells`), with COVID-protocol absences split out -- in 2020-21 and 2021-22 they are
  642 spells, and a protocol absence says nothing about how durable a player is. `retired`
  spells (a source tag for a contract still on the books) are dropped.
- `team_games.parquet`: regular-season games per team per season (`Reference.Schedule`,
  GameType 2), so games played can be read as a share of the games there were -- 48 in 2012-13,
  56 in 2020-21, 68-71 in 2019-20.

Birth dates come from players.parquet (build_players.py).

    python build_season_history.py
"""

import logging

import pandas as pd

import nhlstats_db
import paths

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("season-history")

REGULAR_SEASON = "2"
COVID_TYPES = ("COVID-related",)


def fetch(cursor, sql, *params) -> pd.DataFrame:
    cursor.execute(sql, params)
    return pd.DataFrame.from_records(cursor.fetchall(), columns=[d[0] for d in cursor.description])


def main():
    cursor = nhlstats_db.connect().cursor()
    totals = fetch(cursor, """
        SELECT s.DisplayName AS season, t.PlayerID AS player_id, CAST(t.IsGoalie AS BIT) AS is_goalie,
               t.PositionCode AS position, t.TeamAbbrevs AS teams, t.GamesPlayed AS gp,
               t.Goals AS goals, t.Assists AS assists, t.PowerPlayPoints AS ppp,
               t.ShortHandedPoints AS shp, t.Shots AS shots, t.Hits AS hits, t.Blocks AS blocks,
               t.PenaltyMinutes AS pim, t.TOIPerGameSeconds AS toi_per_game,
               t.GamesStarted AS starts, t.Wins AS wins, t.Losses AS losses, t.OTLosses AS ot_losses,
               t.Shutouts AS shutouts, t.GoalsAgainst AS goals_against,
               t.ShotsAgainst AS shots_against, t.Saves AS saves
        FROM Stats.SeasonTotals t
        JOIN Reference.Seasons s ON s.SeasonID = t.SeasonID""")
    totals["is_goalie"] = totals["is_goalie"].astype(bool)
    numeric = [c for c in totals.columns
               if c not in ("season", "player_id", "is_goalie", "position", "teams")]
    totals[numeric] = totals[numeric].apply(pd.to_numeric)

    spells = fetch(cursor, """
        SELECT s.DisplayName AS season, i.PlayerID AS player_id, i.InjuryType AS injury_type,
               i.GamesMissed AS games_missed
        FROM Injuries.Spells i
        JOIN Reference.Seasons s ON s.SeasonID = i.SeasonID
        WHERE i.PlayerID IS NOT NULL AND i.PositionGroup <> 'G' AND i.IsRetiredContract = 0""")
    spells["covid"] = spells["injury_type"].isin(COVID_TYPES)
    spells["games_missed"] = spells["games_missed"].astype(int)
    injury = (spells[~spells["covid"]].groupby(["season", "player_id"])
              .agg(injury_games=("games_missed", "sum"), spells=("games_missed", "size"),
                   longest_spell=("games_missed", "max")))
    covid = spells[spells["covid"]].groupby(["season", "player_id"])["games_missed"].sum()
    injury = injury.join(covid.rename("covid_games"), how="outer").fillna(0).astype(int)

    team_games = fetch(cursor, """
        SELECT se.DisplayName AS season, t.team_id, COUNT(*) AS games
        FROM (SELECT SeasonID, HomeTeamID AS team_id FROM Reference.Schedule WHERE GameType = ?
              UNION ALL
              SELECT SeasonID, AwayTeamID FROM Reference.Schedule WHERE GameType = ?) t
        JOIN Reference.Seasons se ON se.SeasonID = t.SeasonID
        GROUP BY se.DisplayName, t.team_id""", REGULAR_SEASON, REGULAR_SEASON)

    out = paths.ensure(paths.FEATURES_DIR)
    totals.to_parquet(out / "season_totals.parquet", index=False)
    injury.reset_index().to_parquet(out / "injury_seasons.parquet", index=False)
    team_games.to_parquet(out / "team_games.parquet", index=False)
    log.info("season_totals %d rows (%d seasons), injury_seasons %d, team_games %d -> %s",
             len(totals), totals["season"].nunique(), len(injury), len(team_games), out)


if __name__ == "__main__":
    main()

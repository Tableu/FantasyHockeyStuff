#!/usr/bin/env python
"""Fills in the goalie boxscore line and the game outcome for already-ingested games.

Why: `ingest/official_stats.py` parsed a goalie's shots against, saves and goals against and
then wrote only his position, PIM and ice time -- and never looked at `decision` or
`starter` at all. Nothing else in the database records who took the win or who officially
started, so wins, losses, overtime losses and shutouts simply did not exist as facts, and a
goalie feature table could not have targets. `Game.Games` likewise dropped
`gameOutcome.lastPeriodType`, which is what separates a regulation loss from an overtime one.

The NHL supplies all of it. `decision` is 'W', 'L' or 'O' and appears on exactly the two
goalies of record, assigned by the league -- so there is nothing here to derive or
approximate, only to store.

No API calls: every game's boxscore payload is already in Ingestion.RawApiResponses
(EndpointType = 'BOXSCORE'). Nothing downstream needs recomputing either, because these are
new columns rather than corrections to old ones -- unlike backfill_shift_merge.py, which had
to replay the whole analytics chain.

Rerunnable, and safe to interrupt: each game is committed on its own, and a game whose
stored values already match is skipped.

Usage:
    python backfill_goalie_boxscore.py --dry-run          # report what would change
    python backfill_goalie_boxscore.py                    # every game in the database
    python backfill_goalie_boxscore.py --season 2025-26   # one season
"""

import argparse
import json
import logging

from nhl_pipeline import db
from nhl_pipeline.api import field_map

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_goalie_boxscore")


def parse_args():
    parser = argparse.ArgumentParser(description="Store the goalie boxscore line and game outcome")
    parser.add_argument("--season", default=None, metavar="YYYY-YY", help="Only this season's games")
    parser.add_argument("--dry-run", action="store_true", help="Report the changes, write nothing")
    return parser.parse_args()


def player_map(cursor) -> dict:
    cursor.execute("SELECT NHLPlayerID, PlayerID FROM Reference.Players WHERE NHLPlayerID IS NOT NULL")
    return {row[0]: row[1] for row in cursor.fetchall()}


def team_map_for_game(cursor, game_id: int) -> dict:
    """{'homeTeam': TeamID, 'awayTeam': TeamID} for this game.

    Taken from Game.Games rather than the payload's own team ids, for the same reason
    backfill_shift_merge.py narrows its team map per game: a payload is not guaranteed to be
    about only the game it was filed under.
    """
    cursor.execute("SELECT HomeTeamID, AwayTeamID FROM Game.Games WHERE GameID = ?", game_id)
    home, away = cursor.fetchone()
    return {"homeTeam": home, "awayTeam": away}


def stored_goalie_rows(cursor, game_id: int) -> dict:
    cursor.execute(
        """SELECT PlayerID, TeamID, ShotsAgainst, Saves, GoalsAgainst, Decision, IsStarter
           FROM Stats.PlayerGameStats WHERE GameID = ? AND PositionCode = 'G'""",
        game_id,
    )
    return {(row[0], row[1]): tuple(row[2:]) for row in cursor.fetchall()}


def payload_goalie_rows(payload: dict, teams: dict, players: dict) -> dict:
    """{(PlayerID, TeamID): (shots, saves, GA, decision, starter)} from the boxscore."""
    last_period_type = field_map.game_outcome_fields(payload)["last_period_type"]
    rows = {}
    for side, team_id in teams.items():
        if team_id is None:
            continue
        for row in payload.get("playerByGameStats", {}).get(side, {}).get("goalies", []):
            fields = field_map.goalie_boxscore_fields(row)
            player_id = players.get(fields["nhl_player_id"])
            if player_id is None:
                continue
            starter = fields["is_starter"]
            rows[(player_id, team_id)] = (
                fields["shots_against"], fields["saves"], fields["goals_against"],
                field_map.normalize_decision(fields["decision"], last_period_type),
                None if starter is None else int(bool(starter)),
            )
    return rows


def main():
    args = parse_args()
    conn = db.connect()
    cursor = conn.cursor()

    sql = """
        SELECT g.GameID, g.NHLGameID, g.LastPeriodType, g.OvertimePeriods, r.RawJSON
        FROM Game.Games g
        JOIN Ingestion.RawApiResponses r ON r.GameID = g.GameID AND r.EndpointType = 'BOXSCORE'
        JOIN Reference.Seasons s ON s.SeasonID = g.SeasonID
    """
    params = []
    if args.season:
        sql += " WHERE s.DisplayName = ?"
        params.append(args.season)
    cursor.execute(sql + " ORDER BY g.GameDate, g.NHLGameID", params)
    games = cursor.fetchall()
    log.info("%d game(s) with a stored boxscore payload", len(games))

    players = player_map(cursor)
    changed_games = changed_rows = changed_outcomes = missing = decisions = 0

    for game_id, nhl_game_id, stored_period_type, stored_ot, raw in games:
        payload = json.loads(raw)
        teams = team_map_for_game(cursor, game_id)
        wanted = payload_goalie_rows(payload, teams, players)
        stored = stored_goalie_rows(cursor, game_id)
        outcome = field_map.game_outcome_fields(payload)

        game_touched = False
        for key, values in wanted.items():
            if key not in stored:
                missing += 1
                continue
            if stored[key] == values:
                continue
            changed_rows += 1
            game_touched = True
            if values[3]:
                decisions += 1
            if not args.dry_run:
                db.upsert(
                    cursor, "Stats.PlayerGameStats",
                    {"GameID": game_id, "PlayerID": key[0], "TeamID": key[1]},
                    {"ShotsAgainst": values[0], "Saves": values[1], "GoalsAgainst": values[2],
                     "Decision": values[3], "IsStarter": values[4]},
                )

        if (stored_period_type, stored_ot) != (outcome["last_period_type"], outcome["overtime_periods"]):
            changed_outcomes += 1
            game_touched = True
            if not args.dry_run:
                cursor.execute(
                    "UPDATE Game.Games SET LastPeriodType = ?, OvertimePeriods = ? WHERE GameID = ?",
                    outcome["last_period_type"], outcome["overtime_periods"], game_id,
                )

        if game_touched:
            changed_games += 1
            if not args.dry_run:
                conn.commit()

    verb = "would update" if args.dry_run else "updated"
    log.info("%s %d goalie row(s) across %d game(s); %d carried a decision",
             verb, changed_rows, changed_games, decisions)
    log.info("%s %d game outcome(s)", verb, changed_outcomes)
    if missing:
        log.warning("%d goalie line(s) had no Stats.PlayerGameStats row to update -- "
                    "those games were ingested without a boxscore row for that player", missing)
    conn.close()


if __name__ == "__main__":
    main()

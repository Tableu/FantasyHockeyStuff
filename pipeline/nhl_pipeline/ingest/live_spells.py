"""Injuries.Spells for a live season, rebuilt from the Live schema's reports (source "Live
snapshots") -- so the features and the season engine read the current season exactly the way
they read the NHL Injury Viz history.

A missed game is a played team game where the player was reported OUT or SUSPENDED in
Live.PlayerStatus as of puck drop (the merged status: latest change at or before the game's
StartTimeUTC, for that team) and did not dress in Lineups.GameLineups. A spell is a run of
consecutive team games (regular season, by the team's own game number) he missed that way; a
game he dressed for, or one he was not reported out for, ends it. Unlike a history spell, the
first game of a live spell was known before the lock by construction (ModelFeatures'
store.injured_known_on counts it).

Rebuilt wholesale each night from the reports: the snapshots are the record, the spells a
derived table, so a fixed alias or a late-ingested game corrects every spell it touches.
"""

import logging
from bisect import bisect_right
from collections import defaultdict

from nhl_pipeline import db

log = logging.getLogger("ingest.live_spells")

SOURCE_NAME = "Live snapshots"
SOURCE_DESCRIPTION = ("Spells built from the live injury reports (Live.PlayerStatus) and who "
                      "dressed (Lineups.GameLineups); first game known before the lock")
INJURED = ("OUT", "SUSP")
_POSITION_GROUP = {"C": "F", "L": "F", "LW": "F", "R": "F", "RW": "F", "D": "D", "G": "G"}


def _status_timeline(cursor) -> dict:
    """{PlayerID: ([ChangedAt...], [(Status, TeamID)...])}, each sorted by time."""
    cursor.execute("SELECT PlayerID, ChangedAt, Status, TeamID FROM Live.PlayerStatus "
                   "ORDER BY PlayerID, ChangedAt, PlayerStatusID")
    timeline: dict = defaultdict(lambda: ([], []))
    for row in cursor.fetchall():
        times, states = timeline[row.PlayerID]
        times.append(row.ChangedAt)
        states.append((row.Status, row.TeamID))
    return timeline


def _injured_at(timeline: dict, when) -> dict:
    """{TeamID: {PlayerID}} reported OUT/SUSP as of `when`."""
    out: dict = defaultdict(set)
    for player_id, (times, states) in timeline.items():
        i = bisect_right(times, when) - 1
        if i >= 0 and states[i][0] in INJURED and states[i][1] is not None:
            out[states[i][1]].add(player_id)
    return out


def rebuild(cursor, season_id: int) -> dict:
    if not db.fetch_scalar(cursor, "SELECT OBJECT_ID('Live.PlayerStatus', 'U')"):
        log.info("live spells: no Live.PlayerStatus yet, nothing to build")
        return {"spells": 0}
    source_id = db.upsert_get_id(cursor, "Injuries.Sources", "SourceID",
                                 {"SourceName": SOURCE_NAME}, {"Description": SOURCE_DESCRIPTION})

    # Every regular-season team game with its number, and whether it has been ingested.
    cursor.execute("""
        SELECT s.NHLGameID, s.GameDate, s.StartTimeUTC, s.HomeTeamID, s.AwayTeamID, g.GameID
        FROM Reference.Schedule s LEFT JOIN Game.Games g ON g.NHLGameID = s.NHLGameID
        WHERE s.SeasonID = ? AND s.GameType = '2'
        ORDER BY s.StartTimeUTC, s.NHLGameID""", season_id)
    games = cursor.fetchall()
    number: dict = defaultdict(int)
    team_games = []      # (TeamID, game number, NHLGameID, GameDate, StartTimeUTC, GameID)
    for g in games:
        for team_id in (g.HomeTeamID, g.AwayTeamID):
            number[team_id] += 1
            team_games.append((team_id, number[team_id], g.NHLGameID, g.GameDate, g.StartTimeUTC, g.GameID))

    cursor.execute("""
        SELECT l.GameID, l.TeamID, l.PlayerID FROM Lineups.GameLineups l
        JOIN Game.Games g ON g.GameID = l.GameID
        WHERE g.SeasonID = ? AND l.Dressed = 1""", season_id)
    dressed: dict = defaultdict(set)
    for row in cursor.fetchall():
        dressed[(row.GameID, row.TeamID)].add(row.PlayerID)

    timeline = _status_timeline(cursor)
    missed: dict = defaultdict(list)   # (TeamID, PlayerID) -> [(number, NHLGameID, GameDate)]
    for team_id, n, nhl_game_id, game_date, puck, game_id in team_games:
        if game_id is None or (game_id, team_id) not in dressed or puck is None:
            continue   # not played / not ingested yet
        for player_id in _injured_at(timeline, puck).get(team_id, ()):
            if player_id not in dressed[(game_id, team_id)]:
                missed[(team_id, player_id)].append((n, nhl_game_id, game_date))

    cursor.execute("SELECT PlayerID, FullName, PositionCode FROM Reference.Players WHERE PlayerID IN "
                   "(SELECT DISTINCT PlayerID FROM Live.PlayerStatus)")
    players = {row.PlayerID: (row.FullName, row.PositionCode) for row in cursor.fetchall()}

    db.delete_where(cursor, "Injuries.Spells", {"SourceID": source_id, "SeasonID": season_id})
    spells = 0
    for (team_id, player_id), games_missed in missed.items():
        runs, current = [], [games_missed[0]]
        for game in games_missed[1:]:
            if game[0] == current[-1][0] + 1:
                current.append(game)
            else:
                runs.append(current)
                current = [game]
        runs.append(current)
        name, position = players.get(player_id, (str(player_id), None))
        for run in runs:
            cursor.execute(
                "INSERT INTO Injuries.Spells (SourceID, SeasonID, TeamID, PlayerID, RawPlayerName, PositionGroup, "
                "GamesMissed, StartGameNumber, EndGameNumber, StartDate, EndDate, StartNHLGameID, EndNHLGameID) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                source_id, season_id, team_id, player_id, name, _POSITION_GROUP.get(position, "F"),
                len(run), run[0][0], run[-1][0], run[0][2], run[-1][2], run[0][1], run[-1][1])
            spells += 1
    log.info("live spells: %d spells over %d player-teams, %d missed games",
             spells, len(missed), sum(len(v) for v in missed.values()))
    return {"spells": spells}

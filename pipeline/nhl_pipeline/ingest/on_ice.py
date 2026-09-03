"""Derives Game.PlayOnIcePlayers from Game.Shifts -- NOT separately ingested from the API.
Pure interval-overlap join done set-based in SQL (Shifts.ShiftStart/EndSeconds and
Plays.PeriodTimeSeconds share the same period-elapsed-seconds clock). Scoped to
shot-attempt events since that's all Corsi/Fenwick/on-ice-xG need.
"""

_DELETE_SQL = """
    DELETE FROM Game.PlayOnIcePlayers
    WHERE PlayID IN (SELECT PlayID FROM Game.Plays WHERE GameID = ?)
"""

_INSERT_SQL = """
    INSERT INTO Game.PlayOnIcePlayers (PlayID, PlayerID, TeamID, RoleCode)
    SELECT DISTINCT p.PlayID, s.PlayerID, s.TeamID,
           CASE WHEN pl.PositionCode = 'G' THEN 'GOALIE' ELSE 'SKATER' END
    FROM Game.Plays p
    JOIN Game.Shifts s
      ON s.GameID = p.GameID
     AND s.PeriodNumber = p.PeriodNumber
     AND s.ShiftStartSeconds <= p.PeriodTimeSeconds
     AND s.ShiftEndSeconds >= p.PeriodTimeSeconds
    JOIN Reference.Players pl ON pl.PlayerID = s.PlayerID
    WHERE p.GameID = ?
      AND p.EventType IN ('shot-on-goal', 'missed-shot', 'blocked-shot', 'goal')
"""


def derive_on_ice_players(cursor, game_id: int) -> None:
    cursor.execute(_DELETE_SQL, game_id)
    cursor.execute(_INSERT_SQL, game_id)

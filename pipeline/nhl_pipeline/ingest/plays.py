from nhl_pipeline import db
from nhl_pipeline.api import field_map


def sync_plays(cursor, game_id: int, plays_raw: list, team_id_by_nhl: dict, player_id_by_nhl: dict) -> dict:
    """Upserts every play for the game. Returns {NHLPlayID: PlayID}."""
    for play in plays_raw:
        f = field_map.play_fields(play)
        db.upsert(
            cursor, "Game.Plays",
            {"GameID": game_id, "NHLPlayID": f["nhl_play_id"]},
            {
                "PeriodNumber": f["period_number"],
                "PeriodTimeSeconds": f["period_time_seconds"],
                "PeriodTimeRemaining": f["period_time_remaining"],
                "EventType": f["event_type"],
                "TeamID": team_id_by_nhl.get(f["team_nhl_id"]),
                "PlayerID": player_id_by_nhl.get(f["player_nhl_id"]),
                "SecondaryPlayerID": player_id_by_nhl.get(f["secondary_player_nhl_id"]),
                "XCoordinate": f["x_coord"],
                "YCoordinate": f["y_coord"],
                "HomeScore": f["home_score"],
                "AwayScore": f["away_score"],
                "StrengthCode": f["strength_code"],
            },
        )

    cursor.execute("SELECT NHLPlayID, PlayID FROM Game.Plays WHERE GameID = ?", game_id)
    return {row.NHLPlayID: row.PlayID for row in cursor.fetchall()}

from nhl_pipeline import db
from nhl_pipeline.api import field_map
from nhl_pipeline.calc import shot_characteristics


def sync_shots(
    cursor, game_id: int, plays_raw: list, play_id_by_nhl: dict,
    team_id_by_nhl: dict, player_id_by_nhl: dict, home_team_id: int,
) -> dict:
    """Upserts Shots for every shot-attempt play. Returns {NHLPlayID: ShotID}.

    Coordinate normalization happens here (not as a later pass) because
    homeTeamDefendingSide -- needed to normalize -- lives only on the raw play, not in the
    schema, and would be gone by the next run otherwise.
    """
    for play in plays_raw:
        if play["typeDescKey"] not in field_map.SHOT_ATTEMPT_EVENT_TYPES:
            continue
        if field_map.is_shootout_play(play):
            continue

        f = field_map.shot_fields(play)
        play_id = play_id_by_nhl[play["eventId"]]
        team_id = team_id_by_nhl.get(f["team_nhl_id"])
        team_is_home = team_id == home_team_id

        norm_x, norm_y = shot_characteristics.normalize_coordinates(
            f["x_coord"], f["y_coord"], f["home_defending_side"], team_is_home
        )
        distance, angle = shot_characteristics.compute_distance_angle(norm_x, norm_y)

        db.upsert(
            cursor, "Game.Shots",
            {"PlayID": play_id},
            {
                "GameID": game_id,
                "TeamID": team_id,
                "ShooterPlayerID": player_id_by_nhl.get(f["shooter_nhl_player_id"]),
                "GoaliePlayerID": player_id_by_nhl.get(f["goalie_nhl_player_id"]),
                "PeriodNumber": f["period_number"],
                "PeriodTimeSeconds": f["period_time_seconds"],
                "ShotEventType": f["shot_event_type"],
                "ShotType": f["shot_type"],
                "XCoordinate": norm_x,
                "YCoordinate": norm_y,
                "DistanceFeet": distance,
                "AngleDegrees": angle,
                "IsGoal": f["is_goal"],
                "IsBlocked": f["is_blocked"],
                "IsMissed": f["is_missed"],
                "StrengthCode": f["strength_code"],
                "HomeScore": f["home_score"],
                "AwayScore": f["away_score"],
            },
        )

    cursor.execute(
        "SELECT p.NHLPlayID, s.ShotID FROM Game.Shots s "
        "JOIN Game.Plays p ON p.PlayID = s.PlayID WHERE s.GameID = ?",
        game_id,
    )
    return {row.NHLPlayID: row.ShotID for row in cursor.fetchall()}

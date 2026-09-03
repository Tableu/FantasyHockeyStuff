from nhl_pipeline import db
from nhl_pipeline.api import field_map
from nhl_pipeline.calc import situation_resolver


def sync_goals(
    cursor, game_id: int, plays_raw: list, play_id_by_nhl: dict, shot_id_by_nhl: dict,
    team_id_by_nhl: dict, player_id_by_nhl: dict, code_map: dict, home_team_id: int,
) -> None:
    for play in plays_raw:
        if play["typeDescKey"] != "goal":
            continue
        if field_map.is_shootout_play(play):
            continue

        f = field_map.goal_fields(play)
        play_id = play_id_by_nhl[play["eventId"]]
        shot_id = shot_id_by_nhl[play["eventId"]]
        team_id = team_id_by_nhl.get(f["team_nhl_id"])
        team_is_home = team_id == home_team_id

        is_power_play, is_short_handed = situation_resolver.classify_strength(
            code_map, f["strength_code"], team_is_home
        )
        is_empty_net = f["goalie_nhl_player_id"] is None

        db.upsert(
            cursor, "Game.Goals",
            {"ShotID": shot_id},
            {
                "PlayID": play_id,
                "GameID": game_id,
                "TeamID": team_id,
                "ScoringPlayerID": player_id_by_nhl.get(f["scoring_nhl_player_id"]),
                "Assist1PlayerID": player_id_by_nhl.get(f["assist1_nhl_player_id"]),
                "Assist2PlayerID": player_id_by_nhl.get(f["assist2_nhl_player_id"]),
                "PeriodNumber": f["period_number"],
                "PeriodTimeSeconds": f["period_time_seconds"],
                "StrengthCode": f["strength_code"],
                "IsPowerPlay": is_power_play,
                "IsShortHanded": is_short_handed,
                "IsEmptyNet": is_empty_net,
                "HomeScore": f["home_score"],
                "AwayScore": f["away_score"],
                "HighlightClipID": f["highlight_clip_id"],
                "DiscreteClipID": f["discrete_clip_id"],
                "ClipSharingURL": f["clip_sharing_url"],
            },
        )

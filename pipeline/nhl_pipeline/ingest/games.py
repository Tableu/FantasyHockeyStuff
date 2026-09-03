from datetime import datetime

from nhl_pipeline import db


def ensure_game(
    cursor, *, nhl_game_id, season_id, game_type, game_date,
    home_team_id, away_team_id, home_score, away_score, game_state,
) -> int:
    if isinstance(game_date, str):
        game_date = datetime.strptime(game_date, "%Y-%m-%d").date()

    return db.upsert_get_id(
        cursor, "Game.Games", "GameID",
        {"NHLGameID": nhl_game_id},
        {
            "SeasonID": season_id,
            "GameType": str(game_type),
            "GameDate": game_date,
            "HomeTeamID": home_team_id,
            "AwayTeamID": away_team_id,
            "HomeScore": home_score,
            "AwayScore": away_score,
            "GameStatus": game_state,
        },
    )

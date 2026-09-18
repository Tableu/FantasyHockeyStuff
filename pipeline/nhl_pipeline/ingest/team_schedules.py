"""Reference.Teams / Reference.Schedule -- one team's full season from the club-schedule-season
endpoint (api.club_schedule), and the team-game-number -> game mapping the injury-history
import needs (its source publishes absences as "missed team games 39-40", not dates).

Cheaper than ingest.schedule.sync_schedule for whole past seasons: one request per
team-season instead of one per week, and it's the only way to reach a defunct team
(ATL/PHX/ARI) since those never appear in a current-season week. Upserts every game it sees
-- both teams of each game are upserted into Reference.Teams first (same call ingest.
teams_players.sync_teams makes), which is what gives the defunct franchises their rows.
"""

import logging
from datetime import datetime

from nhl_pipeline import db
from nhl_pipeline.api import club_schedule, field_map

log = logging.getLogger("ingest.team_schedules")

REGULAR_SEASON_GAME_TYPE = 2


def _upsert_team(cursor, side: dict) -> int:
    f = field_map.team_from_schedule_side(side)
    return db.upsert_get_id(
        cursor, "Reference.Teams", "TeamID",
        {"NHLTeamID": f["nhl_team_id"]},
        {"Abbreviation": f["abbreviation"], "TeamName": f["team_name"], "Location": f["location"]},
    )


def sync_team_season(cursor, abbrev: str, nhl_season_id: int, season_id: int) -> dict:
    """Returns {"team_id": TeamID, "games": {game_number: {"date": date, "nhl_game_id": int}},
    "schedule_rows": int}. game_number counts this team's regular-season games in schedule
    order (1..N; N is 82 in a normal season, 48/56/68-71 in 2012-13/2020-21/2019-20) --
    exactly the numbering the NHL Injury Viz database's Start/End columns use."""
    games = club_schedule.get_club_season_schedule(abbrev, nhl_season_id)
    if not games:
        raise ValueError(f"No schedule returned for {abbrev} {nhl_season_id}")

    team_ids: dict = {}
    team_id = None
    for game in games:
        for side_key in ("homeTeam", "awayTeam"):
            side = game[side_key]
            if side["id"] not in team_ids:
                team_ids[side["id"]] = _upsert_team(cursor, side)
            if side.get("abbrev") == abbrev:
                team_id = team_ids[side["id"]]

        f = field_map.schedule_row_fields(game, game["gameDate"])
        db.upsert(
            cursor, "Reference.Schedule",
            {"NHLGameID": f["nhl_game_id"]},
            {
                "SeasonID": season_id,
                "GameType": str(f["game_type"]) if f["game_type"] is not None else None,
                "GameDate": datetime.strptime(f["game_date"], "%Y-%m-%d").date(),
                "StartTimeUTC": datetime.fromisoformat(f["start_time_utc"]) if f["start_time_utc"] else None,
                "HomeTeamID": team_ids[f["home_nhl_team_id"]],
                "AwayTeamID": team_ids[f["away_nhl_team_id"]],
                "GameState": f["game_state"],
            },
        )

    if team_id is None:
        raise ValueError(f"{abbrev} never appears as home or away in its own {nhl_season_id} schedule")

    # Past seasons only list games that were actually played (verified: 2019-20 shows 70 for
    # TOR, not the 82 originally scheduled), but a cancelled game in a live season would
    # carry a non-'OK' gameScheduleState and mustn't consume a game number.
    regular = sorted(
        (
            g for g in games
            if g.get("gameType") == REGULAR_SEASON_GAME_TYPE and (g.get("gameScheduleState") or "OK") == "OK"
        ),
        key=lambda g: (g["gameDate"], g.get("startTimeUTC") or ""),
    )
    numbered = {
        number: {"date": datetime.strptime(g["gameDate"], "%Y-%m-%d").date(), "nhl_game_id": g["id"]}
        for number, g in enumerate(regular, start=1)
    }
    return {"team_id": team_id, "games": numbered, "schedule_rows": len(games)}

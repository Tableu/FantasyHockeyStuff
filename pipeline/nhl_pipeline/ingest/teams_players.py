from nhl_pipeline import db
from nhl_pipeline.api import field_map


def sync_teams(cursor, home_side: dict, away_side: dict) -> dict:
    """Upserts both teams for a game. Returns {NHLTeamID: TeamID}."""
    result = {}
    for side in (home_side, away_side):
        f = field_map.team_from_schedule_side(side)
        team_id = db.upsert_get_id(
            cursor, "Reference.Teams", "TeamID",
            {"NHLTeamID": f["nhl_team_id"]},
            {"Abbreviation": f["abbreviation"], "TeamName": f["team_name"], "Location": f["location"]},
        )
        result[f["nhl_team_id"]] = team_id
    return result


def sync_players(cursor, roster_spots: list) -> dict:
    """Upserts every player on both rosters for the game. Returns {NHLPlayerID: PlayerID}.

    Player <-> team affiliation is not recorded here: per game it is the TeamID on
    Plays/Shots/Goals/Shifts, and the team a player is signed with today
    (Reference.PlayerTeamHistory) comes from his NHL page, ingest/player_teams.py.
    """
    result = {}
    for spot in roster_spots:
        f = field_map.player_from_roster_spot(spot)
        player_id = db.upsert_get_id(
            cursor, "Reference.Players", "PlayerID",
            {"NHLPlayerID": f["nhl_player_id"]},
            {
                "FirstName": f["first_name"],
                "LastName": f["last_name"],
                "FullName": f["full_name"],
                "PositionCode": f["position_code"],
            },
        )
        result[f["nhl_player_id"]] = player_id
    return result

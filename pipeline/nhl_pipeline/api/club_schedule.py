"""GET https://api-web.nhle.com/v1/club-schedule-season/{abbrev}/{season} -- one team's full
season schedule (preseason/regular/playoffs) in a single response. Verified live back to
2000-01 and for defunct abbreviations (ATL, PHX, ARI, and 2024-25's UTA), each of which the
endpoint reports under its own NHL team id (11, 27, 53, 59). Game dicts are the same shape
as the schedule/{date} endpoint's, so field_map.schedule_row_fields / team_from_schedule_side
parse them unchanged.
"""

from nhl_pipeline.http_client import get_json

BASE = "https://api-web.nhle.com/v1"


def get_club_season_schedule(abbrev: str, nhl_season_id: int) -> list:
    data = get_json(f"{BASE}/club-schedule-season/{abbrev}/{nhl_season_id}")
    return data.get("games", [])

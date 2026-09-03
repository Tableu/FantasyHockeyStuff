"""GET https://api-web.nhle.com/v1/draft/picks/{year}/all -- verified live for the 2026
draft (state 'over'). Each pick has firstName/lastName/positionCode/height/weight and the
drafting team, but NOT a permanent NHL player ID -- see api/player_search.py for how that's
resolved.
"""

from nhl_pipeline.http_client import get_json

BASE = "https://api-web.nhle.com/v1"


def get_draft_picks(year: int) -> list:
    data = get_json(f"{BASE}/draft/picks/{year}/all")
    return data.get("picks", [])

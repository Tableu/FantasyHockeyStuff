"""GET https://search.d3.nhle.com/api/v1/search/player -- an undocumented but public
endpoint (NHL.com's own site search uses it) that resolves a player name to their permanent
numeric NHL player ID plus bio basics (height/weight/current team). Unlike the draft-picks
endpoint (api/draft.py), this ID is the same one used everywhere else in the pipeline
(rosterSpots, boxscore, etc.) -- including for a player who hasn't debuted yet, since
NHL.com creates their search-indexed profile as soon as they're drafted.
"""

from nhl_pipeline.http_client import get_json

BASE = "https://search.d3.nhle.com/api/v1"


def search_player(name: str, limit: int = 10) -> list:
    data = get_json(f"{BASE}/search/player", params={"culture": "en-us", "limit": limit, "q": name})
    return data or []

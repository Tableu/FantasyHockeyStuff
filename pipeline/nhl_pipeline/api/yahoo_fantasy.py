"""GET https://pub-api-ro.fantasysports.yahoo.com/fantasy/v2/... -- Yahoo's public
read-only Fantasy API. Verified live: no OAuth, no cookies, no login needed -- unlike
Yahoo's regular OAuth-gated fantasysports.yahooapis.com API, which has no platform-wide
player pool at all (every resource there is scoped to a league the authenticated user
belongs to). This is the same endpoint Yahoo's own public Draft Analysis page
(hockey.fantasysports.yahoo.com/hockey/draftanalysis) calls client-side, found by
inspecting that page's network requests.

"{game_key}.l.public" is a special pseudo-league key Yahoo uses to expose ADP
(draft_analysis) and position eligibility (eligible_positions) for the whole NHL player
pool without requiring membership in a real league -- the game_key itself (477 for the
2026-27 season) is looked up live via /game/nhl rather than hardcoded, since Yahoo
increments it every season same as ESPN's per-year seasons/{year} path.

One call with a high enough count returns the entire pool (1573 players as of this
writing, no further pagination needed, unlike Fleaflicker's forced result_offset paging).
"""

from nhl_pipeline.http_client import get_json

BASE = "https://pub-api-ro.fantasysports.yahoo.com/fantasy/v2"
_LIMIT = 3000


def get_game_key() -> str:
    data = get_json(f"{BASE}/game/nhl", params={"format": "json_f"})
    return data["fantasy_content"]["game"]["game_key"]


def get_players(game_key: str) -> list:
    url = (
        f"{BASE}/league/{game_key}.l.public/players;position=ALL;"
        f"start=0;count={_LIMIT};sort=average_pick/draft_analysis"
    )
    data = get_json(url, params={"format": "json_f"})
    return [p["player"] for p in data["fantasy_content"]["league"]["players"]]

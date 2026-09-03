"""GET https://www.fleaflicker.com/api/FetchPlayerListing -- verified live. No API key
needed, but every Fleaflicker endpoint (confirmed against their published API docs) is
scoped to one specific league_id -- there's no platform-wide player pool the way ESPN has.
FLEAFLICKER_PROXY_LEAGUE_ID in ingest/fantasy_fleaflicker.py picks a real, well-populated
public NHL league to stand in for one, since real position eligibility is far more stable
across leagues (it's essentially just the player's real position) than something like ADP
would be. Paginated 30 players/page via result_offset; verified live at 1300 total NHL
players for the chosen league.
"""

from nhl_pipeline.http_client import get_json

BASE = "https://www.fleaflicker.com/api"
_PAGE_SIZE = 30


def get_players(league_id: int) -> list:
    """Pages via result_offset until resultTotal players have been collected -- the
    endpoint doesn't reliably return a short final page to signal the end (a naive
    "stop when the page is smaller than the page size" loop kept paging past the real
    total and got rate-limited), so the authoritative count from the first response is
    used as the stopping point instead."""
    players: list = []
    offset = 0
    total = None
    while total is None or offset < total:
        data = get_json(
            f"{BASE}/FetchPlayerListing",
            params={"sport": "NHL", "league_id": league_id, "result_offset": offset},
        )
        if total is None:
            total = data.get("resultTotal", 0)
        page = data.get("players", [])
        if not page:
            break
        players.extend(page)
        offset += _PAGE_SIZE
    return players

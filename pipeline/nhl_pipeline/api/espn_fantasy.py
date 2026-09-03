"""GET https://lm-api-reads.fantasy.espn.com/apis/v3/games/fhl/seasons/{year}/players --
ESPN's public NHL fantasy player pool (fhl = "fantasy hockey league", ESPN's internal game
code). Verified live: no API key or league_id needed, unlike Yahoo (OAuth-only) or
Fleaflicker (every endpoint is scoped to one specific league_id, with no platform-wide
equivalent) -- this one genuinely returns the whole player universe with each player's
ownership.averageDraftPosition and eligibleSlots in a single call (1738 players, no
pagination needed as of this writing; limit is set well above that as a margin).
"""

import json

from nhl_pipeline.http_client import get_json

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fhl/seasons"
_LIMIT = 3000


def get_players(year: int) -> list:
    filter_header = json.dumps({
        "players": {
            "limit": _LIMIT,
            "sortDraftRanks": {"sortPriority": 1, "sortAsc": True, "value": "STANDARD"},
        }
    })
    return get_json(
        f"{BASE}/{year}/players",
        params={"scoringPeriodId": 0, "view": "kona_player_info"},
        extra_headers={"X-Fantasy-Filter": filter_header},
    )

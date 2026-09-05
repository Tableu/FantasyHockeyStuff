"""Fantrax's public REST API (fxea/general/*), documented live at fantrax.com/developer.
Verified live: no API key, userSecretId, or league ownership needed for any of these three --
only account/league-management endpoints like getLeagues need a userSecretId.

No single endpoint has name + full position eligibility + ADP together, so three calls
combine, all keyed by the same Fantrax player id (verified live -- an id from one response
looks up the same player in the others):

- getPlayerIds -- the master id -> name/team/single-position map for every player Fantrax
  tracks (8,966 for NHL as of writing). Names come back "Last, First".
- getLeagueInfo?leagueId=... -- id -> eligiblePos for every player in one specific league's
  pool (8,682 for NHL) -- real multi-position eligibility here (e.g. "LW,C"), unlike
  getPlayerIds' single `position` field. See ingest/fantasy_fantrax.py for which league id
  and why.
- getAdp -- id -> ADP for the much smaller (~477) subset of players with real ADP data. No
  ADP=0 placeholder observed live -- every player returned here has a real ADP.

See ingest/fantasy_fantrax.py for how the three are joined.
"""

from nhl_pipeline.http_client import get_json

BASE = "https://www.fantrax.com/fxea/general"


def get_player_ids(sport: str = "NHL") -> dict:
    return get_json(f"{BASE}/getPlayerIds", params={"sport": sport})


def get_league_positions(league_id: str) -> dict:
    data = get_json(f"{BASE}/getLeagueInfo", params={"leagueId": league_id})
    return data["playerInfo"]


def get_adp(sport: str = "NHL") -> list:
    return get_json(f"{BASE}/getAdp", params={"sport": sport})

"""GET https://search.d3.nhle.com/api/v1/search/player -- an undocumented but public
endpoint (NHL.com's own site search uses it) that resolves a player name to their permanent
numeric NHL player ID plus bio basics (height/weight/current team). Unlike the draft-picks
endpoint (api/draft.py), this ID is the same one used everywhere else in the pipeline
(rosterSpots, boxscore, etc.) -- including for a player who hasn't debuted yet, since
NHL.com creates their search-indexed profile as soon as they're drafted.
"""

from nhl_pipeline.api import field_map
from nhl_pipeline.http_client import get_json

BASE = "https://search.d3.nhle.com/api/v1"


def search_player(name: str, limit: int = 10) -> list:
    data = get_json(f"{BASE}/search/player", params={"culture": "en-us", "limit": limit, "q": name})
    return data or []


def find_exact_match(full_name: str, team_abbrev: str | None = None) -> dict | None:
    """Searches for full_name and returns the single field_map.player_search_result_fields-
    shaped match, or None if zero or still-ambiguous candidates come back. Shared by
    ingest.draft (a draft pick, which has a team_abbrev to disambiguate same-named players
    with) and ingest.player_backfill (a bare unresolved name, no team context) -- the two
    "has a real NHL id, hasn't dressed for an ingested game yet" resolution paths, so a fix to
    the matching logic (accents, suffixes, whitespace) only ever has to happen once."""
    candidates = [
        field_map.player_search_result_fields(r)
        for r in search_player(full_name)
        if (r.get("name") or "").strip().lower() == full_name.strip().lower()
    ]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1 and team_abbrev:
        team_matches = [c for c in candidates if c["team_abbrev"] == team_abbrev]
        if len(team_matches) == 1:
            return team_matches[0]
    return None

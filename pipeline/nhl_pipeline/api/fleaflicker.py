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
    """Pages via result_offset until the listing is exhausted -- the endpoint doesn't reliably
    return a short final page to signal the end (a naive "stop when the page is smaller than the
    page size" loop kept paging past the real total and got rate-limited). A league that reports
    `resultTotal` stops there; one that does not (league 12090 returns only `resultOffsetNext`)
    stops when `resultOffsetNext` is gone. Reading `resultTotal` alone, a league without it
    returned just the first page of 30."""
    players: list = []
    offset = 0
    total = None
    while total is None or offset < total:
        data = get_json(
            f"{BASE}/FetchPlayerListing",
            params={"sport": "NHL", "league_id": league_id, "result_offset": offset},
        )
        if total is None and "resultTotal" in data:
            total = data["resultTotal"]
        page = data.get("players", [])
        if not page:
            break
        players.extend(page)
        following = data.get("resultOffsetNext")
        if total is None and following is None:
            break
        offset = following if following is not None else offset + _PAGE_SIZE
    return players


def injuries(players: list) -> list:
    """The listing's injured players, from `get_players`. Verified 2026-09-25 on league 12090
    (1,320 players, 39 flagged): `proPlayer.injury` = {typeAbbreviaition (sic, Fleaflicker's
    spelling), typeFull, severity, description}; types seen OUT and IR, severity OUT for both.
    IR is the league's own designation, i.e. the one that makes a player IR-slot eligible."""
    rows = []
    for entry in players:
        pro = entry["proPlayer"]
        injury = pro.get("injury")
        if not injury:
            continue
        rows.append({
            "external_id": str(pro["id"]),
            "name": pro["nameFull"],
            "position": pro.get("position"),
            "team_abbreviation": pro.get("proTeamAbbreviation"),
            "type": injury.get("typeAbbreviaition") or injury.get("typeAbbreviation"),
            "type_full": injury.get("typeFull"),
            "severity": injury.get("severity"),
            "description": injury.get("description"),
        })
    return rows

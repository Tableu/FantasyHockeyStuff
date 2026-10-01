"""GET https://www.fleaflicker.com/api/FetchPlayerListing -- verified live. No API key
needed, but every Fleaflicker endpoint (confirmed against their published API docs) is
scoped to one specific league_id -- there's no platform-wide player pool the way ESPN has.
FLEAFLICKER_PROXY_LEAGUE_ID in ingest/fantasy_fleaflicker.py picks a real, well-populated
public NHL league to stand in for one, since real position eligibility is far more stable
across leagues (it's essentially just the player's real position) than something like ADP
would be. Paginated 30 players/page via result_offset; verified live at 1300 total NHL
players for the chosen league.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

import requests

from nhl_pipeline.http_client import get_json

log = logging.getLogger("api.fleaflicker")

BASE = "https://www.fleaflicker.com/api"
_PAGE_SIZE = 30
# Pages fetched at a time once the first page has given the total: the 44 pages of a full
# listing took 15.8 s one after another and 4.8 s four at a time (2026-09-30, league 12090: the
# same 1,320 players in the same order, the same 94 injured). Each worker keeps http_client's
# per-request pause, so the request rate is four times the sequential one, not unbounded.
PAGE_WORKERS = 4
# Fewer players than this is not the whole league's listing. Once a season's first scoring period
# opens, the default listing sorts by the current period and stops at 200 (league 12090,
# 2026-09-29: resultTotal 200) -- everyone not scoring, the injured included, drops out of it.
# The whole pool is ~1,300.
FULL_LISTING = 1000


class PartialListing(RuntimeError):
    """The listing reported fewer than FULL_LISTING players: a capped or partial read."""


def get_players(league_id: int, sort_season: int | None = None) -> list:
    """Every player in `league_id`'s listing, paged via result_offset.

    `sort_season`: sort by that season's totals (Fleaflicker's season = its starting year). A
    completed season lists every player (1,300 on 2026-09-29) with each one's CURRENT injury flag;
    in-season the default listing is capped at 200. Raises PartialListing when the listing reports
    fewer than FULL_LISTING players, so a capped read can never pass for the whole league.

    A listing that reports its total has the rest of its pages fetched PAGE_WORKERS at a time; one
    that does not, or a parallel read Fleaflicker rate-limits (429 after http_client's retries),
    is paged one at a time as before."""
    params = {"sport": "NHL", "league_id": league_id}
    if sort_season is not None:
        params["sort_season"] = sort_season
    first = get_json(f"{BASE}/FetchPlayerListing", params={**params, "result_offset": 0})
    total = first.get("resultTotal")
    if total is not None and total < FULL_LISTING:
        raise PartialListing(
            f"league {league_id}'s listing reports {total} players (sort {first.get('sort')}); "
            f"a full one has ~1,300 -- pass sort_season=<a completed season>")
    players = None
    if total is not None:
        try:
            players = _pages_in_parallel(params, first, total)
        except requests.HTTPError as error:
            if error.response is None or error.response.status_code != 429:
                raise
            log.warning("Fleaflicker rate-limited the parallel listing read; paging one at a time")
    if players is None:
        players = _pages_in_sequence(params, first, total)
    if len(players) < FULL_LISTING:
        raise PartialListing(f"league {league_id}'s listing returned {len(players)} players; "
                             f"a full one has ~1,300")
    return players


def _pages_in_parallel(params: dict, first: dict, total: int) -> list:
    """The first page plus every later page up to `total`, in offset order."""
    step = first.get("resultOffsetNext") or _PAGE_SIZE

    def page(offset):
        return get_json(f"{BASE}/FetchPlayerListing",
                        params={**params, "result_offset": offset}).get("players", [])

    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
        rest = list(pool.map(page, range(step, total, step)))
    return first.get("players", []) + [player for later in rest for player in later]


def _pages_in_sequence(params: dict, first: dict, total: int | None) -> list:
    """Pages via result_offset until the listing is exhausted -- the endpoint doesn't reliably
    return a short final page to signal the end (a naive "stop when the page is smaller than the
    page size" loop kept paging past the real total and got rate-limited). A listing with a
    `resultTotal` stops there; one without (league 12090 before its season, returning only
    `resultOffsetNext`) stops when `resultOffsetNext` is gone."""
    players: list = []
    data = first
    offset = 0
    while True:
        page = data.get("players", [])
        if not page:
            break
        players.extend(page)
        following = data.get("resultOffsetNext")
        if total is None and following is None:
            break
        offset = following if following is not None else offset + _PAGE_SIZE
        if total is not None and offset >= total:
            break
        data = get_json(f"{BASE}/FetchPlayerListing", params={**params, "result_offset": offset})
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

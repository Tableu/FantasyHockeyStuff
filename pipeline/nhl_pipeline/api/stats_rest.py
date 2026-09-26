"""GET https://api.nhle.com/stats/rest/en/{skater|goalie}/{report} -- the NHL.com stats pages'
season reports. Verified live 2026-09-26: with `isAggregate=false` and a `seasonId` filter they
return ONE row per player per season, already summed across the teams he played for
(`teamAbbrevs` = "ATL,MTL"), and `limit=-1` returns the whole season in one page. Skater
`realtime` reports hits and blocked shots as 0 (not null) for every player before 2005-06, when
the NHL started tracking them."""

from nhl_pipeline.http_client import get_json

BASE = "https://api.nhle.com/stats/rest/en"
REGULAR_SEASON = 2


def get_season_report(kind: str, report: str, nhl_season_id: int) -> list:
    """Every player's row of one report (`skater/summary`, `skater/realtime`, `skater/bios`,
    `goalie/summary`, `goalie/bios`) for one regular season."""
    payload = get_json(f"{BASE}/{kind}/{report}", params={
        "isAggregate": "false", "isGame": "false", "start": 0, "limit": -1,
        "cayenneExp": f"seasonId={nhl_season_id} and gameTypeId={REGULAR_SEASON}",
    })
    rows = payload["data"]
    if len(rows) != payload["total"]:
        raise RuntimeError(f"{kind}/{report} {nhl_season_id}: {len(rows)} rows of {payload['total']}")
    return rows

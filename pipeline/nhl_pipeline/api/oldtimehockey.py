"""Old Time Hockey's ADP (roldtimehockey.com/adp), the average draft position across the r/OldTimeHockey
leagues' drafts -- a set of public Fleaflicker leagues. The page is a React app; its data is one
public JSON endpoint (found in the page's script bundle, verified live 2026-09-26):

    GET https://roldtimehockey.com/node/adp?year=<season start year>[&tiers=...]

-> a list of {PlayerId, PlayerName, ADP, MinPick, MaxPick, TimesDrafted, PlayerTeam,
PlayerPositions}, best ADP first. `PlayerId` is the player's Fleaflicker id (all 252 rows of the
2026-27 list matched Fantasy.PlatformPlayerIDs' Fleaflicker ids to the same player). `TimesDrafted`
is how many of the leagues' drafts took him, so the largest value is the number of drafts so far.
An empty list means no draft that season yet.
"""

from nhl_pipeline.http_client import get_json

URL = "https://roldtimehockey.com/node/adp"


def get_adp(season_start_year: int) -> list:
    return get_json(URL, params={"year": season_start_year})

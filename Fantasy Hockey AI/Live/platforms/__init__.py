"""Read-only adapters, one per fantasy platform: everything a Live tool reads from a real league.

    fleaflicker.py   Fleaflicker's public API (no auth): the draft board, rosters and IR, a
                     team's current lineup slots, transactions (moves used this week), the
                     matchup, the league's roster rules
    standalone.py    no platform: a draft board built from the pick order alone (--standalone)
    espn.py          ESPN's league API (a private league with the owner's cookies): settings and
                     scoring, rosters and IR, lineup slots, the matchup, the draft

An adapter returns platform ids and names; mapping them to our PlayerIDs is `base.PlayerIds`
(ModelFeatures' platform_ids.parquet). Nothing here writes anywhere, and nothing here submits a
pick or a move: every platform's API is read, never written.
"""

from platforms import espn, fleaflicker, standalone
from platforms.base import Matchup, PlayerIds, TeamRoster


def for_league(league, season=None):
    """The adapter for a registry league (leagues.load), or None for a league with no readable
    platform (it runs --standalone). `season` reads a past season (e.g. 2025) where the platform
    allows it."""
    if league.platform == "fleaflicker" and league.league_id is not None:
        return fleaflicker.Fleaflicker(league.league_id, season=season)
    if league.platform == "espn" and league.league_id is not None:
        import leagues
        # `season` is the season's START year everywhere (2025 = 2025-26); ESPN numbers the end year.
        return espn.Espn(league.league_id, season + 1 if season else espn.espn_year(league.season),
                         cookies=leagues.credentials(league))
    return None


__all__ = ["Matchup", "PlayerIds", "TeamRoster", "espn", "fleaflicker", "for_league", "standalone"]

"""Read-only adapters, one per fantasy platform: everything a Live tool reads from a real league.

    fleaflicker.py   Fleaflicker's public API (no auth): the draft board, rosters and IR, a
                     team's current lineup slots, transactions (moves used this week), the
                     matchup, the league's roster rules
    standalone.py    no platform: a draft board built from the pick order alone (--standalone)
    espn.py          not yet -- ESPN's public league API, once a league is joined (plan Step 2)

An adapter returns platform ids and names; mapping them to our PlayerIDs is `base.PlayerIds`
(ModelFeatures' platform_ids.parquet). Nothing here writes anywhere, and nothing here submits a
pick or a move: every platform's API is read, never written.
"""

from platforms import fleaflicker, standalone
from platforms.base import Matchup, PlayerIds, TeamRoster


def for_league(league, season=None):
    """The adapter for a registry league (leagues.load), or None for a league with no readable
    platform (it runs --standalone). `season` reads a past season (e.g. 2025) where the platform
    allows it."""
    if league.platform == "fleaflicker" and league.league_id is not None:
        return fleaflicker.Fleaflicker(league.league_id, season=season)
    if league.platform == "espn" and league.league_id is not None:
        raise NotImplementedError("the ESPN adapter is not built yet (plan Step 2: once a league "
                                  "is joined, so its responses can be checked)")
    return None


__all__ = ["Matchup", "PlayerIds", "TeamRoster", "fleaflicker", "for_league", "standalone"]

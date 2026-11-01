"""Fantasy.PlatformPlayerIDs, in season: the ids of players the preseason imports missed, ADDED to
what is there -- never a replace.

The preseason imports (fantasy_espn.sync_espn, fantasy_fleaflicker.sync_fleaflicker) delete and
rewrite a platform's whole season of ids from its player pool as it stood then. A call-up who
was not in that pool has no id all season, so a live league that rosters him reads him as an
unknown platform id: Server/live.py leaves him out ("no PlayerID") and his team plays a spot short
in the plan. Rerunning those imports in season is no fix: Fleaflicker's listing is capped at 200
once the season opens (api/fleaflicker.get_players raises on it), and a replace would drop every
id it no longer lists.

So this only adds:

- **ESPN**: the platform-wide pool, every entry whose id has no row yet and that is live --
  `active`, or on an NHL team with this season's game details. ESPN leaves a call-up's current
  entry `active: false` (Matt Tomkins, EDM, 3069614, confirmed to start 2026-11-01), which the
  preseason filter on `active` drops; a stale duplicate id has no team and no game details
  (Tomkins' 4392363), and neither has a retired player, news or not (Patrice Bergeron).
- **Fleaflicker**: every rostered player in each registry Fleaflicker league (FetchLeagueRosters,
  one call per league) whose id has no row yet -- the ids a live plan cannot do without. A free
  agent needs none: the plan's free agents are its player pool less the rostered.

Names resolve as in the preseason imports (name_resolver, Fantasy aliases). A player resolved here
with no platform positions for the season gets them too (Fantasy.PlayerPositions), so his slots
are the platform's rather than his NHL position's.
"""

import logging

from nhl_pipeline import db, name_resolver
from nhl_pipeline.api import espn_fantasy, field_map, fleaflicker
from nhl_pipeline.ingest.fantasy_fleaflicker import (ALIAS_TABLE, CONFIRMED_IDS, IDS_TABLE, UNRESOLVED_TABLE,
                                                     ensure_ids_table, get_or_create_platform)

log = logging.getLogger("ingest.platform_ids")

# External ids whose platform name does not resolve, pinned by hand: {platform: {id: PlayerID}}.
CONFIRMED = {
    "Fleaflicker": CONFIRMED_IDS,
    # Vancouver's defenceman (b. 2004), "Elias N. Pettersson" on ESPN -- live_snapshots pins his
    # Daily Faceoff spelling to the same player. Rostered in espn-la, unmatched until 2026-11-01.
    "ESPN": {5238086: 784},
}


def _known(cursor, platform_id: int, season_id: int) -> tuple:
    """(external ids with a row this season, players with platform positions this season)."""
    cursor.execute(f"SELECT ExternalPlayerID FROM {IDS_TABLE} WHERE FantasyPlatformID = ? AND SeasonID = ?",
                   platform_id, season_id)
    ids = {str(row[0]) for row in cursor.fetchall()}
    cursor.execute("SELECT DISTINCT PlayerID FROM Fantasy.PlayerPositions WHERE FantasyPlatformID = ? AND SeasonID = ?",
                   platform_id, season_id)
    return ids, {int(row[0]) for row in cursor.fetchall()}


def _add(cursor, platform_id: int, season_id: int, entries: list, platform: str) -> dict:
    """Resolve and add each (external id, name, position codes) not yet known. Returns counts and
    the names added, for the log."""
    ensure_ids_table(cursor)
    known_ids, positioned = _known(cursor, platform_id, season_id)
    player_index = name_resolver.load_player_index(cursor)
    alias_map = name_resolver.load_alias_map(cursor, ALIAS_TABLE, platform_id)
    counts = {"ids": 0, "positions": 0, "unresolved": 0, "added": []}
    for external_id, name, codes in entries:
        if external_id is None or not name or str(external_id) in known_ids:
            continue
        player_id = CONFIRMED.get(platform, {}).get(int(external_id))
        if player_id is None:
            player_id = name_resolver.resolve_player_id(cursor, ALIAS_TABLE, UNRESOLVED_TABLE, platform_id, name,
                                                        alias_map, player_index, position_codes=codes)
        if player_id is None:
            counts["unresolved"] += 1
            log.warning("%s: %s (%s) did not resolve to a player -- see Fantasy.UnresolvedPlayerNames",
                        platform, name, external_id)
            continue
        db.upsert(cursor, IDS_TABLE, {"FantasyPlatformID": platform_id, "SeasonID": season_id,
                                      "ExternalPlayerID": str(external_id)}, {"PlayerID": player_id})
        known_ids.add(str(external_id))
        counts["ids"] += 1
        counts["added"].append(f"{name} ({external_id} -> {player_id})")
        if player_id not in positioned:
            for code in codes:
                db.upsert(cursor, "Fantasy.PlayerPositions",
                          {"FantasyPlatformID": platform_id, "PlayerID": player_id, "SeasonID": season_id,
                           "PositionCode": code}, None)
                counts["positions"] += 1
            positioned.add(player_id)
    log.info("%s: %d id(s) added, %d position row(s), %d unresolved%s", platform, counts["ids"],
             counts["positions"], counts["unresolved"],
             (": " + ", ".join(counts["added"])) if counts["added"] else "")
    return counts


def live_espn_entry(player: dict) -> bool:
    """An ESPN pool entry for a player in the league now: `active`, or on an NHL team (proTeamId 0
    is none) with this season's game details."""
    return bool(player.get("active") or (player.get("proTeamId") and player.get("proGamePlayerDetail")))


def add_espn(cursor, year: int, season_id: int) -> dict:
    """ESPN ids missing for live pool entries. `year`: ESPN's season, the year it ends."""
    platform_id = get_or_create_platform(cursor, "ESPN")
    entries = []
    for player in espn_fantasy.get_players(year):
        if not live_espn_entry(player):
            continue
        f = field_map.espn_player_fields(player)
        entries.append((f["espn_player_id"], f["full_name"], f["position_codes"]))
    return _add(cursor, platform_id, season_id, entries, "ESPN")


def add_fleaflicker(cursor, season_id: int, league_ids) -> dict:
    """Fleaflicker ids missing for the players rostered in these leagues."""
    platform_id = get_or_create_platform(cursor, "Fleaflicker")
    entries = []
    for league_id in league_ids:
        for player in fleaflicker.get_league_rosters(league_id):
            f = field_map.fleaflicker_player_fields(player)
            entries.append((f["fleaflicker_player_id"], f["full_name"], f["position_codes"]))
    return _add(cursor, platform_id, season_id, entries, "Fleaflicker")

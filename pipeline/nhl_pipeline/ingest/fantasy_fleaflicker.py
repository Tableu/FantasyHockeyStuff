"""Fantasy.PlayerPositions -- Fleaflicker. Unlike ESPN, Fleaflicker has no platform-wide
player pool: every endpoint (verified against their published API docs) requires a specific
league_id. FLEAFLICKER_PROXY_LEAGUE_ID picks a real, well-populated public NHL league
(https://www.fleaflicker.com/nhl/leagues/100) as a stand-in, since position eligibility is
essentially just the player's real position and is far more stable across leagues than
something usage-driven like ADP would be -- ADP itself is deliberately not pulled from here
for that reason.

Fleaflicker's own player IDs aren't NHLPlayerID, so names are resolved through
nhl_pipeline.name_resolver, same two-tier approach as every other external source in this
pipeline (see that module's docstring).

Every run fully replaces this platform's PlayerPositions rows for the season rather than only
upserting: a player whose real eligibility changed since the last run, or who dropped out of
the proxy league's roster entirely, must not keep a stale row forever (see fantasy_espn.py's
docstring for how this bit ESPN in practice).
"""

import logging

from nhl_pipeline import db, name_resolver
from nhl_pipeline.api import field_map, fleaflicker

log = logging.getLogger("ingest.fantasy_fleaflicker")

ALIAS_TABLE = "Fantasy.PlayerNameAliases"
UNRESOLVED_TABLE = "Fantasy.UnresolvedPlayerNames"

FLEAFLICKER_PROXY_LEAGUE_ID = 100


def get_or_create_platform(cursor, platform_name: str) -> int:
    return db.upsert_get_id(
        cursor, "Fantasy.Platforms", "FantasyPlatformID",
        {"PlatformName": platform_name}, None,
    )


def sync_fleaflicker(cursor, season_id: int, league_id: int = FLEAFLICKER_PROXY_LEAGUE_ID) -> dict:
    platform_id = get_or_create_platform(cursor, "Fleaflicker")

    player_index = name_resolver.load_player_index(cursor)
    alias_map = name_resolver.load_alias_map(cursor, ALIAS_TABLE, platform_id)
    raw_players = fleaflicker.get_players(league_id)

    db.delete_where(cursor, "Fantasy.PlayerPositions", {"FantasyPlatformID": platform_id, "SeasonID": season_id})

    counts = {"positions": 0, "unresolved": 0}
    for raw in raw_players:
        f = field_map.fleaflicker_player_fields(raw)
        if not f["full_name"]:
            continue

        player_id = name_resolver.resolve_player_id(
            cursor, ALIAS_TABLE, UNRESOLVED_TABLE, platform_id, f["full_name"], alias_map, player_index,
        )
        if player_id is None:
            counts["unresolved"] += 1
            continue

        for position_code in f["position_codes"]:
            db.upsert(
                cursor, "Fantasy.PlayerPositions",
                {
                    "FantasyPlatformID": platform_id, "PlayerID": player_id,
                    "SeasonID": season_id, "PositionCode": position_code,
                },
                None,
            )
            counts["positions"] += 1

    log.info(
        "Fleaflicker: %d position row(s), %d unresolved name(s)",
        counts["positions"], counts["unresolved"],
    )
    return counts

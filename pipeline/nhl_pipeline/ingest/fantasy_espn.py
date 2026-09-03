"""Fantasy.PlayerADP / Fantasy.PlayerPositions -- ESPN. ESPN's player pool is keyed by its
own numeric player ID, not NHLPlayerID, and doesn't include one, so each player is resolved
by name through nhl_pipeline.name_resolver against Fantasy.PlayerNameAliases/
UnresolvedPlayerNames (same two-tier approach as the Projections sources, see that module's
docstring) rather than trusting a name match blindly.

A player with no averageDraftPosition (undrafted-in-practice, e.g. deep prospects/emergency
call-ups) is skipped for PlayerADP but still gets its PlayerPositions rows -- position
eligibility and ADP are independent facts.

Before the draft season has produced any real aggregate data (verified live in late August,
well before ESPN's typical Sept/Oct fantasy hockey draft window), ESPN returns the exact same
placeholder averageDraftPosition for every single player rather than omitting it -- silently
indistinguishable from real data by looking at one player alone. Guarded against here: if the
whole fetched pool has fewer than 2 distinct ADP values, none of it is written (positions
still are), since a real draft population is never that uniform.
"""

import logging

from nhl_pipeline import db, name_resolver
from nhl_pipeline.api import espn_fantasy, field_map

log = logging.getLogger("ingest.fantasy_espn")

ALIAS_TABLE = "Fantasy.PlayerNameAliases"
UNRESOLVED_TABLE = "Fantasy.UnresolvedPlayerNames"


def get_or_create_platform(cursor, platform_name: str) -> int:
    return db.upsert_get_id(
        cursor, "Fantasy.Platforms", "FantasyPlatformID",
        {"PlatformName": platform_name}, None,
    )


def sync_espn(cursor, year: int, season_id: int) -> dict:
    platform_id = get_or_create_platform(cursor, "ESPN")

    player_index = name_resolver.load_player_index(cursor)
    alias_map = name_resolver.load_alias_map(cursor, ALIAS_TABLE, platform_id)

    fields = [field_map.espn_player_fields(p) for p in espn_fantasy.get_players(year)]
    distinct_adp = {f["average_draft_position"] for f in fields if f["average_draft_position"]}
    adp_is_live = len(distinct_adp) > 1
    if not adp_is_live:
        log.warning(
            "ESPN ADP looks like a not-yet-live placeholder (only %d distinct value(s) across "
            "the whole pool) -- skipping PlayerADP this run, positions still imported", len(distinct_adp),
        )

    counts = {"adp": 0, "positions": 0, "unresolved": 0}
    for f in fields:
        if not f["full_name"]:
            continue

        player_id = name_resolver.resolve_player_id(
            cursor, ALIAS_TABLE, UNRESOLVED_TABLE, platform_id, f["full_name"], alias_map, player_index,
        )
        if player_id is None:
            counts["unresolved"] += 1
            continue

        if adp_is_live and f["average_draft_position"]:
            db.upsert(
                cursor, "Fantasy.PlayerADP",
                {"FantasyPlatformID": platform_id, "PlayerID": player_id, "SeasonID": season_id},
                {"ADP": round(f["average_draft_position"], 2)},
            )
            counts["adp"] += 1

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
        "ESPN: %d ADP row(s), %d position row(s), %d unresolved name(s)",
        counts["adp"], counts["positions"], counts["unresolved"],
    )
    return counts

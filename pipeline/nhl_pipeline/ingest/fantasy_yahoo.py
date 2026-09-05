"""Fantasy.PlayerADP / Fantasy.PlayerPositions -- Yahoo. Pulled live from Yahoo's public
read-only Fantasy API (pub-api-ro.fantasysports.yahoo.com) via nhl_pipeline.api.yahoo_fantasy
-- the same endpoint that powers Yahoo's public Draft Analysis page
(hockey.fantasysports.yahoo.com/hockey/draftanalysis), confirmed to need no OAuth/login,
unlike Yahoo's regular fantasy API. Same two-tier name resolution as fantasy_espn.py
(Fantasy.PlayerNameAliases/UnresolvedPlayerNames), since Yahoo's raw names don't always
match Reference.Players' spelling either.

A player with no averageDraftPosition (Yahoo's own "-" placeholder for undrafted-in-practice
players) is skipped for PlayerADP but still gets its PlayerPositions rows -- position
eligibility and ADP are independent facts (same convention as fantasy_espn.py).
"""

import logging

from nhl_pipeline import db, name_resolver
from nhl_pipeline.api import field_map, yahoo_fantasy

log = logging.getLogger("ingest.fantasy_yahoo")

ALIAS_TABLE = "Fantasy.PlayerNameAliases"
UNRESOLVED_TABLE = "Fantasy.UnresolvedPlayerNames"


def get_or_create_platform(cursor, platform_name: str) -> int:
    return db.upsert_get_id(
        cursor, "Fantasy.Platforms", "FantasyPlatformID",
        {"PlatformName": platform_name}, None,
    )


def sync_yahoo(cursor, season_id: int) -> dict:
    platform_id = get_or_create_platform(cursor, "Yahoo")

    player_index = name_resolver.load_player_index(cursor)
    alias_map = name_resolver.load_alias_map(cursor, ALIAS_TABLE, platform_id)

    game_key = yahoo_fantasy.get_game_key()
    fields = [field_map.yahoo_player_fields(p) for p in yahoo_fantasy.get_players(game_key)]

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

        if f["average_draft_position"] is not None:
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
        "Yahoo: %d ADP row(s), %d position row(s), %d unresolved name(s)",
        counts["adp"], counts["positions"], counts["unresolved"],
    )
    return counts

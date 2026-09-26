"""Fantasy.PlayerADP -- Old Time Hockey (the r/OldTimeHockey leagues' ADP, nhl_pipeline.api.oldtimehockey).
ADP only: the leagues play on Fleaflicker, so their positions are Fleaflicker's, which
import_fantasy_fleaflicker.py already loads.

Each row carries the player's Fleaflicker id, looked up in Fantasy.PlatformPlayerIDs' Fleaflicker
rows for the season (import_fantasy_fleaflicker.py writes them); a player that league's pool did not
list falls back to the usual name resolution (nhl_pipeline.name_resolver), under this platform's
own aliases. A name with no candidate at all is skipped without an unresolved row, as for Fantrax.

Every run fully replaces this platform's PlayerADP rows for the season -- the list is re-averaged
as each league drafts -- unless the site has no draft yet for the season (an empty list), when
last run's rows are left alone.
"""

import logging

from nhl_pipeline import db, name_resolver
from nhl_pipeline.api import oldtimehockey
from nhl_pipeline.ingest.fantasy_fleaflicker import IDS_TABLE

log = logging.getLogger("ingest.fantasy_oldtimehockey")

PLATFORM_NAME = "OldTimeHockey"
ALIAS_TABLE = "Fantasy.PlayerNameAliases"
UNRESOLVED_TABLE = "Fantasy.UnresolvedPlayerNames"


def get_or_create_platform(cursor, platform_name: str) -> int:
    return db.upsert_get_id(
        cursor, "Fantasy.Platforms", "FantasyPlatformID",
        {"PlatformName": platform_name}, None,
    )


def fleaflicker_ids(cursor, season_id: int) -> dict:
    cursor.execute(f"""
        SELECT i.ExternalPlayerID, i.PlayerID
        FROM {IDS_TABLE} i
        JOIN Fantasy.Platforms p ON p.FantasyPlatformID = i.FantasyPlatformID
        WHERE p.PlatformName = 'Fleaflicker' AND i.SeasonID = ?
    """, season_id)
    return {str(external): int(player) for external, player in cursor.fetchall()}


def sync_oldtimehockey(cursor, season_id: int, season_start_year: int) -> dict:
    platform_id = get_or_create_platform(cursor, PLATFORM_NAME)
    rows = oldtimehockey.get_adp(season_start_year)
    if not rows:
        log.warning("Old Time Hockey: no drafts yet for %d-%s -- PlayerADP left as it was",
                    season_start_year, str(season_start_year + 1)[2:])
        return {"adp": 0, "drafts": 0}

    by_fleaflicker_id = fleaflicker_ids(cursor, season_id)
    if not by_fleaflicker_id:
        log.warning("no Fleaflicker ids for this season (run import_fantasy_fleaflicker.py "
                    "first); matching Old Time Hockey players by name alone")
    player_index = name_resolver.load_player_index(cursor)
    alias_map = name_resolver.load_alias_map(cursor, ALIAS_TABLE, platform_id)

    db.delete_where(cursor, "Fantasy.PlayerADP", {"FantasyPlatformID": platform_id, "SeasonID": season_id})

    counts = {"adp": 0, "by_id": 0, "by_name": 0, "unresolved": 0, "not_in_db": 0,
              "drafts": max(int(r.get("TimesDrafted") or 0) for r in rows)}
    seen = set()
    for raw in rows:
        player_id = by_fleaflicker_id.get(str(raw.get("PlayerId")))
        if player_id is not None:
            counts["by_id"] += 1
        else:
            name = (raw.get("PlayerName") or "").strip()
            if not name:
                continue
            if not name_resolver.has_known_name(name, alias_map, player_index):
                counts["not_in_db"] += 1
                continue
            positions = [p for p in str(raw.get("PlayerPositions") or "").replace(",", "/").split("/") if p]
            player_id = name_resolver.resolve_player_id(
                cursor, ALIAS_TABLE, UNRESOLVED_TABLE, platform_id, name, alias_map, player_index,
                position_codes=positions or None,
            )
            if player_id is None:
                counts["unresolved"] += 1
                continue
            counts["by_name"] += 1
        if player_id in seen:
            continue
        seen.add(player_id)
        db.upsert(
            cursor, "Fantasy.PlayerADP",
            {"FantasyPlatformID": platform_id, "PlayerID": player_id, "SeasonID": season_id},
            {"ADP": round(float(raw["ADP"]), 2)},
        )
        counts["adp"] += 1

    log.info(
        "Old Time Hockey %d-%s: %d draft(s); %d ADP row(s) (%d by Fleaflicker id, %d by name), "
        "%d unresolved name(s), %d skipped (not in our database)",
        season_start_year, str(season_start_year + 1)[2:], counts["drafts"], counts["adp"],
        counts["by_id"], counts["by_name"], counts["unresolved"], counts["not_in_db"],
    )
    return counts

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

Same not-yet-live-season guard as fantasy_espn.py: if the whole fetched pool has fewer than 2
distinct ADP values, none of it is written (positions still are). ESPN is the platform this
was actually observed on (querying the wrong season year made every player come back with the
same placeholder), but there's no reason to assume Yahoo's pub-api-ro couldn't do the same
before its own draft_analysis data goes live, and the risk is worse now that a bad run would
also delete every previously-good ADP row instead of just leaving them unchanged (see below).

Every run fully replaces this platform's PlayerPositions rows for the season, and PlayerADP
too when ADP is live, rather than only upserting: a player whose real eligibility/ADP changed
since the last run, or who dropped out of the pool entirely, must not keep a stale row forever
(see fantasy_espn.py's docstring for how this bit ESPN in practice).
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
    distinct_adp = {f["average_draft_position"] for f in fields if f["average_draft_position"]}
    adp_is_live = len(distinct_adp) > 1
    if not adp_is_live:
        log.warning(
            "Yahoo ADP looks like a not-yet-live placeholder (only %d distinct value(s) across "
            "the whole pool) -- skipping PlayerADP this run, positions still imported", len(distinct_adp),
        )

    db.delete_where(cursor, "Fantasy.PlayerPositions", {"FantasyPlatformID": platform_id, "SeasonID": season_id})
    if adp_is_live:
        # Left alone entirely when not live -- clearing this out on a placeholder-looking run
        # would delete last run's real ADP for nothing gained.
        db.delete_where(cursor, "Fantasy.PlayerADP", {"FantasyPlatformID": platform_id, "SeasonID": season_id})

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

        if adp_is_live and f["average_draft_position"] is not None:
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

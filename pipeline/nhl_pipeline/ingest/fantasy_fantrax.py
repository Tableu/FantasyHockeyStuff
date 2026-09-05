"""Fantasy.PlayerADP / Fantasy.PlayerPositions -- Fantrax. Pulled live from Fantrax's public
REST API (fxea/general/*, documented at fantrax.com/developer) via nhl_pipeline.api.fantrax,
rather than the manually-exported workbook this used to read with openpyxl.

No single Fantrax endpoint has name + full position eligibility + ADP together, so three
calls are joined by Fantrax player id (see api/fantrax.py's docstring for why each is
needed): getPlayerIds for the name, getLeagueInfo for real multi-position eligibility,
getAdp for ADP. FANTRAX_LEAGUE_ID is a league created purely to read via getLeagueInfo --
same idea as FLEAFLICKER_PROXY_LEAGUE_ID in ingest/fantasy_fleaflicker.py, except this one is
a league on this project's own Fantrax account rather than someone else's public league,
since getLeagueInfo needs no userSecretId/ownership check to read.

getPlayerIds is the wider list (8,966 for NHL as of writing) and is what's iterated -- a
player missing from getLeagueInfo's pool (rare, ~280 players) still gets resolved and gets an
ADP row if getAdp has one, just no PlayerPositions rows that run. Same two-tier name
resolution as fantasy_espn.py/fantasy_yahoo.py (Fantasy.PlayerNameAliases/
UnresolvedPlayerNames), with two additions:

- name_resolver.has_known_name() is checked first and a name with zero match candidates is
  skipped outright, without ever touching UnresolvedPlayerNames -- per user instruction,
  prospects/depth players who were never going to be in Reference.Players aren't worth a
  manual-review row (confirmed live: 6,805 of 6,809 Fantrax unresolved-name rows had zero
  candidates; only 4 were a real ambiguous match).
- Each record's own `position` (getPlayerIds' single default position) is passed as
  resolve_player_id's position_codes tiebreaker, so a name matching more than one real
  player resolves automatically when only one candidate's real PositionCode matches (e.g.
  the two real "Elias Pettersson"s, one a C one a D -- Fantrax's own pool carries both as
  separate records, each correctly tagged). Not every ambiguous name has a position that
  disambiguates it (e.g. two real "Matt Murray"s, both goalies) -- those still fall through
  to UnresolvedPlayerNames for manual review, same as before.

Same not-yet-live-season guard as fantasy_espn.py/fantasy_yahoo.py: if the whole fetched ADP
set has fewer than 2 distinct values, none of it is written (positions still are), since a
real draft population is never that uniform.

Every run fully replaces this platform's PlayerPositions rows for the season, and PlayerADP
too when ADP is live, rather than only upserting: a player whose real eligibility/ADP changed
since the last run, or who dropped out of the pool entirely, must not keep a stale row forever
(see fantasy_espn.py's docstring for how this bit ESPN in practice).
"""

import logging

from nhl_pipeline import db, name_resolver
from nhl_pipeline.api import fantrax, field_map

log = logging.getLogger("ingest.fantasy_fantrax")

ALIAS_TABLE = "Fantasy.PlayerNameAliases"
UNRESOLVED_TABLE = "Fantasy.UnresolvedPlayerNames"

FANTRAX_LEAGUE_ID = "9ag6lqydmtop2ffo"


def get_or_create_platform(cursor, platform_name: str) -> int:
    return db.upsert_get_id(
        cursor, "Fantasy.Platforms", "FantasyPlatformID",
        {"PlatformName": platform_name}, None,
    )


def sync_fantrax(cursor, season_id: int, league_id: str = FANTRAX_LEAGUE_ID) -> dict:
    platform_id = get_or_create_platform(cursor, "Fantrax")

    player_index = name_resolver.load_player_index(cursor)
    alias_map = name_resolver.load_alias_map(cursor, ALIAS_TABLE, platform_id)

    player_ids = fantrax.get_player_ids()
    league_positions = fantrax.get_league_positions(league_id)
    adp_by_id = {p["id"]: p["ADP"] for p in fantrax.get_adp()}

    distinct_adp = set(adp_by_id.values())
    adp_is_live = len(distinct_adp) > 1
    if not adp_is_live:
        log.warning(
            "Fantrax ADP looks like a not-yet-live placeholder (only %d distinct value(s) across "
            "the whole pool) -- skipping PlayerADP this run, positions still imported", len(distinct_adp),
        )

    db.delete_where(cursor, "Fantasy.PlayerPositions", {"FantasyPlatformID": platform_id, "SeasonID": season_id})
    if adp_is_live:
        # Left alone entirely when not live -- clearing this out on a placeholder-looking run
        # would delete last run's real ADP for nothing gained.
        db.delete_where(cursor, "Fantasy.PlayerADP", {"FantasyPlatformID": platform_id, "SeasonID": season_id})

    counts = {"adp": 0, "positions": 0, "unresolved": 0, "not_in_db": 0}
    for fantrax_id, raw in player_ids.items():
        full_name = field_map.fantrax_name_fields(raw)["full_name"]
        if not full_name:
            continue

        if not name_resolver.has_known_name(full_name, alias_map, player_index):
            counts["not_in_db"] += 1
            continue

        position_hint = [raw["position"]] if raw.get("position") else None
        player_id = name_resolver.resolve_player_id(
            cursor, ALIAS_TABLE, UNRESOLVED_TABLE, platform_id, full_name, alias_map, player_index,
            position_codes=position_hint,
        )
        if player_id is None:
            counts["unresolved"] += 1
            continue

        average_draft_position = adp_by_id.get(fantrax_id)
        if adp_is_live and average_draft_position is not None:
            db.upsert(
                cursor, "Fantasy.PlayerADP",
                {"FantasyPlatformID": platform_id, "PlayerID": player_id, "SeasonID": season_id},
                {"ADP": round(average_draft_position, 2)},
            )
            counts["adp"] += 1

        eligible_pos = (league_positions.get(fantrax_id) or {}).get("eligiblePos")
        for position_code in field_map.fantrax_position_codes(eligible_pos):
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
        "Fantrax: %d ADP row(s), %d position row(s), %d unresolved name(s), "
        "%d skipped (no match, not in our database)",
        counts["adp"], counts["positions"], counts["unresolved"], counts["not_in_db"],
    )
    return counts

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
# The league whose positions the draft and lineups are played under is the registry's active
# Fleaflicker league (nhl_pipeline.fantasy_leagues; today 12090, the user's), passed in by the caller.
IDS_TABLE = "Fantasy.PlatformPlayerIDs"

# Spellings with no exact Reference.Players name, each checked by hand (2026-09-24).
CONFIRMED_ALIASES = {"Freddy Gaudreau": 438, "Joseph Veleno": 19, "Mike Hoffman": 3786,
                     "Samuel Blais": 7}
# Names two players share, settled by Fleaflicker's own id: Matt Murray 2774 is the 1994 goalie
# (a free agent), 9041 the 1998 goalie listed at Nashville.
CONFIRMED_IDS = {2774: 806, 9041: 1005}
_IDS_DDL = """CREATE TABLE Fantasy.PlatformPlayerIDs
(
    FantasyPlatformID   INT NOT NULL,
    SeasonID            INT NOT NULL,
    ExternalPlayerID    VARCHAR(50) NOT NULL,
    PlayerID            BIGINT NOT NULL,
    UpdatedAt           DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_FantasyPlatformPlayerIDs PRIMARY KEY (FantasyPlatformID, SeasonID, ExternalPlayerID),
    CONSTRAINT FK_FPPI_Platform FOREIGN KEY (FantasyPlatformID)
        REFERENCES Fantasy.Platforms(FantasyPlatformID),
    CONSTRAINT FK_FPPI_Season FOREIGN KEY (SeasonID)
        REFERENCES Reference.Seasons(SeasonID),
    CONSTRAINT FK_FPPI_Player FOREIGN KEY (PlayerID)
        REFERENCES Reference.Players(PlayerID)
);"""


def get_or_create_platform(cursor, platform_name: str) -> int:
    return db.upsert_get_id(
        cursor, "Fantasy.Platforms", "FantasyPlatformID",
        {"PlatformName": platform_name}, None,
    )


def ensure_ids_table(cursor) -> None:
    """Create Fantasy.PlatformPlayerIDs if this database predates it (nhl_database_schema.sql)."""
    cursor.execute("IF OBJECT_ID('Fantasy.PlatformPlayerIDs', 'U') IS NULL EXEC('" +
                   ' '.join(line.strip() for line in _IDS_DDL.splitlines()).replace("'", "''") +
                   "')")


def sync_fleaflicker(cursor, season_id: int, league_id: int) -> dict:
    platform_id = get_or_create_platform(cursor, "Fleaflicker")

    for raw, player_id in CONFIRMED_ALIASES.items():
        db.upsert(cursor, ALIAS_TABLE, {"SourceID": platform_id, "RawName": raw}, {"PlayerID": player_id})
        cursor.execute(f"DELETE FROM {UNRESOLVED_TABLE} WHERE SourceID = ? AND RawName = ?", platform_id, raw)
    player_index = name_resolver.load_player_index(cursor)
    alias_map = name_resolver.load_alias_map(cursor, ALIAS_TABLE, platform_id)
    raw_players = fleaflicker.get_players(league_id)

    db.delete_where(cursor, "Fantasy.PlayerPositions", {"FantasyPlatformID": platform_id, "SeasonID": season_id})
    ensure_ids_table(cursor)
    db.delete_where(cursor, IDS_TABLE, {"FantasyPlatformID": platform_id, "SeasonID": season_id})

    counts = {"positions": 0, "unresolved": 0, "ids": 0}
    for raw in raw_players:
        f = field_map.fleaflicker_player_fields(raw)
        if not f["full_name"]:
            continue

        player_id = CONFIRMED_IDS.get(f.get("fleaflicker_player_id"))
        if player_id is None:
            player_id = name_resolver.resolve_player_id(
                cursor, ALIAS_TABLE, UNRESOLVED_TABLE, platform_id, f["full_name"], alias_map,
                player_index, position_codes=f["position_codes"],
            )
        if player_id is None:
            counts["unresolved"] += 1
            continue

        if f.get("fleaflicker_player_id") is not None:
            db.upsert(cursor, IDS_TABLE,
                      {"FantasyPlatformID": platform_id, "SeasonID": season_id,
                       "ExternalPlayerID": str(f["fleaflicker_player_id"])},
                      {"PlayerID": player_id})
            counts["ids"] += 1

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
        "Fleaflicker league %d: %d player(s) listed, %d position row(s), %d id(s), %d unresolved name(s)",
        league_id, len(raw_players), counts["positions"], counts["ids"], counts["unresolved"],
    )
    return counts

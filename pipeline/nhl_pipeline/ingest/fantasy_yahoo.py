"""Fantasy.PlayerADP / Fantasy.PlayerPositions -- Yahoo. Unlike ESPN (a live public API),
there's no public Yahoo fantasy-hockey endpoint without OAuth app registration, so this reads
the same 'dailyfaceoff yahoo.csv' export the workbook used to read directly (Player, Team,
ADP, Site Pos columns -- Site Pos is comma-separated multi-position eligibility, e.g. "C,LW",
one Fantasy.PlayerPositions row per code). Same two-tier name resolution as fantasy_espn.py
(Fantasy.PlayerNameAliases/UnresolvedPlayerNames), since this sheet's raw names don't always
match Reference.Players' spelling either.

A player with no ADP value in the sheet is skipped for PlayerADP but still gets its
PlayerPositions rows -- position eligibility and ADP are independent facts (same convention
as fantasy_espn.py).
"""

import csv
import logging
from pathlib import Path

from nhl_pipeline import config, db, name_resolver, team_resolver

log = logging.getLogger("ingest.fantasy_yahoo")

ALIAS_TABLE = "Fantasy.PlayerNameAliases"
UNRESOLVED_TABLE = "Fantasy.UnresolvedPlayerNames"
FILENAME = "dailyfaceoff yahoo.csv"


def get_or_create_platform(cursor, platform_name: str) -> int:
    return db.upsert_get_id(
        cursor, "Fantasy.Platforms", "FantasyPlatformID",
        {"PlatformName": platform_name}, None,
    )


def _to_float(value):
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None  # e.g. Yahoo's " - " placeholder for a player with no ADP yet


def _rows(sheets_dir: Path):
    with open(sheets_dir / FILENAME, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            yield {
                "raw_name": r["Player"],
                "team_raw": r["Team"],
                "adp": _to_float(r["ADP"]),
                "position_codes": [p.strip() for p in r["Site Pos"].split(",") if p.strip()],
            }


def sync_yahoo(cursor, season_id: int, sheets_dir: Path = None) -> dict:
    sheets_dir = sheets_dir or (config.PROJECT_ROOT / "Sheets")
    platform_id = get_or_create_platform(cursor, "Yahoo")

    player_index = name_resolver.load_player_index(cursor)
    alias_map = name_resolver.load_alias_map(cursor, ALIAS_TABLE, platform_id)
    team_index, team_name_pairs = team_resolver.load_team_index(cursor)

    counts = {"adp": 0, "positions": 0, "unresolved": 0}
    for r in _rows(sheets_dir):
        player_id = name_resolver.resolve_player_id(
            cursor, ALIAS_TABLE, UNRESOLVED_TABLE, platform_id, r["raw_name"], alias_map, player_index,
        )
        if player_id is None:
            counts["unresolved"] += 1
            continue

        if r["adp"] is not None:
            db.upsert(
                cursor, "Fantasy.PlayerADP",
                {"FantasyPlatformID": platform_id, "PlayerID": player_id, "SeasonID": season_id},
                {"ADP": round(r["adp"], 2)},
            )
            counts["adp"] += 1

        for position_code in r["position_codes"]:
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

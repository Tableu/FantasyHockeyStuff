"""Fantasy.PlayerADP / Fantasy.PlayerPositions -- Fantrax. Reads the same doms Fantrax
workbook ('The List' sheet) that nhl_pipeline.projections.sources.fantrax reads for stat
projections -- read positionally there for the same reason (duplicate 'GP' header), so this
does too: name=col B (index 1), POS=col D (index 3, comma-separated multi-position
eligibility e.g. "C,LW"), TEAM=col F (index 5), ADP=col M (index 12).

Same two-tier name resolution as fantasy_espn.py/fantasy_yahoo.py (Fantasy.PlayerNameAliases/
UnresolvedPlayerNames). A player with no ADP value is skipped for PlayerADP but still gets
its PlayerPositions rows.
"""

import logging
from pathlib import Path

import openpyxl

from nhl_pipeline import config, db, name_resolver, team_resolver

log = logging.getLogger("ingest.fantasy_fantrax")

ALIAS_TABLE = "Fantasy.PlayerNameAliases"
UNRESOLVED_TABLE = "Fantasy.UnresolvedPlayerNames"
FILENAME = "doms 2026-27-Fantasy-Projections-Fantrax.xlsx"
SHEET = "The List"


def get_or_create_platform(cursor, platform_name: str) -> int:
    return db.upsert_get_id(
        cursor, "Fantasy.Platforms", "FantasyPlatformID",
        {"PlatformName": platform_name}, None,
    )


def _to_float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _rows(sheets_dir: Path):
    wb = openpyxl.load_workbook(sheets_dir / FILENAME, data_only=True)
    ws = wb[SHEET]
    for r in ws.iter_rows(min_row=2, values_only=True):
        name = r[1]
        if not name:
            continue
        pos = r[3]
        yield {
            "raw_name": name,
            "team_raw": r[5],
            "adp": _to_float(r[12]),
            "position_codes": [p.strip() for p in str(pos).split(",") if p.strip()] if pos else [],
        }


def sync_fantrax(cursor, season_id: int, sheets_dir: Path = None) -> dict:
    sheets_dir = sheets_dir or (config.PROJECT_ROOT / "Sheets")
    platform_id = get_or_create_platform(cursor, "Fantrax")

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
        "Fantrax: %d ADP row(s), %d position row(s), %d unresolved name(s)",
        counts["adp"], counts["positions"], counts["unresolved"],
    )
    return counts

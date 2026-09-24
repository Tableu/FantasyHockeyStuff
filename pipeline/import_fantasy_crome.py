#!/usr/bin/env python
"""2025-26 platform data from the Crome workbook: Fantasy.PlayerPositions (Yahoo, Fantrax, ESPN,
Fleaflicker) and Fantasy.PlayerADP (Yahoo, Fantrax) for SeasonID 2025-26 only.

    python import_fantasy_crome.py             # write
    python import_fantasy_crome.py --dry-run   # the same run, rolled back

The live platform importers (import_fantasy_yahoo.py and friends) write the current season from
the platforms' own APIs; those APIs no longer serve 2025-26, so its snapshot comes from the
workbook, which captured it before that season (ADP as of 12 Sep 2025 for Yahoo, 4 Sep for
Fantrax). Like them, each run replaces its platform's rows for the season -- and only for this
season: every delete and upsert is keyed by 2025-26's SeasonID, and the script refuses to run
against any other, so the 2026-27 rows cannot be touched.

Names go through the platforms' shared alias table (Fantasy.PlayerNameAliases), raw name first,
then Crome's fixed spelling, then a position tiebreak; the rest are queued in
Fantasy.UnresolvedPlayerNames, as the live importers do.
"""

import argparse
import logging

from nhl_pipeline import config, db, name_resolver
from nhl_pipeline.ingest.season import ensure_season
from nhl_pipeline.projections.sources import crome_workbook

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_fantasy_crome")

SEASON_CFG = {"SeasonID_NHL": 20252026, "DisplayName": "2025-26"}
ALIAS_TABLE = "Fantasy.PlayerNameAliases"
UNRESOLVED_TABLE = "Fantasy.UnresolvedPlayerNames"


def resolve(cursor, platform_id, raw, fixed, codes, alias_map, player_index):
    """Raw name, then Crome's fixed name (saved as an alias for the raw one), then the resolver's
    own path with its position tiebreak and unresolved queue. Returns (PlayerID or None, how)."""
    if raw not in alias_map and len(name_resolver.candidates(raw, player_index)) != 1:
        if fixed and fixed != raw and len(found := name_resolver.candidates(fixed, player_index)) == 1:
            player_id = next(iter(found))
            db.upsert(cursor, ALIAS_TABLE, {"SourceID": platform_id, "RawName": raw}, {"PlayerID": player_id})
            cursor.execute(f"DELETE FROM {UNRESOLVED_TABLE} WHERE SourceID = ? AND RawName = ?", platform_id, raw)
            alias_map[raw] = player_id
            return player_id, "fixed"
    player_id = name_resolver.resolve_player_id(
        cursor, ALIAS_TABLE, UNRESOLVED_TABLE, platform_id, raw, alias_map, player_index,
        position_codes=codes or None)
    return player_id, ("raw" if player_id else "unresolved")


def main():
    parser = argparse.ArgumentParser(description="Import 2025-26 positions and ADP from the Crome workbook")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = db.connect()
    cursor = conn.cursor()
    season_id = ensure_season(cursor, SEASON_CFG)
    cursor.execute("SELECT DisplayName FROM Reference.Seasons WHERE SeasonID = ?", season_id)
    if cursor.fetchone()[0] != "2025-26":
        raise SystemExit(f"SeasonID {season_id} is not 2025-26; refusing to write")

    workbook = crome_workbook.open_workbook(config.PROJECT_ROOT / "ProjectionSheets" / "2025-26")
    player_index = name_resolver.load_player_index(cursor)
    platforms = {name: db.upsert_get_id(cursor, "Fantasy.Platforms", "FantasyPlatformID",
                                        {"PlatformName": name}, None)
                 for name in crome_workbook.PLATFORMS}
    for pid in platforms.values():
        for raw, player_id in crome_workbook.CONFIRMED_ALIASES.items():
            db.upsert(cursor, ALIAS_TABLE, {"SourceID": pid, "RawName": raw}, {"PlayerID": player_id})
            cursor.execute(f"DELETE FROM {UNRESOLVED_TABLE} WHERE SourceID = ? AND RawName = ?", pid, raw)
    aliases = {name: name_resolver.load_alias_map(cursor, ALIAS_TABLE, pid) for name, pid in platforms.items()}
    # Names two players share, settled for this workbook by team: in memory for this run only, so
    # nothing ambiguous reaches the shared alias table (see crome_workbook.PLATFORM_RUN_ALIASES).
    for alias_map in aliases.values():
        for raw, player_id in crome_workbook.PLATFORM_RUN_ALIASES.items():
            alias_map.setdefault(raw, player_id)

    report = {}
    # Positions: replace each platform's 2025-26 rows, and nothing else.
    for name, pid in platforms.items():
        db.delete_where(cursor, "Fantasy.PlayerPositions", {"FantasyPlatformID": pid, "SeasonID": season_id})
    for platform, player, team, codes in crome_workbook.positions(workbook):
        pid = platforms[platform]
        player_id, how = resolve(cursor, pid, player, None, codes, aliases[platform], player_index)
        entry = report.setdefault((platform, "positions"), {"players": 0, "rows": 0, "unresolved": []})
        if player_id is None:
            entry["unresolved"].append(player)
            continue
        entry["players"] += 1
        for code in codes:
            db.upsert(cursor, "Fantasy.PlayerPositions",
                      {"FantasyPlatformID": pid, "PlayerID": player_id, "SeasonID": season_id,
                       "PositionCode": code}, None)
            entry["rows"] += 1

    # ADP: the same, for the two platforms the workbook snapshots.
    for platform in ("Yahoo", "Fantrax"):
        pid = platforms[platform]
        as_of, rows = crome_workbook.adp(workbook, platform)
        db.delete_where(cursor, "Fantasy.PlayerADP", {"FantasyPlatformID": pid, "SeasonID": season_id})
        entry = report.setdefault((platform, "adp"), {"players": 0, "rows": 0, "unresolved": [], "as_of": as_of})
        for raw, fixed, team, codes, value in rows:
            player_id, how = resolve(cursor, pid, raw, fixed, codes, aliases[platform], player_index)
            if player_id is None:
                entry["unresolved"].append(raw)
                continue
            db.upsert(cursor, "Fantasy.PlayerADP",
                      {"FantasyPlatformID": pid, "PlayerID": player_id, "SeasonID": season_id},
                      {"ADP": round(value, 2), "UpdatedAt": as_of})
            entry["players"] += 1
            entry["rows"] += 1

    for (platform, kind), e in sorted(report.items()):
        extra = f" as of {e['as_of']}" if e.get("as_of") else ""
        print(f"{platform:12s} {kind:9s} {e['players']:5d} players, {e['rows']:5d} rows, "
              f"{len(e['unresolved']):3d} unresolved{extra}"
              + (f"\n    unresolved: {', '.join(e['unresolved'][:20])}" if e["unresolved"] else ""))
    if args.dry_run:
        conn.rollback()
        log.info("Dry run: rolled back, nothing written.")
    else:
        conn.commit()
        log.info("Committed 2025-26 positions and ADP.")


if __name__ == "__main__":
    main()

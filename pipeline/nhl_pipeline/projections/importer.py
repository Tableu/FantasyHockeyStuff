"""General engine for loading a fantasy-projection sheet into Projections.SkaterProjections /
Projections.GoalieProjections. Each source module under projections/sources/ parses its own
file format (CSV columns, or an xlsx sheet read positionally) and yields plain dicts; this
module does the shared work every source needs regardless of format: get-or-create the
Projections.Sources row, resolve each row's raw player name to a PlayerID (name_resolver),
resolve its team string to a TeamID (team_resolver), and upsert into the right target table.
"""

import logging

from nhl_pipeline import db, name_resolver, team_resolver

log = logging.getLogger("projections.importer")

ALIAS_TABLE = "Projections.PlayerNameAliases"
UNRESOLVED_TABLE = "Projections.UnresolvedPlayerNames"


def get_or_create_source(cursor, source_name: str, season_id: int, description: str | None = None) -> int:
    return db.upsert_get_id(
        cursor, "Projections.Sources", "SourceID",
        {"SourceName": source_name, "SeasonID": season_id},
        {"Description": description},
    )


def import_rows(cursor, source_name: str, season_id: int, rows, description: str | None = None) -> dict:
    """rows: iterable of {"raw_name": str, "team_raw": str | None, "is_goalie": bool,
    "stats": {target_column: value, ...}}."""
    source_id = get_or_create_source(cursor, source_name, season_id, description)

    player_index = name_resolver.load_player_index(cursor)
    alias_map = name_resolver.load_alias_map(cursor, ALIAS_TABLE, source_id)
    team_index, team_name_pairs = team_resolver.load_team_index(cursor)

    counts = {"skaters": 0, "goalies": 0, "unresolved": 0}
    for row in rows:
        player_id = name_resolver.resolve_player_id(
            cursor, ALIAS_TABLE, UNRESOLVED_TABLE, source_id, row["raw_name"], alias_map, player_index,
        )
        if player_id is None:
            counts["unresolved"] += 1
            continue

        team_id = team_resolver.resolve_team_id(row.get("team_raw"), team_index, team_name_pairs)
        table = "Projections.GoalieProjections" if row["is_goalie"] else "Projections.SkaterProjections"
        db.upsert(
            cursor, table,
            {"SourceID": source_id, "PlayerID": player_id},
            {"TeamID": team_id, **row["stats"]},
        )
        counts["goalies" if row["is_goalie"] else "skaters"] += 1

    log.info(
        "%s: %d skater(s), %d goalie(s), %d unresolved name(s)",
        source_name, counts["skaters"], counts["goalies"], counts["unresolved"],
    )
    return counts


def seed_aliases(cursor, source_id: int, source_name: str, from_season_id: int, player_index: dict) -> int:
    """Copy the same-named source's aliases from another season onto this one -- a source spells
    a player the same way from one year to the next. Existing aliases here are left alone, and a
    name two real players share is never copied: which of them the source meant last season says
    nothing about this one."""
    cursor.execute(
        f"""SELECT a.RawName, a.PlayerID FROM {ALIAS_TABLE} a
            JOIN Projections.Sources s ON s.SourceID = a.SourceID
            WHERE s.SourceName = ? AND s.SeasonID = ?
              AND NOT EXISTS (SELECT 1 FROM {ALIAS_TABLE} b WHERE b.SourceID = ? AND b.RawName = a.RawName)""",
        source_name, from_season_id, source_id,
    )
    copied = 0
    for raw, player_id in cursor.fetchall():
        if len(name_resolver.candidates(raw, player_index)) > 1:
            continue
        db.upsert(cursor, ALIAS_TABLE, {"SourceID": source_id, "RawName": raw}, {"PlayerID": player_id})
        copied += 1
    return copied


def import_workbook_rows(cursor, source_name: str, season_id: int, rows, description: str | None = None,
                         published_on=None, seed_from_season_id: int | None = None,
                         confirmed_aliases: dict | None = None) -> dict:
    """Like import_rows, for a sheet that also carries a second spelling of each name -- the
    Crome workbook's column A, a name someone already matched by hand. Resolution, first hit wins:
    the source's raw name (alias, then a unique match); then the fixed name, a unique match of
    which is saved as an alias for the raw one so the fix persists; then the raw name's position
    tiebreak; otherwise it is queued in UnresolvedPlayerNames, as always. Nothing is aliased on a
    guess: a name matching more than one player stays unresolved."""
    source_id = get_or_create_source(cursor, source_name, season_id, description)
    if published_on is not None:
        cursor.execute("UPDATE Projections.Sources SET PublishedOn = ? WHERE SourceID = ?",
                       published_on, source_id)
    player_index = name_resolver.load_player_index(cursor)
    seeded = (seed_aliases(cursor, source_id, source_name, seed_from_season_id, player_index)
              if seed_from_season_id is not None else 0)

    for raw, player_id in (confirmed_aliases or {}).items():
        db.upsert(cursor, ALIAS_TABLE, {"SourceID": source_id, "RawName": raw}, {"PlayerID": player_id})
        cursor.execute(f"DELETE FROM {UNRESOLVED_TABLE} WHERE SourceID = ? AND RawName = ?", source_id, raw)
    player_index = name_resolver.load_player_index(cursor)
    alias_map = name_resolver.load_alias_map(cursor, ALIAS_TABLE, source_id)
    team_index, team_name_pairs = team_resolver.load_team_index(cursor)

    counts = {"skaters": 0, "goalies": 0, "by_raw": 0, "by_crome": 0, "unresolved": 0,
              "no_team": 0, "seeded_aliases": seeded, "unresolved_names": []}
    for row in rows:
        raw = row["raw_name"]
        player_id = None
        if raw not in alias_map and len(name_resolver.candidates(raw, player_index)) != 1:
            fixed = row.get("crome_name")
            if fixed and fixed != raw and len(found := name_resolver.candidates(fixed, player_index)) == 1:
                player_id = next(iter(found))
                db.upsert(cursor, ALIAS_TABLE, {"SourceID": source_id, "RawName": raw},
                          {"PlayerID": player_id})
                cursor.execute(f"DELETE FROM {UNRESOLVED_TABLE} WHERE SourceID = ? AND RawName = ?",
                               source_id, raw)
                alias_map[raw] = player_id
                counts["by_crome"] += 1
        if player_id is None:
            player_id = name_resolver.resolve_player_id(
                cursor, ALIAS_TABLE, UNRESOLVED_TABLE, source_id, raw, alias_map, player_index,
                position_codes=row.get("position_codes") or None,
            )
            if player_id is None:
                counts["unresolved"] += 1
                counts["unresolved_names"].append(raw)
                continue
            counts["by_raw"] += 1

        team_id = team_resolver.resolve_team_id(row.get("team_raw"), team_index, team_name_pairs)
        if team_id is None and row.get("team_raw"):
            counts["no_team"] += 1
        table = "Projections.GoalieProjections" if row["is_goalie"] else "Projections.SkaterProjections"
        db.upsert(cursor, table, {"SourceID": source_id, "PlayerID": player_id},
                  {"TeamID": team_id, **row["stats"]})
        counts["goalies" if row["is_goalie"] else "skaters"] += 1

    log.info("%s: %d skater(s), %d goalie(s); resolved %d by raw name, %d by Crome's name; "
             "%d unresolved; %d seeded alias(es)", source_name, counts["skaters"], counts["goalies"],
             counts["by_raw"], counts["by_crome"], counts["unresolved"], seeded)
    return counts

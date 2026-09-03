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

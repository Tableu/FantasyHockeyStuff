"""Reference.Players -- backfills a player who has a real, permanent NHL player ID but has
never appeared in any ingested game's rosterSpots (so ingest.teams_players.sync_players has
never seen them), e.g. a veteran sidelined for an entire tracked season (Alex Pietrangelo
missed all of 2025-26 and so was invisible to every fantasy source's name resolution even
though he's a rostered NHL player). Same "has an ID, hasn't dressed yet" gap ingest.draft.py
already solves for draft picks, resolved the same way: api.player_search, matched by exact
name. Using the real ID means that if this player later does dress for an ingested game,
teams_players.sync_players upserts onto this same row instead of creating a duplicate.

Driven off a source's own unresolved-name queue's zero-candidate rows (CandidatePlayerIDs
IS NULL) rather than blind guessing -- a name with multiple local candidates already has
Reference.Players rows to disambiguate between and needs the existing human review, not this.

After backfilling, the newly-added players still won't show up in Fantasy.PlayerADP/
PlayerPositions until that source's import is re-run -- this only populates Reference.Players.
"""

import logging

from nhl_pipeline import db
from nhl_pipeline.api import player_search

log = logging.getLogger("ingest.player_backfill")


def backfill_unresolved_names(cursor, unresolved_table: str) -> dict:
    """A name matched in the NHL search is added to Reference.Players under its NHL id -- unless
    that id is already there under another spelling (Anton Silaev for Silayev, Joseph for Joe
    Veleno): then the name becomes that source's alias for the existing player, and its
    unresolved row goes. The existing row is never renamed after one source's spelling."""
    alias_table = unresolved_table.replace("UnresolvedPlayerNames", "PlayerNameAliases")
    cursor.execute(f"SELECT DISTINCT RawName, SourceID FROM {unresolved_table} WHERE CandidatePlayerIDs IS NULL")
    # A blank RawName (one projection row has none) is skipped: the search refuses an empty q.
    rows = [(row.RawName, row.SourceID) for row in cursor.fetchall() if (row.RawName or "").strip()]
    cursor.execute("SELECT NHLPlayerID, PlayerID FROM Reference.Players WHERE NHLPlayerID IS NOT NULL")
    known = {r.NHLPlayerID: r.PlayerID for r in cursor.fetchall()}

    counts = {"added": 0, "aliased": 0, "still_unresolved": 0}
    player_ids = set()      # every player added or aliased, returned as counts["player_ids"]
    matches = {}
    for raw_name, source_id in rows:
        if raw_name not in matches:
            matches[raw_name] = player_search.find_exact_match(raw_name)
        match = matches[raw_name]
        if match is None:
            counts["still_unresolved"] += 1
            continue

        existing = known.get(match["nhl_player_id"])
        if existing is not None:
            db.upsert(cursor, alias_table, {"SourceID": source_id, "RawName": raw_name},
                      {"PlayerID": existing})
            player_ids.add(existing)
            cursor.execute(f"DELETE FROM {unresolved_table} WHERE SourceID = ? AND RawName = ?",
                           source_id, raw_name)
            counts["aliased"] += 1
            continue

        # FirstName/LastName left unset (unlike ingest.draft/ingest.teams_players, which both
        # have a real first/last split to write) -- the NHL search result only ever gives a
        # combined "name" string, and splitting it ourselves would mangle any multi-word last
        # name, so this is a deliberate gap, not an oversight.
        known[match["nhl_player_id"]] = db.upsert_get_id(
            cursor, "Reference.Players", "PlayerID",
            {"NHLPlayerID": match["nhl_player_id"]},
            {
                "FullName": raw_name,
                "PositionCode": match["position_code"],
                "HeightInches": match["height_inches"],
                "WeightLbs": match["weight_lbs"],
            },
        )
        player_ids.add(known[match["nhl_player_id"]])
        counts["added"] += 1

    log.info(
        "%s: added %d player(s) via NHL search, %d aliased to an existing player, "
        "%d still unresolved (of %d candidate name(s))",
        unresolved_table, counts["added"], counts["aliased"], counts["still_unresolved"], len(rows),
    )
    counts["player_ids"] = player_ids
    return counts

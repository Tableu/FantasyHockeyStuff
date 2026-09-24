"""Fill Reference.Players' bio columns (BirthDate, HeightInches, WeightLbs, Shoots) from the
player landing endpoint, archiving what was read.

The raw payload goes to Ingestion.RawPlayerResponses rather than Ingestion.RawApiResponses,
which is keyed by game -- a bio belongs to no game. Only the bio and draft keys are archived
(field_map.PLAYER_LANDING_KEYS); the rest of the ~30 KB payload is career stats.

Writes are UPDATEs of existing rows and set only fields the payload actually has, so a known
value is never overwritten with null and no player row is ever created here.
"""

import json
import logging
from datetime import datetime, timezone

import requests

from nhl_pipeline import db
from nhl_pipeline.api import field_map, player_landing

log = logging.getLogger("player_bio")

ENDPOINT = "PLAYER_LANDING"


def players_to_fetch(cursor, refetch: bool = False, played_only: bool = False) -> list:
    """(PlayerID, NHLPlayerID) of players with no BirthDate, players who have appeared in an
    ingested game first. Players already fetched are skipped unless `refetch`, so a rerun makes
    no calls for players the endpoint simply has no birth date for."""
    already = "" if refetch else (
        "AND NOT EXISTS (SELECT 1 FROM Ingestion.RawPlayerResponses r "
        f"WHERE r.NHLPlayerID = p.NHLPlayerID AND r.EndpointType = '{ENDPOINT}')")
    cursor.execute(f"""
        SELECT p.PlayerID, p.NHLPlayerID,
               CASE WHEN EXISTS (SELECT 1 FROM Stats.PlayerGameStats s WHERE s.PlayerID = p.PlayerID)
                    THEN 0 ELSE 1 END AS Priority
        FROM Reference.Players p
        WHERE p.NHLPlayerID IS NOT NULL AND p.BirthDate IS NULL {already}
              {"AND EXISTS (SELECT 1 FROM Stats.PlayerGameStats s WHERE s.PlayerID = p.PlayerID)" if played_only else ""}
        ORDER BY Priority, p.PlayerID""")
    return [(r.PlayerID, r.NHLPlayerID) for r in cursor.fetchall()]


def sync_player_bio(cursor, player_id: int, nhl_player_id: int) -> dict | None:
    """Fetch, archive and apply one player's bio. Returns the fields written, or None when the
    endpoint has no such player (404)."""
    try:
        payload = player_landing.get_player_landing(nhl_player_id)
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == 404:
            log.warning("no landing page for NHL player %s (PlayerID %s)", nhl_player_id, player_id)
            return None
        raise
    kept = {k: payload[k] for k in field_map.PLAYER_LANDING_KEYS if k in payload}
    db.upsert(cursor, "Ingestion.RawPlayerResponses",
              {"NHLPlayerID": nhl_player_id, "EndpointType": ENDPOINT},
              {"RawJSON": json.dumps(kept), "RetrievedAt": datetime.now(timezone.utc)})
    fields = field_map.player_bio_fields(kept)
    if fields:
        assignments = ", ".join(f"{column} = ?" for column in fields)
        cursor.execute(f"UPDATE Reference.Players SET {assignments} WHERE PlayerID = ?",
                       *fields.values(), player_id)
    return fields

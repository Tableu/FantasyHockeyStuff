import json
from datetime import datetime, timezone

from nhl_pipeline import db


def save_raw_response(cursor, game_id: int, endpoint_type: str, raw_json: dict) -> None:
    db.upsert(
        cursor, "Ingestion.RawApiResponses",
        {"GameID": game_id, "EndpointType": endpoint_type},
        {"RawJSON": json.dumps(raw_json), "RetrievedAt": datetime.now(timezone.utc)},
    )

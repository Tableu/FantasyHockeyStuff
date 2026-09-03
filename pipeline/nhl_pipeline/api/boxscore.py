"""GET https://api-web.nhle.com/v1/gamecenter/{gameId}/boxscore — verified live."""

from nhl_pipeline.http_client import get_json

BASE = "https://api-web.nhle.com/v1"


def get_boxscore(nhl_game_id: int) -> dict:
    return get_json(f"{BASE}/gamecenter/{nhl_game_id}/boxscore")

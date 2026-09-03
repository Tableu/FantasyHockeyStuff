"""GET https://api-web.nhle.com/v1/gamecenter/{gameId}/play-by-play — verified live."""

from nhl_pipeline.http_client import get_json

BASE = "https://api-web.nhle.com/v1"


def get_play_by_play(nhl_game_id: int) -> dict:
    return get_json(f"{BASE}/gamecenter/{nhl_game_id}/play-by-play")

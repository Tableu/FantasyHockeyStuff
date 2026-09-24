"""GET https://api-web.nhle.com/v1/player/{playerId}/landing -- verified live.

The one NHL endpoint that carries a player's bio: birth date, height, weight, shoots/catches,
plus his draft details. The pipeline had never called it, so Reference.Players.BirthDate was
empty for every player -- which is what blocked an age feature (and an aging curve) downstream.
"""

from nhl_pipeline.http_client import get_json

BASE = "https://api-web.nhle.com/v1"


def get_player_landing(nhl_player_id: int) -> dict:
    return get_json(f"{BASE}/player/{nhl_player_id}/landing")

"""GET https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={id} — verified
live: returns one row per shift interval directly (data[]), no shift-change pairing needed."""

from nhl_pipeline.http_client import get_json

BASE = "https://api.nhle.com/stats/rest/en"


def get_shift_charts(nhl_game_id: int) -> dict:
    return get_json(f"{BASE}/shiftcharts", params={"cayenneExp": f"gameId={nhl_game_id}"})

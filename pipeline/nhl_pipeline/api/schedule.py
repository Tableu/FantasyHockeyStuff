"""GET https://api-web.nhle.com/v1/schedule/{date} — verified live against a real date.

The endpoint always returns a full week regardless of which date in it you ask for, so
get_games_for_date() picks out just the requested day's games.
"""

from nhl_pipeline.http_client import get_json

BASE = "https://api-web.nhle.com/v1"


def get_schedule_week(date_str: str) -> dict:
    return get_json(f"{BASE}/schedule/{date_str}")


def get_games_for_date(date_str: str) -> list:
    data = get_schedule_week(date_str)
    for day in data.get("gameWeek", []):
        if day.get("date") == date_str:
            return day.get("games", [])
    return []

"""GET https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries -- verified live
2026-09-25 (31 teams, 93 players). No key. Answers 403 to browser-spoofed User-Agents, so this
does NOT go through http_client.get_text (which sends one); a plain client UA works.

Per injury: `status` (Out / Day-To-Day / Injured Reserve / Suspension), `date` (of the latest
note), `shortComment`, `details.fantasyStatus.abbreviation` (OUT / Day-To-Day / IR / IR-LT /
IR-NR), `details.type` (body part), `details.returnDate` (ESPN's estimate, sometimes absent),
and the athlete with ESPN's own id (inside the headshot/player-card URLs), position and team.
"""

import re

import requests

URL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries"
_ATHLETE_ID = re.compile(r"/id/(\d+)|/players/full/(\d+)\.png")


def _athlete_id(athlete: dict):
    urls = [link.get("href", "") for link in athlete.get("links", [])]
    urls.append((athlete.get("headshot") or {}).get("href", ""))
    for url in urls:
        match = _ATHLETE_ID.search(url)
        if match:
            return match.group(1) or match.group(2)
    return None


def get_injuries() -> list:
    response = requests.get(URL, headers={"User-Agent": "python-requests"}, timeout=30)
    response.raise_for_status()
    rows = []
    for team in response.json().get("injuries", []):
        for injury in team.get("injuries", []):
            athlete = injury.get("athlete", {})
            details = injury.get("details") or {}
            rows.append({
                "external_id": _athlete_id(athlete),
                "name": athlete.get("displayName"),
                "position": (athlete.get("position") or {}).get("abbreviation"),
                "team_abbreviation": (athlete.get("team") or {}).get("abbreviation"),
                "status": injury.get("status"),
                "fantasy_status": (details.get("fantasyStatus") or {}).get("abbreviation"),
                "body_part": details.get("type"),
                "return_date": details.get("returnDate"),
                "note_date": injury.get("date"),
                "comment": injury.get("shortComment"),
            })
    return rows

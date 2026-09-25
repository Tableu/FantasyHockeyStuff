"""Daily Faceoff's live line charts and starting-goalie reports -- verified live 2026-09-25.

Both pages are Next.js and carry their data as JSON in `<script id="__NEXT_DATA__">`, so one
parser serves both. robots.txt allows these pages and disallows only `/api/`; poll gently.

- Line chart: https://www.dailyfaceoff.com/teams/{slug}/line-combinations
  `pageProps.combinations` = team fields (`teamAbbreviation`, `sourceName` -- e.g. "Training
  Camp 2026" -- and `updatedAt`) plus `players`: one entry per (player, group), so a player on
  the second line and PP1 appears twice. `groupIdentifier` is f1-f4, d1-d3, pp1/pp2, pk1/pk2, g
  or ir; `injuryStatus` is null, 'out' or 'dtd'; `gameTimeDecision` a bool.
- Starting goalies: https://www.dailyfaceoff.com/starting-goalies/{YYYY-MM-DD}
  `pageProps.data` = one entry per game with home*/away* goalie fields, `*NewsStrengthName`
  (Confirmed / Likely / ... or null), `*NewsCreatedAt` (when the news posted), and the source
  (usually a beat reporter's tweet). Past dates work back to at least 2022-01.

Players carry Daily Faceoff's own id (`playerId` / `*GoalieId`), not an NHL id; names are
resolved downstream.
"""

import json
import re

from nhl_pipeline.http_client import get_text

BASE = "https://www.dailyfaceoff.com"
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _page_props(url: str) -> dict:
    match = _NEXT_DATA.search(get_text(url))
    if match is None:
        raise ValueError(f"no __NEXT_DATA__ on {url}")
    return json.loads(match.group(1))["props"]["pageProps"]


def team_slugs() -> list:
    """The 32 team slugs, from the team list every line-chart page carries."""
    props = _page_props(f"{BASE}/teams/anaheim-ducks/line-combinations")
    return [t["slug"] for t in props["sortedTeams"]]


def line_chart(slug: str) -> dict:
    """One team's current chart: team fields plus one row per (player, group)."""
    c = _page_props(f"{BASE}/teams/{slug}/line-combinations")["combinations"]
    return {
        "team_abbreviation": c["teamAbbreviation"],
        "team_name": c["teamName"],
        "source_label": c.get("sourceName") or None,
        "updated_at": c.get("updatedAt"),
        "players": [
            {
                "external_id": str(p["playerId"]),
                "name": p["name"],
                "position": p.get("positionIdentifier"),
                "group": p["groupIdentifier"],
                "injury_status": p.get("injuryStatus"),
                "game_time_decision": bool(p.get("gameTimeDecision")),
            }
            for p in c.get("players", [])
        ],
    }


def starting_goalies(day: str) -> list:
    """One row per (game, side) for the date: the goalie Daily Faceoff names and how sure it is."""
    rows = []
    for g in _page_props(f"{BASE}/starting-goalies/{day}").get("data", []):
        for side in ("home", "away"):
            goalie_id = g.get(f"{side}GoalieId")
            rows.append({
                "date": g["date"],
                "puck_utc": g.get("dateGmt"),
                "side": side,
                "team_name": g[f"{side}TeamName"],
                "home_team_name": g["homeTeamName"],
                "away_team_name": g["awayTeamName"],
                "external_id": str(goalie_id) if goalie_id is not None else None,
                "name": g.get(f"{side}GoalieName"),
                "strength": g.get(f"{side}NewsStrengthName"),
                "news_created_at": g.get(f"{side}NewsCreatedAt"),
                "news_source_name": g.get(f"{side}NewsSourceName"),
                "news_source_url": g.get(f"{side}NewsSourceUrl"),
            })
    return rows

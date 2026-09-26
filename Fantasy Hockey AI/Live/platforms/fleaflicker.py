"""Fleaflicker's public API (no key; every endpoint is scoped to one league_id) -- read only.

The draft (moved here from draft_assistant.py, unchanged): FetchLeagueDraftBoard, the 2025 replay
rehearsal, the playoff weeks. The season, checked against league 12090's 2025 season
(`season=2025`, 2026-09-25):

    FetchLeagueRosters     every team's players (no slots)
    FetchRoster            one team's lineup: START slots (C C LW LW RW RW F D D D D F/D G G), an
                           unnamed bench group (BN x4), INJURED (IR); per period (`season`)
    FetchLeagueTransactions  newest first, 30 a page: TRANSACTION_DROP, TRANSACTION_CLAIM, and
                           free-agent adds with NO type field (275 of the last 600)
    FetchLeagueScoreboard  a scoring period's games: home/away team ids and scores;
                           eligibleSchedulePeriods = the weeks, each a low/high day
    FetchLeagueRules       rosterPositions, numStarters 14, numBench 4, maxRosterSize 20

Moves used this week = the team's adds and claims since its period began (drops are free, and IR
moves are not transactions) -- the league's rule (Settings/rosters/league.json move_cost).
"""

import datetime as dt
import json
import urllib.request

import pandas as pd

from platforms.base import Matchup, TeamRoster

API = "https://www.fleaflicker.com/api"


def fetch_board(league_id: int, season=None) -> dict:
    """FetchLeagueDraftBoard, this season's or a past one's (`season`, e.g. 2025 -- what the
    rehearsal replays, see base.Replay)."""
    url = f"{API}/FetchLeagueDraftBoard?sport=NHL&league_id={league_id}"
    if season:
        url += f"&season={season}"
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.load(response)


def playoff_window(league_id: int, weeks: int):
    """First and last day of the league's fantasy playoffs: its last `weeks` scoring periods on
    Fleaflicker's schedule (league 12090: weeks 24-26, 2027-03-15 to 2027-04-04)."""
    url = f"{API}/FetchLeagueScoreboard?sport=NHL&league_id={league_id}"
    with urllib.request.urlopen(url, timeout=20) as response:
        periods = json.load(response)["eligibleSchedulePeriods"]

    def day(bound):     # the period boundaries are early-morning UTC instants on the NHL's day
        return dt.datetime.fromtimestamp(int(bound["startEpochMilli"]) / 1000,
                                         dt.timezone(dt.timedelta(hours=-5))).date()
    last = sorted(periods, key=lambda p: p["ordinal"])[-weeks:]
    return pd.Timestamp(day(last[0]["low"])), pd.Timestamp(day(last[-1]["high"]))


def picks_from(board_json: dict) -> list:
    """Every pick cell in draft order: overall, round, team id, team name, and the picked player's
    platform id and name (None until he is picked)."""
    cells = []
    for row in board_json.get("rows", []):
        for cell in row.get("cells", []):
            player = (cell.get("player") or {}).get("proPlayer") or {}
            cells.append({"overall": cell["slot"]["overall"], "round": cell["slot"]["round"],
                          "team_id": cell["team"]["id"], "team": cell["team"]["name"],
                          "platform_id": player.get("id"), "name": player.get("nameFull")})
    return sorted(cells, key=lambda c: c["overall"])


def team_id_for(board_json, name):
    """A team by its name, or by its Fleaflicker team id (Burnaby Beagles is 63341; the id stays
    the same across seasons while the name may not -- in 2025 it was One if by Landeskog)."""
    for team in board_json.get("draftOrder", []):
        if (team["name"].strip().lower() == name.strip().lower()
                or str(team["id"]) == name.strip()):
            return team["id"], team["name"]
    names = ", ".join(t["name"] for t in board_json.get("draftOrder", []))
    raise SystemExit(f"no team named {name!r} in this draft; teams: {names}")


MOVE_TYPES = (None, "TRANSACTION_CLAIM")        # an add has no type; a waiver claim is one


def _get(endpoint: str, **params) -> dict:
    query = "&".join(f"{k}={v}" for k, v in {"sport": "NHL", **params}.items() if v is not None)
    with urllib.request.urlopen(f"{API}/{endpoint}?{query}", timeout=30) as response:
        return json.load(response)


def _day(bound) -> dt.date:
    """A period boundary (an early-morning UTC instant on the NHL's day) as that day."""
    return dt.datetime.fromtimestamp(int(bound["startEpochMilli"]) / 1000,
                                     dt.timezone(dt.timedelta(hours=-5))).date()


class Fleaflicker:
    """One Fleaflicker league, read. `season` reads a past season (e.g. 2025) where the endpoint
    takes it; transactions do not (the endpoint refuses `season`), so they are always current."""

    platform = "fleaflicker"
    platform_name = "Fleaflicker"       # its name in platform_ids.parquet

    def __init__(self, league_id: int, season=None):
        self.league_id = int(league_id)
        self.season = season

    def _get(self, endpoint, **params):
        return _get(endpoint, league_id=self.league_id, **params)

    # ---------- the draft ----------

    def draft_board(self) -> dict:
        return fetch_board(self.league_id, self.season)

    def playoff_window(self, weeks: int):
        return playoff_window(self.league_id, weeks)

    # ---------- rosters ----------

    def teams(self) -> dict:
        """{team id: name}."""
        rosters = self._get("FetchLeagueRosters", season=self.season).get("rosters", [])
        return {r["team"]["id"]: r["team"]["name"] for r in rosters}

    def roster(self, team_id: int) -> TeamRoster:
        """One team's players by lineup slot: START labels, BN, IR (FetchRoster)."""
        raw = self._get("FetchRoster", team_id=team_id, season=self.season)
        lineup, roster, ir = {}, [], []
        for group in raw.get("groups", []):
            for slot in group.get("slots", []):
                player = (slot.get("leaguePlayer") or {}).get("proPlayer")
                if not player:
                    continue
                label = slot["position"]["label"]
                lineup.setdefault(label, []).append(player["id"])
                (ir if group.get("group") == "INJURED" else roster).append(player["id"])
        name = next((t["name"] for t in raw.get("eligibleTeams", []) if t.get("id") == team_id), str(team_id))
        return TeamRoster(team_id=team_id, name=name, roster=roster, ir=ir, lineup=lineup)

    def rosters(self) -> list:
        """Every team, in the league's roster order, with IR and lineup slots (one call a team)."""
        names = self.teams()
        out = []
        for team_id, name in names.items():
            team = self.roster(team_id)
            team.name = name
            out.append(team)
        return out

    # ---------- the week ----------

    def periods(self) -> list:
        """[(period number, first day, last day)] -- the league's matchup weeks."""
        raw = self._get("FetchLeagueScoreboard", season=self.season)
        return sorted((p["ordinal"], _day(p["low"]), _day(p["high"]))
                      for p in raw.get("eligibleSchedulePeriods", []))

    def period_of(self, day: dt.date):
        return next(((n, lo, hi) for n, lo, hi in self.periods() if lo <= day <= hi), None)

    def matchup(self, team_id: int, day: dt.date | None = None) -> Matchup | None:
        """The team's game in the period containing `day` (default: the platform's current one)."""
        period = self.period_of(day) if day else None
        raw = self._get("FetchLeagueScoreboard", season=self.season,
                        scoring_period=None if period is None else _scoring_period(self, period))
        number = raw.get("schedulePeriod", {}).get("ordinal")
        for game in raw.get("games", []):
            for me, them in (("home", "away"), ("away", "home")):
                if game[me]["id"] == team_id:
                    return Matchup(period=number, team_id=team_id, opponent_id=game[them]["id"],
                                   points=_score(game, me), opponent_points=_score(game, them))
        return None

    def transactions(self, since_ms: int | None = None, pages: int = 20) -> list:
        """Newest-first transactions, as {time_ms, type, team_id, player_id}, back to `since_ms`."""
        out, offset = [], 0
        for _ in range(pages):
            raw = self._get("FetchLeagueTransactions", result_offset=offset)
            for item in raw.get("items", []):
                when = int(item["timeEpochMilli"])
                if since_ms is not None and when < since_ms:
                    return out
                t = item["transaction"]
                if "player" not in t:          # a traded draft pick: no player, never a move
                    continue
                out.append({"time_ms": when, "type": t.get("type"), "team_id": t["team"]["id"],
                            "player_id": t["player"]["proPlayer"]["id"]})
            offset = raw.get("resultOffsetNext")
            if offset is None:
                break
        return out

    def moves_used(self, team_id: int, day: dt.date) -> int:
        """Adds and claims by the team since the first day of `day`'s period."""
        period = self.period_of(day)
        if period is None:
            return 0
        start = dt.datetime.combine(period[1], dt.time(0), tzinfo=dt.timezone(dt.timedelta(hours=-5)))
        since = int(start.timestamp() * 1000)
        return sum(1 for t in self.transactions(since_ms=since)
                   if t["team_id"] == team_id and t["type"] in MOVE_TYPES)

    # ---------- the league ----------

    def rules(self) -> dict:
        """FetchLeagueRules: roster positions (label, eligibility, starters) and sizes."""
        raw = self._get("FetchLeagueRules")
        return {"slots": {p["label"]: p.get("start", 0) for p in raw.get("rosterPositions", [])
                          if p.get("group") == "START"},
                "slot_positions": {p["label"]: p.get("eligibility", []) for p in raw.get("rosterPositions", [])
                                   if p.get("group") == "START"},
                "bench": raw.get("numBench"), "max_roster": raw.get("maxRosterSize"),
                "ir": next((p.get("start", 0) for p in raw.get("rosterPositions", [])
                            if p.get("group") == "INJURED"), 0)}


def _score(game, side) -> float:
    return float(((game.get(f"{side}Score") or {}).get("score") or {}).get("value", 0.0))


def _scoring_period(adapter, period) -> int:
    """A day inside `period`, as Fleaflicker's scoring_period (the season's day ordinal)."""
    first = adapter.periods()[0][1]
    return (period[1] - first).days + 1

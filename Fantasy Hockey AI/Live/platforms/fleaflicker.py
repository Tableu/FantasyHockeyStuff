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
import html
import json
import logging
import re
import time
import urllib.request
import zoneinfo
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from platforms.base import Matchup, TeamRoster

log = logging.getLogger("platforms.fleaflicker")

# Fleaflicker's scoring days roll over at 6:00 AM Eastern, daylight time included (period starts
# are 10:00 UTC in October, 11:00 UTC after the clocks change).
EASTERN = zoneinfo.ZoneInfo("America/New_York")

# One league read asks for the schedule and the transaction list once per team; they are fetched
# once and reused for this long (an adapter lives for one read in the plan window, but the draft
# tools keep theirs). Team rosters are fetched this many at a time: Fleaflicker answers a burst
# with 403 Forbidden for a while (2026-09-27: ~280 transaction pages in a row did it).
REUSE_SECONDS = 60
ROSTER_WORKERS = 3
# The team page's own count, e.g. "Acquisitions Week 4 /6": moves used this week and this week's
# limit. The API has neither (FetchRoster, FetchLeagueRules: checked 2026-10-01).
TEAM_PAGE = "https://www.fleaflicker.com/nhl/leagues/{league}/teams/{team}"
_ACQUISITIONS = re.compile(r"Acquisitions\s*Week\s*(\d+)\s*/\s*(\d+)")

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

    def day(bound):     # the period boundaries are 6:00 AM Eastern on the NHL's day
        return dt.datetime.fromtimestamp(int(bound["startEpochMilli"]) / 1000, EASTERN).date()
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


def _instant(bound) -> dt.datetime:
    """A period boundary as the instant it is (aware, UTC)."""
    return dt.datetime.fromtimestamp(int(bound["startEpochMilli"]) / 1000, dt.timezone.utc)


def _day(bound) -> dt.date:
    """A period boundary (6:00 AM Eastern on the NHL's day) as that day."""
    return dt.datetime.fromtimestamp(int(bound["startEpochMilli"]) / 1000, EASTERN).date()


class Fleaflicker:
    """One Fleaflicker league, read. `season` reads a past season (e.g. 2025) where the endpoint
    takes it; transactions do not (the endpoint refuses `season`), so they are always current."""

    platform = "fleaflicker"
    platform_name = "Fleaflicker"       # its name in platform_ids.parquet

    def __init__(self, league_id: int, season=None):
        self.league_id = int(league_id)
        self.season = season
        self._reused = {}                  # key -> (fetched at, value): see REUSE_SECONDS

    def _reuse(self, key, fetch):
        """`fetch()`, or its value from under REUSE_SECONDS ago."""
        hit = self._reused.get(key)
        if hit is not None and time.monotonic() - hit[0] < REUSE_SECONDS:
            return hit[1]
        value = fetch()
        self._reused[key] = (time.monotonic(), value)
        return value

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
        """Every team, in the league's roster order, with IR and lineup slots (one call a team,
        ROSTER_WORKERS at a time -- one after another took 9 s of a 15 s read)."""
        names = self.teams()
        with ThreadPoolExecutor(max_workers=ROSTER_WORKERS) as pool:
            teams = list(pool.map(self.roster, names))
        for team, name in zip(teams, names.values()):
            team.name = name
        return teams

    # ---------- the week ----------

    def periods(self) -> list:
        """[(period number, first day, last day)] -- the league's matchup weeks."""
        return [(n, lo, hi) for n, lo, hi, _, _ in self._period_rows()]

    def _period_rows(self) -> list:
        """[(number, first day, last day, start, end)]: `start` the instant the period begins
        (6:00 AM Eastern on its first day), `end` the instant after its last day (UTC)."""
        def fetch():
            raw = self._get("FetchLeagueScoreboard", season=self.season)
            return sorted((p["ordinal"], _day(p["low"]), _day(p["high"]), _instant(p["low"]),
                           _instant(p["high"]) + dt.timedelta(days=1))
                          for p in raw.get("eligibleSchedulePeriods", []))
        # Asked once per team's move count and per period lookup: 17-45 fetches a read, now one.
        return self._reuse(("periods", self.season), fetch)

    def period_at(self, when: dt.datetime):
        """The period a move made at `when` (an aware datetime) counts toward: the one running then,
        by Fleaflicker's instants -- before 6:00 AM on a week's first day that is still the week
        before. None before the first period begins, after the last ends, or in a break."""
        return next(((n, lo, hi) for n, lo, hi, start, end in self._period_rows()
                     if start <= when < end), None)

    def before_first_period(self, when: dt.datetime) -> bool:
        """Whether `when` is before the season's first period: a move then counts toward no
        week's limit (the user, 2026-09-27)."""
        rows = self._period_rows()
        return bool(rows) and when < rows[0][3]

    def week_days(self, day: dt.date, when: dt.datetime | None = None) -> int | None:
        """Days in the period a move made now counts toward (6 in 2026-27's week 1, 14 in week 19)."""
        period = self.period_at(when) if when is not None else self.period_of(day)
        return None if period is None else (period[2] - period[1]).days + 1

    def acquisitions(self, team_id: int):
        """(moves used this week, this week's limit) as the team's page shows them, or None when the
        page cannot be read or does not show them. Read live, never from a past `season`."""
        url = TEAM_PAGE.format(league=self.league_id, team=team_id)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                page = response.read().decode("utf-8", "replace")
        except Exception as error:     # the plan goes on with the computed limit
            log.warning("Fleaflicker team page %s not read: %s", url, error)
            return None
        match = _ACQUISITIONS.search(html.unescape(re.sub(r"<[^>]+>", " ", page)))
        return None if match is None else (int(match.group(1)), int(match.group(2)))

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

    def moves_used(self, team_id: int, day: dt.date, when: dt.datetime | None = None) -> int:
        """Adds and claims by the team in the period a move made now counts toward: the one
        running at `when` (an aware datetime), or `day`'s when no instant is given. Counted from
        the instant Fleaflicker gives for its start (6:00 AM Eastern on its first day), not a
        midnight rebuilt from the date: a fixed UTC-5 midnight opened the window five hours early
        in daylight time, charging the new week for moves made in the old one's last hours."""
        period = self.period_at(when) if when is not None else self.period_of(day)
        if period is None:
            return 0
        start = next(s for n, _, _, s, _ in self._period_rows() if n == period[0])
        since = int(start.timestamp() * 1000)
        # The whole league's list, fetched once for every team's count (it was once per team).
        moves = self._reuse(("transactions", since), lambda: self.transactions(since_ms=since))
        return sum(1 for t in moves if t["team_id"] == team_id and t["type"] in MOVE_TYPES)

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

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


def period_rows(scoreboard: dict) -> list:
    """[(number, first day, last day, start, end)] from a FetchLeagueScoreboard answer."""
    return sorted((p["ordinal"], _day(p["low"]), _day(p["high"]), _instant(p["low"]),
                   _instant(p["high"]) + dt.timedelta(days=1))
                  for p in scoreboard.get("eligibleSchedulePeriods", []))


class Fleaflicker:
    """One Fleaflicker league, read. `season` reads a past season (e.g. 2025) where the endpoint
    takes it; transactions do not (the endpoint refuses `season`), so they are always current."""

    platform = "fleaflicker"
    platform_name = "Fleaflicker"       # its name in platform_ids.parquet
    week_turns_over = "6:00 AM Eastern (3:00 AM Pacific)"    # a period's first instant (period_at)

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
            return period_rows(self._get("FetchLeagueScoreboard", season=self.season))
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

    def league_settings(self) -> dict:
        """settings_from this league's answers (current season)."""
        url = RULES_PAGE.format(league=self.league_id)
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            page = response.read().decode("utf-8", "replace")
        return settings_from(self._get("FetchLeagueRules"), page,
                             self._get("FetchLeagueScoreboard", season=self.season),
                             self.draft_board(), len(self.teams()))


# ---------- league settings (Fleaflicker.league_settings) ----------

RULES_PAGE = "https://www.fleaflicker.com/nhl/leagues/{league}/rules"
UNLIMITED_MOVES = 99      # the harness needs a number; no real week comes near it
POSITIONS = ("C", "LW", "RW", "D", "G")
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
# The rules page's wording for the rules Season/league.py names, as league 12090 shows it
# (2026-10-07), matched on how the text starts. Wording missing here fails the read with the text:
# add it from the league's settings page, never guess.
LINEUP_LOCKS = {"At game start": "per_game"}
CLAIMS = {"Waiver Priority": "rolling", "Blind Bid": "faab"}
GAME_START = {"Dropped players and all free agents after game start": True, "Dropped players only": False}
IR_NAMES = {"Day-to-day": "DTD", "Out": "OUT", "Suspended": "SUSP", "Injured Reserve": "IR"}
# Fleaflicker-wide, not league settings: an add or a claim is one transaction against the limit,
# drops and IR moves are free (MOVE_TYPES), and an add counts at once -- from his next game.
MOVE_COST = {"add": 1, "claim": 1, "drop": 0, "ir_stash": 0, "ir_activate": 0}
# FetchLeagueRules category id -> our scoring key (Settings/scoring/*.json); a scored category not
# here is reported as not projected, never dropped silently.
CATEGORIES = {1: "goals", 5: "assists", 36: "ppp", 37: "shp", 4: "shots", 8: "pim", 13: "hits",
              14: "blocks", 19: "wins", 20: "losses", 22: "ot_losses", 23: "shutouts", 26: "saves",
              27: "goals_against"}
GOALIE_KEYS = {"wins", "losses", "ot_losses", "shutouts", "saves", "goals_against"}


def _clean(fragment: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment)).replace("\ufffd", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_rules_page(page: str) -> dict:
    """{label: (text, html)} for every <dt>/<dd> pair on a rules page, and "tooltips": {id: text}."""
    out = {_clean(label): (_clean(value), value)
           for label, value in re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", page, flags=re.S)}
    data = re.search(r"window\.pageData\s*=\s*(\{.*?\});", page, flags=re.S)
    tips = json.loads(data.group(1)).get("tooltips", []) if data else []
    out["tooltips"] = {i: _clean(t["contents"]) for t in tips for i in t["ids"]}
    return out


def _text(page: dict, label: str) -> str:
    if label not in page:
        raise ValueError(f"Fleaflicker rules page has no {label!r} (the page changed?)")
    return page[label][0]


def _phrase(label: str, text: str, table: dict):
    for start, value in table.items():
        if text.startswith(start):
            return value
    raise ValueError(f"Fleaflicker {label} {text!r} is not translated yet (platforms/fleaflicker.py)")


def _ir_statuses(page: dict) -> list:
    """The IR slot's tooltip: "The following injury statuses are eligible: Day-to-day, Out, ..."."""
    tip = re.search(r'id="(ttId[\d_]+)">IR<', page["Totals"][1])
    text = page["tooltips"].get(tip.group(1), "") if tip else ""
    names = [n.strip() for n in text.split("eligible:", 1)[1].split(",")] if "eligible:" in text else []
    unknown = [n for n in names if n not in IR_NAMES]
    if not names or unknown:
        raise ValueError(f"Fleaflicker IR eligibility {text!r} not read (unknown: {unknown})")
    return [IR_NAMES[n] for n in names]


def _scoring(api: dict, notes: list) -> dict:
    """{"skaters", "goalies"}: points per unit of each scored category (a rule's points over its
    `forEvery`). A bounded or bonus rule fails: points per unit cannot hold it."""
    out = {"skaters": {}, "goalies": {}}
    for group in api.get("groups", []):
        for rule in group.get("scoringRules", []):
            if any(k in rule for k in ("boundLower", "boundUpper", "isBonus")):
                raise ValueError(f"Fleaflicker scoring rule {rule['description']!r}: bounds and "
                                 f"bonuses are not supported")
            key = CATEGORIES.get(rule["category"]["id"])
            points = rule["points"]["value"] / rule.get("forEvery", 1)
            if key is None:
                notes.append(f"{rule['category']['abbreviation']} {points:+g} scored but not projected")
            else:
                out["goalies" if key in GOALIE_KEYS else "skaters"][key] = round(points, 6)
    return out


def settings_from(api: dict, page: str, scoreboard: dict, board: dict, teams: int) -> dict:
    """The league's settings in Season/league.py's terms -- {"settings": a Settings/rosters body
    (no name, description or eligibility), "scoring": {"skaters", "goalies"}, "notes": [what
    the league does that those terms cannot hold]}: slots, sizes and scoring from
    FetchLeagueRules, the weeks from FetchLeagueScoreboard, the draft order from the board, the
    rest from the rules page. Fleaflicker's wording is translated here and only here; wording
    not in the tables below fails the read rather than mapping to the nearest.
    The arguments are the raw answers -- FetchLeagueRules, the /rules page's HTML, FetchLeagueScoreboard,
    FetchLeagueDraftBoard, the number of teams; nothing here reads the network."""
    page = parse_rules_page(page)
    notes = []
    start = [p for p in api["rosterPositions"] if p.get("group") == "START"]
    caps = {p["label"]: p["max"] for p in start
            if p.get("max") is not None and p["label"] in POSITIONS and p["max"] < api["maxActive"]}

    limit = re.search(r"Week:\s*(\d+)", _text(page, "Transaction Limits"))
    if limit is None:
        notes.append(f"no weekly transaction limit (written as {UNLIMITED_MOVES} a week)")
    hours = re.fullmatch(r"(\d+) Hours?", _text(page, "Time on Waivers After Drop"))
    if hours is None:
        raise ValueError(f"Fleaflicker waiver time {_text(page, 'Time on Waivers After Drop')!r} not read")
    waivers = _phrase("How Are Claims Resolved", _text(page, "How Are Claims Resolved"), CLAIMS)
    if waivers == "rolling" and _text(page, "Reset Order Weekly") == "Yes":
        waivers = "reset_weekly"
    if _text(page, "Break Regular Season Ties") != "No":
        notes.append("regular-season ties are broken by the league's tiebreakers (the harness splits them)")
    notes.append("a tied playoff matchup goes to the higher starter total, then the best single "
                 "starter, then the bench (the harness: more regular-season points)")
    # "Most average points/game" first: a record tie goes to the higher points per game, which
    # is points_for over the same games played.
    rank = _text(page, "Power & Playoff Rank Tiebreakers")
    if not rank.startswith(("Most average points/game", "Most total points")):
        raise ValueError(f"Fleaflicker rank tiebreakers {rank!r} not translated")
    if _text(page, "Rank division winners higher") != "No":
        notes.append("division winners are seeded first (the harness has no divisions)")

    playoffs = re.search(r"(\d+)\s*Teams; Weeks:\s*(\d+)-(\d+).*?(\d+) byes?\s*\((no re-seeding|re-seeding)\)",
                         _text(page, "Playoffs"))
    if playoffs is None:
        raise ValueError(f"Fleaflicker playoffs {_text(page, 'Playoffs')!r} not read")
    teams_in, first_week, last_week, byes = (int(g) for g in playoffs.groups()[:4])
    rounds = next(r for r in range(1, 6) if 2 ** r >= teams_in)
    weeks = last_week - first_week + 1
    if weeks % rounds:
        raise ValueError(f"Fleaflicker playoffs: {weeks} weeks over {rounds} rounds")
    if playoffs.group(5) != "no re-seeding":
        notes.append("the bracket is re-seeded each round (the harness keeps it fixed)")

    periods = period_rows(scoreboard)
    regular = [row for row in periods if row[0] < first_week]
    long_weeks = [n for n, lo, hi, _, _ in regular[1:] if (hi - lo).days + 1 > 7]
    if long_weeks:
        notes.append(f"week(s) {long_weeks} run two weeks on Fleaflicker (the harness's calendar "
                     f"splits them; the live plan reads the platform's weeks)")
    picks = picks_from(board)
    overall = {(c["round"], c["team_id"]): c["overall"] for c in picks}
    first = next(c for c in picks if c["overall"] == 1)
    size = sum(1 for c in picks if c["round"] == 1)
    draft = "snake" if overall.get((2, first["team_id"])) == 2 * size else "linear"

    return {"settings": {
        "teams": teams,
        "active_slots": {p["label"]: p["start"] for p in start},
        "bench": api["numBench"],
        "ir": next((p.get("start", 0) for p in api["rosterPositions"] if p.get("group") == "INJURED"), 0),
        "moves_per_week": int(limit.group(1)) if limit else UNLIMITED_MOVES,
        "moves_carry_over": False,
        "waiver_days": max(1, round(int(hours.group(1)) / 24)),
        "ties": "split",
        "schedule": {"type": "round_robin",
                     # the regular season ends with the last period before the playoffs
                     "regular_season_end": regular[-1][2].strftime("%m-%d"),
                     "week_starts_on": WEEKDAYS[periods[1][1].weekday()]},
        "draft": {"type": draft, "order": "lottery", "keepers": 0},
        "playoffs": {"teams": teams_in, "byes": byes, "rounds": rounds,
                     "weeks_per_round": weeks // rounds, "seeding": "record",
                     "tiebreak": "points_for"},
        "slot_positions": {p["label"]: p["eligibility"] for p in start},
        "rules": {
            "lineup_lock": _phrase("Lineup Locking", _text(page, "Lineup Locking"), LINEUP_LOCKS),
            "add_effective": "immediate",
            "waivers": waivers,
            "game_start_waivers": _phrase("Who is Placed on Waivers",
                                          _text(page, "Who is Placed on Waivers"), GAME_START),
            "ir_eligible": _ir_statuses(page),
            "position_max": caps,
            "move_cost": dict(MOVE_COST)}},
        "scoring": _scoring(api, notes), "notes": notes}


def _score(game, side) -> float:
    return float(((game.get(f"{side}Score") or {}).get("score") or {}).get("value", 0.0))


def _scoring_period(adapter, period) -> int:
    """A day inside `period`, as Fleaflicker's scoring_period (the season's day ordinal)."""
    first = adapter.periods()[0][1]
    return (period[1] - first).days + 1

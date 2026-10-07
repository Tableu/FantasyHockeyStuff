"""ESPN's fantasy hockey league API -- read only.

    https://lm-api-reads.fantasy.espn.com/apis/v3/games/fhl/seasons/{end year}/segments/0/leagues/{id}
    ?view=mSettings | mTeam | mRoster | mMatchupScore | mDraftDetail | mStatus

A public league answers anyone; a private one needs the owner's `espn_s2` and `SWID` cookies
(Settings/secrets.json, via the registry's `auth`). ESPN numbers a season by the year it ENDS
(2026-27 = 2027). Checked against a private 14-team league (with the owner's cookies), its 2026 and 2027 seasons
(2026-09-25):

    lineupSlotId   0 C, 1 LW, 2 RW, 3 F, 4 D, 5 G, 6 UTIL (any skater), 7 bench, 8 IR -- decoded from
                   rosters (84 players in 3 = 14 teams x 6 F, 56 in 4, 14 in 6) and eligibleSlots
    statId         decoded by matching 2025-26 season totals to our database (STAT_KEYS below):
                   skaters 85-100% exact, goalies 94-100%
    draft          mDraftDetail picks carry every pick slot before the draft (playerId -1 until
                   made). But the order is re-drawn when the draft starts, and a live draft's
                   picks stay -1 until it is over (public league 1747127466, 2026-09-26: 0 of
                   them 2.5 minutes in, all 220 at the end) -- so a live ESPN draft runs
                   --standalone, and only a finished one can be read
    waivers        kona_player_info with an x-fantasy-filter on status WAIVERS: each player's
                   waiverProcessDate (after the draft every undrafted player sits on waivers
                   until the next midnight Pacific but one)
    transactions   mTransactions2 returned nothing for any scoring period, with or without an
                   x-fantasy-filter header -- not read. Moves used this week come from mTeam's
                   transactionCounter.matchupAcquisitionTotals instead ({matchup period: adds})
    move limit     acquisitionSettings.matchupAcquisitionLimit, per day when
                   matchupLimitPerScoringPeriod (`acquisitions`); acquisitionLimit is -1 on a
                   league with a weekly limit
"""

import collections
import datetime as dt
import json
import zoneinfo

import pandas as pd
import requests

import livepaths
from platforms.base import Matchup, PlayerIds, TeamRoster

SEASON_API = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fhl/seasons/{year}"
API = SEASON_API + "/segments/0/leagues/{league_id}"
EASTERN = zoneinfo.ZoneInfo("America/New_York")

SLOT_LABELS = {0: "C", 1: "LW", 2: "RW", 3: "F", 4: "D", 5: "G", 6: "UTIL", 7: "BN", 8: "IR"}
SLOT_POSITIONS = {"C": ["C"], "LW": ["LW"], "RW": ["RW"], "F": ["C", "LW", "RW"], "D": ["D"],
                  "G": ["G"], "UTIL": ["C", "LW", "RW", "D"]}

# ESPN statId -> our scoring key (Settings/scoring/*.json); None = a stat ESPN scores that our
# projections do not carry (reported by `scoring()` so it is never silently dropped).
STAT_KEYS = {
    13: "goals", 14: "assists", 16: None,  # points: a sum of the two, 0 in leagues seen so far
    15: None,  # plus/minus
    17: "pim", 18: None, 19: None,  # PPG / PPA separately (ppp is 38)
    20: None, 21: None,  # SHG / SHA separately (shp is 39)
    23: None, 24: None,  # faceoffs won / lost
    29: "shots", 31: "hits", 32: "blocks", 38: "ppp", 39: "shp",
    0: None,  # goalie games started
    1: "wins", 2: "losses", 9: "ot_losses", 7: "shutouts", 6: "saves", 4: "goals_against",
    3: None,  # shots against
}
STAT_NAMES = {13: "G", 14: "A", 15: "+/-", 16: "PTS", 17: "PIM", 18: "PPG", 19: "PPA", 20: "SHG",
              21: "SHA", 23: "FOW", 24: "FOL", 29: "SOG", 31: "HIT", 32: "BLK", 38: "PPP", 39: "SHP",
              0: "GS", 1: "W", 2: "L", 3: "SA", 4: "GA", 6: "SV", 7: "SO", 9: "OTL"}
GOALIE_KEYS = {"wins", "losses", "ot_losses", "shutouts", "saves", "goals_against"}


# ESPN's codes for the rules Season/league.py names, as seen in leagues so far (espn-la 2026-27,
# a private 14-team league 2025-26/2026-27). A code missing here fails `league_settings` with its
# name: look it up on the league's settings page and add it, never guess.
LINEUP_LOCKS = {"INDIVIDUAL_GAME": "per_game"}
# rosterLocktimeType: adds and drops made after the day's first game take effect tomorrow.
ROSTER_LOCKS = {"FIRSTGAME_SCORINGPERIOD": "next_day_after_first_game"}
# A traditional order that is never reset is the rolling one (a winner goes to the back).
WAIVER_TYPES = {"WAIVERS_TRADITIONAL": "rolling"}
DRAFT_TYPES = {"SNAKE": "snake"}
SEEDINGS = {"H2H_RECORD": "record"}
# positionLimits keys: ESPN's position ids (a limit <= 0 is none).
POSITION_IDS = {1: "C", 2: "LW", 3: "RW", 4: "D", 5: "G"}
# ESPN-wide, not league settings: IR holds players ruled out (or on the NHL's IR); each add or
# claim is one acquisition, drops and IR moves are free.
IR_ELIGIBLE = ("OUT", "SUSP", "IR")
MOVE_COST = {"add": 1, "claim": 1, "drop": 0, "ir_stash": 0, "ir_activate": 0}
UNLIMITED_MOVES = 99      # the harness needs a number; no real week comes near it


def _code(field: str, code, table: dict):
    if code not in table:
        raise ValueError(f"ESPN {field} {code!r} is not translated yet (platforms/espn.py); known: "
                         f"{sorted(table)}")
    return table[code]


def _weekly_limit(acquisition: dict) -> int | None:
    """Adds a full (7-day) matchup week allows, None = unlimited: the matchup limit (per day when
    matchupLimitPerScoringPeriod), else acquisitionLimit."""
    per_matchup = acquisition.get("matchupAcquisitionLimit") or 0
    if per_matchup > 0:
        return int(round(per_matchup * (7 if acquisition.get("matchupLimitPerScoringPeriod") else 1)))
    return None if acquisition["acquisitionLimit"] < 0 else acquisition["acquisitionLimit"]


def espn_year(season: str) -> int:
    """'2026-27' -> 2027: ESPN's season number is the year the season ends."""
    return int(season[:4]) + 1


def settings_from(s: dict) -> dict:
    """The league's settings in Season/league.py's terms -- {"settings": a Settings/rosters body
    (no name, description or eligibility), "scoring": {"skaters", "goalies"}, "notes": [what
    the league does that those terms cannot hold]}. ESPN's codes are translated here and only
    here; a code not in the tables below fails the read rather than mapping to the nearest.
    `s` is mSettings' "settings" (Espn.settings); nothing here reads the network."""
    roster, acquisition = s["rosterSettings"], s["acquisitionSettings"]
    schedule, scoring = s["scheduleSettings"], s["scoringSettings"]
    notes = []
    counts = {SLOT_LABELS[int(k)]: v for k, v in roster["lineupSlotCounts"].items() if v}
    limit = _weekly_limit(acquisition)
    if limit is None:
        notes.append(f"no acquisition limit (written as {UNLIMITED_MOVES} a week)")
    if acquisition["isUsingAcquisitionBudget"]:
        waivers = "faab"
    elif acquisition["waiverOrderReset"]:
        waivers = "reset_weekly"
    else:
        waivers = _code("acquisitionType", acquisition["acquisitionType"], WAIVER_TYPES)
    teams = schedule["playoffTeamCount"]
    rounds = next(r for r in range(1, 6) if 2 ** r >= teams)
    if len(schedule.get("divisions") or []) > 1:
        notes.append(f"{len(schedule['divisions'])} divisions (the harness schedules one table)")
    if scoring["playoffMatchupTieRule"] != "NONE" or scoring["matchupTieRule"] != "NONE":
        notes.append(f"tie rules {scoring['matchupTieRule']}/{scoring['playoffMatchupTieRule']} "
                     f"(the harness splits a regular-season tie)")
    notes.append("a tied playoff matchup: ESPN's rule is not reported; the harness gives it to "
                 "the team with more regular-season points")
    caps = {POSITION_IDS[int(k)]: v for k, v in roster["positionLimits"].items()
            if v > 0 and int(k) in POSITION_IDS}
    out = scoring_from(s)
    notes += [f"{stat} {points:+g} scored but not projected" for stat, points in out.pop("unsupported").items()]
    return {"settings": {
        "teams": s["size"],
        "active_slots": {k: v for k, v in counts.items() if k not in ("BN", "IR")},
        "bench": counts.get("BN", 0), "ir": counts.get("IR", 0),
        "moves_per_week": limit or UNLIMITED_MOVES,
        "moves_carry_over": False,
        "waiver_days": max(1, round(acquisition["waiverHours"] / 24)),
        "ties": "split",
        "schedule": {"type": "round_robin", "regular_season_weeks": schedule["matchupPeriodCount"],
                     "week_starts_on": "MON"},
        "draft": {"type": _code("draft type", s["draftSettings"]["type"], DRAFT_TYPES),
                  "order": "lottery", "keepers": s["draftSettings"]["keeperCount"]},
        "playoffs": {"teams": teams, "byes": 2 ** rounds - teams, "rounds": rounds,
                     "weeks_per_round": schedule["playoffMatchupPeriodLength"],
                     "seeding": _code("playoffSeedingRule", schedule["playoffSeedingRule"], SEEDINGS),
                     "tiebreak": "points_for"},
        "slot_positions": {**{k: SLOT_POSITIONS[k] for k in counts if k in SLOT_POSITIONS}, "G": ["G"]},
        "rules": {
            "lineup_lock": _code("lineupLocktimeType", roster["lineupLocktimeType"], LINEUP_LOCKS),
            "add_effective": _code("rosterLocktimeType", roster["rosterLocktimeType"], ROSTER_LOCKS),
            "waivers": waivers,
            "game_start_waivers": False,
            "ir_eligible": list(IR_ELIGIBLE),
            "position_max": caps,
            "move_cost": dict(MOVE_COST)}},
        "scoring": out, "notes": notes}


def scoring_from(s: dict) -> dict:
    """{"skaters": {...}, "goalies": {...}, "unsupported": {stat: points}} -- the league's points
    per stat in Settings/scoring terms, and what our projections cannot score."""
    out = {"skaters": {}, "goalies": {}, "unsupported": {}}
    for item in s["scoringSettings"]["scoringItems"]:
        points = float(item["points"])
        if points == 0.0:
            continue
        key = STAT_KEYS.get(item["statId"])
        if key is None:
            out["unsupported"][STAT_NAMES.get(item["statId"], f"stat {item['statId']}")] = points
        else:
            out["goalies" if key in GOALIE_KEYS else "skaters"][key] = points
    return out


class Espn:
    platform = "espn"
    platform_name = "ESPN"              # its name in platform_ids.parquet

    def __init__(self, league_id: int, year: int, cookies: dict | None = None):
        self.league_id = int(league_id)
        self.season = year
        self.cookies = {k: cookies[k] for k in ("espn_s2", "SWID")} if cookies else {}
        self._cache = {}

    def _get(self, view: str) -> dict:
        if view not in self._cache:
            r = requests.get(API.format(year=self.season, league_id=self.league_id), params={"view": view},
                             cookies=self.cookies, headers={"User-Agent": "python-requests"}, timeout=30)
            if r.status_code == 401:
                raise SystemExit(f"ESPN league {self.league_id}: not authorized -- a private league needs "
                                 f"fresh espn_s2/SWID cookies in Settings/secrets.json (they expire on logout)")
            r.raise_for_status()
            self._cache[view] = r.json()
        return self._cache[view]

    # ---------- the league ----------

    def teams(self) -> dict:
        """{team id: name}."""
        return {t["id"]: (t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}").strip()
                for t in self._get("mTeam")["teams"]}

    def settings(self) -> dict:
        return self._get("mSettings")["settings"]

    def league_settings(self) -> dict:
        """settings_from(this league's mSettings)."""
        return settings_from(self.settings())

    def scoring(self) -> dict:
        return scoring_from(self.settings())

    # ---------- rosters ----------

    def rosters(self) -> list:
        names = self.teams()
        out = []
        for team in self._get("mRoster")["teams"]:
            lineup, roster, ir = {}, [], []
            for entry in team.get("roster", {}).get("entries", []):
                label = SLOT_LABELS.get(entry["lineupSlotId"], str(entry["lineupSlotId"]))
                lineup.setdefault(label, []).append(entry["playerId"])
                (ir if label == "IR" else roster).append(entry["playerId"])
            out.append(TeamRoster(team_id=team["id"], name=names.get(team["id"], str(team["id"])),
                                  roster=roster, ir=ir, lineup=lineup))
        return out

    def roster(self, team_id: int) -> TeamRoster:
        return next(t for t in self.rosters() if t.team_id == team_id)

    def waivers(self, limit: int = 3000) -> dict:
        """{ESPN player id: the local date he clears waivers} for every player on waivers now; he
        is a free agent from that date on."""
        # ESPN refuses a limit without a sort (400); the owned share is its usual one.
        flt = {"players": {"filterStatus": {"value": ["WAIVERS"]}, "limit": limit,
                           "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
        r = requests.get(API.format(year=self.season, league_id=self.league_id),
                         params={"view": "kona_player_info"}, cookies=self.cookies, timeout=30,
                         headers={"User-Agent": "python-requests", "x-fantasy-filter": json.dumps(flt)})
        r.raise_for_status()
        players = r.json().get("players", [])
        if len(players) >= limit:
            raise RuntimeError(f"ESPN returned {limit} players on waivers, the limit: raise it")
        return {p["id"]: dt.datetime.fromtimestamp(p["waiverProcessDate"] / 1000).date().isoformat()
                for p in players if p.get("status") == "WAIVERS" and p.get("waiverProcessDate")}

    # ---------- the week ----------

    def matchup(self, team_id: int, day=None, period: int | None = None) -> Matchup | None:
        """The team's game in a matchup period (default: ESPN's current one). `day` is accepted for
        the interface; ESPN's periods are read by number."""
        data = self._get("mMatchupScore")
        period = period or data["status"]["currentMatchupPeriod"]
        for game in data.get("schedule", []):
            if game.get("matchupPeriodId") != period:
                continue
            for me, them in (("home", "away"), ("away", "home")):
                if (game.get(me) or {}).get("teamId") == team_id:
                    other = game.get(them) or {}
                    return Matchup(period=period, team_id=team_id, opponent_id=other.get("teamId"),
                                   points=float(game[me].get("totalPoints", 0.0)),
                                   opponent_points=float(other.get("totalPoints", 0.0)))
        return None

    def moves_used(self, team_id: int, day=None, when=None) -> int:
        """Acquisitions in the matchup period a move made now counts toward (`move_period`): the
        team's transactionCounter.matchupAcquisitionTotals, keyed by matchup period. `day` and
        `when` are accepted for the interface."""
        period = str(self.move_period()[0])
        team = next(t for t in self._get("mTeam")["teams"] if t["id"] == team_id)
        totals = (team.get("transactionCounter") or {}).get("matchupAcquisitionTotals") or {}
        return int(totals.get(period, 0))

    def _scoring_day(self, scoring_period: int) -> dt.date:
        """The Eastern date of a scoring period. ESPN numbers the season's days one apart (2026-27:
        1 = Tue Sep 29), so the games' dates (proTeamSchedules_wl) anchor them all."""
        if "day_one" not in self._cache:
            r = requests.get(SEASON_API.format(year=self.season), params={"view": "proTeamSchedules_wl"},
                             headers={"User-Agent": "python-requests"}, timeout=30)
            r.raise_for_status()
            offsets = collections.Counter(
                dt.datetime.fromtimestamp(game["date"] / 1000, EASTERN).date() - dt.timedelta(days=int(sp))
                for team in r.json()["settings"]["proTeams"]
                for sp, games in (team.get("proGamesByScoringPeriod") or {}).items() for game in games)
            self._cache["day_one"] = offsets.most_common(1)[0][0] + dt.timedelta(days=1)
        return self._cache["day_one"] + dt.timedelta(days=scoring_period - 1)

    def move_period(self):
        """(matchup period, its first day, its last day) that a move made now counts toward: the
        one holding status.transactionScoringPeriod, the day a move takes effect. After the day's
        first puck that is tomorrow, so a Sunday-night move counts toward next week (the user,
        2026-10-05). A period is the Monday-to-Sunday week holding its days, cut to the season's
        first and last (espn-la 2026-27: week 1 is scoring periods 1-6, Tue-Sun, as its
        pointsByScoringPeriod showed)."""
        status = self._get("mStatus")["status"]
        monday = lambda d: d - dt.timedelta(days=d.weekday())
        now = monday(self._scoring_day(status["latestScoringPeriod"]))
        start = monday(self._scoring_day(status.get("transactionScoringPeriod") or status["latestScoringPeriod"]))
        period = status["currentMatchupPeriod"] + (start - now).days // 7
        first = max(start, self._scoring_day(status["firstScoringPeriod"]))
        last = min(start + dt.timedelta(days=6), self._scoring_day(status["finalScoringPeriod"]))
        return period, first, last

    def week_days(self, day=None, when=None) -> int | None:
        """Days in the matchup period a move made now counts toward (`move_period`). `day` and
        `when` are accepted for the interface."""
        _, first, last = self.move_period()
        return (last - first).days + 1

    def acquisitions(self, team_id: int):
        """(adds in the matchup period a move made now counts toward, its limit), or None when the
        league sets no matchup limit.
        The limit is acquisitionSettings.matchupAcquisitionLimit, per scoring period (a day) when
        matchupLimitPerScoringPeriod: espn-la's 1.0 is 6 in its 6-day week 1 and 7 in a full week
        (the user, 2026-10-05; its 5 adds in week 1 rule out 1 a week). acquisitionLimit (-1 there)
        is not it."""
        settings = self.settings()["acquisitionSettings"]
        limit = settings.get("matchupAcquisitionLimit") or 0
        if limit <= 0:
            return None
        if settings.get("matchupLimitPerScoringPeriod"):
            limit *= self.week_days()
        return self.moves_used(team_id), int(round(limit))

    # ---------- the draft ----------

    def playoff_window(self, weeks: int):
        """Not yet: ESPN's playoff weeks come from its matchup periods, which need a real league to
        check the dates against. None = the draft board has no OFF/POG columns for this league."""
        return None

    def draft_board(self) -> dict:
        """The draft in the shape Fleaflicker's FetchLeagueDraftBoard returns (what the draft tools
        parse): every pick slot in order, the player once picked. Names are ours, via PlayerIds.
        Fetched fresh on every call: the draft tools poll it, and a cached copy froze the board."""
        self._cache.pop("mDraftDetail", None)
        data = self._get("mDraftDetail")
        names = self.teams()
        ids = PlayerIds(self.platform)
        players = pd.read_parquet(livepaths.season_paths.players()).set_index("player_id")["name"]
        order = data["settings"]["draftSettings"].get("pickOrder") or []
        rows = {}
        for pick in sorted(data["draftDetail"].get("picks", []), key=lambda p: p["overallPickNumber"]):
            cell = {"slot": {"overall": pick["overallPickNumber"], "round": pick["roundId"]},
                    "team": {"id": pick["teamId"], "name": names.get(pick["teamId"], str(pick["teamId"]))}}
            if pick["playerId"] and pick["playerId"] > 0:
                player_id = ids.get(pick["playerId"])
                cell["player"] = {"proPlayer": {"id": pick["playerId"],
                                                "nameFull": players.get(player_id, f"ESPN player {pick['playerId']}")}}
            rows.setdefault(pick["roundId"], []).append(cell)
        return {"draftOrder": [{"id": t, "name": names.get(t, str(t))} for t in order],
                "rows": [{"cells": rows[r]} for r in sorted(rows)]}

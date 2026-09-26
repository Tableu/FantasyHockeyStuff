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
                   made), so the whole snake order is known in advance
    transactions   mTransactions2 returned nothing for any scoring period, with or without an
                   x-fantasy-filter header -- not read. That league has no acquisition limit, so
                   nothing depends on it yet; a limited ESPN league needs this solved first.
"""

import json

import pandas as pd
import requests

import livepaths
from platforms.base import Matchup, PlayerIds, TeamRoster

API = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fhl/seasons/{year}/segments/0/leagues/{league_id}"

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


def espn_year(season: str) -> int:
    """'2026-27' -> 2027: ESPN's season number is the year the season ends."""
    return int(season[:4]) + 1


class Espn:
    platform = "espn"

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

    def rules(self) -> dict:
        """The roster rules in our terms: starting slots, bench, IR, slot eligibility, lock timing,
        acquisition limit (None = unlimited), waivers, schedule."""
        s = self.settings()
        counts = {SLOT_LABELS[int(k)]: v for k, v in s["rosterSettings"]["lineupSlotCounts"].items() if v}
        acquisition = s["acquisitionSettings"]
        schedule = s["scheduleSettings"]
        return {"teams": s["size"],
                "slots": {k: v for k, v in counts.items() if k not in ("BN", "IR")},
                "slot_positions": {k: SLOT_POSITIONS[k] for k in counts if k in SLOT_POSITIONS},
                "bench": counts.get("BN", 0), "ir": counts.get("IR", 0),
                "max_goalies": s["rosterSettings"]["positionLimits"].get("5"),
                "lineup_lock": s["rosterSettings"]["lineupLocktimeType"],
                "acquisition_limit": None if acquisition["acquisitionLimit"] < 0 else acquisition["acquisitionLimit"],
                "waivers": acquisition["acquisitionType"], "waiver_hours": acquisition["waiverHours"],
                "faab": acquisition["isUsingAcquisitionBudget"],
                "regular_season_weeks": schedule["matchupPeriodCount"],
                "playoff_teams": schedule["playoffTeamCount"],
                "scoring_type": s["scoringSettings"]["scoringType"]}

    def scoring(self) -> dict:
        """{"skaters": {...}, "goalies": {...}, "unsupported": {stat: points}} -- the league's points
        per stat in Settings/scoring terms, and what our projections cannot score."""
        out = {"skaters": {}, "goalies": {}, "unsupported": {}}
        for item in self.settings()["scoringSettings"]["scoringItems"]:
            points = float(item["points"])
            if points == 0.0:
                continue
            key = STAT_KEYS.get(item["statId"])
            if key is None:
                out["unsupported"][STAT_NAMES.get(item["statId"], f"stat {item['statId']}")] = points
            else:
                out["goalies" if key in GOALIE_KEYS else "skaters"][key] = points
        return out

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

    def moves_used(self, team_id: int, day=None) -> int:
        if self.rules()["acquisition_limit"] is None:
            return 0          # no limit in this league: nothing to count against
        raise NotImplementedError("ESPN transactions are not readable yet (see the module docstring); "
                                  "a league with an acquisition limit needs them")

    # ---------- the draft ----------

    def draft_board(self) -> dict:
        """The draft in the shape Fleaflicker's FetchLeagueDraftBoard returns (what the draft tools
        parse): every pick slot in order, the player once picked. Names are ours, via PlayerIds."""
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

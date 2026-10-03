"""The live games view's data (Fantasy Hockey Phone App Plan, Phase 5b): tonight's games, the goal
feed and each game's lines, from the NHL's public feeds, held in memory only -- nothing goes to the
database.

    Feeds          one shared copy of each NHL response. A JSON feed is fetched again only once the
                   NHL's cache says its copy expired (Cache-Control max-age less the Age header,
                   about 20 s), and only when someone asks: a game nobody is watching is not
                   fetched. A finished game is fetched once more, then kept.
    Owners         which fantasy team holds each NHL player, from the newest saved plan (your roster,
                   your opponent's) and players.parquet's NHL ids (ModelFeatures/build_players.py)
    games()        today's games (score/now): score, period, clock, your players and your
                   opponent's on each
    goals()        every goal today, newest first, with the fantasy points it earned each owner
    game(id)       one game: line score, shots by period, team stats, box score with fantasy
                   points, the plays after a sortOrder
    lines(id)      each team's forward lines, defence pairs and special-teams units as used, from
                   the NHL's HTML time-on-ice reports (about once a minute) and the play-by-play's
                   who was on the ice each second -- the nightly lineup build's clustering (pipeline
                   nhl_pipeline/calc/lineups.py) over the last 10 minutes of 5v5 and the game so far

Fantasy points follow the league's scoring (Settings/scoring): skaters from the box score, with
power-play and shorthanded points from each goal's strength; goalies' saves and goals against as
they happen, and the win, loss, overtime loss and shutout once the NHL records the decision.
"""

import datetime as dt
import json
import re
import sys
import threading
import time
from collections import defaultdict
from email.utils import formatdate
from pathlib import Path

import pandas as pd
import requests

PIPELINE = Path(__file__).resolve().parents[2] / "pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

from nhl_pipeline.api import html_shift_report  # noqa: E402
from nhl_pipeline.calc import lineups as lineup_calc  # noqa: E402

API = "https://api-web.nhle.com/v1"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
DEFAULT_MAX_AGE_S = 20          # a JSON response without Cache-Control
REPORT_INTERVAL_S = 60          # the HTML TOI reports change about once a minute; never asked faster
FEED_IDLE_S = 3 * 3600          # a feed nobody asked about for this long is dropped
FINAL_STATES = {"OFF", "FINAL"}
LIVE_STATES = {"LIVE", "CRIT"}
# The goal feed's order across games: a goal's wall-clock time, estimated from puck drop -- about
# 8 minutes to the first faceoff, 37 minutes of real time per 20-minute period, 18 between periods.
FIRST_FACEOFF_S, PERIOD_REAL_S, INTERMISSION_S = 8 * 60, 37 * 60, 18 * 60
RECENT_5V5_S = 600              # the lines' recent window: the last 10 minutes of 5v5
MIN_UNIT_S = lineup_calc.MIN_SPECIAL_TEAMS_SECONDS   # shared ice a unit needs before it is shown
PERIOD_S = lineup_calc.PERIOD_SECONDS
FORWARDS = set(lineup_calc.FORWARD_POSITIONS)
STRENGTHS = {"ev": "EV", "pp": "PP", "sh": "SH"}
TEAM_SUFFIX = re.compile(r"\(([A-Z]{2,3})\)\s*$")


def clock(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def mmss(text) -> int:
    minutes, seconds = (text or "0:00").split(":")
    return int(minutes) * 60 + int(seconds)


class Feed:
    """One NHL response, shared by every caller. JSON: fetched again once the NHL's cache copy has
    expired. HTML (the TOI reports): at most once a REPORT_INTERVAL_S, asking with
    If-Modified-Since so an unchanged page costs little."""

    def __init__(self, url, html=False):
        self.url, self.html = url, html
        self.body, self.final = None, False
        self.expires = 0.0
        self.modified = None            # the HTML page's Last-Modified
        self.fetched_at = None
        self.version = 0                # bumped whenever the body changes
        self.used = time.monotonic()
        self.lock = threading.Lock()

    def get(self):
        self.used = time.monotonic()
        with self.lock:
            if self.body is not None and (self.final or time.monotonic() < self.expires):
                return self.body
            try:
                self._fetch()
            except requests.RequestException:
                if self.body is None:
                    raise
                self.expires = time.monotonic() + 5        # keep the last copy, try again soon
            return self.body

    def _fetch(self):
        headers = {"User-Agent": USER_AGENT}
        if self.html and self.modified:
            headers["If-Modified-Since"] = self.modified
        response = requests.get(self.url, headers=headers, timeout=20)
        self.fetched_at = dt.datetime.now()
        if response.status_code == 304:
            self.expires = time.monotonic() + REPORT_INTERVAL_S
            return
        response.raise_for_status()
        if self.html:
            response.encoding = "utf-8"
            body = response.text
            self.modified = response.headers.get("Last-Modified") or formatdate(usegmt=True)
            self.expires = time.monotonic() + REPORT_INTERVAL_S
        else:
            body = response.json()
            match = re.search(r"max-age=(\d+)", response.headers.get("Cache-Control", ""))
            max_age = int(match.group(1)) if match else DEFAULT_MAX_AGE_S
            age = int(response.headers.get("Age", "0") or 0)
            self.expires = time.monotonic() + max(1, max_age - age)
        if body != self.body:
            self.body = body
            self.version += 1


class Owners:
    """Which fantasy team holds each NHL player: yours and your opponent's, from the newest plan."""

    def __init__(self, newest_plan, scoreset):
        self.newest_plan, self.scoreset = newest_plan, scoreset
        self.key, self.by_nhl, self.names, self.teams = None, {}, {}, {}
        self.players_mtime, self.nhl_ids = None, {}

    def _nhl_ids(self, players_path):
        mtime = players_path.stat().st_mtime
        if mtime != self.players_mtime:
            players = pd.read_parquet(players_path, columns=["player_id", "nhl_id"])
            self.nhl_ids = dict(zip(players["player_id"].astype(int), players["nhl_id"].astype(int)))
            self.players_mtime = mtime
        return self.nhl_ids

    def refresh(self, players_path):
        path = self.newest_plan()
        key = (path, path.stat().st_mtime if path else None, players_path.stat().st_mtime)
        if key == self.key:
            return
        self.key, self.by_nhl, self.teams = key, {}, {"me": [], "opp": []}
        self.names = {"me": "you", "opp": "opponent"}
        if path is None:
            return
        plan = json.loads(path.read_text(encoding="utf-8"))
        self.names = {"me": plan.get("team") or "you", "opp": plan.get("opponent") or "opponent"}
        ids = self._nhl_ids(players_path)
        for owner, roster in (("me", plan.get("roster", [])), ("opp", plan.get("opponent_roster", []))):
            for r in roster:
                nhl = ids.get(int(r["player_id"])) if r.get("player_id") is not None else None
                if nhl is not None:
                    self.by_nhl[nhl] = owner
                match = TEAM_SUFFIX.search(r.get("player", ""))
                if match:
                    self.teams[owner].append(match.group(1))

    def of(self, nhl_id):
        return self.by_nhl.get(nhl_id)

    def count(self, owner, team_abbrevs) -> int:
        return sum(1 for t in self.teams.get(owner, []) if t in team_abbrevs)


class Games:
    """The live view's feeds and what is computed from them, for one league."""

    def __init__(self, newest_plan, scoreset, players_path):
        self.scoreset, self.players_path = scoreset, players_path
        self.owners = Owners(newest_plan, scoreset)
        self.feeds: dict[str, Feed] = {}
        self.lock = threading.Lock()
        self.lines_cache = {}           # game id -> (feed versions, result)

    # ---------- feeds ----------

    def feed(self, url, html=False) -> Feed:
        with self.lock:
            now = time.monotonic()
            for stale in [u for u, f in self.feeds.items() if now - f.used > FEED_IDLE_S]:
                del self.feeds[stale]
            if url not in self.feeds:
                self.feeds[url] = Feed(url, html)
            return self.feeds[url]

    def score_now(self) -> dict:
        return self.feed(f"{API}/score/now").get()

    def game_feed(self, game_id, part) -> dict:
        feed = self.feed(f"{API}/gamecenter/{game_id}/{part}")
        body = feed.get()
        if body.get("gameState") in FINAL_STATES or self._is_final(game_id):
            feed.final = True               # this copy was fetched after the end: keep it
        return body

    def _is_final(self, game_id) -> bool:
        game = self._score_game(game_id)
        return game is not None and game.get("gameState") in FINAL_STATES

    def _score_game(self, game_id):
        return next((g for g in self.score_now().get("games", []) if g["id"] == game_id), None)

    def _owners(self):
        self.owners.refresh(self.players_path)
        return self.owners

    # ---------- fantasy points ----------

    def _points(self, stats: dict, goalie: bool) -> float:
        weights = self.scoreset.goalies if goalie else self.scoreset.skaters
        return round(sum(w * stats.get(k, 0) for k, w in weights.items()), 2)

    def _special_points(self, goals) -> dict:
        """{nhl_id: {"ppp": n, "shp": n}} from goals' strengths (the box score has neither)."""
        counts = defaultdict(lambda: {"ppp": 0, "shp": 0})
        for g in goals:
            key = {"pp": "ppp", "sh": "shp"}.get(g.get("strength"))
            if key:
                for player in [g["playerId"]] + [a["playerId"] for a in g.get("assists", [])]:
                    counts[player][key] += 1
        return counts

    # ---------- games list and goal feed ----------

    def games(self) -> dict:
        data, owners = self.score_now(), self._owners()
        rows = []
        for g in data.get("games", []):
            away, home = g["awayTeam"], g["homeTeam"]
            teams = {away["abbrev"], home["abbrev"]}
            rows.append({
                "id": g["id"], "state": g["gameState"], "start_utc": g["startTimeUTC"],
                "away": {k: away.get(k) for k in ("abbrev", "score", "sog")},
                "home": {k: home.get(k) for k in ("abbrev", "score", "sog")},
                "period": (g.get("periodDescriptor") or {}).get("number"),
                "period_type": (g.get("periodDescriptor") or {}).get("periodType"),
                "clock": (g.get("clock") or {}).get("timeRemaining"),
                "intermission": bool((g.get("clock") or {}).get("inIntermission")),
                "mine": owners.count("me", teams), "opp": owners.count("opp", teams),
            })
        return {"date": data.get("currentDate"), "team": owners.names["me"],
                "opponent": owners.names["opp"], "games": rows}

    def goals(self) -> dict:
        data, owners = self.score_now(), self._owners()
        weights = self.scoreset.skaters
        rows, totals = [], {"me": 0.0, "opp": 0.0}
        for g in data.get("games", []):
            away, home = g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"]
            for n, goal in enumerate(g.get("goals", [])):
                strength = goal.get("strength", "ev")
                bonus = weights.get({"pp": "ppp", "sh": "shp"}.get(strength, ""), 0)

                def person(player_id, name, to_date, base):
                    owner = owners.of(player_id)
                    points = round(weights.get(base, 0) + bonus, 2) if owner else None
                    if owner:
                        totals[owner] += points
                    return {"id": player_id, "name": name, "to_date": to_date, "owner": owner,
                            "points": points}

                period = goal.get("periodDescriptor") or {}
                start = dt.datetime.fromisoformat(g["startTimeUTC"].replace("Z", "+00:00")).timestamp()
                number = period.get("number", 1)
                rows.append({
                    "game_id": g["id"],
                    "order": (start + FIRST_FACEOFF_S + (number - 1) * (PERIOD_REAL_S + INTERMISSION_S)
                              + mmss(goal.get("timeInPeriod")) * PERIOD_REAL_S / PERIOD_S, n),
                    "period": period.get("number"), "period_type": period.get("periodType"),
                    "time": goal.get("timeInPeriod"), "team": goal.get("teamAbbrev"),
                    "scorer": person(goal["playerId"], goal["name"]["default"], goal.get("goalsToDate"), "goals"),
                    "assists": [person(a["playerId"], a["name"]["default"], a.get("assistsToDate"), "assists")
                                for a in goal.get("assists", [])],
                    "strength": STRENGTHS.get(strength, strength.upper()),
                    "modifier": None if goal.get("goalModifier") in (None, "none") else goal["goalModifier"],
                    "score": f"{away} {goal.get('awayScore')}-{goal.get('homeScore')} {home}",
                    "clip": goal.get("highlightClipSharingUrl"),
                })
        # Newest first, by each goal's estimated wall-clock time (game clocks alone don't compare
        # across games); within a game the NHL's own order breaks a tie.
        rows.sort(key=lambda r: r["order"], reverse=True)
        for r in rows:
            del r["order"]
        return {"team": owners.names["me"], "opponent": owners.names["opp"],
                "totals": {k: round(v, 2) for k, v in totals.items()}, "goals": rows}

    # ---------- one game ----------

    def game(self, game_id: int, after: int = -1) -> dict:
        owners = self._owners()
        summary = self._score_game(game_id) or {}
        box = self.game_feed(game_id, "boxscore")
        rail = self.game_feed(game_id, "right-rail")
        pbp = self.game_feed(game_id, "play-by-play")
        special = self._special_points(summary.get("goals", []))
        sides = {}
        for side in ("awayTeam", "homeTeam"):
            team = box[side]["abbrev"]
            stats = box.get("playerByGameStats", {}).get(side, {})
            players = []
            for group in ("forwards", "defense"):
                for p in stats.get(group, []):
                    counts = {"goals": p.get("goals", 0), "assists": p.get("assists", 0),
                              "points": p.get("points", 0), "shots": p.get("sog", 0),
                              "hits": p.get("hits", 0), "blocks": p.get("blockedShots", 0),
                              "pim": p.get("pim", 0), "plus_minus": p.get("plusMinus", 0),
                              **special.get(p["playerId"], {"ppp": 0, "shp": 0})}
                    players.append({"id": p["playerId"], "name": p["name"]["default"], "team": team,
                                    "number": p.get("sweaterNumber"), "position": p.get("position"),
                                    "toi": p.get("toi"), "owner": owners.of(p["playerId"]),
                                    "fantasy": self._points(counts, False), **counts})
            for p in stats.get("goalies", []):
                decision = p.get("decision")
                counts = {"saves": p.get("saves", 0), "goals_against": p.get("goalsAgainst", 0),
                          "shots_against": p.get("shotsAgainst", 0),
                          "wins": int(decision == "W"), "losses": int(decision == "L"),
                          "ot_losses": int(decision == "O"),
                          "shutouts": int(decision == "W" and p.get("goalsAgainst", 0) == 0)}
                players.append({"id": p["playerId"], "name": p["name"]["default"], "team": team,
                                "number": p.get("sweaterNumber"), "position": "G", "toi": p.get("toi"),
                                "owner": owners.of(p["playerId"]), "decision": decision,
                                "fantasy": self._points(counts, True), **counts})
            sides[side] = {"abbrev": team, "score": box[side].get("score"), "sog": box[side].get("sog"),
                           "players": players}
        names = {s["playerId"]: f"{s['firstName']['default'][:1]}. {s['lastName']['default']}"
                 for s in pbp.get("rosterSpots", [])}
        abbrev = {pbp["awayTeam"]["id"]: pbp["awayTeam"]["abbrev"], pbp["homeTeam"]["id"]: pbp["homeTeam"]["abbrev"]}
        plays = [self._play(p, names, abbrev, owners) for p in pbp.get("plays", [])]
        return {
            "id": game_id, "state": box.get("gameState"),
            "period": (box.get("periodDescriptor") or {}).get("number"),
            "period_type": (box.get("periodDescriptor") or {}).get("periodType"),
            "clock": (box.get("clock") or {}).get("timeRemaining"),
            "intermission": bool((box.get("clock") or {}).get("inIntermission")),
            "away": sides["awayTeam"], "home": sides["homeTeam"],
            "linescore": (rail.get("linescore") or {}).get("byPeriod", []),
            "shots_by_period": rail.get("shotsByPeriod", []),
            "team_stats": rail.get("teamGameStats", []),
            "plays": [p for p in reversed(plays) if p["sort"] > after],
            "last_sort": max((p["sort"] for p in plays), default=-1),
            "team": owners.names["me"], "opponent": owners.names["opp"],
        }

    @staticmethod
    def _play(p, names, abbrev, owners) -> dict:
        d, kind = p.get("details") or {}, p.get("typeDescKey", "")
        who = lambda key: names.get(d.get(key), "") if d.get(key) else ""
        if kind == "goal":
            helpers = ", ".join(n for n in (who("assist1PlayerId"), who("assist2PlayerId")) if n)
            text = f"{who('scoringPlayerId')}" + (f" ({helpers})" if helpers else " (unassisted)")
        elif kind in ("shot-on-goal", "missed-shot"):
            text = who("shootingPlayerId") + (f", {d['shotType']}" if d.get("shotType") else "")
        elif kind == "blocked-shot":
            text = f"{who('blockingPlayerId')} blocks {who('shootingPlayerId')}"
        elif kind == "hit":
            text = f"{who('hittingPlayerId')} on {who('hitteePlayerId')}"
        elif kind == "faceoff":
            text = f"{who('winningPlayerId')} beats {who('losingPlayerId')}"
        elif kind == "penalty":
            text = (f"{who('committedByPlayerId') or 'bench'}: {d.get('descKey', '').replace('-', ' ')}, "
                    f"{d.get('duration', '')} min")
        elif kind in ("giveaway", "takeaway"):
            text = who("playerId")
        elif kind == "stoppage":
            text = (d.get("reason") or "").replace("-", " ")
        else:
            text = ""
        involved = [d[k] for k in ("scoringPlayerId", "assist1PlayerId", "assist2PlayerId", "shootingPlayerId",
                                   "blockingPlayerId", "hittingPlayerId", "hitteePlayerId", "winningPlayerId",
                                   "losingPlayerId", "committedByPlayerId", "playerId") if d.get(k)]
        marks = sorted({o for o in (owners.of(i) for i in involved) if o})
        period = p.get("periodDescriptor") or {}
        return {"sort": p.get("sortOrder", 0), "period": period.get("number"),
                "period_type": period.get("periodType"), "time": p.get("timeInPeriod"), "type": kind,
                "team": abbrev.get(d.get("eventOwnerTeamId"), ""), "text": text, "owners": marks}

    # ---------- lines ----------

    def lines(self, game_id: int) -> dict:
        pbp_feed = self.feed(f"{API}/gamecenter/{game_id}/play-by-play")
        pbp = self.game_feed(game_id, "play-by-play")
        if pbp.get("gameState") not in LIVE_STATES | FINAL_STATES:
            return {"id": game_id, "state": pbp.get("gameState"), "teams": [],
                    "note": "the game has not started"}
        reports = [self.feed(html_shift_report.report_url(game_id, home), html=True) for home in (False, True)]
        bodies = []
        for report in reports:
            try:
                bodies.append(report.get())
            except requests.HTTPError as error:     # not posted yet, early in a game
                return {"id": game_id, "state": pbp.get("gameState"), "teams": [],
                        "note": f"time-on-ice report not available yet ({error.response.status_code})"}
        if pbp.get("gameState") in FINAL_STATES:
            for report in reports:
                report.final = True
        versions = (pbp_feed.version, *(r.version for r in reports))
        cached = self.lines_cache.get(game_id)
        owners = self._owners()
        if cached and cached[0] == versions and cached[2] == owners.key:
            return cached[1]
        result = compute_lines(game_id, pbp, bodies, owners)
        result["reports_modified"] = [r.modified for r in reports]
        self.lines_cache[game_id] = (versions, result, owners.key)
        return result


def compute_lines(game_id, pbp, report_bodies, owners) -> dict:
    """Both teams' units from the TOI reports (away, home) and the play-by-play."""
    away_id, home_id = pbp["awayTeam"]["id"], pbp["homeTeam"]["id"]
    abbrev = {away_id: pbp["awayTeam"]["abbrev"], home_id: pbp["homeTeam"]["abbrev"]}
    spots = pbp.get("rosterSpots", [])
    by_sweater = {(s["teamId"], int(s["sweaterNumber"])): s["playerId"] for s in spots if s.get("sweaterNumber") is not None}
    position = {s["playerId"]: s.get("positionCode") for s in spots}
    names = {s["playerId"]: f"{s['firstName']['default'][:1]}. {s['lastName']['default']}" for s in spots}

    shifts = []
    for team_id, body in ((away_id, report_bodies[0]), (home_id, report_bodies[1])):
        for row in html_shift_report.parse_report(body):
            player = by_sweater.get((team_id, row["sweater_number"]))
            if player is not None and row["period"] != lineup_calc.SHOOTOUT_PERIOD:
                start = (row["period"] - 1) * PERIOD_S
                shifts.append((team_id, player, start + row["start_seconds"], start + row["end_seconds"]))
    total = max((end for *_, end in shifts), default=0)
    if total == 0:
        return {"id": game_id, "state": pbp.get("gameState"), "teams": [], "note": "no shifts reported yet"}

    on_ice = {t: [set() for _ in range(total)] for t in (away_id, home_id)}
    for team_id, player, start, end in shifts:
        for sec in range(start, min(end, total)):
            on_ice[team_id][sec].add(player)

    # Strength each second from who the reports put on the ice, not from the play-by-play's
    # situation codes: live, those lag and misreport (2026-10-02, BOS-WPG: a power play coded as
    # both goalies pulled, and a power-play code back after the penalty had expired). A line-change
    # overlap can show a sixth skater for a moment, so 5v5 asks for at least five a side with both
    # goalies in, and a power play for an opponent with its goalie in and four or fewer skaters.
    # A goalie's shift is written only when it ends (usually at the period's end), so after the
    # last written one he is taken to be in net; before it, a gap is a pulled goalie.
    def counts(team):
        written = max((end for t, p, _, end in shifts if t == team and position.get(p) == "G"), default=0)
        skaters, goalie = [0] * total, [sec >= written for sec in range(total)]
        for sec, players in enumerate(on_ice[team]):
            for p in players:
                if position.get(p) == "G":
                    goalie[sec] = True
                else:
                    skaters[sec] += 1
        return skaters, goalie

    skaters, goalie = {}, {}
    for team in (away_id, home_id):
        skaters[team], goalie[team] = counts(team)
    # The reports are rewritten during the game and can stop short of their longest shift: on
    # 2026-10-02 (BOS-WPG) a few shifts ran to 19:59 of the 2nd while nobody was on the ice after
    # 16:05. The game so far ends at the last second both teams have skaters on the ice.
    covered = max((sec for sec in range(total) if skaters[away_id][sec] >= 3 and skaters[home_id][sec] >= 3),
                  default=-1) + 1
    if covered == 0:
        return {"id": game_id, "state": pbp.get("gameState"), "teams": [], "note": "no shifts reported yet"}
    total = covered
    for team in (away_id, home_id):
        on_ice[team], skaters[team], goalie[team] = on_ice[team][:total], skaters[team][:total], goalie[team][:total]
    # Play the reports leave out before that (2026-10-02, BOS-WPG: 16:05-19:21 of the 2nd, for
    # everyone): counted as no strength at all, and said so.
    missing = sum(1 for sec in range(total) if skaters[away_id][sec] < 3 or skaters[home_id][sec] < 3)
    strength = {"5v5": [goalie[away_id][s] and goalie[home_id][s] and skaters[away_id][s] >= 5
                        and skaters[home_id][s] >= 5 for s in range(total)],
                "pp": {team: [goalie[other][s] and skaters[other][s] <= 4 and skaters[team][s] > skaters[other][s]
                              for s in range(total)]
                       for team, other in ((away_id, home_id), (home_id, away_id))}}
    ev = [sec for sec in range(total) if strength["5v5"][sec]]
    windows = {"recent": ev[-RECENT_5V5_S:], "game": ev}
    last = total - 1
    as_of = f"P{last // PERIOD_S + 1} {clock(last % PERIOD_S)}"

    def player(p, toi):
        return {"id": p, "name": names.get(p, str(p)), "position": position.get(p), "toi": toi.get(p, 0),
                "owner": owners.of(p)}

    def units(team, seconds, eligible, size, limit):
        pair, toi = lineup_calc._shared_seconds(on_ice[team], seconds, eligible)
        clusters = [u for u in lineup_calc._cluster(pair, toi, size) if len(u) >= 2][:limit]
        out = []
        for rank, unit in enumerate(clusters, 1):
            together = sum(1 for sec in seconds if unit <= on_ice[team][sec])
            members = sorted(unit, key=lambda p: (position.get(p) not in ("C",), position.get(p) == "D", -toi.get(p, 0)))
            out.append({"rank": rank, "shared": together, "thin": together < MIN_UNIT_S,
                        "players": [player(p, toi) for p in members]})
        return out

    teams = []
    for team in (away_id, home_id):
        other = home_id if team == away_id else away_id
        dressed = {p for secs in on_ice[team] for p in secs}
        forwards = {p for p in dressed if position.get(p) in FORWARDS}
        defence = {p for p in dressed if position.get(p) == "D"}
        skaters = forwards | defence
        entry = {"team": abbrev[team], "windows": {}}
        for name, seconds in windows.items():
            entry["windows"][name] = {
                "seconds": len(seconds),
                "forwards": units(team, seconds, forwards, 3, lineup_calc.MAX_FORWARD_LINES),
                "defence": units(team, seconds, defence, 2, lineup_calc.MAX_DEFENSE_PAIRS),
            }
        pp = [sec for sec in range(total) if strength["pp"][team][sec]]
        sh = [sec for sec in range(total) if strength["pp"][other][sec]]
        entry["pp_seconds"], entry["sh_seconds"] = len(pp), len(sh)
        entry["pp"] = units(team, pp, skaters, 5, lineup_calc.MAX_SPECIAL_TEAMS_UNITS) if len(pp) >= MIN_UNIT_S else []
        entry["pk"] = units(team, sh, skaters, 4, lineup_calc.MAX_SPECIAL_TEAMS_UNITS) if len(sh) >= MIN_UNIT_S else []
        teams.append(entry)
    return {"id": game_id, "state": pbp.get("gameState"), "as_of": as_of, "missing_seconds": missing,
            "teams": teams}

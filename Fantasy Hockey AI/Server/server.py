#!/usr/bin/env python
"""The phone app's server (Fantasy Hockey Phone App Plan, Phase 1.5): the plan refreshes behind a
small HTTP API, which the app (app/) shows, one job at a time. Open to anyone since 2026-11-15:
a browser adds its own leagues (POST /leagues) and its cookie (auth.py) is all that ties them to
it (users.py); every user is treated the same.

    python server.py                                   # the users' leagues, today, 127.0.0.1:8000
    python server.py --host 0.0.0.0                    # in the container (compose publishes 8000)
    python server.py --league beagles --date 2026-09-29 \
        --league-file fixtures/beagles/fake_league.json --skip-snapshots --no-daily   # rehearsal

Every league call takes ?league=<name> (default: the caller's first league) and answers 404 for a
league the caller's cookie doesn't link -- with {"add": true} when it links none:

    GET  /plan                 the league's newest plan for the plan's day -- today, or tomorrow once
                               every game today has started: {league, file, saved_at, plan}
    POST /refresh {mode}       the ?league= league refreshed, mode (planpass.Planner.run): 'full'
                               (reports with line charts, projections, league read again), 'quick'
                               (the same without line charts), 'news' (reports and projections,
                               planned on the kept read), 'league' (read again, no reports), 'plan'
                               (your choices changed: planned on the kept read, about 2 s). Fresh
                               reports are reused (planpass.FRESH_FOR). Returns the job; asked for
                               while one runs, the queued job it joined. 429 within TAP_LEAGUE_S of
                               the league's last tap or TAP_USER_S of the user's ('plan' excepted)
    POST /leagues/preview {url, espn_s2?, swid?}   a league's teams and settings from its link
    POST /leagues {url, team_id, espn_s2?, swid?}  your team added (and this browser's cookie made)
    GET  /leagues, DELETE /leagues/{name}          your leagues; one unlinked (deleted once nobody has it)
    POST /forget               this browser's cookie dropped
    GET  /jobs/{id}?after=n    the job's state and its progress lines after line n
    GET  /jobs                 the recent jobs, newest first, without their lines (the app's refresh log)
    GET  /status               the leagues served (each with its team), the day, the running job, when
                               each step last ran (the league's own read included), the league's newest plan
    GET  /games?date=          today's games (or a day this week's): score, clock, your players and
                               your opponent's on each
    GET  /games/week           each day of this fantasy week with its NHL games (the Games day picker)
    GET  /goals                every goal today, newest first, with the fantasy points it earned
    GET  /games/{id}?after=n   one game: its goals, team stats, shots by period, box score with fantasy
                               points, plays after sortOrder n
    GET  /games/{id}/lines     each team's lines, pairs and special-teams units as used (games.py)
    GET  /tonight              every player you and your opponent hold: his game today, his live box
                               line and fantasy points; your lineup's live total (games.py)
    GET  /roster               the app's Roster screen from the newest plan: lineup as it stands,
                               bench, IR, each player's season in the league's scoring (roster.py).
    GET  /players              the app's Players screen from the newest plan: every free agent, his
                               season, ROS, waivers and the nights he plays the rest of this week (players.py)
    GET  /choices              what you chose in the app (choices.py): {league,
                               choices: {days: {date: {moves}}, together}}
    PUT  /choices {choices}    replaces them; returns {league, choices}. Nothing is re-planned
                               until a refresh (or a 'plan')
    POST /choices/together {on, week}   the week plans planned after today's upgrades (true) or apart
                               (false); saved (choices.py), re-planned once the app asks for a 'plan'
    GET  /suggestions?week=    the app's AI suggestions from the newest plan: today's moves, the
                               upgrades, the week plans ('this' or 'next') with your picks marked
    POST /choices/pick {week, label, index, player_id}   a week plan's option picked into your week
                               with the plan's drop, or taken back (choices.pick_from_plan); returns
                               the suggestions as they now stand
    GET  /week?week=&show=     the app's Plan tab: your week from your picks on plan A's
                               workbench (week.py) -- each night's points, open slots, picks, slots;
                               show=<label>: that week plan's week instead, its picks not yours marked
    GET  /week/picker?day=&slot=&occupant=   the slot picker's free agents at the slot's positions
                               (plan picks first); occupant: the rental or rostered player whose
                               slot it is -- then priced on the swap
    GET  /week/drops?day=&add=&occupant=   who that pick may drop, each with the week's gain
    POST /week/edit {week, action, day, add, drop, rental, label, index, show}   pick, replace,
                               unpick, clear, take (one of a plan's picks) or use (the whole plan);
                               saved (choices.py), returns the calendar as it now stands
    GET  /matchup              the app's Matchup: the week's score, each day's points for you and your
                               opponent, the league's week, from the platform (kept a minute), with
                               the plan's projection and P(win) (matchup.py)
    GET  /matchup/players      the Matchup's head-to-head: every player either side held this week,
                               each day's slot (the platform's), projection, game and points (headtohead.py);
                               live_win: the plan's P(win) carried through tonight's games (livewin.py)
    GET  /app/                 the phone app (Server/app/: static files, it calls the API above)

A refresh is planpass.Planner's: the snapshots and tonight's projections once, then each league's
read, plan and saved plan files in a child process that exits after -- so the server's memory does
not grow with the leagues it plans -- and one league's failure (its platform down) leaves the
others' plans. The daily pass runs here (DAILY_JOB): a Full refresh of every league a user came by in
the last 14 days, DAILY_LEAD before the day's first game and no later than DAILY_FALLBACK, then the
cleanup of users and leagues long gone (users.cleanup); beyond it a league is planned on its users'
taps. (Until 2026-11-15 an auto window re-planned every league before each group of games.) Once
today's last game has started the plan is tomorrow's (moves, lineup and projections), since tonight
has no lineup left to set. The live games' NHL feeds are fetched once for all leagues; each league
sees them with its own rosters and scoring.
"""

import argparse
import concurrent.futures
import datetime as dt
import itertools
import json
import re
import sys
import threading
import time
from pathlib import Path

import pandas as pd  # noqa: E402
import requests  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException, Request, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import auth  # noqa: E402
import users  # noqa: E402
import corelayer  # noqa: E402,F401 -- puts Core/ on sys.path; see corelayer.py
import choices  # noqa: E402
import games as live_games  # noqa: E402
import headtohead  # noqa: E402
import league as league_module  # noqa: E402
import leagues  # noqa: E402
import livepaths  # noqa: E402
import matchup as matchup_module  # noqa: E402
import livewin  # noqa: E402
import roster  # noqa: E402
import suggestions  # noqa: E402
import week as week_module  # noqa: E402
import paths  # noqa: E402
import league_secrets  # noqa: E402
import planpass  # noqa: E402
import platforms  # noqa: E402
import platforms.espn  # noqa: E402,F401
import platforms.fleaflicker  # noqa: E402,F401
import players as players_module  # noqa: E402
import scheduled  # noqa: E402
import simlayer  # noqa: E402
import sync_league_settings  # noqa: E402

APP = Path(__file__).resolve().parent / "app"     # the phone app's static files, served at /app/
JOBS_CHECK_S = 60               # how often the jobs loop looks at the clock
JOBS_KEPT = 20                  # finished jobs /jobs still answers for
DAILY_JOB = "daily_plans"       # the daily pass: every league planned once a day (Worker._daily)
DAILY_LEAD = dt.timedelta(minutes=30)   # ... this long before the day's first game,
DAILY_FALLBACK = dt.time(15, 30)        # ... and no later than this (local)
TAP_LEAGUE_S = 5 * 60           # one refresh per league per 5 min, whoever asks
TAP_USER_S = 2 * 60             # one per user per 2 min, across its leagues
LEAGUES_PER_USER = 3
LEAGUES_MAX = 40                # leagues the server plans, all users together
ADD_TRIES_PER_HOUR = 5          # add-league checks from one address (each reads a platform)
PREVIEW_KEEP_S = 10 * 60        # a league looked up is kept this long for the add that follows
NHL_API = "https://api-web.nhle.com/v1"
_first_pucks: dict = {}         # date -> its first puck (local, naive) or None


def first_puck(day: dt.date):
    """The day's first NHL game's start, local time (naive), from the NHL's schedule; None
    when it has none or can't be read (read again next time)."""
    if day in _first_pucks:
        return _first_pucks[day]
    try:
        week = requests.get(f"{NHL_API}/schedule/{day.isoformat()}", timeout=20).json()["gameWeek"]
    except (requests.RequestException, ValueError, KeyError):
        return None
    starts = [g["startTimeUTC"] for d in week if d.get("date") == day.isoformat() for g in d.get("games", [])
              if g.get("gameType") in (2, 3) and g.get("startTimeUTC")]
    first = None
    if starts:
        first = dt.datetime.fromisoformat(min(starts).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    _first_pucks[day] = first
    return first


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def iso(when) -> str | None:
    return None if when is None else when.isoformat(timespec="seconds")


class Job:
    def __init__(self, number: int, mode: str, by: str, only=None, state: str = "running"):
        self.id, self.mode, self.by = str(number), mode, by
        self.state = state              # queued -> running -> done | failed
        self.lines: list[str] = []
        self.started, self.finished = (dt.datetime.now() if state == "running" else None), None
        self.error = None
        self.plan_files = {}            # league -> the plan file this job saved for it
        self.league_errors = {}         # league -> why it failed (shown only to its users)
        self.only = list(only or [])    # the leagues it plans (none: a scheduled data job)
        self.fresh = False              # every report fetched anew (the daily pass, an added league)

    def echo(self, text):
        self.lines.append(f"{dt.datetime.now():%H:%M:%S}  {text}")

    def view(self, after=0, mine=None) -> dict:
        """`mine`: the leagues the caller links. A job for none of them is shown without its
        lines, its leagues or its files -- another user's leagues are not this caller's to see."""
        shown = mine is None or not self.only or bool(set(self.only) & set(mine))
        lines = self.lines if shown else []
        errors = self.league_errors
        if shown and mine is not None and self.only:
            # A line about another user's league -- one that starts with its name: "fl-123-4: plan:
            # ...", "fl-123-4 settings: ..." -- is left out, and so is its error.
            others = {name for name in self.only if name not in mine}
            lines = [line for line in lines if line_league(line) not in others]
            errors = {name: e for name, e in errors.items() if name not in others}
        error = "; ".join([*([self.error] if self.error else []),
                           *(f"{name}: {e}" for name, e in errors.items())]) or None
        return {"id": self.id, "mode": self.mode,
                "by": self.by if shown else "another user's refresh", "state": self.state,
                "started": iso(self.started), "finished": iso(self.finished),
                "error": error if shown else None,
                "plan_files": {k: v for k, v in self.plan_files.items() if mine is None or k in mine},
                "lines": lines[after:], "next": len(lines), "mine": shown}


def line_league(line: str) -> str:
    """The league a progress line is about: its first word after the time, up to ':' or ' '
    ("21:32:01  fl-123-4: plan: ..." -> "fl-123-4")."""
    text = line.split("  ", 1)[1] if "  " in line else line
    return re.split(r"[: ]", text, maxsplit=1)[0]


# How a refresh tapped while another is waiting merges with it: the bigger refresh, for both
# sets of leagues (news + league together is quick: the reports and a fresh read).
MODE_RANK = {"plan": 0, "league": 1, "news": 2, "quick": 3, "full": 4}


def merged_mode(a: str, b: str) -> str:
    if {a, b} == {"news", "league"}:
        return "quick"
    return a if MODE_RANK[a] >= MODE_RANK[b] else b


class Worker:
    """Runs one job at a time on its own thread -- a refresh of some users' leagues, or a scheduled
    job -- and keeps the day's Planner (when each step last ran) and the recent jobs. A refresh
    asked for while one runs waits as one queued job, which every later ask joins (its leagues
    added). The leagues are planned in a child process per refresh (planpass.plan_in_child), so
    this process stays small.

    Every user is treated the same (the user, 2026-11-15): a league is served to the users whose
    cookie links it (users.py), and planned in the daily pass and on its users' taps."""

    def __init__(self, args):
        self.args = args
        # --league: leagues every caller sees, cookie or not -- a rehearsal's (--league-file) or a
        # test server's; none on the real server, where a caller sees the leagues it added.
        self.shown_to_all = list(args.league or [])
        self.cache: dict = {}           # league name -> leagues.League, loaded on first use
        self.caches: list = []          # the app's per-league objects (make_app's PerLeague)
        self.planner = None
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None
        self.queued: Job | None = None  # the refresh waiting for the running job
        self.numbers = itertools.count(1)
        self.lock = threading.Lock()
        self.tapped: dict = {}          # league name -> when its users last asked for a refresh
        self.tapped_by: dict = {}       # user id -> when it last asked
        self.added_by_ip: dict = {}     # address -> the times it added a league (ADD_TRIES_PER_HOUR)
        self.scheduled_state = scheduled.load_state()   # job -> the scheduled time it last ran for

    # ---------- leagues and who sees them ----------

    def loaded(self, name: str):
        league = self.cache.get(name)
        if league is None:
            league = self.cache[name] = leagues.load(name)
        return league

    def forget(self, name: str | None = None):
        """A league's loaded objects dropped (None: every league's) -- it was deleted, or its
        settings may have changed (the daily pass re-detects them)."""
        for cache in [self.cache, *self.caches]:
            if name is None:
                cache.clear()
            else:
                cache.pop(name, None)

    def mine(self) -> list:
        """The leagues this call's caller sees: those its cookie links, and --league's."""
        names = auth.leagues_of(auth.viewer.get())
        return list(dict.fromkeys(names + self.shown_to_all))

    def league(self, name=None):
        """A league the caller sees, by name (None: its first); a 404 for any other -- the same
        as for no such league, so another user's league names nothing. A caller with no league
        gets a 404 saying so (`add`: the app shows its add-league screen)."""
        mine = self.mine()
        if not mine:
            raise HTTPException(404, {"detail": "no league yet: add one", "add": True})
        if name is None:
            return self.loaded(mine[0])
        if name not in mine:
            raise HTTPException(404, f"no league {name} here")
        return self.loaded(name)

    def daily_leagues(self) -> list:
        """The leagues the daily pass plans: every league a user came by in the last
        users.ACTIVE_DAYS days, and --league's."""
        names = list(dict.fromkeys(users.active_leagues(self.today()) + self.shown_to_all))
        out = []
        for name in names:
            try:
                out.append(self.loaded(name))
            except (SystemExit, ValueError) as error:
                print(f"daily pass: {name} not loaded ({error})", file=sys.stderr)
        return out

    # ---------- the clock ----------

    def today(self) -> dt.date:
        return dt.date.fromisoformat(self.args.date) if self.args.date else dt.date.today()

    def day(self) -> dt.date:
        """The day the plan is for: today, or tomorrow once every game today has started -- no
        lineup is left to set tonight, so the moves shown are tomorrow's (the user, 2026-10-05).
        Today's puck times come from its tonight build, so before a refresh has made one, today."""
        today = self.today()
        pucks = planpass.puck_times(today)
        return today + dt.timedelta(days=1) if pucks and pucks[-1] <= pd.Timestamp(self.now()) else today

    def now(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.args.now) if self.args.now else utc_now()

    # ---------- taps ----------

    def tap_wait(self, uid, names) -> int:
        """Seconds before this user may refresh these leagues again (0: now): one refresh per
        league per TAP_LEAGUE_S, one per user per TAP_USER_S, whoever's league it is."""
        now = time.time()
        waits = [self.tapped.get(name, 0) + TAP_LEAGUE_S - now for name in names]
        if uid is not None:
            waits.append(self.tapped_by.get(uid, 0) + TAP_USER_S - now)
        return max(0, int(max(waits, default=0)) + 1) if max(waits, default=0) > 0 else 0

    def tapped_now(self, uid, names):
        now = time.time()
        for name in names:
            self.tapped[name] = now
        if uid is not None:
            self.tapped_by[uid] = now

    # ---------- jobs ----------

    def start(self, mode: str, by: str = "tap", only=None, fresh: bool = False) -> Job:
        """A refresh of the leagues `only`, or a scheduled job (`only` None). Asked for while a job
        runs, a refresh waits as the one queued job, which later asks join: the bigger refresh
        (merged_mode) for every league asked for. A scheduled job asked for while one runs is the
        jobs loop's to start again next minute (the daily pass too); it returns the running one.
        `fresh`: every report fetched anew rather than reused while fresh (the daily pass, an
        added league's first refresh; a tap reuses them)."""
        only = list(only or [])
        with self.lock:
            if self.current is not None:
                if mode in scheduled.JOBS or mode == DAILY_JOB:
                    return self.current
                if self.queued is None:
                    self.queued = self._new_job(mode, by, only, state="queued")
                    self.queued.fresh = fresh
                else:
                    q = self.queued
                    q.mode = merged_mode(q.mode, mode)
                    q.only = list(dict.fromkeys(q.only + only))
                    q.fresh = q.fresh or fresh
                    if by not in q.by:
                        q.by = f"{q.by}; {by}"
                return self.queued
            job = self.current = self._new_job(mode, by, only)
            job.fresh = fresh
        threading.Thread(target=self._work, args=(job,), daemon=True).start()
        return job

    def _new_job(self, mode, by, only, state="running") -> Job:
        job = Job(next(self.numbers), mode, by, only, state)
        self.jobs[job.id] = job
        for old in list(self.jobs)[:-JOBS_KEPT]:
            if self.jobs[old] not in (self.current, self.queued, job):
                del self.jobs[old]
        return job

    def _work(self, job: Job):
        try:
            day = self.day()
            if self.planner is None or self.planner.day != day:
                a = self.args
                self.planner = planpass.Planner([], day, a.league_file, a.platform_season, a.skip_snapshots)
            if job.mode in scheduled.JOBS:
                scheduled.run(job.mode, self.today(), job.echo, self.args.jobs_db)
                job.state = "done"
                return
            if job.mode == DAILY_JOB:
                self._daily(job)
                return
            chosen = []
            for name in job.only:
                try:
                    chosen.append(self.loaded(name))
                except (SystemExit, ValueError) as error:
                    job.echo(f"{name}: not planned ({error})")
            results = self.planner.run(job.mode, self.now(), job.echo, today=self.today(), leagues=chosen,
                                       reuse=not job.fresh)
            self._finish(job, results)
        except BaseException as error:  # SystemExit from a step included: report it, keep serving
            job.error = f"{type(error).__name__}: {error}"
            job.echo(job.error)
            job.state = "failed"
        finally:
            job.finished = dt.datetime.now()
            with self.lock:
                self.current, nxt = None, self.queued
                self.queued = None
                if nxt is not None:
                    nxt.state, nxt.started = "running", dt.datetime.now()
                    self.current = nxt
            if nxt is not None:
                threading.Thread(target=self._work, args=(nxt,), daemon=True).start()

    def _finish(self, job: Job, results: dict):
        failed = {name: r for name, r in results.items() if isinstance(r, BaseException)}
        job.plan_files = {name: r for name, r in results.items() if name not in failed}
        job.league_errors = {name: f"{type(e).__name__}: {e}" for name, e in failed.items()}
        job.state = "failed" if results and len(failed) == len(results) else "done"

    def _daily(self, job: Job):
        """The daily pass: a full refresh of every league a user came by lately, then the
        cleanup of users long gone and the leagues nobody links (users.cleanup)."""
        chosen = self.daily_leagues()
        job.only = [league.name for league in chosen]
        job.echo(f"daily pass: {len(chosen)} league(s)")
        results = (self.planner.run("full", self.now(), job.echo, today=self.today(), leagues=chosen, reuse=False)
                   if chosen else {})
        self._finish(job, results)
        self.forget()                   # settings re-detected: each league loaded afresh
        try:
            gone = users.cleanup(self.today())
            for name in [g.split(" ", 1)[1] for g in gone if g.startswith("league ")]:
                self.forget(name)
            if gone:
                job.echo("cleanup: " + ", ".join(gone))
        except Exception as error:  # noqa: BLE001 -- a failed cleanup waits for tomorrow
            job.echo(f"cleanup failed: {type(error).__name__}: {error}")

    def daily_due(self, now: dt.datetime):
        """The daily pass's time today once it has passed and the pass has not run today:
        DAILY_LEAD before the day's first game (the NHL's schedule), but no later than
        DAILY_FALLBACK -- also the time when the schedule can't be read or has no games."""
        at = dt.datetime.combine(now.date(), DAILY_FALLBACK)
        first = first_puck(now.date())
        if first is not None:
            at = min(at, first - DAILY_LEAD)
        ran = self.scheduled_state.get(DAILY_JOB)
        if now < at or (ran is not None and dt.datetime.fromisoformat(ran).date() == now.date()):
            return None
        return at

    def jobs_loop(self):
        """The daily jobs (scheduled.py) and the daily pass, checked each minute: one that is due
        starts when nothing else runs, nightly first; its scheduled time is kept so a restart does
        not run it again."""
        shift = dt.timedelta(minutes=self.args.jobs_shift)
        while True:
            try:
                now = dt.datetime.now()
                due = None
                if self.args.jobs:
                    for name in scheduled.JOBS:
                        at = scheduled.due(name, now, self.scheduled_state, shift)
                        if at is not None:
                            due = (name, at, scheduled.JOBS[name]["label"])
                            break                     # one at a time, nightly first
                if due is None and not self.args.no_daily:
                    at = self.daily_due(now)
                    if at is not None:
                        due = (DAILY_JOB, at, "Daily plans")
                if due is not None and self.current is None:
                    name, at, label = due
                    late = now - at > dt.timedelta(minutes=10)
                    by = f"{'catch-up' if late else 'scheduled'}: {label} {at:%a %H:%M}"
                    job = self.start(name, by=by)
                    if job.mode == name and job.by == by:     # ours, not one already running
                        self.scheduled_state[name] = at.isoformat()
                        scheduled.save_state(self.scheduled_state)
            except Exception as error:  # noqa: BLE001 -- a bad check must not end the loop
                print(f"scheduled job check failed: {type(error).__name__}: {error}", file=sys.stderr)
            time.sleep(JOBS_CHECK_S)

    # ---------- plans ----------

    def newest_plan(self, league):
        """The newest plan for the plan's day -- or, once it is tomorrow and none is saved yet,
        today's -- or, before any today (the daily pass plans once a day), the newest saved."""
        for day in dict.fromkeys((self.day(), self.today())):
            files = planpass.saved_plans(league, day)
            if files:
                return files[-1]
        return planpass.newest_saved(league)

    def games_plan(self, league):
        """The newest plan for today, whose games the live views show: once they have all started
        the plan's day is tomorrow, which can be a new week with a new opponent, and tonight's
        owners are still this week's (the user, 2026-10-11). None saved today, the newest plan."""
        files = planpass.saved_plans(league, self.today())
        return files[-1] if files else self.newest_plan(league)


class AppFiles(StaticFiles):
    """The app's files, revalidated on every load (an ETag answers 304 when unchanged), so an edit
    shows at once instead of whenever the browser's guess of their freshness runs out."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class Refresh(BaseModel):
    mode: str = "quick"             # for the ?league= league (every refresh is one league's)


class LeagueAsk(BaseModel):
    url: str                        # the league's page on Fleaflicker or ESPN
    team_id: int | None = None      # POST /leagues: the team that is yours
    espn_s2: str | None = None      # a private ESPN league's cookies (league_secrets.py)
    swid: str | None = None


class Choices(BaseModel):
    choices: dict = {}


class WeekEdit(BaseModel):
    week: str = "this"
    action: str                     # 'pick', 'replace', 'unpick', 'clear', 'take' or 'use' (week.edit)
    day: str | None = None
    add: int | None = None
    drop: int | None = None         # a pick's drop; None: into an open spot
    rental: int | None = None       # 'replace': the rental (or rostered player) whose slot it is
    label: str | None = None        # 'take' / 'use': the week plan
    index: int | None = None        # 'take': its slot
    show: str | None = None         # the plan drawn on the calendar sent back (GET /week's show)


class Together(BaseModel):
    on: bool                        # the week plans after today's upgrades (else apart, before them)
    week: str = "this"              # the week plans shown in the suggestions returned


class Pick(BaseModel):
    week: str = "this"              # 'this' or 'next' (next week's plans, on the week's last day)
    label: str                      # the week plan, "A", "B", ...
    index: int                      # its slot
    player_id: int                  # the option chosen


def make_app(worker: Worker) -> FastAPI:
    app = FastAPI(title="Fantasy hockey plan server", docs_url=None, redoc_url=None, openapi_url=None)
    issue = auth.install(app, auth.Cookies())
    store = live_games.FeedStore(livepaths.REPORTS_DIR / "goals")
    threading.Thread(target=store.watch_loop, daemon=True).start()

    class PerLeague(dict):
        """A league's object, made on first use (leagues come and go as users add them)."""

        def __init__(self, make):
            super().__init__()
            self.make = make
            worker.caches.append(self)

        def __missing__(self, name):
            value = self[name] = self.make(worker.loaded(name))
            return value

    scoresets = PerLeague(lambda league: simlayer.load_scoreset(league.scoring))
    # Each league's lineup slots in its own order (C C LW LW ... G G), as the window's calendar,
    # and the lineup solver's order and fits (weekbook.Week, as the window's).
    configs = PerLeague(lambda league: league_module.load(league.rules))
    slot_rows = PerLeague(lambda league: [slot for slot, count in configs[league.name].active_slots.items()
                                          for _ in range(count)])
    views = PerLeague(lambda league: live_games.Games(lambda: worker.games_plan(league),
                                                      scoresets[league.name], paths.players(), store))

    def nhl(league, call, *args):
        """A live-games call through the league's view; the NHL unreachable is a 502, not a crash.
        `league`: a name (checked against the caller's leagues) or a League already checked -- what
        a worker thread passes, since the caller (auth.viewer) is not known on it."""
        name = league.name if isinstance(league, leagues.League) else worker.league(league).name
        view = views[name]
        try:
            return getattr(view, call)(*args)
        except requests.RequestException as error:
            raise HTTPException(502, f"NHL feed: {type(error).__name__}: {error}")

    @app.get("/games")
    def get_games(league: str | None = None, date: str | None = None):
        """date: a day of this fantasy week (the Games day picker); none, today."""
        if date is None:
            return nhl(league, "games")
        try:
            day = dt.date.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, f"date is a date, not {date}")
        return nhl(league, "games", day)

    @app.get("/games/week")
    def get_games_week(league: str | None = None):
        """The Games day picker: each day of this fantasy week, its state and its NHL games."""
        chosen = worker.league(league)
        today = worker.today()
        days = []
        for day in roster.week_dates(today, configs[chosen.name].week_starts_on):
            try:
                count = views[chosen.name].games_on(day)
            except requests.RequestException:
                count = None
            days.append({"date": day.isoformat(), "games": count,
                         "state": "final" if day < today else "today" if day == today else "ahead"})
        return {"league": chosen.name, "week": days}

    @app.get("/goals")
    def get_goals(league: str | None = None):
        return nhl(league, "goals")

    @app.get("/games/{game_id}")
    def get_game(game_id: int, after: int = -1, league: str | None = None):
        return nhl(league, "game", game_id, after)

    @app.get("/games/{game_id}/lines")
    def get_lines(game_id: int, league: str | None = None):
        return nhl(league, "lines", game_id)

    @app.get("/tonight")
    def get_tonight(league: str | None = None):
        return nhl(league, "tonight")

    def newest(league):
        chosen = worker.league(league)
        path = worker.newest_plan(chosen)
        if path is None:
            raise HTTPException(404, f"no {chosen.name} plan saved for {worker.day().isoformat()} yet")
        return chosen, path, json.loads(path.read_text(encoding="utf-8"))

    @app.get("/plan")
    def get_plan(league: str | None = None):
        chosen, path, plan = newest(league)
        return {"league": chosen.name, "file": path.name,
                "saved_at": iso(dt.datetime.fromtimestamp(path.stat().st_mtime)), "plan": plan}

    matchups = matchup_module.Matchups()
    plans_read = {}             # plan path -> (mtime, plan): the week's plans, read once each

    def read_plan(path):
        mtime = path.stat().st_mtime
        hit = plans_read.get(path)
        if hit is None or hit[0] != mtime:
            hit = plans_read[path] = (mtime, json.loads(path.read_text(encoding="utf-8")))
        return hit[1]

    def week_projection(league, plan):
        """`plan`'s matchup_projection with each night taken from the newest plan of its week that
        projects it: once tonight's games have all started the newest plan is tomorrow's, which has
        no tonight, while today's morning plan has an older tomorrow. Overview, Players and the live
        P(win) all read this, so a day shows one projection everywhere (2026-11-14: Sunday 34.46 on
        the Overview, 29.34 on Players)."""
        base = (plan or {}).get("matchup_projection")
        if not base or not plan.get("game_date"):
            return base
        plan_day = dt.date.fromisoformat(plan["game_date"])
        out = {side: {**(base.get(side) or {}), "nights": {}, "night_var": {}} for side in ("me", "opp")}
        for day in sorted((d for d in roster.week_dates(plan_day, configs[league.name].week_starts_on)
                           if d <= plan_day), reverse=True):
            files = planpass.saved_plans(league, day)
            if not files:
                continue
            p = read_plan(files[-1])
            if p.get("week") != plan.get("week"):
                continue
            mp = p.get("matchup_projection") or {}
            for night in (mp.get("me") or {}).get("nights", {}):
                if night in out["me"]["nights"]:
                    continue            # a newer plan has it
                for side in ("me", "opp"):
                    got = mp.get(side) or {}
                    out[side]["nights"][night] = (got.get("nights") or {}).get(night)
                    if night in (got.get("night_var") or {}):
                        out[side]["night_var"][night] = got["night_var"][night]
        for side in ("me", "opp"):
            if "night_var" not in (base.get(side) or {}):
                del out[side]["night_var"]          # an old plan without it: livewin says no
        return out

    @app.get("/roster")
    def get_roster(league: str | None = None):
        """The Roster's Season table: the newest plan's roster as it stands (roster.py)."""
        chosen, path, plan = newest(league)
        nhl_ids = views[chosen.name].owners._nhl_ids(paths.players())
        plan_day = dt.date.fromisoformat(plan["game_date"]) if plan.get("game_date") else worker.today()

        def photo(player_id, team):
            return live_games.headshot(nhl_ids.get(int(player_id)), team, plan_day)

        return {"league": chosen.name, "file": path.name,
                **roster.roster_view(plan, scoresets[chosen.name], slot_rows[chosen.name], photo)}

    @app.get("/players")
    def get_players(league: str | None = None):
        chosen, path, plan = newest(league)
        nhl_ids = views[chosen.name].owners._nhl_ids(paths.players())
        plan_day = dt.date.fromisoformat(plan["game_date"]) if plan.get("game_date") else worker.today()

        def photo(player_id, team):
            return live_games.headshot(nhl_ids.get(int(player_id)), team, plan_day)

        def games_on(date):
            try:
                return views[chosen.name].games_on(dt.date.fromisoformat(date))
            except requests.RequestException:
                return None

        workbench = (suggestions.plans_shown(plan, "this") or [{}])[0].get("workbench")
        return {"league": chosen.name, "file": path.name,
                **players_module.players_view(plan, scoresets[chosen.name], workbench, photo, games_on)}

    @app.get("/choices")
    def get_choices(league: str | None = None):
        chosen = worker.league(league)
        return {"league": chosen.name, "choices": choices.load(chosen.name, worker.day())}

    @app.put("/choices")
    def put_choices(body: Choices, league: str | None = None):
        chosen = worker.league(league)
        return {"league": chosen.name, "choices": choices.save(chosen.name, body.choices, worker.day())}

    def book(league, week):
        chosen, path, plan = newest(league)
        mine = choices.load(chosen.name, worker.day())
        try:
            return chosen, path, mine, week_module.Book(plan, mine, week, configs[chosen.name].slot_order(),
                                                        configs[chosen.name].accepts)
        except week_module.NoWeek as reason:
            raise HTTPException(409, str(reason))

    @app.get("/week")
    def get_week(week: str = "this", show: str | None = None, league: str | None = None):
        chosen, path, _, shown = book(league, week)
        return {"league": chosen.name, "file": path.name, **shown.view(slot_rows[chosen.name], show)}

    @app.get("/week/picker")
    def get_picker(day: str, week: str = "this", occupant: int | None = None, slot: str | None = None,
                   league: str | None = None):
        chosen, path, _, shown = book(league, week)
        return {"league": chosen.name, **shown.picker(day, occupant, slot)}

    @app.get("/week/drops")
    def get_drops(day: str, add: int, week: str = "this", occupant: int | None = None, league: str | None = None):
        chosen, path, _, shown = book(league, week)
        return {"league": chosen.name, **shown.drops(day, add, occupant)}

    @app.post("/week/edit")
    def edit_week(body: WeekEdit, league: str | None = None):
        """Your week changed (week.edit): saved, and the calendar as it now stands."""
        chosen, path, mine, shown = book(league, body.week)
        try:
            new = week_module.edit(mine, shown, body)
        except ValueError as error:
            raise HTTPException(422, str(error))
        saved = choices.save(chosen.name, new, worker.day())
        _, _, _, shown = book(league, body.week)
        return {"league": chosen.name, "file": path.name, "choices": saved,
                **shown.view(slot_rows[chosen.name], body.show)}


    @app.get("/matchup")
    def get_matchup(league: str | None = None):
        """The week's matchup: the platform's scores (kept a minute) and the newest plan's
        projection and P(win) (matchup.py)."""
        chosen = worker.league(league)
        if not chosen.readable:
            raise HTTPException(409, f"{chosen.name} has no platform to read its matchup from")
        path = worker.newest_plan(chosen)
        plan = read_plan(path) if path else None
        if plan:
            plan = {**plan, "matchup_projection": week_projection(chosen, plan)}
        try:
            return {"league": chosen.name, **matchups.view(chosen, plan)}
        except Exception as error:  # noqa: BLE001 -- the platform unreachable or changed: say so
            raise HTTPException(502, f"{chosen.platform}: {type(error).__name__}: {error}")

    @app.get("/matchup/players")
    def get_matchup_players(league: str | None = None):
        chosen = worker.league(league)
        today = worker.today()
        dates = roster.week_dates(today, configs[chosen.name].week_starts_on)
        saved = planpass.saved_plans(chosen, today)
        path = saved[-1] if saved else worker.newest_plan(chosen)
        if path is None:
            raise HTTPException(404, f"no {chosen.name} plan saved for {today.isoformat()} yet")
        today_plan = read_plan(path)
        plans, lineups = {}, {}
        for date in dates:
            own = planpass.saved_plans(chosen, date)
            plans[date.isoformat()] = read_plan(own[-1]) if own else (today_plan if date >= today else None)
            if date <= today:
                try:
                    lineups[date.isoformat()] = matchups.day_lineup(chosen, date, keep=date < today)
                except (Exception, SystemExit) as error:  # noqa: BLE001 -- the plan's lineup stands in
                    print(f"{chosen.name} {date}: platform lineup unread: {type(error).__name__}: {error}", file=sys.stderr)
                    lineups[date.isoformat()] = None
        nhl_ids = views[chosen.name].owners._nhl_ids(paths.players())
        held = [{"player_id": pid, "nhl_id": nhl_ids.get(pid), "team": p["team"]}
                for pid, p in headtohead.people(plans).items()]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            boxes = dict(zip([d.isoformat() for d in dates],
                             pool.map(lambda d: nhl(chosen, "day", d, held)["players"], dates)))
        workbench = (suggestions.plans_shown(today_plan, "this") or [{}])[0].get("workbench")
        # The projection the Overview shows (week_projection); the newest plan only while it is
        # still this week's -- on the week's last night it can be next week's.
        newest_path = worker.newest_plan(chosen)
        newest_plan = read_plan(newest_path) if newest_path else today_plan
        projection = week_projection(chosen, newest_plan if newest_plan.get("week") == today_plan.get("week") else today_plan)
        h2h = headtohead.view([d.isoformat() for d in dates], today.isoformat(), plans, lineups, boxes, workbench,
                              projection)
        for p in h2h["players"]:
            p["photo"] = live_games.headshot(nhl_ids.get(p["player_id"]), p["team"], today)
        # The plan's P(win) carried through tonight's games (livewin.py): no call beyond the above.
        return {"league": chosen.name, **h2h, "live_win": livewin.p_win(h2h, projection, today.isoformat())}

    @app.get("/suggestions")
    def get_suggestions(week: str = "this", league: str | None = None):
        chosen, path, plan = newest(league)
        return {"league": chosen.name, "file": path.name,
                **suggestions.suggestions_view(plan, choices.load(chosen.name, worker.day()), week)}

    @app.post("/choices/pick")
    def pick(body: Pick, league: str | None = None):
        """A week plan's option picked into your week with the plan's drop, or taken back if picked
        (choices.pick_from_plan, as the window's click); returns the suggestions as they now stand."""
        chosen, path, plan = newest(league)
        shown = next((p for p in suggestions.plans_shown(plan, body.week) if p["label"] == body.label), None)
        if shown is None or not 0 <= body.index < len(shown.get("moves", [])):
            raise HTTPException(404, f"no slot {body.index} in week plan {body.label} of {path.name}")
        if body.player_id not in {o.get("player_id") for o in shown["moves"][body.index].get("options") or []}:
            raise HTTPException(422, f"player {body.player_id} is not an option in that slot")
        saved = choices.save(chosen.name, choices.pick_from_plan(choices.load(chosen.name, worker.day()), shown,
                                                                 body.index, body.player_id), worker.day())
        return {"league": chosen.name, "file": path.name, "choices": saved,
                **suggestions.suggestions_view(plan, saved, body.week)}

    @app.post("/choices/together")
    def set_together(body: Together, league: str | None = None):
        """Whether the week plans take today's upgrades into account (choices.together): saved, and
        the suggestions as they now stand (the plan's own until a 'plan' runs on it)."""
        chosen, path, plan = newest(league)
        mine = choices.load(chosen.name, worker.day())
        saved = choices.save(chosen.name, {**mine, "together": body.on}, worker.day())
        return {"league": chosen.name, "file": path.name, "choices": saved,
                **suggestions.suggestions_view(plan, saved, body.week)}

    @app.post("/refresh")
    def refresh(body: Refresh, league: str | None = None):
        """A refresh of the ?league= league (planpass.Planner.run's modes). Anyone's league is
        refreshed alike: once per TAP_LEAGUE_S per league and TAP_USER_S per user, a 429 sooner
        -- but a 'plan' (your choices changed: planned on the kept read) any time."""
        if body.mode not in planpass.MODES:
            raise HTTPException(422, f"mode must be one of {', '.join(planpass.MODES)}")
        chosen = worker.league(league)
        uid = auth.viewer.get()
        if body.mode != "plan":
            wait = worker.tap_wait(uid, [chosen.name])
            if wait:
                raise HTTPException(429, f"refreshed a moment ago: try again in {(wait + 59) // 60} min")
            worker.tapped_now(uid, [chosen.name])
        return worker.start(body.mode, by="choices" if body.mode == "plan" else "tap",
                            only=[chosen.name]).view(mine=worker.mine())

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str, after: int = 0):
        job = worker.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"no job {job_id}")
        return job.view(after, mine=worker.mine())

    @app.get("/jobs")
    def get_jobs():
        """The recent jobs that planned the caller's leagues, and the scheduled data jobs."""
        mine = worker.mine()
        shown = [job.view(mine=mine) for job in reversed(list(worker.jobs.values()))]
        return [{**v, "lines": []} for v in shown if v["mine"]]

    @app.get("/status")
    def status(league: str | None = None):
        chosen, planner, args = worker.league(league), worker.planner, worker.args
        mine = worker.mine()
        path = worker.newest_plan(chosen)
        today = planner is not None and planner.day == worker.day()
        last = {**planner.last, **planner.league_last.get(chosen.name, {})} if today else {}
        running, queued = worker.current, worker.queued
        teams = {}
        for name in mine:
            try:
                lg = worker.loaded(name)
                teams[name] = {"team": lg.team_name, "platform": lg.platform}
            except (SystemExit, ValueError):
                continue
        return {
            "league": chosen.name, "leagues": list(teams), "day": worker.day().isoformat(),
            "teams": teams,
            "job": None if running is None else running.view(after=len(running.lines), mine=mine),
            "queued": None if queued is None else queued.view(after=len(queued.lines), mine=mine),
            "last": {step: iso(when) for step, when in last.items()},
            "plan_file": None if path is None else path.name,
            "plan_saved_at": None if path is None else iso(dt.datetime.fromtimestamp(path.stat().st_mtime)),
            "daily": {"last": worker.scheduled_state.get(DAILY_JOB),
                      "lead_min": int(DAILY_LEAD.total_seconds() // 60), "latest": DAILY_FALLBACK.strftime("%H:%M")},
            "tap_wait_s": worker.tap_wait(auth.viewer.get(), [chosen.name]),
            "skip_snapshots": args.skip_snapshots,
            "scheduled": None if not args.jobs else {
                name: {"label": job["label"], "times": job["times"], "shift_min": args.jobs_shift,
                       "last": worker.scheduled_state.get(name)} for name, job in scheduled.JOBS.items()},
            "league_file": args.league_file,
        }

    # ---------- adding a league ----------

    previews: dict = {}         # (platform, league id, cookies) -> (when, what inspect found)

    def address(request: Request) -> str:
        # Through the tunnel every call comes from cloudflared; Cloudflare names the real one.
        return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")

    def parse_url(url: str) -> tuple:
        url = (url or "").strip()
        found = re.search(r"fleaflicker\.com/nhl/leagues/(\d+)", url, re.I)
        if found:
            return "fleaflicker", int(found.group(1))
        found = re.search(r"espn\.com/.*hockey.*[?&]leagueId=(\d+)", url, re.I)
        if found:
            return "espn", int(found.group(1))
        raise HTTPException(422, "paste the league's page: fleaflicker.com/nhl/leagues/<id> or "
                                 "fantasy.espn.com/hockey/league?leagueId=<id>")

    def cookies_of(ask: LeagueAsk) -> dict | None:
        if not (ask.espn_s2 or ask.swid):
            return None
        if not (ask.espn_s2 and ask.swid):
            raise HTTPException(422, "a private ESPN league needs both espn_s2 and SWID")
        swid = ask.swid.strip().strip("{}")
        return {"espn_s2": ask.espn_s2.strip(), "SWID": "{" + swid + "}"}

    def inspect(ask: LeagueAsk, request: Request) -> dict:
        """The league's teams and settings as its platform shows them, and why it can't be planned
        if it can't -- kept PREVIEW_KEEP_S, so the add that follows reads nothing again."""
        platform, league_id = parse_url(ask.url)
        cookies = cookies_of(ask)
        key = (platform, league_id, json.dumps(cookies, sort_keys=True))
        now = time.time()
        for old in [k for k, (at, _) in previews.items() if now - at >= PREVIEW_KEEP_S]:
            del previews[old]           # an expired lookup goes, its ESPN cookies with it
        for old in [k for k, times in worker.added_by_ip.items() if all(now - t >= 3600 for t in times)]:
            del worker.added_by_ip[old]
        hit = previews.get(key)
        if hit:
            return hit[1]
        who = address(request)
        recent = [t for t in worker.added_by_ip.get(who, []) if time.time() - t < 3600]
        if len(recent) >= ADD_TRIES_PER_HOUR:
            raise HTTPException(429, "too many leagues looked up from here: try again in an hour")
        worker.added_by_ip[who] = recent + [time.time()]
        season = scheduled.season_of(worker.today())
        name = leagues.added_name(platform, league_id, 0)
        if platform == "fleaflicker":
            adapter = platforms.fleaflicker.Fleaflicker(league_id)
        else:
            adapter = platforms.espn.Espn(league_id, platforms.espn.espn_year(season), cookies=cookies)
        try:
            teams = adapter.teams()
        except SystemExit:          # ESPN's 401: a private league
            raise HTTPException(401, {"detail": "this ESPN league is private: it needs your espn_s2 and SWID cookies"
                                                + (" (those didn't work)" if cookies else ""), "needs_cookies": True})
        except Exception as error:  # noqa: BLE001 -- no such league, the platform down
            raise HTTPException(404, f"couldn't read {platform} league {league_id} ({type(error).__name__})")
        if not teams:
            raise HTTPException(404, f"{platform} league {league_id} has no teams")
        draft = leagues.League(name=name, description="", platform=platform, league_id=league_id, season=season,
                               team=None, rules=name, scoring=name, strategy=None, eligibility_platform=platform,
                               adp_platform="espn", active=True, auth=None, overrides=leagues.ADDED_OVERRIDES,
                               draft_window={}, path=Path("(not saved)"))
        rosters = scoring = None
        notes, problem = [], None
        try:
            rosters, scoring, notes = sync_league_settings.assemble(draft, adapter.league_settings())
            problem = sync_league_settings.supported(rosters, scoring)
        except (Exception, SystemExit) as error:  # noqa: BLE001 -- wording or a rule not read
            problem = f"this league's settings couldn't be read ({type(error).__name__}: {error})"
        found = {"platform": platform, "league_id": league_id, "season": season, "cookies": cookies,
                 "teams": {int(k): v for k, v in teams.items()}, "rosters": rosters, "scoring": scoring,
                 "notes": notes, "problem": problem}
        previews[key] = (time.time(), found)
        return found

    def summary(found) -> dict:
        taken = users.linked_by_anyone()
        known = leagues.index()
        rows = []
        for team_id, team_name in sorted(found["teams"].items(), key=lambda t: str(t[1]).lower()):
            existing = leagues.find(found["platform"], found["league_id"], team_id, known)
            rows.append({"id": team_id, "name": team_name, "linked": bool(existing and existing.name in taken)})
        rosters, scoring = found["rosters"] or {}, found["scoring"] or {}
        slots = " ".join(f"{slot}" + (f" x{n}" if n > 1 else "")
                         for slot, n in (rosters.get("active_slots") or {}).items())
        points = ", ".join(f"{k} {v:g}" for k, v in {**(scoring.get("skaters") or {}),
                                                     **(scoring.get("goalies") or {})}.items())
        return {"platform": found["platform"], "league_id": found["league_id"], "teams": rows,
                "summary": {"teams": len(rows), "slots": slots, "bench": rosters.get("bench"),
                            "moves_per_week": rosters.get("moves_per_week"), "points": points,
                            "not_held": found["notes"]},
                "problem": found["problem"], "private": bool(found["cookies"])}

    @app.post("/leagues/preview")
    def preview_league(ask: LeagueAsk, request: Request):
        """A league's teams and settings, read from its page's link (and, for a private ESPN
        league, its cookies): what the add screen shows before you pick your team."""
        return summary(inspect(ask, request))

    @app.post("/leagues")
    def add_league(ask: LeagueAsk, request: Request, response: Response):
        """Your team added: the league set up (its settings detected and written) unless someone
        has it already, linked to this browser (its cookie made now if it has none), and given a
        full refresh at once (reports, projections, its read and plan; reports still fresh are reused)."""
        if ask.team_id is None:
            raise HTTPException(422, "pick your team")
        found = inspect(ask, request)
        if found["problem"]:
            raise HTTPException(409, found["problem"])
        if ask.team_id not in found["teams"]:
            raise HTTPException(404, f"no team {ask.team_id} in that league")
        uid = auth.viewer.get()
        mine = users.linked(uid)
        existing = leagues.find(found["platform"], found["league_id"], ask.team_id)
        if existing is not None and existing.name in mine:
            return {"league": existing.name, "job": None, "leagues": mine}
        if len(mine) >= LEAGUES_PER_USER:
            raise HTTPException(409, f"{LEAGUES_PER_USER} leagues at most: remove one first")
        if existing is None and len(users.linked_by_anyone()) >= LEAGUES_MAX:
            raise HTTPException(503, "the server is planning as many leagues as it can: try again later")
        with worker.lock:
            if existing is None:
                team_name = found["teams"][ask.team_id]
                league = leagues.create(found["platform"], found["league_id"], ask.team_id, team_name,
                                        found["season"], f"{found['platform']} league {found['league_id']}, "
                                        f"team {team_name}; added in the app {worker.today()}.",
                                        private=bool(found["cookies"]))
                if found["cookies"]:
                    league_secrets.put(league.auth, found["cookies"])
                sync_league_settings.write_settings(league, found["rosters"], found["scoring"], found["notes"],
                                                    write=True)
                worker.forget(league.name)
            else:
                league = existing
                if found["cookies"] and league.auth and leagues.is_added(league):
                    league_secrets.put(league.auth, found["cookies"])   # fresher cookies
            uid = issue(response, uid)
            user = users.link(uid, league.name)
        token, token_user = auth.viewer.set(uid), auth.viewer_user.set(user)
        try:
            worker.tapped_now(uid, [league.name])
            job = worker.start("full", by="added", only=[league.name], fresh=True)
            return {"league": league.name, "job": job.view(mine=worker.mine()), "leagues": worker.mine()}
        finally:
            auth.viewer.reset(token)
            auth.viewer_user.reset(token_user)

    @app.get("/leagues")
    def get_leagues():
        out = []
        for name in worker.mine():
            try:
                lg = worker.loaded(name)
            except (SystemExit, ValueError):
                continue
            out.append({"name": name, "team": lg.team_name, "platform": lg.platform,
                        "league_id": lg.league_id, "private": bool(lg.auth)})
        return {"leagues": out, "max": LEAGUES_PER_USER}

    @app.delete("/leagues/{name}")
    def remove_league(name: str):
        """Your link to a league gone; the league itself too once nobody links it (an added one;
        Settings/leagues' never are)."""
        uid = auth.viewer.get()
        if name not in users.linked(uid):
            raise HTTPException(404, f"no league {name} here")
        auth.viewer_user.set(users.unlink(uid, name))
        if users.drop_league(name):
            worker.forget(name)
        return {"leagues": worker.mine()}

    app.mount("/app", AppFiles(directory=APP, html=True), name="app")
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--league", action="append", default=None,
                        help="A league every caller sees, cookie or not (a rehearsal's or a test server's); "
                             "repeat for several. Without it a caller sees the leagues it added")
    parser.add_argument("--date", default=None, help="Game date (default: today on this machine)")
    parser.add_argument("--league-file", default=None,
                        help="A league snapshot JSON instead of the platform (one league)")
    parser.add_argument("--platform-season", type=int, default=None, help="Read a past season's league (rehearsal)")
    parser.add_argument("--now", default=None, help="UTC moment for the per-game lock (default: now; rehearsal)")
    parser.add_argument("--no-daily", action="store_true", help="No daily pass (every league planned once a day)")
    parser.add_argument("--skip-snapshots", action="store_true",
                        help="Use the scheduled snapshots instead of taking fresh ones (faster)")
    parser.add_argument("--jobs", action="store_true",
                        help="Run the daily jobs (scheduled.py): nightly import, player teams, injury listing")
    parser.add_argument("--jobs-db", default=None,
                        help="The jobs' database server (NHLSTATS_DB_SERVER) if not the server's own, e.g. db,1433")
    parser.add_argument("--jobs-shift", type=int, default=0,
                        help="Minutes added to every job time (a trial beside the Windows tasks)")
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 in the container")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.league_file and (args.league is None or len(args.league) != 1):
        parser.error("--league-file is one league's snapshot: give that league alone with --league")
    worker = Worker(args)
    threading.Thread(target=worker.jobs_loop, daemon=True).start()     # the daily pass, and --jobs' jobs
    uvicorn.run(make_app(worker), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

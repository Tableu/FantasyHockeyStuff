#!/usr/bin/env python
"""The phone app's server (Fantasy Hockey Phone App Plan, Phase 1.5): the plan refreshes behind a
small HTTP API, which the plan window (Live/plan_gui.py) shows. Every active league in
Settings/leagues/ (beagles, espn-la), one refresh at a time.

    python server.py                                   # the active leagues, today, 127.0.0.1:8000
    python server.py --league beagles                  # one league (repeat --league for several)
    python server.py --host 0.0.0.0                    # in the container (compose publishes 8000)
    python server.py --league beagles --date 2026-09-29 \
        --league-file ../Live/fixtures/beagles/fake_league.json --skip-snapshots --no-auto   # rehearsal

Every call but the job's takes ?league=<name> (default: the first league served, beagles):

    GET  /plan                 the league's newest plan for the plan's day -- today, or tomorrow once
                               every game today has started: {league, file, saved_at, plan}
    POST /refresh {mode, scope}   mode (planpass.Planner.run): 'full' (reports with line charts,
                               projections, leagues read again), 'quick' (the same without line
                               charts), 'news' (reports and projections, leagues planned on their
                               kept read), 'league' (leagues read again, no reports), 'plan' (your
                               choices changed: planned on the kept read, about 2 s). scope:
                               'league' (the ?league= one) or 'all'; default 'all' for full and
                               quick, 'league' otherwise. Returns the job. A tap while a refresh
                               runs joins it (a 'plan' is queued behind it); the auto window plans
                               every league
    GET  /jobs/{id}?after=n    the job's state and its progress lines after line n
    GET  /jobs                 the recent jobs, newest first, without their lines (the app's refresh log)
    GET  /status               the leagues served (each with its team), the day, the running job, when
                               each step last ran (the league's own read included), the league's newest plan
    GET  /games                today's games: score, clock, your players and your opponent's on each
    GET  /goals                every goal today, newest first, with the fantasy points it earned
    GET  /games/{id}?after=n   one game: line score, team stats, box score with fantasy points, plays
                               after sortOrder n
    GET  /games/{id}/lines     each team's lines, pairs and special-teams units as used (games.py)
    GET  /tonight              every player you and your opponent hold: his game today, his live box
                               line and fantasy points; your lineup's live total (games.py)
    GET  /roster               the app's Roster screen from the newest plan: lineup as it stands,
                               bench, IR, tonight's projection, season in the league's scoring (roster.py)
    GET  /choices              what you chose in the plan window (Live/choices.py): {league,
                               choices: {days: {date: {moves}}, together}}
    PUT  /choices {choices}    replaces them; returns {league, choices}. Nothing is re-planned
                               until a refresh (or a 'plan') -- the window says so
    POST /choices/together {on, week}   the week plans planned after today's upgrades (true) or apart
                               (false); saved (choices.py), re-planned once the app asks for a 'plan'
    GET  /suggestions?week=    the app's AI suggestions from the newest plan: today's moves, the
                               upgrades, the week plans ('this' or 'next') with your picks marked
    POST /choices/pick {week, label, index, player_id}   a week plan's option picked into your week
                               with the plan's drop, or taken back (choices.pick_from_plan); returns
                               the suggestions as they now stand
    GET  /week?week=           the app's Weekly planner: your week from your picks on plan A's
                               workbench (week.py) -- each night's points, open slots, picks, slots
    GET  /week/picker?day=&slot=&occupant=   the slot picker's free agents at the slot's positions
                               (plan picks first); occupant: the rental or rostered player whose
                               slot it is -- then priced on the swap
    GET  /week/drops?day=&add=&occupant=   who that pick may drop, each with the week's gain
    POST /week/edit {week, action, day, add, drop, rental}   pick, replace, unpick or clear;
                               saved (choices.py), returns the calendar as it now stands
    GET  /matchup              the app's Matchup: the week's score, each day's points for you and your
                               opponent, the league's week, from the platform (kept a minute), with
                               the plan's projection and P(win) (matchup.py)
    POST /login {password}     a session cookie for the app (auth.py); 401 wrong, 429 too many tries
    POST /logout               ends this phone's session
    GET  /app/                 the phone app (Server/app/: static files, it calls the API above)

A refresh is planpass.Planner's: the snapshots and tonight's projections once, then each league's
read, plan and saved plan files in a child process that exits after -- so the server's memory does
not grow with the leagues it plans -- and one league's failure (its platform down) leaves the
others' plans. The auto window runs here: a refresh about 30 minutes before each group of games, once
per group -- Full when the line charts are none today or over 4 h old, else Quick -- for every league, and one more once today's last game has started -- the plan is then
tomorrow's (moves, lineup and projections), since tonight has no lineup left to set. The live games' NHL feeds are fetched once for all leagues; each league
sees them with its own rosters and scoring. Every call but /login, /logout and the app's files needs
a session (the app's password) or plan_gui's key (auth.py; `python auth.py set-password` first).
"""

import argparse
import datetime as dt
import itertools
import json
import sys
import threading
import time
from pathlib import Path

LIVE = Path(__file__).resolve().parents[1] / "Live"
sys.path.insert(0, str(LIVE))

import pandas as pd  # noqa: E402
import requests  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import auth  # noqa: E402
import seasonlayer  # noqa: E402,F401 -- puts Season/ on sys.path; see seasonlayer.py
import choices  # noqa: E402
import games as live_games  # noqa: E402
import league as league_module  # noqa: E402
import leagues  # noqa: E402
import matchup as matchup_module  # noqa: E402
import roster  # noqa: E402
import suggestions  # noqa: E402
import week as week_module  # noqa: E402
import paths  # noqa: E402
import planpass  # noqa: E402
import scheduled  # noqa: E402
import simlayer  # noqa: E402

APP = Path(__file__).resolve().parent / "app"     # the phone app's static files, served at /app/
AUTO_CHECK_S = 60               # how often the auto window looks at the clock
LINES_MAX_AGE = dt.timedelta(hours=4)   # an auto window runs Full when the line charts are older
JOBS_KEPT = 20                  # finished jobs /jobs still answers for


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def iso(when) -> str | None:
    return None if when is None else when.isoformat(timespec="seconds")


class Job:
    def __init__(self, number: int, mode: str, by: str):
        self.id, self.mode, self.by = str(number), mode, by
        self.state = "running"          # running -> done | failed
        self.lines: list[str] = []
        self.started, self.finished = dt.datetime.now(), None
        self.error = None
        self.plan_files = {}            # league -> the plan file this job saved for it
        self.only = None                # the job's leagues (a tap's scope, or whose choices changed); None: all

    def echo(self, text):
        self.lines.append(f"{dt.datetime.now():%H:%M:%S}  {text}")

    def view(self, after=0) -> dict:
        return {"id": self.id, "mode": self.mode, "by": self.by, "state": self.state,
                "started": iso(self.started), "finished": iso(self.finished), "error": self.error,
                "plan_files": self.plan_files, "lines": self.lines[after:], "next": len(self.lines)}


class Worker:
    """Runs one refresh at a time on its own thread, for every league served; keeps the day's
    Planner (when each step last ran) and the recent jobs. The leagues are planned in a child
    process per refresh (planpass.plan_in_child), so this process stays small."""

    def __init__(self, args):
        self.args = args
        names = args.league or [league.name for league in leagues.active()]
        self.leagues = {name: leagues.load(name) for name in names}
        self.planner = None
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None
        self.numbers = itertools.count(1)
        self.lock = threading.Lock()
        self.windows_done = set()       # puck times the auto window re-planned since it started
        self.pending = set()            # leagues whose choices changed during a running job
        self.queued = None              # (mode, only, by): a refresh tapped during a scheduled job
        self.scheduled_state = scheduled.load_state()   # job -> the scheduled time it last ran for

    def league(self, name=None):
        """A league served, by name (None: the first); a 404 for one it does not serve."""
        if name is None:
            return next(iter(self.leagues.values()))
        if name not in self.leagues:
            raise HTTPException(404, f"this server plans {', '.join(self.leagues)}, not {name}")
        return self.leagues[name]

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

    def start(self, mode: str, by: str = "tap", only=None) -> Job:
        """A new refresh or scheduled job, or the running one if there is one. A 'plan' (`only`:
        the leagues whose choices changed) asked for while one runs is queued: it starts when that
        one ends, with the choices saved by then. So is a refresh tapped during a scheduled job (the
        nightly import): it runs after it, on what it imported, instead of joining it."""
        with self.lock:
            if self.current is not None:
                if mode == "plan":
                    self.pending.update(only or self.leagues)
                elif self.current.mode in scheduled.JOBS and mode not in scheduled.JOBS:
                    self.queued = (mode, only, by)
                return self.current
            job = Job(next(self.numbers), mode, by)
            job.only = only
            self.jobs[job.id] = job
            for old in list(self.jobs)[:-JOBS_KEPT]:
                del self.jobs[old]
            self.current = job
        threading.Thread(target=self._work, args=(job,), daemon=True).start()
        return job

    def _work(self, job: Job):
        try:
            day = self.day()
            if self.planner is None or self.planner.day != day:
                a = self.args
                self.planner = planpass.Planner(list(self.leagues.values()), day, a.league_file,
                                                a.platform_season, a.skip_snapshots)
            if job.mode in scheduled.JOBS:
                scheduled.run(job.mode, self.today(), job.echo, self.args.jobs_db)
                job.state = "done"
                return
            results = self.planner.run(job.mode, self.now(), job.echo, only=job.only, today=self.today())
            failed = {name: r for name, r in results.items() if isinstance(r, BaseException)}
            job.plan_files = {name: r for name, r in results.items() if name not in failed}
            if failed:
                job.error = "; ".join(f"{name}: {type(e).__name__}: {e}" for name, e in failed.items())
            job.state = "failed" if len(failed) == len(results) else "done"
        except BaseException as error:  # SystemExit from a step included: report it, keep serving
            job.error = f"{type(error).__name__}: {error}"
            job.echo(job.error)
            job.state = "failed"
        finally:
            job.finished = dt.datetime.now()
            with self.lock:
                self.current = None
                queued, self.pending = sorted(self.pending), set()
                tapped, self.queued = self.queued, None
            if tapped:
                self.start(tapped[0], tapped[2], only=tapped[1])
            elif queued:
                self.start("plan", "choices", only=queued)

    def planned_for(self, window) -> bool:
        """Whether this group of games already has its plans: re-planned by the auto window since
        the server started, or (on the real clock) every league with a plan saved inside its lead
        -- so a restart does not re-run a window the server ran before it, and a tap there counts."""
        if window in self.windows_done:
            return True
        if self.args.now:                   # rehearsal: the files' real times are not its clock
            return False
        since = (window - planpass.WINDOW_LEAD).to_pydatetime()

        def saved_since(league):
            return any(dt.datetime.fromtimestamp(f.stat().st_mtime, dt.timezone.utc).replace(tzinfo=None) >= since
                       for f in planpass.saved_plans(league, self.day()))
        return all(saved_since(league) for league in self.leagues.values())

    def lines_age(self) -> dt.timedelta | None:
        """How old the line charts this server fetched today are; None: none today (or since a
        restart, which forgets -- that window takes them again, ~15 s)."""
        fetched = self.planner.last.get("lines") if self.planner is not None else None
        if fetched is None or fetched.date() != dt.date.today():
            return None
        return dt.datetime.now() - fetched

    def jobs_loop(self):
        """The daily jobs (scheduled.py), checked each minute: one that is due starts when nothing
        else runs, nightly first; its scheduled time is kept so a restart does not run it again."""
        shift = dt.timedelta(minutes=self.args.jobs_shift)
        while True:
            try:
                now = dt.datetime.now()
                for name in scheduled.JOBS:
                    at = scheduled.due(name, now, self.scheduled_state, shift)
                    if at is None or self.current is not None:
                        continue
                    late = now - at > dt.timedelta(minutes=10)
                    by = f"{'catch-up' if late else 'scheduled'}: {scheduled.JOBS[name]['label']} {at:%a %H:%M}"
                    job = self.start(name, by=by)
                    if job.mode == name and job.by == by:     # ours, not one already running
                        self.scheduled_state[name] = at.isoformat()
                        scheduled.save_state(self.scheduled_state)
                    break                                     # one at a time, nightly first
            except Exception as error:  # noqa: BLE001 -- a bad check must not end the loop
                print(f"scheduled job check failed: {type(error).__name__}: {error}", file=sys.stderr)
            time.sleep(AUTO_CHECK_S)

    def auto_loop(self):
        """A refresh ~30 minutes before each group of games, once per group: Full when the line
        charts are stale (none today, or older than LINES_MAX_AGE -- most days the first window,
        after the morning skates; a matinee day's later ones too), else Quick."""
        while True:
            try:
                window = planpass.next_window(self.day(), self.now())
                if window is not None and self.current is None and not self.planned_for(window):
                    self.windows_done.add(window)
                    age = self.lines_age()
                    mode = "full" if age is None or age > LINES_MAX_AGE else "quick"
                    why = ("" if mode == "quick" else " (no line charts today)" if age is None
                           else f" (line charts {age.total_seconds() / 3600:.0f} h old)")
                    self.start(mode, by=f"auto: games at {window:%H:%M} UTC{why}")
                # Once today's last game starts, tomorrow's plan, once (or on a restart before
                # any plan for tomorrow was saved).
                day = self.day()
                if (day != self.today() and self.current is None and day not in self.windows_done
                        and not all(planpass.saved_plans(league, day) for league in self.leagues.values())):
                    self.windows_done.add(day)
                    self.start("quick", by=f"auto: today's games have started, planning {day:%a}")
            except Exception as error:  # noqa: BLE001 -- a bad check must not end the loop
                print(f"auto window check failed: {type(error).__name__}: {error}", file=sys.stderr)
            time.sleep(AUTO_CHECK_S)

    def newest_plan(self, league):
        """The newest plan for the plan's day -- or, once it is tomorrow and none is saved yet,
        today's."""
        for day in dict.fromkeys((self.day(), self.today())):
            files = planpass.saved_plans(league, day)
            if files:
                return files[-1]
        return None

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
    mode: str = "quick"
    # Which leagues: "league" the ?league= one, "all" every league served. Default: "all" for
    # full and quick (plan_gui's buttons, as before), "league" for news, league and plan.
    scope: str | None = None


class Choices(BaseModel):
    choices: dict = {}


class WeekEdit(BaseModel):
    week: str = "this"
    action: str                     # 'pick', 'replace', 'unpick' or 'clear' (week.edit)
    day: str | None = None
    add: int | None = None
    drop: int | None = None         # a pick's drop; None: into an open spot
    rental: int | None = None       # 'replace': the rental (or rostered player) whose slot it is


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
    auth.install(app, auth.Login())
    store = live_games.FeedStore(LIVE / "reports" / "goals")
    threading.Thread(target=store.watch_loop, daemon=True).start()
    scoresets = {name: simlayer.load_scoreset(league.scoring) for name, league in worker.leagues.items()}
    # Each league's lineup slots in its own order (C C LW LW ... G G), as the window's calendar,
    # and the lineup solver's order and fits (weekbook.Week, as the window's).
    configs = {name: league_module.load(league.rules) for name, league in worker.leagues.items()}
    slot_rows = {name: [slot for slot, count in config.active_slots.items() for _ in range(count)]
                 for name, config in configs.items()}
    views = {name: live_games.Games(lambda league=league: worker.games_plan(league),
                                    scoresets[name], paths.players(), store)
             for name, league in worker.leagues.items()}

    def nhl(league, call, *args):
        """A live-games call through the league's view; the NHL unreachable is a 502, not a crash."""
        view = views[worker.league(league).name]
        try:
            return getattr(view, call)(*args)
        except requests.RequestException as error:
            raise HTTPException(502, f"NHL feed: {type(error).__name__}: {error}")

    @app.get("/games")
    def get_games(league: str | None = None):
        return nhl(league, "games")

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

    @app.get("/roster")
    def get_roster(league: str | None = None, day: str | None = None):
        """day: 'today' or 'tomorrow' -- that day's newest plan, and which days have one (the
        user, 2026-10-13: a switch, not the roll to tomorrow once today's games have started);
        none given, the plan's day as before."""
        chosen = worker.league(league)
        today = worker.today()
        days = {"today": today, "tomorrow": today + dt.timedelta(days=1)}
        saved = {name: planpass.saved_plans(chosen, d) for name, d in days.items()}
        if day is None:
            path = worker.newest_plan(chosen)
        elif day in days:
            path = saved[day][-1] if saved[day] else None
        else:
            raise HTTPException(400, f"day is 'today' or 'tomorrow', not {day}")
        if path is None:
            raise HTTPException(404, f"no {chosen.name} plan saved for {(days.get(day) or worker.day()).isoformat()} yet")
        plan = json.loads(path.read_text(encoding="utf-8"))
        nhl_ids = views[chosen.name].owners._nhl_ids(paths.players())
        plan_day = dt.date.fromisoformat(plan["game_date"]) if plan.get("game_date") else today

        def photo(player_id, team):
            return live_games.headshot(nhl_ids.get(int(player_id)), team, plan_day)

        return {"league": chosen.name, "file": path.name,
                "days": {name: {"date": d.isoformat(), "planned": bool(saved[name])} for name, d in days.items()},
                **roster.roster_view(plan, scoresets[chosen.name], slot_rows[chosen.name], photo)}

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
    def get_week(week: str = "this", league: str | None = None):
        chosen, path, _, shown = book(league, week)
        return {"league": chosen.name, "file": path.name, **shown.view(slot_rows[chosen.name])}

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
        return {"league": chosen.name, "file": path.name, "choices": saved, **shown.view(slot_rows[chosen.name])}

    matchups = matchup_module.Matchups()

    @app.get("/matchup")
    def get_matchup(league: str | None = None):
        """The week's matchup: the platform's scores (kept a minute) and the newest plan's
        projection and P(win) (matchup.py)."""
        chosen = worker.league(league)
        if not chosen.readable:
            raise HTTPException(409, f"{chosen.name} has no platform to read its matchup from")
        path = worker.newest_plan(chosen)
        plan = json.loads(path.read_text(encoding="utf-8")) if path else None
        try:
            return {"league": chosen.name, **matchups.view(chosen, plan)}
        except Exception as error:  # noqa: BLE001 -- the platform unreachable or changed: say so
            raise HTTPException(502, f"{chosen.platform}: {type(error).__name__}: {error}")

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
        if body.mode not in planpass.MODES:
            raise HTTPException(422, f"mode must be one of {', '.join(planpass.MODES)}")
        scope = body.scope or ("all" if body.mode in ("full", "quick") else "league")
        if scope not in ("league", "all"):
            raise HTTPException(422, "scope must be 'league' or 'all'")
        only = [worker.league(league).name] if scope == "league" else None
        return worker.start(body.mode, only=only).view()

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str, after: int = 0):
        job = worker.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"no job {job_id}")
        return job.view(after)

    @app.get("/jobs")
    def get_jobs():
        return [job.view(after=len(job.lines)) for job in reversed(list(worker.jobs.values()))]

    @app.get("/status")
    def status(league: str | None = None):
        chosen, planner, args = worker.league(league), worker.planner, worker.args
        path = worker.newest_plan(chosen)
        today = planner is not None and planner.day == worker.day()
        last = {**planner.last, **planner.league_last.get(chosen.name, {})} if today else {}
        return {
            "league": chosen.name, "leagues": list(worker.leagues), "day": worker.day().isoformat(),
            "teams": {name: {"team": lg.team_name, "platform": lg.platform} for name, lg in worker.leagues.items()},
            "job": None if worker.current is None else worker.current.view(after=len(worker.current.lines)),
            "last": {step: iso(when) for step, when in last.items()},
            "plan_file": None if path is None else path.name,
            "plan_saved_at": None if path is None else iso(dt.datetime.fromtimestamp(path.stat().st_mtime)),
            "auto": not args.no_auto, "skip_snapshots": args.skip_snapshots,
            "scheduled": None if not args.jobs else {
                name: {"label": job["label"], "times": job["times"], "shift_min": args.jobs_shift,
                       "last": worker.scheduled_state.get(name)} for name, job in scheduled.JOBS.items()},
            "league_file": args.league_file,
        }

    app.mount("/app", AppFiles(directory=APP, html=True), name="app")
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--league", action="append", default=None,
                        help="A league in Settings/leagues/; repeat for several (default: every active one)")
    parser.add_argument("--date", default=None, help="Game date (default: today on this machine)")
    parser.add_argument("--league-file", default=None,
                        help="A league snapshot JSON instead of the platform (one league)")
    parser.add_argument("--platform-season", type=int, default=None, help="Read a past season's league (rehearsal)")
    parser.add_argument("--now", default=None, help="UTC moment for the per-game lock (default: now; rehearsal)")
    parser.add_argument("--no-auto", action="store_true", help="No auto window")
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
    if not args.no_auto:
        threading.Thread(target=worker.auto_loop, daemon=True).start()
    if args.jobs:
        threading.Thread(target=worker.jobs_loop, daemon=True).start()
    uvicorn.run(make_app(worker), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

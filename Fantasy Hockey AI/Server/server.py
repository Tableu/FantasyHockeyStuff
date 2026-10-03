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

    GET  /plan                 the league's newest plan today: {league, file, saved_at, plan}
    POST /refresh {mode}       'full' or 'quick' -- re-plans EVERY league; returns the job. A tap
                               while a refresh runs joins it
    GET  /jobs/{id}?after=n    the job's state and its progress lines after line n
    GET  /status               the leagues served, the day, the running job, when each step last ran
                               (the league's own read included), the league's newest plan
    GET  /games                today's games: score, clock, your players and your opponent's on each
    GET  /goals                every goal today, newest first, with the fantasy points it earned
    GET  /games/{id}?after=n   one game: line score, team stats, box score with fantasy points, plays
                               after sortOrder n
    GET  /games/{id}/lines     each team's lines, pairs and special-teams units as used (games.py)

A refresh is planpass.Planner's: the snapshots and tonight's projections once, then each league's
read, plan and saved plan files -- one league's failure (its platform down) leaves the others'
plans. The auto window runs here: a quick refresh about 30 minutes before each group of games, once
per group, for every league. The live games' NHL feeds are fetched once for all leagues; each league
sees them with its own rosters and scoring. No login yet: it listens on this PC only until it has
one.
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

import requests  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import seasonlayer  # noqa: E402,F401 -- puts Season/ on sys.path; see seasonlayer.py
import games as live_games  # noqa: E402
import leagues  # noqa: E402
import paths  # noqa: E402
import planpass  # noqa: E402
import simlayer  # noqa: E402

AUTO_CHECK_S = 60               # how often the auto window looks at the clock
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

    def echo(self, text):
        self.lines.append(f"{dt.datetime.now():%H:%M:%S}  {text}")

    def view(self, after=0) -> dict:
        return {"id": self.id, "mode": self.mode, "by": self.by, "state": self.state,
                "started": iso(self.started), "finished": iso(self.finished), "error": self.error,
                "plan_files": self.plan_files, "lines": self.lines[after:], "next": len(self.lines)}


class Worker:
    """Runs one refresh at a time on its own thread, for every league served; keeps the day's
    Planner (each league's board and sampler are built once a day) and the recent jobs."""

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

    def league(self, name=None):
        """A league served, by name (None: the first); a 404 for one it does not serve."""
        if name is None:
            return next(iter(self.leagues.values()))
        if name not in self.leagues:
            raise HTTPException(404, f"this server plans {', '.join(self.leagues)}, not {name}")
        return self.leagues[name]

    def day(self) -> dt.date:
        return dt.date.fromisoformat(self.args.date) if self.args.date else dt.date.today()

    def now(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.args.now) if self.args.now else utc_now()

    def start(self, mode: str, by: str = "tap") -> Job:
        """A new refresh, or the running one if there is one."""
        with self.lock:
            if self.current is not None:
                return self.current
            job = Job(next(self.numbers), mode, by)
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
            results = self.planner.run(job.mode, self.now(), job.echo)
            failed = {name: r for name, r in results.items() if isinstance(r, BaseException)}
            for name in results:
                if name not in failed:
                    job.plan_files[name] = planpass.saved_plans(self.leagues[name], day)[-1].name
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

    def auto_loop(self):
        """A quick refresh ~30 minutes before each group of games, once per group."""
        while True:
            try:
                window = planpass.next_window(self.day(), self.now())
                if window is not None and self.current is None and not self.planned_for(window):
                    self.windows_done.add(window)
                    self.start("quick", by=f"auto: games at {window:%H:%M} UTC")
            except Exception as error:  # noqa: BLE001 -- a bad check must not end the loop
                print(f"auto window check failed: {type(error).__name__}: {error}", file=sys.stderr)
            time.sleep(AUTO_CHECK_S)

    def newest_plan(self, league):
        files = planpass.saved_plans(league, self.day())
        return files[-1] if files else None


class Refresh(BaseModel):
    mode: str = "quick"


def make_app(worker: Worker) -> FastAPI:
    app = FastAPI(title="Fantasy hockey plan server")
    store = live_games.FeedStore()
    views = {name: live_games.Games(lambda league=league: worker.newest_plan(league),
                                    simlayer.load_scoreset(league.scoring), paths.players(), store)
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

    @app.get("/plan")
    def get_plan(league: str | None = None):
        chosen = worker.league(league)
        path = worker.newest_plan(chosen)
        if path is None:
            raise HTTPException(404, f"no {chosen.name} plan saved for {worker.day().isoformat()} yet")
        return {"league": chosen.name, "file": path.name,
                "saved_at": iso(dt.datetime.fromtimestamp(path.stat().st_mtime)),
                "plan": json.loads(path.read_text(encoding="utf-8"))}

    @app.post("/refresh")
    def refresh(body: Refresh):
        if body.mode not in ("full", "quick"):
            raise HTTPException(422, "mode must be 'full' or 'quick'")
        return worker.start(body.mode).view()

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str, after: int = 0):
        job = worker.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"no job {job_id}")
        return job.view(after)

    @app.get("/status")
    def status(league: str | None = None):
        chosen, planner, args = worker.league(league), worker.planner, worker.args
        path = worker.newest_plan(chosen)
        today = planner is not None and planner.day == worker.day()
        last = {**planner.last, **planner.league_last.get(chosen.name, {})} if today else {}
        return {
            "league": chosen.name, "leagues": list(worker.leagues), "day": worker.day().isoformat(),
            "job": None if worker.current is None else worker.current.view(after=len(worker.current.lines)),
            "last": {step: iso(when) for step, when in last.items()},
            "plan_file": None if path is None else path.name,
            "plan_saved_at": None if path is None else iso(dt.datetime.fromtimestamp(path.stat().st_mtime)),
            "auto": not args.no_auto, "skip_snapshots": args.skip_snapshots,
            "league_file": args.league_file,
        }

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
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 in the container")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.league_file and (args.league is None or len(args.league) != 1):
        parser.error("--league-file is one league's snapshot: give that league alone with --league")
    worker = Worker(args)
    if not args.no_auto:
        threading.Thread(target=worker.auto_loop, daemon=True).start()
    uvicorn.run(make_app(worker), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

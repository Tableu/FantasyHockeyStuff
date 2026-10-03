#!/usr/bin/env python
"""The phone app's server (Fantasy Hockey Phone App Plan, Phase 1.5): the plan refreshes behind a
small HTTP API, which the plan window (Live/plan_gui.py) shows. One league, one refresh at a time.

    python server.py                                   # league beagles, today, 127.0.0.1:8000
    python server.py --host 0.0.0.0                    # in the container (compose publishes 8000)
    python server.py --date 2026-09-29 --league-file ../Live/fixtures/beagles/fake_league.json \
        --skip-snapshots --no-auto                     # rehearsal

    GET  /plan                 the day's newest saved plan: {file, saved_at, plan}
    POST /refresh {mode}       'full' or 'quick'; returns the job. A tap while a refresh runs joins it
    GET  /jobs/{id}?after=n    the job's state and its progress lines after line n
    GET  /status               the day, the running job, when each step last ran, the newest plan
    GET  /games                today's games: score, clock, your players and your opponent's on each
    GET  /goals                every goal today, newest first, with the fantasy points it earned
    GET  /games/{id}?after=n   one game: line score, team stats, box score with fantasy points, plays
                               after sortOrder n
    GET  /games/{id}/lines     each team's lines, pairs and special-teams units as used (games.py)

The refreshes are planpass.Planner's, the same steps as run_live.py's, and save the same plan
files. The auto window runs here: a quick refresh about 30 minutes before each group of games,
once per group. No login yet: it listens on this PC only until it has one.
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
        self.plan_file = None

    def echo(self, text):
        self.lines.append(f"{dt.datetime.now():%H:%M:%S}  {text}")

    def view(self, after=0) -> dict:
        return {"id": self.id, "mode": self.mode, "by": self.by, "state": self.state,
                "started": iso(self.started), "finished": iso(self.finished), "error": self.error,
                "plan_file": self.plan_file, "lines": self.lines[after:], "next": len(self.lines)}


class Worker:
    """Runs one refresh at a time on its own thread; keeps the day's Planner (its board and sampler
    are built once a day) and the recent jobs."""

    def __init__(self, args):
        self.args = args
        self.league = leagues.load(args.league)
        self.planner = None
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None
        self.numbers = itertools.count(1)
        self.lock = threading.Lock()
        self.windows_done = set()       # puck times the auto window re-planned since it started

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
                self.planner = planpass.Planner(self.league, day, a.league_file, a.platform_season,
                                                a.skip_snapshots)
            self.planner.run(job.mode, self.now(), job.echo)
            job.plan_file = planpass.saved_plans(self.league, day)[-1].name
            job.state = "done"
        except BaseException as error:  # SystemExit from a step included: report it, keep serving
            job.error = f"{type(error).__name__}: {error}"
            job.echo(job.error)
            job.state = "failed"
        finally:
            job.finished = dt.datetime.now()
            with self.lock:
                self.current = None

    def planned_for(self, window) -> bool:
        """Whether this group of games already has its plan: re-planned by the auto window since
        the server started, or (on the real clock) a plan saved inside its lead -- so a restart
        does not re-run a window the server ran before it, and a tap there counts too."""
        if window in self.windows_done:
            return True
        if self.args.now:                   # rehearsal: the files' real times are not its clock
            return False
        since = (window - planpass.WINDOW_LEAD).to_pydatetime()
        return any(dt.datetime.fromtimestamp(f.stat().st_mtime, dt.timezone.utc).replace(tzinfo=None) >= since
                   for f in planpass.saved_plans(self.league, self.day()))

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

    def newest_plan(self):
        files = planpass.saved_plans(self.league, self.day())
        return files[-1] if files else None


class Refresh(BaseModel):
    mode: str = "quick"


def make_app(worker: Worker) -> FastAPI:
    app = FastAPI(title="Fantasy hockey plan server")
    feeds = live_games.Games(worker.newest_plan, simlayer.load_scoreset(worker.league.scoring),
                             paths.players())

    def nhl(call, *args):
        """A live-games call; the NHL unreachable is a 502, not a crash."""
        try:
            return call(*args)
        except requests.RequestException as error:
            raise HTTPException(502, f"NHL feed: {type(error).__name__}: {error}")

    @app.get("/games")
    def get_games():
        return nhl(feeds.games)

    @app.get("/goals")
    def get_goals():
        return nhl(feeds.goals)

    @app.get("/games/{game_id}")
    def get_game(game_id: int, after: int = -1):
        return nhl(feeds.game, game_id, after)

    @app.get("/games/{game_id}/lines")
    def get_lines(game_id: int):
        return nhl(feeds.lines, game_id)

    @app.get("/plan")
    def get_plan():
        path = worker.newest_plan()
        if path is None:
            raise HTTPException(404, f"no plan saved for {worker.day().isoformat()} yet")
        return {"file": path.name, "saved_at": iso(dt.datetime.fromtimestamp(path.stat().st_mtime)),
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
    def status():
        planner, path, args = worker.planner, worker.newest_plan(), worker.args
        return {
            "league": worker.league.name, "day": worker.day().isoformat(),
            "job": None if worker.current is None else worker.current.view(after=len(worker.current.lines)),
            "last": {} if planner is None or planner.day != worker.day()
                    else {step: iso(when) for step, when in planner.last.items()},
            "plan_file": None if path is None else path.name,
            "plan_saved_at": None if path is None else iso(dt.datetime.fromtimestamp(path.stat().st_mtime)),
            "auto": not args.no_auto, "skip_snapshots": args.skip_snapshots,
            "league_file": args.league_file,
        }

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--league", default=leagues.DEFAULT_LEAGUE, help="A league in Settings/leagues/")
    parser.add_argument("--date", default=None, help="Game date (default: today on this machine)")
    parser.add_argument("--league-file", default=None, help="A league snapshot JSON instead of the platform")
    parser.add_argument("--platform-season", type=int, default=None, help="Read a past season's league (rehearsal)")
    parser.add_argument("--now", default=None, help="UTC moment for the per-game lock (default: now; rehearsal)")
    parser.add_argument("--no-auto", action="store_true", help="No auto window")
    parser.add_argument("--skip-snapshots", action="store_true",
                        help="Use the scheduled snapshots instead of taking fresh ones (faster)")
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 in the container")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    worker = Worker(args)
    if not args.no_auto:
        threading.Thread(target=worker.auto_loop, daemon=True).start()
    uvicorn.run(make_app(worker), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

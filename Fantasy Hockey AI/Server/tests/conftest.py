"""The plan server's tests: offline (no platform, database, NHL feed or data/), seconds, the same
on this PC and in CI (.github/workflows/server-tests.yml).

Every file the code under test writes goes to one temporary folder: FH_STATE_DIR (users, added
leagues, secrets of leagues, scheduled-job state), FH_REPORTS_DIR (plans, choices) and
FH_LOGIN_SECRETS (the signing secret and the league key) are set here, before any project module
is imported, since some read them at import.
"""

import argparse
import itertools
import os
import sys
import tempfile
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
_ROOT = Path(tempfile.mkdtemp(prefix="fh-tests-"))
os.environ["FH_STATE_DIR"] = str(_ROOT / "state")
os.environ["FH_REPORTS_DIR"] = str(_ROOT / "reports")
os.environ["FH_LOGIN_SECRETS"] = str(_ROOT / "secrets.json")
sys.path.insert(0, str(SERVER))

import pytest  # noqa: E402

import auth  # noqa: E402
import leagues  # noqa: E402
import planpass  # noqa: E402
import sync_league_settings  # noqa: E402
import users  # noqa: E402

_teams = itertools.count(900001)


def make_league(platform="fleaflicker", league_id=12090, team_id=None):
    """An added league (state/leagues/) with its settings from the saved platform answers in
    fixtures/settings/ -- no platform read."""
    team_id = team_id if team_id is not None else next(_teams)
    league = leagues.create(platform, league_id, team_id, f"Team {team_id}", "2026-27", "test", private=False)
    rosters, scoring, notes = sync_league_settings.assemble(league, sync_league_settings.from_fixture(league))
    sync_league_settings.write_settings(league, rosters, scoring, notes, write=True)
    return leagues.load(league.name)


_workers = []                           # the test's server, whose jobs end before its leagues go


def drop_league(name):
    """Delete a test league once its jobs are done; Windows can't delete a file another thread
    has open, so a few tries."""
    for worker in _workers:
        wait_idle(worker)
    for attempt in range(20):
        try:
            leagues.delete(leagues.load(name))
            return
        except PermissionError:
            time.sleep(0.1)
        except SystemExit:
            return                      # gone already


@pytest.fixture
def added_league():
    league = make_league()
    yield league
    drop_league(league.name)


@pytest.fixture
def make_user():
    """make_user(*league names) -> (user dict, its cookie value)."""
    cookies = auth.Cookies()

    def make(*names):
        user = users.create()
        for name in names:
            user = users.link(user["id"], name)
        return user, cookies.value(user["id"])
    return make


class Recorder:
    """Planner.run's stand-in: what each refresh was asked for, a fake plan per league."""

    def __init__(self, seconds=0.3):
        self.calls, self.seconds = [], seconds

    # Set on the class as an object, not a function, so it is not bound: no `planner` argument.
    def __call__(self, mode, now, echo=print, only=None, today=None, leagues=None, reuse=True):
        names = [league.name for league in leagues or []]
        self.calls.append({"mode": mode, "leagues": names, "reuse": reuse})
        for name in names:
            echo(f"{name}: plan: fake")
        time.sleep(self.seconds)
        return {name: "plan_fake.json" for name in names}


def server_args(**over):
    base = dict(league=None, date=None, league_file=None, platform_season=None, now=None, no_daily=True,
                skip_snapshots=True, jobs=False, jobs_db=None, jobs_shift=0)
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def server_app(monkeypatch):
    """(worker, call, recorder): a real server with the planner, the goal watcher and the NHL
    schedule stubbed. call(method, path, cookie=None, **kw) sends one request as that browser."""
    import games
    import server
    from fastapi.testclient import TestClient

    recorder = Recorder()
    monkeypatch.setattr(planpass.Planner, "run", recorder)
    monkeypatch.setattr(games.FeedStore, "watch_loop", lambda self: None)
    monkeypatch.setattr(server, "first_puck", lambda day: None)
    worker = server.Worker(server_args())
    _workers.append(worker)
    client = TestClient(server.make_app(worker), base_url="https://testserver")

    def call(method, path, cookie=None, **kw):
        client.cookies.clear()          # each call is its own browser: only the cookie given
        headers = {"Cookie": f"{auth.COOKIE}={cookie}"} if cookie else {}
        return client.request(method, path, headers=headers, **kw)

    yield worker, call, recorder
    wait_idle(worker)                   # a job still running ends before the next test
    _workers.remove(worker)


def wait_idle(worker, seconds=10):
    deadline = time.time() + seconds
    while (worker.current or worker.queued) and time.time() < deadline:
        time.sleep(0.05)

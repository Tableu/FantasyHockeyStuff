"""planpass: which reports a refresh fetches, and which saved plan a league shows."""

import datetime as dt
import os
import time

import pytest

import livepaths
import planpass


@pytest.fixture
def steps(monkeypatch):
    log = []
    monkeypatch.setattr(planpass, "snapshots",
                        lambda kinds, echo, injuries_targeted=False: (log.append(("snap", tuple(kinds))) or set(kinds)))
    monkeypatch.setattr(planpass, "tonight", lambda day, echo: log.append(("tonight",)))
    return log


def quiet(_):
    pass


def test_tap_reuses_fresh_reports(steps):
    p = planpass.Planner([], dt.date.today())
    p._data_steps("full", quiet)
    p._data_steps("quick", quiet)
    assert steps == [("snap", ("injuries", "lines", "goalies")), ("tonight",)]


def test_stale_reports_are_fetched(steps):
    p = planpass.Planner([], dt.date.today())
    p._data_steps("full", quiet)
    for kind in ("injuries", "goalies"):
        p.last[kind] -= planpass.FRESH_FOR[kind] + dt.timedelta(seconds=1)
    p._data_steps("full", quiet)
    assert steps[2:] == [("snap", ("injuries", "goalies")), ("tonight",)]


def test_no_reuse_fetches_everything(steps):
    p = planpass.Planner([], dt.date.today())
    p._data_steps("full", quiet)
    p._data_steps("full", quiet, reuse=False)
    assert steps[2:] == [("snap", ("injuries", "lines", "goalies")), ("tonight",)]


def test_skip_snapshots_still_builds_tonight(steps):
    p = planpass.Planner([], dt.date.today(), skip_snapshots=True)
    p._data_steps("quick", quiet)
    assert steps == [("tonight",)]


class _League:
    name = "fl-test-newest"


def test_newest_saved():
    plans = livepaths.ensure(livepaths.league_reports(_League.name) / "plans")
    today = dt.date.today()
    old = plans / f"plan_{(today - dt.timedelta(days=10)).isoformat()}_0100.json"
    recent = plans / f"plan_{(today - dt.timedelta(days=1)).isoformat()}_0100.json"
    for path in (old, recent):
        path.write_text("{}", encoding="utf-8")
    os.utime(old, (time.time(), time.time()))       # newest on disk, but out of the window
    assert planpass.newest_saved(_League) == recent
    assert planpass.saved_plans(_League, today - dt.timedelta(days=1)) == [recent]

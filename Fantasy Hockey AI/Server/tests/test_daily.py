"""When the daily pass runs: DAILY_LEAD before the day's first game, no later than DAILY_FALLBACK."""

import datetime as dt

import pytest

import server
from conftest import server_args

DAY = dt.date(2026, 11, 15)


@pytest.fixture
def worker(monkeypatch):
    w = server.Worker(server_args(no_daily=False))
    w.scheduled_state = {}
    return w


def at(h, m=0, day=DAY):
    return dt.datetime.combine(day, dt.time(h, m))


def test_capped_at_fallback(worker, monkeypatch):
    monkeypatch.setattr(server, "first_puck", lambda day: at(17, 30))       # a late first game
    assert worker.daily_due(at(15, 0)) is None
    assert worker.daily_due(at(15, 31)) == at(15, 30)


def test_matinee_day(worker, monkeypatch):
    monkeypatch.setattr(server, "first_puck", lambda day: at(11, 0))
    assert worker.daily_due(at(10, 0)) is None
    assert worker.daily_due(at(10, 45)) == at(10, 30)


def test_no_schedule_uses_fallback(worker, monkeypatch):
    monkeypatch.setattr(server, "first_puck", lambda day: None)
    assert worker.daily_due(at(16, 0)) == at(15, 30)


def test_once_a_day(worker, monkeypatch):
    monkeypatch.setattr(server, "first_puck", lambda day: None)
    worker.scheduled_state = {server.DAILY_JOB: at(15, 30).isoformat()}
    assert worker.daily_due(at(20, 0)) is None
    assert worker.daily_due(at(16, 0, DAY + dt.timedelta(days=1))) is not None

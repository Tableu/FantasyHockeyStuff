"""Refreshes: the tap limits, the one queued job, the daily pass, fresh reports."""

import time

import server
from conftest import wait_idle


def test_tap_limit_and_plan_exempt(server_app, added_league, make_user):
    worker, call, recorder = server_app
    _, a = make_user(added_league.name)
    url = f"/refresh?league={added_league.name}"
    assert call("POST", url, a, json={"mode": "league"}).status_code == 200
    assert call("POST", url, a, json={"mode": "quick"}).status_code == 429
    assert call("POST", url, a, json={"mode": "plan"}).status_code == 200    # choices: any time
    wait_idle(worker)
    assert [c["mode"] for c in recorder.calls] == ["league", "plan"]
    assert all(c["reuse"] for c in recorder.calls)                          # taps reuse reports


def test_tap_limit_is_per_league_whoever_asks(server_app, added_league, make_user):
    _, call, _ = server_app
    _, a = make_user(added_league.name)
    _, b = make_user(added_league.name)                     # a co-manager
    url = f"/refresh?league={added_league.name}"
    assert call("POST", url, a, json={"mode": "league"}).status_code == 200
    assert call("POST", url, b, json={"mode": "league"}).status_code == 429


def test_bad_mode(server_app, added_league, make_user):
    _, call, _ = server_app
    _, a = make_user(added_league.name)
    assert call("POST", f"/refresh?league={added_league.name}", a, json={"mode": "nope"}).status_code == 422


def test_asks_during_a_job_merge_into_one_queued_job(server_app):
    worker, _, recorder = server_app
    from conftest import make_league
    one, two = make_league(), make_league()
    running = worker.start("league", only=[one.name])
    q1 = worker.start("news", only=[one.name])
    q2 = worker.start("league", only=[two.name])
    assert q1 is q2 and q1.state == "queued" and q1 is not running
    assert q1.mode == "quick" and q1.only == [one.name, two.name]
    wait_idle(worker)
    assert [c["mode"] for c in recorder.calls] == ["league", "quick"]


def test_merged_mode():
    assert server.merged_mode("news", "league") == "quick"
    assert server.merged_mode("plan", "full") == "full"
    assert server.merged_mode("quick", "plan") == "quick"


def test_daily_pass_is_never_queued(server_app, added_league):
    worker, _, _ = server_app
    running = worker.start("league", only=[added_league.name])
    assert worker.start(server.DAILY_JOB, by="scheduled") is running
    assert worker.queued is None
    # a tap after it still queues normally (no KeyError from the daily pass's mode)
    assert worker.start("quick", only=[added_league.name]).state == "queued"


def test_fresh_reaches_the_planner(server_app, added_league):
    worker, _, recorder = server_app
    worker.start("full", by="added", only=[added_league.name], fresh=True)
    wait_idle(worker)
    assert recorder.calls[-1] == {"mode": "full", "leagues": [added_league.name], "reuse": False}


def test_fresh_survives_a_merge(server_app, added_league):
    worker, _, recorder = server_app
    worker.start("league", only=[added_league.name])
    worker.start("quick", only=[added_league.name])
    worker.start("full", only=[added_league.name], fresh=True)
    wait_idle(worker)
    assert recorder.calls[-1]["reuse"] is False


def test_daily_pass_plans_active_leagues_fresh(server_app, added_league, make_user):
    worker, _, recorder = server_app
    make_user(added_league.name)
    worker.start(server.DAILY_JOB, by="test")
    wait_idle(worker)
    call = recorder.calls[-1]
    assert call["mode"] == "full" and call["reuse"] is False and added_league.name in call["leagues"]


def test_a_failed_league_is_reported_to_its_users_only(server_app, added_league, make_user, monkeypatch):
    worker, call, _ = server_app
    import planpass

    def failing(planner, mode, now, echo=print, only=None, today=None, leagues=None, reuse=True):
        return {lg.name: RuntimeError("platform down") for lg in leagues}
    monkeypatch.setattr(planpass.Planner, "run", failing)
    _, a = make_user(added_league.name)
    _, b = make_user()
    job = worker.start("league", only=[added_league.name])
    wait_idle(worker)
    assert job.state == "failed"
    assert "platform down" in call("GET", f"/jobs/{job.id}", a).json()["error"]
    assert call("GET", f"/jobs/{job.id}", b).json()["error"] is None
    time.sleep(0)

"""Who sees what: a browser's cookie names its user, and a user sees only the leagues it links."""

import datetime as dt

import server
import users


def test_no_cookie_is_asked_to_add_a_league(server_app):
    _, call, _ = server_app
    for path in ("/status", "/choices?league=beagles", "/roster?league=beagles"):
        got = call("GET", path)
        assert got.status_code == 404 and got.json()["detail"]["add"] is True


def test_a_user_sees_its_own_league_only(server_app, added_league, make_user):
    _, call, _ = server_app
    _, a = make_user(added_league.name)
    _, b = make_user()
    got = call("GET", "/status", a)
    assert got.status_code == 200 and got.json()["leagues"] == [added_league.name]
    assert call("GET", f"/choices?league={added_league.name}", a).status_code == 200
    # another browser: nothing; another league: the same 404 as no such league
    assert call("GET", f"/choices?league={added_league.name}", b).json()["detail"]["add"] is True
    assert call("GET", "/choices?league=beagles", a).json()["detail"] == "no league beagles here"


def test_forged_cookie_is_nobody(server_app, added_league, make_user):
    _, call, _ = server_app
    _, a = make_user(added_league.name)
    forged = a.rsplit(".", 1)[0] + "." + "0" * 64
    assert call("GET", "/status", forged).json()["detail"]["add"] is True


def test_leagues_endpoint_and_remove(server_app, make_user):
    from conftest import make_league
    league = make_league()
    _, call, _ = server_app
    _, a = make_user(league.name)
    assert [lg["name"] for lg in call("GET", "/leagues", a).json()["leagues"]] == [league.name]
    assert call("DELETE", f"/leagues/{league.name}", a).json()["leagues"] == []
    assert not league.path.exists()                 # nobody links it: deleted
    assert call("DELETE", "/leagues/beagles", a).status_code == 404


def test_cookie_renewed_on_first_call_of_the_day(server_app, added_league, make_user):
    _, call, _ = server_app
    user, a = make_user(added_league.name)
    assert "set-cookie" not in call("GET", "/status", a).headers       # seen today already
    user = users.load(user["id"])
    user["last_seen"] = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    users._write(user)
    assert "set-cookie" in call("GET", "/status", a).headers
    assert users.load(user["id"])["last_seen"] == dt.date.today().isoformat()


def test_app_files_need_no_user(server_app):
    _, call, _ = server_app
    assert call("GET", "/app/").status_code == 200


def test_line_league():
    assert server.line_league("21:32:01  fl-123-4: plan: x") == "fl-123-4"
    assert server.line_league("21:32:01  fl-123-4 settings: unchanged") == "fl-123-4"
    assert server.line_league("21:32:01  snapshot injuries: 5") == "snapshot"


def test_job_view_hides_other_users_leagues():
    job = server.Job(1, "full", "t", only=["fl-123-45", "fl-123-4"])
    job.echo("fl-123-45: plan: mine")
    job.echo("fl-123-4: plan: theirs")
    job.echo("fl-123-4 settings: theirs")
    job.echo("snapshot injuries: everyone's")
    job.league_errors = {"fl-123-4": "RuntimeError: theirs", "fl-123-45": "RuntimeError: mine"}
    seen = job.view(mine=["fl-123-45"])
    text = " ".join(seen["lines"])
    assert "mine" in text and "everyone's" in text and "theirs" not in text
    assert seen["error"] == "fl-123-45: RuntimeError: mine"
    # a job for none of the caller's leagues: its state only
    other = job.view(mine=["fl-9-9"])
    assert other["lines"] == [] and other["error"] is None and other["mine"] is False


def test_jobs_list_is_filtered(server_app, added_league, make_user):
    worker, call, _ = server_app
    _, a = make_user(added_league.name)
    _, b = make_user()
    worker.start("league", only=[added_league.name])
    from conftest import wait_idle
    wait_idle(worker)
    assert len(call("GET", "/jobs", a).json()) == 1
    assert call("GET", "/jobs", b).json() == []

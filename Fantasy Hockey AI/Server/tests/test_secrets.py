"""Secrets: ESPN cookies encrypted at rest, one signing secret, safe JSON writes."""

import threading

import auth
import jsonfile
import league_secrets
import leagues


def test_league_cookies_round_trip_encrypted():
    league_secrets.put("espn-1-2", {"espn_s2": "AEBsecretvalue", "SWID": "{ABC}"})
    try:
        assert league_secrets.get("espn-1-2") == {"espn_s2": "AEBsecretvalue", "SWID": "{ABC}"}
        stored = (leagues.state_root() / "league_secrets.json").read_text(encoding="utf-8")
        assert "AEBsecretvalue" not in stored and "espn-1-2" in stored
    finally:
        league_secrets.remove("espn-1-2")
    assert league_secrets.get("espn-1-2") is None


def test_signing_secret_is_made_once():
    assert auth.user_secret() == auth.user_secret()


def test_update_is_serialised(tmp_path):
    path = tmp_path / "counter.json"

    def bump(data):
        data["n"] = data.get("n", 0) + 1
        return data, data["n"]

    threads = [threading.Thread(target=lambda: [jsonfile.update(path, bump) for _ in range(25)]) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert jsonfile.read(path)["n"] == 100


def test_write_leaves_no_tmp(tmp_path):
    jsonfile.write(tmp_path / "a" / "b.json", {"x": 1})
    assert jsonfile.read(tmp_path / "a" / "b.json") == {"x": 1}
    assert not list((tmp_path / "a").glob("*.tmp"))

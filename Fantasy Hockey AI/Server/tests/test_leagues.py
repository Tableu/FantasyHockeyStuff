"""The league registry (leagues.py): the hand-made leagues, added leagues and their settings."""

import json
import os

import league as league_module
import leagues
import simlayer
import sync_league_settings as sls
from conftest import make_league


def test_hand_made_leagues_load():
    beagles = leagues.load("beagles")
    assert (beagles.platform, beagles.league_id, beagles.team_id) == ("fleaflicker", 12090, 63341)
    assert not leagues.is_added(beagles)
    assert beagles.rules == "beagles" and beagles.scoring == "beagles"   # names, not paths
    espn_la = leagues.load("espn-la")
    assert (espn_la.platform, espn_la.league_id) == ("espn", 1747127466)
    assert leagues.load("espn").league_id is None


def test_added_league_lives_in_state(added_league):
    assert leagues.is_added(added_league)
    assert added_league.path.parent == leagues.state_dir()
    assert added_league.name == leagues.added_name("fleaflicker", 12090, added_league.team_id)
    # rules and scoring resolved to its own files, which load
    config = league_module.load(added_league.rules)
    assert config.teams == 14
    assert simlayer.load_scoreset(added_league.scoring).skaters["goals"] == 4.0
    # the file itself keeps names, not paths
    raw = json.loads(added_league.path.read_text(encoding="utf-8"))
    assert raw["rules"] == raw["scoring"] == added_league.name


def test_find_and_index(added_league):
    assert leagues.find("fleaflicker", 12090, added_league.team_id).name == added_league.name
    assert leagues.find("fleaflicker", 12090, 63341).name == "beagles"
    assert leagues.find("fleaflicker", 12090, 1) is None
    known = leagues.index()
    assert ("espn", 1747127466, 5) in known


def test_broken_league_file_is_skipped(added_league):
    broken = leagues.state_dir() / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    try:
        names = [lg.name for lg in leagues.everything()]
        assert added_league.name in names and "broken" not in names
        assert leagues.find("fleaflicker", 12090, added_league.team_id) is not None
    finally:
        os.remove(broken)


def test_delete_removes_every_file():
    league = make_league()
    rosters, scoring = leagues.settings_dirs(league)
    leagues.delete(league)
    for path in (league.path, rosters / f"{league.name}.json", scoring / f"{league.name}.json"):
        assert not path.exists()


def test_hand_made_league_is_never_deleted():
    beagles = leagues.load("beagles")
    leagues.delete(beagles)
    assert beagles.path.exists()


def test_espn_added_league_settings():
    league = make_league("espn", 1747127466)
    try:
        assert league_module.load(league.rules).teams == 10
        rosters, scoring, notes = sls.assemble(league, sls.from_fixture(league))
        changes, _, _ = sls.write_settings(league, rosters, scoring, notes, write=False)
        assert changes == []            # re-detected: the same as written
    finally:
        leagues.delete(league)


def test_supported():
    beagles = leagues.load("beagles")
    rosters, scoring, _ = sls.assemble(beagles, sls.from_fixture(beagles))
    assert sls.supported(rosters, scoring) is None
    assert "points" in sls.supported(rosters, {"skaters": {}, "goalies": {}})
    assert "skater" in sls.supported(rosters, {"skaters": {}, "goalies": {"wins": 3}})


def test_added_overrides_make_a_valid_schedule(added_league):
    assert league_module.load(added_league.rules).min_first_week_games == \
        leagues.ADDED_OVERRIDES["schedule"]["min_first_week_games"]


def test_settings_fixtures_translate():
    """The offline check: each hand-made league's saved platform answers give exactly its files."""
    assert sls.check() == 0

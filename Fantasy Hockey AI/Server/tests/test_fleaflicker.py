"""The Fleaflicker adapter's settings translation on saved answers (fixtures/settings/)."""

import copy
import json

import livepaths
from platforms import fleaflicker

FOLDER = livepaths.FIXTURES_DIR / "settings" / "fleaflicker-12090"


def read(name):
    return json.loads((FOLDER / name).read_text(encoding="utf-8"))


def settings(board):
    return fleaflicker.settings_from(read("FetchLeagueRules.json"), (FOLDER / "rules.html").read_text(encoding="utf-8"),
                                     read("FetchLeagueScoreboard.json"), board, read("teams.json")["teams"])


def test_picks_from_skips_cells_without_a_pick():
    board = copy.deepcopy(read("FetchLeagueDraftBoard.json"))
    full = len(fleaflicker.picks_from(board))
    board["rows"][0]["cells"].append({"note": "an empty cell"})        # no slot, no team
    assert len(fleaflicker.picks_from(board)) == full


def test_snake_read_from_the_board():
    assert settings(read("FetchLeagueDraftBoard.json"))["settings"]["draft"]["type"] == "snake"


def test_no_draft_order_is_snake_with_a_note():
    board = copy.deepcopy(read("FetchLeagueDraftBoard.json"))
    for row in board.get("rows", []):
        row["cells"] = [{} for _ in row.get("cells", [])]            # league 12088's empty board
    out = settings(board)
    assert out["settings"]["draft"]["type"] == "snake"
    assert any("no draft order" in note for note in out["notes"])

"""The fantasy leagues we play, read from the registry the model side keeps:
`Fantasy Hockey AI/Settings/leagues/<name>.json` (see `Fantasy Hockey AI/Live/leagues.py` for the
fields). The pipeline only needs which platform league to read -- e.g. the Fleaflicker league whose
positions, player ids and injury designations it imports -- so it reads the JSON by path rather
than importing that folder's code.
"""

import json
from pathlib import Path

from nhl_pipeline.config import PROJECT_ROOT

LEAGUES_DIR = PROJECT_ROOT.parent / "Fantasy Hockey AI" / "Settings" / "leagues"


def load(name: str) -> dict:
    path = LEAGUES_DIR / f"{name}.json"
    if not path.exists():
        known = ", ".join(sorted(p.stem for p in LEAGUES_DIR.glob("*.json"))) or "none"
        raise SystemExit(f"no league {name!r} in {LEAGUES_DIR} (have: {known})")
    return json.loads(path.read_text(encoding="utf-8"))


def fleaflicker_league_id(name: str | None = None) -> int:
    """The Fleaflicker league id of `name`, or of the first active Fleaflicker league."""
    if name is not None:
        league = load(name)
        if league["platform"] != "fleaflicker" or league["league_id"] is None:
            raise SystemExit(f"league {name!r} is not a Fleaflicker league with an id")
        return int(league["league_id"])
    for path in sorted(LEAGUES_DIR.glob("*.json")):
        league = json.loads(path.read_text(encoding="utf-8"))
        if league["platform"] == "fleaflicker" and league["active"] and league["league_id"] is not None:
            return int(league["league_id"])
    raise SystemExit(f"no active Fleaflicker league in {LEAGUES_DIR}")

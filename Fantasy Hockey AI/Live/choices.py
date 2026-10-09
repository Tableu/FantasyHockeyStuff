"""What the user chose in the plan window, per league -- reports/<league>/choices.json, through the
server's /choices:

    {"upgrade_drops": [player id, ...],             AI Suggestions: who an upgrade may drop
     "days": {"2026-10-08": {"moves": [{"add": id, "drop": id or null}]}},   the Weekly planner tab's picks
                                                    that day, with that drop or (null) into an open
                                                    spot: the window's own week (Live/weekbook.py),
                                                    never planned on (the user, 2026-10-08)
     "updated_at": ...}

An empty upgrade list, or no file: the model chooses (Decisions/adddrop.py). A marked player may be
anyone held -- goalies and starters included; a drop may leave a lineup slot empty, priced at the
points it loses, as every move may under roster.fill_check = none (Decisions/strategy.py). A drop
still pays its drop cost, and forced drops (an IR activation into a full roster) stay the model's.
Days before the plan's day are ignored and dropped on the next save.

Until 2026-10-07 this was droppable.py: one list for upgrades and rentals alike (the Roster tab).
Until 2026-10-08 each day also had an OK-to-drop list for the plan's own rentals; the user now
picks each rental's drop with it (the window builds the week, Live/weekbook.py).
"""

import datetime as dt
import json
import os

import livepaths

def _path(league: str):
    return livepaths.league_reports(league) / "choices.json"


def _tidy(raw: dict, day: dt.date | None) -> dict:
    """Ids as ints, lists sorted, empty days and days before `day` left out."""
    out = {"upgrade_drops": sorted({int(p) for p in raw.get("upgrade_drops", [])}),
           "days": {}, "updated_at": raw.get("updated_at")}
    for day_text, chosen in sorted((raw.get("days") or {}).items()):
        if day is not None and dt.date.fromisoformat(day_text) < day:
            continue
        moves = [{"add": int(m["add"]), "drop": None if m.get("drop") is None else int(m["drop"])}
                 for m in chosen.get("moves", [])]
        if moves:
            out["days"][day_text] = {"moves": moves}
    return out


def load(league: str, day: dt.date | None = None) -> dict:
    """The league's choices, days before `day` left out: {"upgrade_drops": [...], "days": {...},
    "updated_at"}. An old droppable.json (one list) reads as the upgrade list."""
    path = _path(league)
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        old = livepaths.league_reports(league) / "droppable.json"
        raw = ({"upgrade_drops": json.loads(old.read_text(encoding="utf-8")).get("player_ids", [])}
               if old.exists() else {})
    return _tidy(raw, day)


def save(league: str, chosen: dict, day: dt.date | None = None) -> dict:
    """Replace the league's choices (written then swapped in, as the plan server reads them while
    a refresh plans). Returns them as saved."""
    path = livepaths.ensure(livepaths.league_reports(league)) / _path(league).name
    body = _tidy({**chosen, "updated_at": dt.datetime.now().isoformat(timespec="seconds")}, day)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body), encoding="utf-8")
    os.replace(tmp, path)
    return body


def upgrade_drops(chosen: dict):
    """Who upgrades may drop, as the view carries them (adddrop: `upgrade_drops`), or None."""
    return set(chosen.get("upgrade_drops", [])) or None


def saved_on(chosen: dict):
    """The date the choices were last saved (a datetime.date), or None."""
    stamp = chosen.get("updated_at")
    return dt.datetime.fromisoformat(stamp).date() if stamp else None

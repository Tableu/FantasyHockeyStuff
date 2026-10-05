"""The players the user marked OK to drop, per league (the plan window's Roster tab, through the
server's /droppable): livepaths.droppable(league), {"player_ids": [...], "updated_at": ...}.

When any are marked, they replace the model's own choice of drops -- the upgrades' (Decisions/
adddrop.py) and the rentals' streaming spots (Decisions/streaming.py spots), goalies and players
above replacement included -- except the players the week plan itself picks up and drops again
within the plan, which it may still drop. A drop still pays its drop cost, and forced drops (an IR
activation into a full roster) stay the model's. None marked, or no file: the model chooses.
"""

import datetime as dt
import json
import os

import livepaths


def load(league: str) -> set | None:
    """The marked players' ids, or None when there are none (the model chooses)."""
    path = livepaths.droppable(league)
    if not path.exists():
        return None
    ids = {int(p) for p in json.loads(path.read_text(encoding="utf-8")).get("player_ids", [])}
    return ids or None


def save(league: str, player_ids) -> list:
    """Replace the league's list (written then swapped in, as the plan server reads it while a
    refresh plans); an empty list clears it. Returns the ids saved, sorted."""
    ids = sorted({int(p) for p in player_ids})
    path = livepaths.ensure(livepaths.league_reports(league)) / livepaths.droppable(league).name
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"player_ids": ids,
                               "updated_at": dt.datetime.now().isoformat(timespec="seconds")}),
                   encoding="utf-8")
    os.replace(tmp, path)
    return ids

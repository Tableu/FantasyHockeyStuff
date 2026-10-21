"""What the user chose in the plan window, per league -- reports/<league>/choices.json, through the
server's /choices:

    {"upgrade_drops": [player id, ...],             AI Suggestions: who an upgrade may drop
     "days": {"2026-10-08": {"moves": [{"add": id, "drop": id or null}]}},   the Weekly planner tab's picks
                                                    that day, with that drop or (null) into an open
                                                    spot: the window's own week (Live/weekbook.py),
                                                    never planned on (the user, 2026-10-08)
     "together": true,                              AI Suggestions: the week plans planned after
                                                    today's upgrades (true, the default) or apart,
                                                    on the roster before them (false; the user,
                                                    2026-10-20 -- orchestrator.DailyPlan.week_view_on)
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

import copy
import datetime as dt
import json
import os

import livepaths

def _path(league: str):
    return livepaths.league_reports(league) / "choices.json"


def _tidy(raw: dict, day: dt.date | None) -> dict:
    """Ids as ints, lists sorted, empty days and days before `day` left out."""
    out = {"upgrade_drops": sorted({int(p) for p in raw.get("upgrade_drops", [])}),
           "days": {}, "together": bool(raw.get("together", True)), "updated_at": raw.get("updated_at")}
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


def together(chosen: dict) -> bool:
    """Whether the week plans are planned after today's upgrades (else apart, before them)."""
    return chosen.get("together", True)


def saved_on(chosen: dict):
    """The date the choices were last saved (a datetime.date), or None."""
    stamp = chosen.get("updated_at")
    return dt.datetime.fromisoformat(stamp).date() if stamp else None


# ---------- your picks, edited (the plan window and the app's server) ----------
# Moved from plan_gui.PlanWindow (2026-10-10) so the phone app's server edits picks the same way.

def day_choice(chosen: dict, day: str) -> dict:
    """`day`'s entry in `chosen`, made if it has none: {"moves": [...]}."""
    return chosen.setdefault("days", {}).setdefault(day, {"moves": []})


def picked(chosen: dict, day: str) -> set:
    """The players picked on `day`."""
    return {m["add"] for m in chosen.get("days", {}).get(day, {}).get("moves", [])}


def pin(chosen: dict, day: str, player_id: int, drop=None):
    """Pick a free agent on `day` in `chosen` (changed in place), dropping `drop` -- None: into an
    open spot. Picked already: nothing changes."""
    moves = day_choice(chosen, day)["moves"]
    if all(m["add"] != player_id for m in moves):
        moves.append({"add": player_id, "drop": drop})


def unpin(chosen: dict, day: str, player_id: int):
    """Take `player_id`'s pick on `day` out of `chosen` (changed in place)."""
    entry = day_choice(chosen, day)
    entry["moves"] = [m for m in entry["moves"] if m["add"] != player_id]


def replace_rental(chosen: dict, day: str, rental: int, player_id: int):
    """`player_id` in place of your rental `rental` from `day` on, in `chosen` (changed in place):
    picked the same day the rental was added, the new player takes his move (and his drop);
    later, he drops the rental. `rental` may be a rostered player too (the app's Weekly planner):
    the new player then drops him."""
    entry = day_choice(chosen, day)
    his = next((m for m in entry["moves"] if m["add"] == rental), None)
    if his is not None:
        his["add"] = player_id
    else:
        entry["moves"].append({"add": player_id, "drop": rental})


def clear_days(chosen: dict, days):
    """Forget your picks on `days` (a week's move days), in `chosen` (changed in place)."""
    chosen["days"] = {d: c for d, c in chosen.get("days", {}).items() if d not in set(days)}


def plan_drop(plan: dict, index: int, chosen: dict):
    """Who slot `index` of week plan `plan` drops, for a pick in your week: its drop when you hold
    him then, else -- an earlier slot's rental -- whoever you picked in that slot, or (none picked)
    that slot's pick, picked into `chosen` with its own drop."""
    drop = plan["moves"][index].get("drop_id")
    earlier = next((i for i, m in enumerate(plan["moves"][:index]) if m.get("add_id") == drop), None)
    if drop is None or earlier is None:
        return drop
    slot = plan["moves"][earlier]
    options = {o.get("player_id") for o in slot.get("options") or []} | {drop}
    mine = next((m["add"] for m in chosen.get("days", {}).get(slot["day"], {}).get("moves", [])
                 if m["add"] in options), None)
    if mine is not None:
        return mine
    pin(chosen, slot["day"], drop, plan_drop(plan, earlier, chosen))
    return drop


def pick_from_plan(chosen: dict, plan: dict, index: int, player_id: int) -> dict:
    """A week plan's slot option chosen: `chosen` with him picked on the slot's day with the plan's
    drop -- or, picked already, taken back. A drop the plan only picks up earlier (a rental chain)
    is that earlier slot's player you picked, or the plan's own pick there, picked with it.
    Returns a changed copy."""
    new = copy.deepcopy(chosen)
    day = plan["moves"][index]["day"]
    if player_id in picked(new, day):
        unpin(new, day, player_id)
    else:
        pin(new, day, player_id, plan_drop(plan, index, new))
    return new

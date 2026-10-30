"""The phone app's Roster screen (plan phone-app-pwa.md, P1): the newest plan's roster as it stands
now -- the lineup (the plan's lineup_now), the bench and IR -- each player with tonight's projection,
his season so far in the league's scoring, and the plan's word on him. Live points come apart from these, from
games.Games.tonight: they change every 20 s, these only with a new plan.

    roster_view(plan, scoreset, slot_rows, photo)
                                              {day, proj_tonight, next_puck, columns, groups:
                                               [{name, goalie, rows}]}
    ahead(view, plan, date, workbench)        the same for a night ahead with no plan of its own
    regroup(view, slots, slot_rows)           a day gone's rows in the slots the platform kept that day
    week_dates(day, starts_on)                the fantasy week's seven days, for the day picker
"""

import datetime as dt
import re

GOALIE_KEYS = ("wins", "losses", "ot_losses", "shutouts", "saves", "goals_against")
TEAM_SUFFIX = re.compile(r"\s*\(([A-Z]{2,3})\)\s*$")
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def week_dates(day: dt.date, starts_on: str) -> list:
    """The seven days of the fantasy week `day` is in (the league's week_starts_on)."""
    start = day - dt.timedelta(days=(day.weekday() - WEEKDAYS.index(starts_on)) % 7)
    return [start + dt.timedelta(days=i) for i in range(7)]


def proj_of(plan: dict) -> float:
    """The plan's lineup's expected points on its day: its lineup as it stands, players with a game."""
    return round(sum(s["mean"] for s in plan.get("lineup_now", []) if s.get("player") and s.get("puck_utc")), 2)


def split_name(full: str) -> tuple:
    """'Sebastian Aho (CAR)' -> ('S. Aho', 'CAR'), the design's short name and the team."""
    match = TEAM_SUFFIX.search(full or "")
    name = TEAM_SUFFIX.sub("", full or "")
    first, _, last = name.partition(" ")
    short = f"{first[:1]}. {last}" if last else name
    return short, match.group(1) if match else None


def season(stats: dict, weights: dict, goalie: bool) -> dict:
    """The season so far: his counts, fantasy points (the league's weights), points a game, and
    for a goalie save percentage."""
    stats = dict(stats or {})
    gp = stats.get("gp") or 0
    fp = round(sum(w * stats.get(k, 0) for k, w in weights.items()), 2) if gp else 0.0
    out = {**stats, "fp": fp, "fp_per_game": round(fp / gp, 2) if gp else None}
    if goalie:
        faced = stats.get("saves", 0) + stats.get("goals_against", 0)
        out["sv_pct"] = round(stats.get("saves", 0) / faced, 3) if faced else None
    return out


def roster_view(plan: dict, scoreset, slot_rows: list, photo=lambda player_id, team: None) -> dict:
    """`slot_rows`: the league's lineup slots in its own order (C C LW LW ... G G), the lineup's order.
    `photo(player_id, team)`: his headshot's URL, or None."""
    by_name = {r["player"]: r for r in plan.get("roster", [])}
    columns = plan.get("stat_columns", [])

    def row_of(r: dict, slot: dict | None, where: str) -> dict:
        goalie = r["positions"] == "G"
        short, team = split_name(r["player"])
        weights = scoreset.goalies if goalie else scoreset.skaters
        if slot is not None:
            xpts, p_plays, puck, locked = slot["mean"], slot["p_plays"], slot["puck_utc"], slot["locked"]
        else:
            # Bench and IR: no lineup slot, so tonight's points are his rate if he plays.
            p_plays = r.get("plays_tonight")
            xpts = round(r["per_game"] * p_plays, 2) if p_plays else None
            puck, locked = None, False
        return {"player_id": r["player_id"], "player": r["player"], "name": short, "team": team,
                "slot": slot["slot"] if slot is not None else ("IR" if where == "ir" else "BN"),
                "positions": r["positions"], "goalie": goalie, "where": where,
                "photo": photo(r["player_id"], team),
                "flag": (slot or {}).get("flag") or r.get("status"),
                "xpts": xpts, "p_plays": p_plays, "puck_utc": puck, "locked": locked,
                "rate": r.get("rate"), "ros": r.get("ros_points"), "games_left": r.get("games_left"),
                "season": season(r.get("stats"), weights, goalie)}

    lineup, empty = [], 0
    rank = {slot: i for i, slot in enumerate(dict.fromkeys(slot_rows))}
    for slot in sorted(plan.get("lineup_now", []), key=lambda s: rank.get(s["slot"], len(rank))):
        r = by_name.get(slot["player"]) if slot.get("player") else None
        if r is None:
            empty += slot.get("player") is None
            continue
        lineup.append(row_of(r, slot, "lineup"))
    bench = [row_of(by_name[n], None, "bench") for n in plan.get("bench_now", []) if n in by_name]
    ir = [row_of(r, None, "ir") for r in plan.get("roster", []) if r.get("on_ir")]
    tonight = [s for s in plan.get("lineup_now", []) if s.get("player") and s.get("puck_utc")]
    ahead = sorted(s["puck_utc"] for s in tonight if not s.get("locked"))
    return {
        "day": plan.get("game_date"), "games_today": plan.get("games_today", True),
        "team": plan.get("team"), "file_generated_at": plan.get("generated_at"),
        "proj_tonight": proj_of(plan),
        "next_puck": ahead[0] if ahead else None, "empty_slots": empty,
        "columns": {"skater": [c for c in columns if c[0] not in GOALIE_KEYS],
                    "goalie": [["gp", "GP"]] + [c for c in columns if c[0] in GOALIE_KEYS]},
        "groups": groups_of(lineup, bench, ir),
    }


def groups_of(lineup: list, bench: list, ir: list) -> list:
    """The screen's groups: lineup, bench and IR, skaters apart from goalies, empty ones left out."""
    groups = [("Lineup", False, [x for x in lineup if not x["goalie"]]),
              ("Lineup · G", True, [x for x in lineup if x["goalie"]]),
              ("Bench", False, [x for x in bench if not x["goalie"]]),
              ("Bench · G", True, [x for x in bench if x["goalie"]]),
              ("IR", False, [x for x in ir if not x["goalie"]]),
              ("IR · G", True, [x for x in ir if x["goalie"]])]
    return [{"name": name, "goalie": goalie, "rows": rows} for name, goalie, rows in groups if rows]


def regroup(view: dict, slots: dict, slot_rows: list) -> dict:
    """A day gone as the platform kept it (the user, 2026-10-30): `view`'s rows (that day's plan)
    put in that day's slots ({player id: slot label}) -- the lineup in the league's slot order,
    then the bench (BN) and IR. A player the platform had that day but the plan didn't is left out
    (`missing` counts them); one the plan had but the platform didn't, too."""
    rank = {slot: i for i, slot in enumerate(dict.fromkeys(slot_rows))}
    rows = {int(r["player_id"]): r for g in view["groups"] for r in g["rows"]}
    placed = [{**rows[pid], "slot": label, "where": "ir" if label == "IR" else "bench" if label == "BN" else "lineup"}
              for pid, label in slots.items() if pid in rows]
    lineup = sorted((r for r in placed if r["where"] == "lineup"), key=lambda r: rank.get(r["slot"], len(rank)))
    return {**view, "groups": groups_of(lineup, [r for r in placed if r["where"] == "bench"],
                                        [r for r in placed if r["where"] == "ir"]),
            "missing": sum(pid not in rows for pid in slots)}


def ahead(view: dict, plan: dict, date: str, workbench: dict | None) -> dict:
    """A night ahead with no plan of its own (the day picker): `view` (today's plan's roster_view)
    with each player's expected points that night from plan A's workbench -- none without a game
    -- and the lineup's from the plan's matchup projection (else the lineup's sum). No P%: the
    workbench's values have the chance he plays in them."""
    values = {int(k): p.get("values", {}) for k, p in ((workbench or {}).get("players") or {}).items()}
    rows = [r for g in view["groups"] for r in g["rows"]]
    for r in rows:
        r.update(xpts=values.get(int(r["player_id"]), {}).get(date), p_plays=None, puck_utc=None, locked=False)
    proj = (((plan.get("matchup_projection") or {}).get("me") or {}).get("nights") or {}).get(date)
    if proj is None:
        proj = sum(r["xpts"] or 0 for r in rows if r["where"] == "lineup")
    return {**view, "day": date, "proj_tonight": round(proj, 2), "next_puck": None, "empty_slots": 0,
            "games_today": any(r["xpts"] is not None for r in rows)}

"""The phone app's Roster screen (plan phone-app-pwa.md, P1): the newest plan's roster as it stands
now -- the lineup (the plan's lineup_now), the bench and IR -- each player with tonight's projection,
his season so far in the league's scoring, and the plan's word on him. The window's Tonight table
shows the same rows (Live/plan_gui.py, PlanWindow.show). Live points come apart from these, from
games.Games.tonight: they change every 20 s, these only with a new plan.

    roster_view(plan, scoreset, slot_rows)    {day, proj_tonight, next_puck, columns, groups:
                                               [{name, goalie, rows}]}
"""

import re

GOALIE_KEYS = ("wins", "losses", "ot_losses", "shutouts", "saves", "goals_against")
TEAM_SUFFIX = re.compile(r"\s*\(([A-Z]{2,3})\)\s*$")


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
    fp = round(sum(w * stats.get(k, 0) for k, w in weights.items()), 1) if gp else 0.0
    out = {**stats, "fp": fp, "fp_per_game": round(fp / gp, 2) if gp else None}
    if goalie:
        faced = stats.get("saves", 0) + stats.get("goals_against", 0)
        out["sv_pct"] = round(stats.get("saves", 0) / faced, 3) if faced else None
    return out


def notes(row: dict, slot: dict | None, watch: dict, goalie_note: dict | None) -> list:
    """What the plan says about him, for the row's tap-open line."""
    out = []
    w = watch.get(row["player"])
    if w:
        out.append(w["status"] + (f": {w['note']}" if w.get("note") else ""))
    if goalie_note is not None:
        out.append(f"P(start) {goalie_note['p_start']:.0%}" + (f" · {goalie_note['note']}" if goalie_note.get("note") else ""))
    if slot is not None and slot.get("after_moves"):
        out.append(f"After the plan's moves, {slot['after_moves']} plays this slot")
    if row.get("plan"):
        out.append(f"Plan: {row['plan']}")
    if row.get("on_waivers"):
        out.append("On waivers")
    return out


def roster_view(plan: dict, scoreset, slot_rows: list) -> dict:
    """`slot_rows`: the league's lineup slots in its own order (C C LW LW ... G G), the lineup's order."""
    by_name = {r["player"]: r for r in plan.get("roster", [])}
    watch = {w["player"]: w for w in plan.get("watch", [])}
    goalie_notes = {g["player"]: g for g in plan.get("goalies", [])}
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
                "flag": (slot or {}).get("flag") or r.get("status"),
                "xpts": xpts, "p_plays": p_plays, "puck_utc": puck, "locked": locked,
                "rate": r.get("rate"), "ros": r.get("ros_points"), "games_left": r.get("games_left"),
                "season": season(r.get("stats"), weights, goalie),
                "notes": notes(r, slot, watch, goalie_notes.get(r["player"]) if goalie else None)}

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
    groups = [("Lineup", False, [x for x in lineup if not x["goalie"]]),
              ("Lineup · G", True, [x for x in lineup if x["goalie"]]),
              ("Bench", False, [x for x in bench if not x["goalie"]]),
              ("Bench · G", True, [x for x in bench if x["goalie"]]),
              ("IR", False, [x for x in ir if not x["goalie"]]),
              ("IR · G", True, [x for x in ir if x["goalie"]])]
    return {
        "day": plan.get("game_date"), "games_today": plan.get("games_today", True),
        "team": plan.get("team"), "file_generated_at": plan.get("generated_at"),
        "proj_tonight": round(sum(s["mean"] for s in tonight), 1),
        "next_puck": ahead[0] if ahead else None, "empty_slots": empty,
        "columns": {"skater": [c for c in columns if c[0] not in GOALIE_KEYS],
                    "goalie": [["gp", "GP"]] + [c for c in columns if c[0] in GOALIE_KEYS]},
        "groups": [{"name": name, "goalie": goalie, "rows": rows} for name, goalie, rows in groups if rows],
    }

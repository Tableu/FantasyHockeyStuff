"""The phone app's Roster screen (plan phone-app-pwa.md, P1): the newest plan's roster as it stands
now -- the lineup (the plan's lineup_now), the bench and IR -- each player with his season so far in
the league's scoring and the plan's word on him. The day's view went to the Matchup's Players
(the user, 2026-11-08); the Roster is the Season table.

    roster_view(plan, scoreset, slot_rows, photo)
                                              {team, columns, groups: [{name, goalie, rows}]}
    week_dates(day, starts_on)                the fantasy week's seven days (the Matchup's, the Plan's)
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


def columns_of(plan: dict) -> dict:
    """The league's scoring stats as the app's columns, skaters' apart from goalies' (GP first)."""
    columns = plan.get("stat_columns", [])
    return {"skater": [c for c in columns if c[0] not in GOALIE_KEYS],
            "goalie": [["gp", "GP"]] + [c for c in columns if c[0] in GOALIE_KEYS]}


def roster_view(plan: dict, scoreset, slot_rows: list, photo=lambda player_id, team: None) -> dict:
    """`slot_rows`: the league's lineup slots in its own order (C C LW LW ... G G), the lineup's order.
    `photo(player_id, team)`: his headshot's URL, or None."""
    by_name = {r["player"]: r for r in plan.get("roster", [])}

    def row_of(r: dict, slot: dict | None, where: str) -> dict:
        goalie = r["positions"] == "G"
        short, team = split_name(r["player"])
        weights = scoreset.goalies if goalie else scoreset.skaters
        return {"player_id": r["player_id"], "player": r["player"], "name": short, "team": team,
                "positions": r["positions"], "goalie": goalie, "where": where,
                "photo": photo(r["player_id"], team),
                "flag": (slot or {}).get("flag") or r.get("status"),
                "rate": r.get("rate"), "ros": r.get("ros_points"), "games_left": r.get("games_left"),
                "season": season(r.get("stats"), weights, goalie)}

    lineup = []
    rank = {slot: i for i, slot in enumerate(dict.fromkeys(slot_rows))}
    for slot in sorted(plan.get("lineup_now", []), key=lambda s: rank.get(s["slot"], len(rank))):
        r = by_name.get(slot["player"]) if slot.get("player") else None
        if r is None:
            continue
        lineup.append(row_of(r, slot, "lineup"))
    bench = [row_of(by_name[n], None, "bench") for n in plan.get("bench_now", []) if n in by_name]
    ir = [row_of(r, None, "ir") for r in plan.get("roster", []) if r.get("on_ir")]
    return {
        "team": plan.get("team"), "file_generated_at": plan.get("generated_at"),
        "columns": columns_of(plan),
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

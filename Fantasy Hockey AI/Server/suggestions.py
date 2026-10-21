"""The phone app's AI suggestions screen (plan phone-app-pwa.md, P2): the newest plan's three
sections as the window's AI Suggestions tab shows them (Live/plan_gui.py) -- today's moves, the
upgrades, and the week plans with their slots and options -- each option marked when you picked
him into your week (Live/choices.py). No "OK to drop" list or Re-plan here (the user, 2026-10-10).
The week plans are planned after today's upgrades or apart from them, as you chose (`together`).

    suggestions_view(plan, chosen, week)    {day, platform, today, upgrades, together, week: {...}}
"""

import datetime as dt

import choices


def weekday(day: str) -> str:
    return dt.date.fromisoformat(day).strftime("%a")


def today_rows(plan: dict) -> list:
    """IR moves, adds and drops, claims and other drops, as the window's Today's moves."""
    rows = [{"kind": "IR: move to", "add": None, "drop": name} for name in plan.get("ir_to", [])]
    rows += [{"kind": "IR: activate", "add": name, "drop": None} for name in plan.get("ir_off", [])]
    for m in plan.get("moves", []):
        notes = []
        if plan.get("moves_from"):          # an add after the day's first puck: from tomorrow
            notes.append(f"adds count from {weekday(plan['moves_from'])}"
                         + (": drop after his game tonight" if m["drop"] else ""))
        if m.get("add_status") not in (None, "ACTIVE"):
            notes.append(f"reported {m['add_status']}")
        rows.append({"kind": m["kind"].capitalize(), "add": m["add"], "add_rate": m.get("add_rate"),
                     "drop": m["drop"], "drop_rate": m.get("drop_rate"), "note": "; ".join(notes)})
    rows += [{"kind": "Waiver claim", "add": c["claim"], "drop": c["drop"], "note": c.get("note") or ""}
             for c in plan.get("claims", [])]
    rows += [{"kind": "Drop", "add": None, "drop": name} for name in plan.get("other_drops", [])]
    return rows


def plans_shown(plan: dict, week: str) -> list:
    """The week plans of the week asked for: next week's exist on the week's last day only."""
    if week == "next" and plan.get("next_week_plans"):
        return plan["next_week_plans"]
    return plan.get("week_plans") or [{"label": "A", "first": None, "week_gain": None,
                                       "moves": plan.get("week_plan", [])}]


def week_view(plan: dict, chosen: dict, week: str) -> dict:
    has_next = bool(plan.get("next_week_plans"))
    which = "next" if week == "next" and has_next else "this"
    out = []
    for p in plans_shown(plan, which):
        if not p.get("moves"):
            continue
        slots = []
        for index, m in enumerate(p["moves"]):
            mine = choices.picked(chosen, m["day"])
            slots.append({
                "index": index, "day": m["day"], "from": m["from"], "until": m.get("until"),
                "team": m.get("team"), "pos": m.get("pos"), "kind": m["kind"],
                "add": m["add"], "add_id": m.get("add_id"), "drop": m["drop"], "drop_id": m.get("drop_id"),
                "gain": m["gain"], "bar": m["bar"], "edge": m["edge"], "expected": m.get("expected"),
                "picked": m.get("add_id") in mine,
                "options": [{**o, "rank": rank, "picked": o.get("player_id") in mine}
                            for rank, o in enumerate(m.get("options") or [], 1)],
                "near": m.get("near") or [],
            })
        out.append({"label": p["label"], "schedule": p.get("schedule") or [m["add"] for m in p["moves"]],
                    "first_day": p["moves"][0]["day"], "games": p.get("games"), "without": p.get("without") or [],
                    "week_gain": p.get("week_gain"), "week_edge": p.get("week_edge"),
                    "expected": p.get("expected"), "thinnest": p.get("thinnest"), "slots": slots})
    empty = ("Streaming decides a day at a time (strategy mode 'daily')." if plan.get("stream_mode") != "week"
             else f"No rentals worth a move {'next' if which == 'next' else 'this'} week.")
    return {"which": which, "has_next": has_next, "week": plan.get("week"), "next_week": plan.get("next_week"),
            "plans": out, "empty": empty}


def suggestions_view(plan: dict, chosen: dict, week: str = "this") -> dict:
    options = plan.get("options", [])
    return {"day": plan.get("game_date"), "platform": plan.get("platform") or "the platform",
            "moves_left": plan.get("moves_left"), "free_moves": plan.get("free_moves"),
            "today": today_rows(plan), "upgrades": options,
            "clearing": sum(1 for o in options if o.get("clears")),
            # How the plan shown was made, and what you chose since (a re-plan runs on the change).
            "together": choices.together(plan.get("choices") or {}), "together_chosen": choices.together(chosen),
            "week": week_view(plan, chosen, week)}

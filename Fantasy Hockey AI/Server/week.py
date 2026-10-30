"""The phone app's Weekly planner (plan phone-app-pwa.md, P3): your week, built from your picks on
the newest plan's plan A workbench -- weekbook.Week puts the picks in and solves each night's lineup; choices.py edits them. Your picks
are yours alone: the server never plans on them (the user, 2026-10-08).

    Book(plan, chosen, week, slot_order, accepts)    the week shown, its picks put in
      .view(slot_rows, show)                         the calendar: per night its points, open slots,
                                                     your picks that day, who starts in each slot,
                                                     and the bench (held, not starting); `show` a
                                                     week plan's label: that plan's week drawn
                                                     instead, its picks not yours marked `ghost`
      .picker(day, occupant)                         the slot picker's free agents
      .drops(day, add, occupant)                     who that pick may drop, with each one's week gain
    edit(chosen, book, body)                         a pick, a rental replaced, a pick taken out, the
                                                     week cleared, one of a plan's picks taken or the
                                                     whole plan used -- a changed copy of `chosen`
"""

import copy

import choices
import suggestions
import weekbook

POSITION_ORDER = ("C", "LW", "RW", "D", "G")       # the picker's position filter, as the window's


class NoWeek(Exception):
    """No week to build: no nights left, or a plan saved before it carried a workbench."""


class Book:
    def __init__(self, plan: dict, chosen: dict, week: str, slot_order: list, accepts: dict):
        self.plan, self.chosen = plan, chosen
        self.which = "next" if week == "next" and plan.get("next_week_plans") else "this"
        self.plans = suggestions.plans_shown(plan, self.which)
        bench = (self.plans[0] if self.plans else {}).get("workbench")
        if bench is None:
            raise NoWeek("Refresh to see the week: this plan was saved before it carried one."
                         if self.plans else "No nights left to plan this week.")
        self.book = weekbook.Week(bench, slot_order, accepts)
        self.moves = self.book.picks(chosen)

    def _pick(self, m) -> dict:
        b = self.book
        return {"picked": m["picked"], "day": m["day"], "effective": m["effective"], "kind": m["kind"],
                "add": m["add"], "add_name": b.name(m["add"]),
                "drop": m["drop"], "drop_name": None if m["drop"] is None else b.name(m["drop"]),
                "problem": m["problem"]}

    def plan_named(self, label):
        return next((p for p in self.plans if p.get("label") == label and p.get("moves")), None)

    def plan_moves(self, plan: dict) -> list:
        """A week plan's moves put in as if they were your picks; each carries its slot's index
        in the plan (`index`)."""
        index = {(m["day"], m.get("add_id")): i for i, m in enumerate(plan["moves"])}
        placed = self.book.place([(m["day"], m["add_id"], m.get("drop_id")) for m in plan["moves"]
                                  if m.get("add_id") is not None])
        return [{**m, "index": index.get((m["picked"], m["add"]))} for m in placed]

    def view(self, slot_rows: list, show: str | None = None) -> dict:
        """`show`: a week plan's label (the Plan tab's chips, the user's design 2026-10-30) -- its week
        drawn on the calendar, its picks and pickups that are not yours marked `ghost`, to take
        one at a time or all; None or unknown: your picks."""
        b = self.book
        shown = self.plan_named(show) if show else None
        moves = self.plan_moves(shown) if shown else self.moves
        mine = {(m["picked"], m["add"]) for m in self.moves}
        my_rentals = {m["add"] for m in b.made(self.moves)}
        rentals = {m["add"] for m in b.made(moves)}
        nights = []
        for n in b.nights_view(moves):
            waiting = {}
            for s in n["started"]:
                waiting.setdefault(s["slot"], []).append(s)
            cells = []
            for slot in slot_rows:
                s = waiting.get(slot, []).pop(0) if waiting.get(slot) else None
                cells.append({"slot": slot, "player_id": None if s is None else s["player_id"],
                              "player": None if s is None else s["player"], "pts": None if s is None else s["pts"],
                              "rental": s is not None and s["player_id"] in rentals,
                              "ghost": s is not None and s["player_id"] in rentals - my_rentals})
            # Held but not started: who plays that night (most points first), then who does not.
            started = {s["player_id"] for s in n["started"]}
            bench = []
            for q in n["held"]:
                if q in started:
                    continue
                pts = b.players.get(q, {}).get("values", {}).get(n["day"])
                bench.append({"player_id": q, "player": b.name(q), "pts": pts, "plays": pts is not None,
                              "status": b.players.get(q, {}).get("status"), "rental": q in rentals,
                              "ghost": q in rentals - my_rentals})
            bench.sort(key=lambda c: (not c["plays"], -(c["pts"] or 0.0), c["player"]))
            nights.append({"day": n["day"], "points": round(n["points"], 2), "open": len(n["open"]),
                           "picks": [{**self._pick(m), "ghost": (m["picked"], m["add"]) not in mine,
                                      "index": m.get("index")} for m in n["moves"]],
                           "cells": cells, "bench": bench})
        used = b.used(self.moves)
        # Each plan's week against yours, for its chip.
        plans = [{"label": p["label"],
                  "points": round(b.week_points([m for m in self.plan_moves(p) if m["problem"] is None]), 2)}
                 for p in self.plans if p.get("moves")]
        return {"shown": shown["label"] if shown else None, "plans": plans,
                "mine_points": round(b.week_points(b.made(self.moves)), 2),
                "which": self.which, "has_next": bool(self.plan.get("next_week_plans")),
                "week": self.plan.get("week"), "next_week": self.plan.get("next_week"),
                "today": b.today, "moves_left": b.bench["moves_left"], "used": used,
                "picks": len(self.moves), "left_out": sum(1 for m in self.moves if m["problem"]),
                "slot_rows": slot_rows, "nights": nights}

    def picker(self, day: str, occupant: int | None, slot: str | None = None) -> dict:
        """The slot picker's free agents, those at the positions `slot` takes -- on the bench (no
        such slot), any position (the user, 2026-10-17). Swapping out `occupant`: priced on the
        swap."""
        b = self.book
        positions = b.accepts.get(slot) if slot else None
        if positions is None and occupant is not None:
            positions = frozenset().union(*b.accepts.values())
        found = b.picker(self.moves, day, self.plans, occupant, positions)
        positions = [q for q in POSITION_ORDER if any(q in f["positions"].split("/") for f in found)]
        # Swapping out a player one of your picks drops already: no swap can go in (he would be
        # dropped twice), so the picker says which pick to take out instead of listing nobody.
        dropped = None if occupant is None else next((m for m in b.made(self.moves) if m["drop"] == occupant), None)
        return {"day": day, "counts_from": b.effective_on(day),
                "occupant": occupant, "occupant_name": None if occupant is None else b.name(occupant),
                "dropped_by": None if dropped is None else {"day": dropped["picked"], "add_name": b.name(dropped["add"])},
                "moves_full": b.used(self.moves) >= b.bench["moves_left"],
                "positions": positions,
                "adds": [{**f, "gain": round(f["gain"], 2), "pts": round(f["pts"], 2)} for f in found]}

    def drops(self, day: str, add: int, occupant: int | None) -> dict:
        """Who a pick of `add` on `day` may drop -- for a rental's or a rostered player's slot, him
        alone -- with the one the window selects first: the plan's drop when it is listed, else the
        first."""
        b = self.book
        if occupant is not None:
            gain = b.replace_gain(self.moves, day, add, occupant)
            listed = [] if gain is None else [{"player_id": occupant, "player": b.name(occupant), "gain": round(gain, 2)}]
            return {"drops": listed, "selected": occupant if listed else None, "replace": True}
        listed = b.drops_for(self.moves, day, add)
        plan_drop = weekbook.recommended(self.plans, day).get(add, (None, None, None))[2]
        preferred = next((o["player_id"] for o in listed if o["player_id"] == plan_drop), None)
        selected = preferred if preferred is not None else (listed[0]["player_id"] if listed else None)
        return {"drops": [{**o, "gain": round(o["gain"], 2)} for o in listed], "selected": selected,
                "has_selection": bool(listed), "replace": False}


def edit(chosen: dict, book: Book, body) -> dict:
    """`body.action`: 'pick' (body.add on body.day, dropping body.drop -- None: an open spot),
    'replace' (body.add in place of body.rental -- your rental, or a rostered player dropped -- from
    body.day), 'unpick' (body.add's pick on body.day taken out) or 'clear' (the week's picks
    forgotten). A changed copy."""
    new = copy.deepcopy(chosen)
    if body.action == "pick":
        choices.pin(new, body.day, body.add, body.drop)
    elif body.action == "replace":
        choices.replace_rental(new, body.day, body.rental, body.add)
    elif body.action == "unpick":
        choices.unpin(new, body.day, body.add)
    elif body.action == "clear":
        choices.clear_days(new, book.book.move_days)
    elif body.action in ("take", "use"):
        # One of a week plan's picks into yours (body.index), or the whole plan in place of
        # this week's picks -- each with the plan's drop (choices.plan_drop).
        plan = book.plan_named(body.label)
        if plan is None:
            raise ValueError(f"no week plan {body.label!r} this week")
        if body.action == "use":
            choices.clear_days(new, book.book.move_days)
        for index in ([body.index] if body.action == "take" else range(len(plan["moves"]))):
            m = plan["moves"][index]
            if m.get("add_id") is not None and m["add_id"] not in choices.picked(new, m["day"]):
                choices.pin(new, m["day"], m["add_id"], choices.plan_drop(plan, index, new))
    else:
        raise ValueError(f"unknown action {body.action!r}")
    return new

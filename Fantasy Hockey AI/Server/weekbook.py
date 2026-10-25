"""Your week in the app, built from your picks as you make them: the Weekly planner tab's calendar,
slot picker and drop list (the user, 2026-10-08: the calendar is a tool for making your own plan,
so it never waits for the plan server to plan again).

The plan server's plan A carries a workbench (Decisions/weekplan.py, WeekPlanner.workbench): the
week's nights, the roster it starts from, its open spots, the move budget, and the players a pick
can involve, each with his value on every night he plays. A Week puts your picks (Live/choices.py)
into it by the planner's rules for a pinned move (WeekPlanner.seeded) -- in day order, each with
its own drop or into an open spot, inside the week's move budget -- and solves each night's lineup
on the roster that leaves (Decisions/slots.py, as the server does). A pick that cannot go in keeps
its reason (`problem`) and changes nothing. Your picks are yours alone: the server never
plans on them -- the AI Suggestions (today's moves, the plans) are its own (the user, 2026-10-08).

Days are ISO text ("2026-10-08"); a claim clearing during a day carries its time
("2026-10-09T03:00:00"), which sorts after that day's night, so it counts from the next.
"""

import seasonlayer  # noqa: F401 -- puts Season/ on sys.path; see seasonlayer.py
from decisionlayer import slots as slots_module


def recommended(plans, day) -> dict:
    """{player id: (the plans that pick him on `day` -- "A", or "B alt" for a slot's other
    option --, his best edge, the plan's drop)}: the slot picker lists these first. `plans`: the
    week plans of the week shown. (Moved from plan_gui, 2026-10-10, for the app's server.)"""
    out = {}
    for plan in plans:
        for m in plan.get("moves", []):
            if m["day"] != day:
                continue
            for o in m.get("options") or [{"player_id": m.get("add_id"), "edge": m["edge"]}]:
                q = o.get("player_id")
                if q is None:
                    continue
                tag = plan["label"] + ("" if q == m.get("add_id") else " alt")
                labels, edge, drop = out.get(q, ([], o["edge"], m.get("drop_id")))
                out[q] = (labels + [tag], max(edge, o["edge"]), drop)
    return {q: (", ".join(labels), edge, drop) for q, (labels, edge, drop) in out.items()}


class Week:
    def __init__(self, bench: dict, slot_order: list, accepts: dict):
        self.bench, self.slot_order, self.accepts = bench, slot_order, accepts
        self.nights, self.today = bench["nights"], bench["today"]
        self.roster = list(bench["roster"])
        self.players = {int(q): f for q, f in bench["players"].items()}
        self.eligibility = {q: frozenset(f["positions"].split("/")) if f["positions"] else frozenset()
                            for q, f in self.players.items()}
        self.move_days = sorted({self.today, *self.nights})

    def name(self, player_id) -> str:
        f = self.players.get(player_id)
        return f["player"] if f else str(player_id)

    def effective_on(self, day):
        """When a move made on `day` counts: that day, or for today's after the first puck on
        ESPN the day after (`moves_from`)."""
        moves_from = self.bench.get("moves_from")
        return max(day, moves_from) if day == self.today and moves_from else day

    # ---------- your picks, put in ----------

    def picks(self, chosen: dict) -> list:
        """Your picks (Live/choices.py) on this week's move days, put in (`place`)."""
        days = set(self.move_days)
        return self.place([(d, m["add"], m["drop"]) for d, c in chosen.get("days", {}).items()
                           if d in days for m in c["moves"]])

    def place(self, picks) -> list:
        """Your picks [(day, add, drop or None)] put in by WeekPlanner.seeded's rules: [{"day" (the
        day it is made: today for a claim), "picked" (the day you chose), "effective", "kind",
        "add", "drop", "cost", "problem" (None: put in; else why it is left out)}], in that order."""
        moves, spent, room = [], {}, self.bench["room"]
        for picked, add, drop in sorted(picks, key=lambda pick: (pick[0], pick[1])):
            day, kind, effective = picked, "add", self.effective_on(picked)
            f = self.players.get(add)
            if f is not None and f["clears"] and (picked == self.today or f["clears"] > picked):
                kind, day = "claim", self.today
                effective = max(f["clears"], self.effective_on(self.today), effective)
            move = {"day": day, "picked": picked, "effective": effective, "kind": kind, "add": add,
                    "drop": drop, "cost": 0, "problem": None}
            moves.append(move)
            done = [m for m in moves if m["problem"] is None and m is not move]
            before = self.roster_at(done, effective)
            if picked not in self.move_days:
                move["problem"] = "not a day this week can still move on"
            elif f is None or not f["free"]:
                move["problem"] = "not a free agent (refresh)"
            elif any(m["add"] == add for m in done):
                move["problem"] = "already picked up"
            elif drop is None and room <= 0:
                move["problem"] = "no open roster spot -- pick a drop"
            elif drop is not None and (drop not in before or any(m["drop"] == drop for m in done)):
                move["problem"] = f"{self.name(drop)} is not on your roster then"
            if move["problem"] is not None:
                continue
            move["cost"] = self.move_cost(day, kind, drop)
            if not self.fits(spent, day, move["cost"]):
                move["problem"] = "over the week's move limit"
                continue
            spent[day] = spent.get(day, 0) + move["cost"]
            room -= drop is None
        return moves

    def move_cost(self, day, kind, drop) -> int:
        if day == self.today and self.bench["free_today"]:
            return 0
        costs = self.bench["costs"]
        return costs[kind] + (costs["drop"] if drop is not None else 0)

    def fits(self, spent, day, cost) -> bool:
        """WeekPlanner.fits: by any day D, the moves spent through D leave that day's reserve."""
        if cost <= 0:
            return True
        through = 0
        for later in self.move_days:
            through += spent.get(later, 0)
            if later >= day and through + cost > self.bench["moves_left"] - self.bench["reserve"].get(later, 0):
                return False
        return True

    @staticmethod
    def made(moves) -> list:
        return [m for m in moves if m["problem"] is None]

    def used(self, moves) -> int:
        return sum(m["cost"] for m in self.made(moves))

    # ---------- the roster and lineup each night ----------

    def roster_at(self, moves, night) -> set:
        """The roster held on `night`: the moves put in, in the order they take effect."""
        held = set(self.roster)
        for m in sorted(self.made(moves), key=lambda m: m["effective"]):
            if m["effective"] > night:
                break
            held.discard(m["drop"])
            held.add(m["add"])
        return held

    def values_on(self, night, held) -> dict:
        return {q: self.players[q]["values"][night] for q in held
                if q in self.players and night in self.players[q]["values"]}

    def night_points(self, night, held) -> float:
        values = self.values_on(night, held)
        return slots_module.assign_value(self.slot_order, values, self.eligibility, self.accepts) if values else 0.0

    def week_points(self, moves, start=None) -> float:
        """The lineup points of the nights from `start` (all the week's by default)."""
        return sum(self.night_points(n, self.roster_at(moves, n)) for n in self.nights
                   if start is None or n >= start)

    def nights_view(self, moves) -> list:
        """Each night: who starts in which slot ({"slot", "player_id", "player", "pts"}), the open
        slots, who is held, the lineup points, and the picks you made for that day."""
        out = []
        for night in self.nights:
            held = self.roster_at(moves, night)
            values = self.values_on(night, held)
            lineup = slots_module.assign(self.slot_order, values, self.eligibility, self.accepts)
            started = [{"slot": self.slot_order[j], "player_id": q, "player": self.name(q), "pts": values[q]}
                       for j, q in sorted(lineup.assigned.items())]
            out.append({"day": night, "started": started,
                        "open": [self.slot_order[j] for j in lineup.unfilled],
                        "held": sorted(held, key=self.name), "points": sum(s["pts"] for s in started),
                        "moves": [m for m in moves if m["picked"] == night]})
        return out

    # ---------- the slot picker and its drop list ----------

    def gain(self, moves, day, add, drop=None):
        """The week's lineup points (the nights from when the move counts) with this move put in
        on `day`, less without it -- None when it would be left out (`place`)."""
        trial = self.place([(m["picked"], m["add"], m["drop"]) for m in self.made(moves)] + [(day, add, drop)])
        if any(m["problem"] is not None for m in trial):
            return None
        new = next(m for m in trial if m["add"] == add)
        return self.week_points(trial, new["effective"]) - self.week_points(moves, new["effective"])

    def free_agents_on(self, moves, night, without=None, positions=None) -> list:
        """The free agents who fit in `night`'s lineup -- added with nobody dropped (or with
        `without`, a rental being replaced, gone), he would start that night, in whichever slot
        the lineup solve puts him -- not picked up already: {"player_id", "player", "positions",
        "status", "pts" (that night), "games" (his nights left this week from then), "days" (the nights he
        plays from `night` on), "gain" (the
        week's lineup points he adds, before any drop)}, most points first.

        `positions`: only free agents at those positions -- the slot clicked takes them, so an open
        F/D slot lists forwards and D, not goalies who would start elsewhere (the user,
        2026-10-12). With `without` too (a player swapped out): every such free agent who plays
        that night, "gain" the week's points of the swap itself (him in, `without` out; one whose
        swap cannot go in is left out). "Would start" is the wrong test for a swap:
        with a forward gone a bench forward takes his slot, so no free-agent forward weaker than
        the bench "started", while D and goalies who would beat a starter there did -- an upgrade
        that has nothing to do with the player swapped out (the user, 2026-10-12)."""
        picked = {m["add"] for m in self.made(moves)}
        start = self.effective_on(night)
        later = [n for n in self.nights if n >= start]
        held = {n: self.roster_at(moves, n) for n in later}
        base = {n: self.night_points(n, held[n]) for n in later}
        tonight = self.roster_at(moves, night) - {without}
        tonight_points = self.night_points(night, tonight)
        out = []
        for q, f in self.players.items():
            if (not f["free"] or q in picked or q in tonight or night not in f["values"]
                    or (f["clears"] and f["clears"] > night)):
                continue
            if positions is not None and not self.eligibility.get(q, frozenset()) & positions:
                continue
            if positions is not None and without is not None:
                gain = self.replace_gain(moves, night, q, without)
                if gain is not None:         # None: the swap would be left out, so he is not listed
                    out.append({"player_id": q, "player": f["player"], "positions": f["positions"],
                                "status": f.get("status"), "pts": f["values"][night], "gain": gain,
                                "games": sum(1 for n in f["nights"] if n >= start),
                                "days": [n for n in f["nights"] if n >= night]})
                continue
            if self.night_points(night, tonight | {q}) <= tonight_points + 1e-9:
                continue                     # he would not start that night
            # No drop: the nights he plays from when he counts, on the roster held then.
            gain = sum(self.night_points(n, held[n] | {q}) - base[n] for n in later if n in f["values"])
            out.append({"player_id": q, "player": f["player"], "positions": f["positions"],
                        "status": f.get("status"), "pts": f["values"][night], "gain": gain,
                        "games": sum(1 for n in f["nights"] if n >= start),
                        "days": [n for n in f["nights"] if n >= night]})
        out.sort(key=lambda r: (-r["pts"], r["player"]))
        return out

    def picker(self, moves, day, plans, without=None, positions=None) -> list:
        """The slot picker's list: `free_agents_on` (`without`: the rental or rostered player whose
        slot it is, `positions`: the positions that slot takes),
        the plans' picks and options that day first (best edge first), then the most points that
        night -- each with "plans" (which, "" for none) and "plan_drop" (the plan's drop, or None)."""
        found = self.free_agents_on(moves, day, without, positions)
        rec = recommended(plans, day)
        found.sort(key=lambda f: (f["player_id"] not in rec,
                                  -rec[f["player_id"]][1] if f["player_id"] in rec else -f["pts"],
                                  f["player"]))
        for f in found:
            plans_, _, drop = rec.get(f["player_id"], ("", None, None))
            f["plans"], f["plan_drop"] = plans_, drop
        return found

    def replace_gain(self, moves, day, add, rental):
        """The week gain of `add` in place of your rental `rental` from `day`: picked the day the
        rental was added, he takes the rental's move (and its drop); later, he drops the rental
        -- as he drops a rostered player in `rental`'s place. None when it would be left out."""
        his = next((m for m in self.made(moves) if m["add"] == rental), None)
        if his is not None and his["picked"] == day:
            return self.gain([m for m in moves if m is not his], day, add, his["drop"])
        return self.gain(moves, day, add, rental)

    def drops_for(self, moves, day, add) -> list:
        """Who a pick of `add` on `day` may drop: everyone held when it counts whose drop would be
        put in with your other picks (`place`; not a rental picked that same day, nor one over the
        move limit), each with the week's lineup points the move adds with him dropped -- best
        first -- and, while that has room, None (an open spot) first."""
        out = []
        for q in [None] + sorted(self.roster_at(moves, self.effective_on(day)), key=self.name):
            gain = self.gain(moves, day, add, q)
            if gain is not None:
                out.append({"player_id": q, "gain": gain,
                            "player": "(nobody: an open roster spot)" if q is None else self.name(q)})
        out.sort(key=lambda r: (r["player_id"] is not None, -r["gain"], r["player"]))
        return out

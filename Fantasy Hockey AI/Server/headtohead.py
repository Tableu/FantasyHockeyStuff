"""The Matchup tab's head-to-head (plan app-redesign.md, step 5): every player you and your
opponent held this week, each day's slot, projection, game and points, for the app's Players
view -- the week's points by player, or one day drawn like the Roster's cards.

    people(plans)                             {player id: {name, player, positions, team, side}}
    view(dates, today, plans, lineups, boxes, workbench)
                                              {days: [{date, state}], players: [{..., days: {date:
                                               {slot, xpts, p_plays, game, points}}}]}

A day gone and today: the slots the platform kept (Matchups.day_lineup, both teams), the
projection that day's own plan made. A day ahead: today's lineup and bench, your projection from
plan A's workbench, your opponent's his points per game when his team plays. Points are the NHL
box scores' in the league's scoring (games.Games.day).
"""

import roster

OUT_OF_LINEUP = ("BN", "IR")


def people(plans: dict) -> dict:
    """Everyone either side held in the week's plans, the latest plan's word on each."""
    out = {}
    for date in sorted(d for d, p in plans.items() if p):
        for side, key in (("me", "roster"), ("opp", "opponent_roster")):
            for r in plans[date].get(key, []):
                short, team = roster.split_name(r["player"])
                out[int(r["player_id"])] = {"player_id": int(r["player_id"]), "player": r["player"], "name": short,
                                            "positions": r["positions"], "team": team, "side": side,
                                            "status": r.get("status")}
    return out


def _plan_slots(plan: dict) -> tuple:
    """(your slot per player from the plan's lineup as it stands -- BN, IR for the rest --, each
    lineup slot's projection and P(plays))."""
    slots, lineup = {}, {}
    for s in plan.get("lineup_now", []):
        if s.get("player_id") is not None:
            slots[int(s["player_id"])] = s["slot"]
            lineup[int(s["player_id"])] = s
    for r in plan.get("roster", []):
        slots.setdefault(int(r["player_id"]), "IR" if r.get("on_ir") else "BN")
    return slots, lineup


def view(dates: list, today: str, plans: dict, lineups: dict, boxes: dict, workbench: dict | None) -> dict:
    """`dates`: the week's days (ISO); `plans`: {date: that day's newest plan, today's for a day
    ahead without one, None}; `lineups`: {date: Matchups.day_lineup's answer or None} for days up
    to today; `boxes`: {date: games.Games.day's players}; `workbench`: plan A's."""
    who = people(plans)
    values = {int(k): p.get("values", {}) for k, p in ((workbench or {}).get("players") or {}).items()}
    today_slots = None
    players = {pid: {**p, "days": {}} for pid, p in who.items()}
    for date in dates:
        plan = plans.get(date)
        state = "final" if date < today else "today" if date == today else "ahead"
        own_plan = bool(plan) and plan.get("game_date") == date
        mine_slots, mine_lineup = _plan_slots(plan) if plan else ({}, {})
        opp_rows = {int(r["player_id"]): r for r in (plan or {}).get("opponent_roster", [])}
        my_rows = {int(r["player_id"]): r for r in (plan or {}).get("roster", [])}
        got = lineups.get(date)
        if got is not None:
            slots = {"me": got["slots"], "opp": got["opp_slots"]}
        elif state == "ahead" and today_slots is not None:
            slots = today_slots
        else:
            # No platform word: your plan's lineup, your opponent's roster without slots.
            slots = {"me": mine_slots, "opp": {pid: None for pid in opp_rows}}
        if state == "today":
            today_slots = slots
        for pid, p in players.items():
            side_slots = slots[p["side"]]
            if pid not in side_slots:
                continue
            box = (boxes.get(date) or {}).get(pid) or {}
            game, line = box.get("game"), box.get("line")
            if p["side"] == "me":
                if own_plan and pid in mine_lineup:
                    xpts, p_plays = mine_lineup[pid]["mean"], mine_lineup[pid]["p_plays"]
                elif own_plan and pid in my_rows:
                    r = my_rows[pid]
                    p_plays = r.get("plays_tonight")
                    xpts = round(r["per_game"] * p_plays, 2) if p_plays else None
                else:
                    xpts, p_plays = values.get(pid, {}).get(date), None
            else:
                r = opp_rows.get(pid)
                if r is None:
                    xpts, p_plays = None, None
                elif own_plan:
                    p_plays = r.get("plays_tonight")
                    xpts = round(r["per_game"] * p_plays, 2) if p_plays else None
                else:
                    xpts, p_plays = (r["per_game"] if game else None), None
            p["days"][date] = {"slot": side_slots[pid], "xpts": xpts, "p_plays": p_plays, "game": game,
                               "points": line["fantasy"] if line else None,
                               "counts": side_slots[pid] not in OUT_OF_LINEUP}
    return {"days": [{"date": d, "state": "final" if d < today else "today" if d == today else "ahead"} for d in dates],
            "players": [p for p in players.values() if p["days"]]}

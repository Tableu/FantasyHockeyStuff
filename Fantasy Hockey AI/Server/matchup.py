"""The phone app's Matchup screen (plan phone-app-pwa.md, P5): the week's score and P(win), each day's
points for you and your opponent (final, live, projected), tonight's live against projected, and
the league's week so far. The scores are the platform's own, read here (platforms/:
scoreboard, daily_points) and kept a minute, so the screen is current between the plan's refreshes;
the projection and P(win) are the newest plan's (matchup_projection, p_win).

    Matchups(leagues).view(league, plan)    the screen's data
"""

import datetime as dt
import threading
import time

import platforms

KEEP_S = 60                      # a league's scores are read again only after this (Fleaflicker's call limit)


class Matchups:
    def __init__(self):
        self.adapters, self.kept, self.lock = {}, {}, threading.Lock()

    def _scores(self, league):
        """(scoreboard, daily points) for the league, read at most once a minute."""
        with self.lock:
            hit = self.kept.get(league.name)
            if hit is not None and time.monotonic() - hit[0] < KEEP_S:
                return hit[1]
            adapter = self.adapters.get(league.name)
            if adapter is None:
                adapter = self.adapters[league.name] = platforms.for_league(league)
            value = (adapter.scoreboard(), adapter.daily_points(league.team_id))
            self.kept[league.name] = (time.monotonic(), value)
            return value

    def view(self, league, plan: dict | None) -> dict:
        board, daily = self._scores(league)
        me = next((t for t in board.teams if t.team_id == league.team_id), None)
        if me is None:
            return {"available": False, "reason": "No matchup this period."}
        opp = next((t for t in board.teams if t.team_id == me.opponent_id), None)
        # The plan's projection and P(win) are for its own week: on the week's last night it may be
        # next week's already, and then neither belongs on this one.
        same_week = bool(plan) and plan.get("week") == board.period
        projection = (plan.get("matchup_projection") or {}) if same_week else {}
        nights = {side: (projection.get(side) or {}).get("nights", {}) for side in ("me", "opp")}
        today = max(daily) if daily else None
        days, run, proj = [], [0.0, 0.0], [0.0, 0.0]
        day = board.first_day
        while day <= board.last_day:
            key = day.isoformat()
            if day in daily and day != today:
                state, you, them = "final", *daily[day]
            elif day == today:
                state, you, them = "live", *daily[day]
            else:
                state, you, them = "proj", None, None
            you_proj, them_proj = nights["me"].get(key), nights["opp"].get(key)
            if state != "proj":
                run = [run[0] + you, run[1] + them]
            # The week projected: final days as scored, today and later as the plan projects
            # them (today's live points when the plan has no projection for it).
            proj[0] += you if state == "final" else (you_proj if you_proj is not None else (you or 0.0))
            proj[1] += them if state == "final" else (them_proj if them_proj is not None else (them or 0.0))
            days.append({"day": key, "state": state, "you": you, "opp": them,
                         "you_proj": you_proj, "opp_proj": them_proj,
                         "run": None if state == "proj" else round(run[0] - run[1], 2)})
            day += dt.timedelta(days=1)
        has_projection = bool(projection)
        league_rows = sorted(({"team_id": t.team_id, "name": t.name, "points": round(t.points, 2),
                               "who": "you" if t.team_id == me.team_id else "opp" if opp and t.team_id == opp.team_id else None}
                              for t in board.teams), key=lambda r: -r["points"])

        def side(t, live, tonight_proj):
            return {"name": t.name, "points": round(t.points, 2), "live": live, "tonight_proj": tonight_proj,
                    "to_play": t.to_play, "in_play": t.in_play, "done": t.done}

        tonight = days[[d["day"] for d in days].index(today.isoformat())] if today and board.first_day <= today <= board.last_day else None
        return {
            "available": True, "period": board.period, "first_day": board.first_day.isoformat(),
            "last_day": board.last_day.isoformat(), "today": today.isoformat() if today else None,
            "days_left": (board.last_day - today).days if today else None,
            "you": side(me, tonight and tonight["you"], tonight and tonight["you_proj"]),
            "opp": side(opp, tonight and tonight["opp"], tonight and tonight["opp_proj"]) if opp else None,
            "projected": {"you": round(proj[0], 2), "opp": round(proj[1], 2)} if has_projection else None,
            "p_win": plan.get("p_win") if same_week else None,
            "plan_file_day": (plan or {}).get("game_date"),
            "days": days, "league": league_rows, "has_projection": has_projection,
        }

"""Week streaming: plan the rest of the matchup week's moves together, make only today's.

`streaming.run` decides a day at a time: today's best rental, against a bar whose `lam` term stands
in for the better streams later in the week that a move spent now would cost. This module prices
those later streams directly instead. Every day it plans the moves left in the week --

    (day, add, drop), ...     e.g. Mon: add A for spot S -- Thu: add B for A -- Sat: add C for T

-- by greedy insertion: of every move it could make on any night still ahead, add the one that
raises the week most, and repeat until the moves run out or nothing left adds. A move is worth

    gain = the week's lineup points with it minus without, each night solved on the roster the
           plan holds that night (`slots.assign_value`, as `valuation.RosterNights`)
    bar  = drop_cost + margin * sd                          (streaming.py's, without `lam`)

and goes in only when gain > bar. Moves are ranked by survival^days x (gain - bar): a free agent
planned for a later night is often gone by then (2024-25 backtest, the shipped field: 71% still
free a day later, 51% after three, ~44% after four to six), so a pickup today beats a slightly
bigger one on Thursday that probably will not be there.

With `next_week` > 0 (off: it did not pay, see LATE_DAYS), a move made on the week's last day
with the week won prices only next week's nights, on the roster the plan leaves, at that weight.

Then it makes today's moves and keeps the rest as the plan; the
next day plans again on what it learned. A move later in the plan is a recommendation, not a
commitment.

What may be dropped: streaming.py's spots (skaters below replacement over the rest of the season),
a player the plan itself picked up on an earlier night (so rentals chain), or nobody into an open
spot. The drop cost is streaming.py's: his value after this week minus the best free agent's at his
positions -- about zero for a real spot, large for a regular. Goalies are not streamed.

Free agents can be added on any night they play. A player on waivers can only be claimed today,
and pays from the day he clears. Moves today cost what the league charges today (nothing before
the first week, `LeagueState.free_moves`); moves on later nights cost the normal move, out of
`moves_left` minus `reserve`, which is held for upgrades as in streaming.py.

A move's gain is its value when it went into the plan; a later move can change it (a rental dropped
again on Thursday earns less than priced on Monday). The executed moves are logged as "rental" /
"rental claim", graded like streaming.py's.
"""

import logging

import slots as slots_module
import streaming
import valuation

log = logging.getLogger("weekplan")

# Goalie rentals never drop a goalie projected to start this share of his team's remaining games.
STARTER_SHARE = 0.5

# `next_week` prices next week's nights only for a move made TODAY on the week's last LATE_DAYS
# days with the week won (z >= CLEAR_WIN_Z, P(win) >= 0.9; the z needs `gate`), and that move's
# nights this week count for nothing. It is off: every version tried lost (2024-25, live strategy,
# 8 drafts, -0.9 to -3.3 pts/wk; this one -1.51 +/- 0.40 at 0.5, -1.45 +/- 0.62 at 1.0), and none
# raised the win rate -- Monday's fresh moves make the next-week pickups anyway. Settings/README.md.
LATE_DAYS = 1
CLEAR_WIN_Z = 1.2816


def run(view, params: streaming.StreamParams, horizon, source, slot_order, accepts, fieldable,
        z=0.0, alternatives=0):
    """Plan the week's streams and make today's. Returns (done today, plans): plans[0] is the plan
    made, then up to `alternatives` others that each ADD A DIFFERENT PLAYER FIRST -- on the plan
    made's first pickup day, the next best pickups in its place (and none of the other plans'
    first pickups), the rest of the week planned around each. For the plan window only: a
    backtest asks for none. Each plan is {"first", "moves", "week_gain", "week_edge"}."""
    if params.spots <= 0 or view.week is None:
        return [], []
    planner = WeekPlanner(view, params, horizon, source, slot_order, accepts, fieldable, z)
    plan = planner.plan(record=alternatives > 0)
    plans = [(plan, planner.first_pickup(plan))]
    if plan and alternatives > 0:
        # Its first pickup day, and the best pickups that day by their value as an opening move.
        day = plan[0]["day"]
        firsts = {plans[0][1]}
        for opening in planner.openings:
            if len(plans) > alternatives:
                break
            incoming = opening[1][-1]["incoming"]
            if opening[4] != day or incoming in firsts:
                continue
            alternative = planner.plan(first=opening, banned=firsts - {incoming})
            plans.append((alternative, incoming))
            firsts.add(incoming)
    # Only the first plan is made. Making the best of 3 by week total instead measured nothing
    # (2024-25, strategy-espn-la, 32 drafts: +0.33 +/- 0.45 pts/wk; best of 5, 16: +0.27 +/- 0.73):
    # the plan is made again every day, so a better week on paper rarely survives to be played.
    out = [planner.summary(p, first) for p, first in plans]   # before today's moves change state
    return planner.execute(plan), out


class WeekPlanner:
    def __init__(self, view, params, horizon, source, slot_order, accepts, fieldable, z):
        self.view, self.params, self.source = view, params, source
        self.slot_order, self.accepts, self.fieldable = slot_order, accepts, fieldable
        self.state = view._state
        self.eligibility = self.state.eligibility
        self.next_weight = params.next_week
        self.today = view.day
        self.nights = [d for d in view.calendar.days_in(view.week) if d >= self.today]
        # A move can be made today (a day with no games included -- before the first week) or on
        # any night ahead.
        self.move_days = sorted({self.today, *self.nights})
        self.roster = list(view.roster)

        eligibility = self.eligibility
        # Goalie rentals (`goalies`): 2024-25, strategy-espn-la, 32 drafts, +1.16 +/- 0.59 pts/wk
        # (fresh half +1.50 +/- 0.79), win +0.010 +/- 0.009, ~10 more moves a season -- priced on
        # tonight's simulated line below; on P(start) x the average line alone, +0.26 +/- 0.60.
        self.goalies = params.goalies
        free = [p for p in view.free_agents()
                if self.goalies or not streaming.is_goalie(eligibility, p)]
        self.claimable = [p for p in free if view.on_waivers(p)] if params.claim else []
        self.addable = [p for p in free if not view.on_waivers(p)]
        pool = self.addable + self.claimable
        self.spots = streaming.spots(view, params, horizon, source, pool, eligibility,
                                     goalies=self.goalies)
        if self.goalies:
            # A starter is never rented away, even when a free agent projects as well: a goalie
            # projected to start STARTER_SHARE of his team's remaining games (his rest-of-season
            # rate over the league-average line; Projections/goalie_workload.py) keeps his spot.
            lines = view.goalie_projections["expected_line"]
            line = float(lines.iloc[0]) if len(lines) else None
            if line:
                self.spots = [p for p in self.spots if not streaming.is_goalie(eligibility, p)
                              or (view.ros_rate(p) or 0.0) < STARTER_SHARE * line]
        self.after = streaming.Replacement(view, pool, horizon, source, eligibility, after_week=True)
        self.horizon = horizon

        self._nights, self._next, self._memo, self._cost = {}, {}, {}, {}
        self.week_end = view.calendar.weeks[view.week - 1].end
        # Buying next week's roster: the week's last day, the matchup won (`gate` supplies z).
        self.late = ((self.week_end - self.today).days < LATE_DAYS and params.gate
                     and z >= CLEAR_WIN_Z)
        # Tonight a goalie is worth tonight's P(start) x the expected line: whether he starts is
        # often known by the lock, and that is what a goalie rental is for. (Other nights, his
        # rate: his usual share of starts.)
        self.tonight = {}
        if self.goalies:
            g = view.goalie_projections
            column = getattr(view, "p_start_column", None) or "p_start"
            column = column if column in g.columns else "p_start"
            self.tonight = {int(p): float(s) * float(line) for p, s, line
                            in zip(g["player_id"], g[column], g["expected_line"])}
            # Better where tonight was simulated: the mean of his draws, which carries the
            # matchup -- goals against are the opponent's sampled goals, the win his own team's.
            for p in self.tonight:
                draws = view.decision_points.get(p)
                if draws is not None and len(draws):
                    self.tonight[p] = float(sum(draws) / len(draws))
        self.candidates = self._shortlist()
        self.rates = {p: valuation.rate(view, p, source)
                      for p in set(self.roster) | {c[0] for c in self.candidates}}
        self.moves_left = view.moves_left
        self.openings = []                 # best opening move per player (plan(record=True))

    # ---------- inputs ----------

    def nights_of(self, player_id) -> list:
        """His team's nights from today to the end of this week."""
        if player_id is None:
            return []
        if player_id not in self._nights:
            self._nights[player_id] = [n for n in self.view.nights_through(player_id, 0)
                                       if n >= self.today]
        return self._nights[player_id]

    def next_nights_of(self, player_id) -> list:
        """His team's nights next week -- priced only when `next_week` > 0."""
        if player_id is None or self.next_weight <= 0.0:
            return []
        if player_id not in self._next:
            this_week = set(self.nights_of(player_id))
            self._next[player_id] = [n for n in self.view.nights_through(player_id, 1)
                                     if n > self.today and n not in this_week]
        return self._next[player_id]

    def weight(self, night, late=False) -> float:
        """A night's points: next week's at `next_week`; this week's at 1, or 0 for a move made
        today on the week's last day with the week won -- that move is bought for next week."""
        if night > self.week_end:
            return self.view.night_weight(night) * self.next_weight
        return self.view.night_weight(night) * (0.0 if late else 1.0)

    def _shortlist(self) -> list:
        """(player, move day, first night he counts): for each day a move can be made, the best
        free agents by rate x games from then, plus the best claims (today, paying from clearing)."""
        view, k = self.view, self.params.shortlist
        rate = {p: valuation.rate(view, p, self.source) for p in self.addable + self.claimable}
        out = []
        for day in self.move_days:
            ahead = self.next_weight if day == self.today and self.late else 0.0
            worth = {p: rate[p] * (sum(1 for n in self.nights_of(p) if n >= day)
                                   + ahead * len(self.next_nights_of(p)))
                        # a goalie tonight at tonight's P(start), not his usual share
                        + (self.tonight[p] - rate[p] if day == self.today and p in self.tonight
                           and self.today in self.nights_of(p) else 0.0)
                     for p in self.addable}
            best = sorted((p for p in worth if worth[p] > 0.0), key=lambda p: (-worth[p], p))[:k]
            out += [(p, day, day) for p in best if day == self.today or day in self.nights_of(p)]
        claims = {p: view.waiver_clears(p) for p in self.claimable}
        ahead = self.next_weight if self.late else 0.0
        worth = {p: rate[p] * (sum(1 for n in self.nights_of(p) if n >= claims[p])
                               + ahead * len(self.next_nights_of(p)))
                 for p in self.claimable}
        best = sorted((p for p in worth if worth[p] > 0.0), key=lambda p: (-worth[p], p))[:k]
        out += [(p, self.today, max(claims[p], self.today)) for p in best]
        return out

    def drop_cost(self, player_id) -> float:
        if player_id is None:
            return 0.0
        if player_id not in self._cost:
            self._cost[player_id] = max(0.0, streaming.value_after_week(
                self.view, player_id, self.horizon, self.source)
                - self.after.at(self.eligibility.get(player_id, frozenset())))
        return self._cost[player_id]

    # ---------- the roster a plan holds ----------

    @staticmethod
    def roster_at(base, moves, night) -> set:
        held = set(base)
        for m in sorted(moves, key=lambda m: m["effective"]):
            if m["effective"] <= night:
                held.discard(m["outgoing"])
                held.add(m["incoming"])
        return held

    def night_value(self, night, held) -> float:
        playing = sorted(p for p in held if (night in self.nights_of(p)
                                             or night in self.next_nights_of(p))
                         and not (night == self.today and p in self.view.unavailable))
        key = (night, tuple(playing))
        if key not in self._memo:
            values = {p: (self.tonight[p] if night == self.today and p in self.tonight
                          else self.rates.get(p, 0.0)) for p in playing}
            self._memo[key] = (slots_module.assign_value(self.slot_order, values, self.eligibility,
                                                          self.accepts) if values else 0.0)
        return self._memo[key]

    def sd(self, incoming, outgoing, effective) -> float:
        games_in = sum(1 for n in self.nights_of(incoming) if n >= effective)
        games_out = sum(1 for n in self.nights_of(outgoing) if n >= effective)
        return valuation.PER_GAME_CV * (games_in * self.rates.get(incoming, 0.0) ** 2
                                        + games_out * self.rates.get(outgoing, 0.0) ** 2) ** 0.5

    def move_cost(self, day, kind, outgoing) -> int:
        if day == self.today:
            return self.state._move_cost(kind, outgoing)
        config = self.state.config
        return config.move_cost(kind) + (config.move_cost("drop") if outgoing is not None else 0)

    # ---------- the plan ----------

    def reserve_on(self, day) -> int:
        """streaming.py's reserve on `day`: `reserve` moves held for upgrades at the week's start,
        falling to 0 on its last day."""
        week = self.view.calendar.weeks[self.view.week - 1]
        span = (week.end - week.start).days
        share = min(1.0, max(0.0, (week.end - day).days / span)) if span > 0 else 0.0
        return int(round(self.params.reserve * share))

    def fits(self, spent, day, cost) -> bool:
        """Whether a move costing `cost` on `day` keeps the plan inside each day's budget: by any
        day D, the moves spent through D leave that day's reserve. The reserve falls through the
        week, so the week's last nights can spend what its first held back -- the whole budget
        by its last day. (Holding today's reserve all week planned two moves short; against it,
        2024-25, 16 drafts: -0.17 +/- 0.58 pts/wk, 147 moves a season against 143 -- neutral, kept
        so the plan shows the whole week.)"""
        if cost <= 0:
            return True
        return all(sum(c for d, c in spent.items() if d <= later) + cost
                   <= self.moves_left - self.reserve_on(later)
                   for later in self.move_days if later >= day)

    def plan(self, first=None, record=False, banned=()) -> list:
        """Greedy insertion, from an empty plan -- or from `first`, an opening move chosen by the
        caller (an alternative plan), never adding a `banned` player. With `record`, the first
        pass keeps each player's best opening move per day in `self.openings`, best first."""
        moves, spent = [], {}
        open_spots = self.view.roster_room()
        if first is not None:
            _, moves, cost, into_open, day = first
            spent[day] = cost
            open_spots -= into_open
        openings = {} if record and first is None else None
        while True:
            best = None
            added = {m["incoming"] for m in moves}
            dropped = {m["outgoing"]: m for m in moves if m["outgoing"] is not None}
            picked_up = {m["incoming"]: m["effective"] for m in moves}
            for incoming, day, effective in self.candidates:
                if incoming in added or incoming in dropped or incoming in banned:
                    continue
                kind = "claim" if incoming in self.claimable else "add"
                before = self.roster_at(self.roster, moves, effective)
                drops = ([None] if open_spots > 0 else []) + sorted(
                    p for p in before
                    if p in self.spots or (p in picked_up and picked_up[p] < effective))
                for outgoing in drops:
                    later = dropped.get(outgoing)
                    if later is not None and (later["effective"] <= effective
                                              or later["day"] == self.today):
                        continue
                    cost = self.move_cost(day, kind, outgoing)
                    if not self.fits(spent, day, cost):
                        continue
                    after = (before - {outgoing}) | {incoming}
                    if outgoing is not None and not self.fieldable(sorted(after), self.eligibility,
                                                                   sorted(before)):
                        continue
                    trial = self._with(moves, incoming, outgoing, day, effective, kind, later)
                    gain, bar = trial[-1]["gain"], trial[-1]["bar"]
                    # A later night's pickup happens only if nobody takes him first.
                    edge = self.params.survival ** (day - self.today).days * (gain - bar)
                    if gain > bar and (best is None or edge > best[0]):
                        best = (edge, trial, cost, outgoing is None, day)
                    if openings is not None and gain > bar and (
                            (incoming, day) not in openings or edge > openings[incoming, day][0]):
                        openings[incoming, day] = (edge, trial, cost, outgoing is None, day)
            if openings is not None:
                self.openings = sorted(openings.values(), key=lambda o: -o[0])
                openings = None
            if best is None:
                return sorted(moves, key=lambda m: (m["day"], m["effective"]))
            _, moves, cost, into_open, day = best
            spent[day] = spent.get(day, 0) + cost
            open_spots -= into_open

    def _with(self, moves, incoming, outgoing, day, effective, kind, later) -> list:
        """The plan with this move in it, the move last. When `later` already drops `outgoing`, it
        drops the new pickup instead -- rentals chain: B holds S's spot until A replaces him -- and
        the drop cost this move adds is B's, not S's (S's is already in `later`)."""
        trial = [dict(m) for m in moves]
        if later is not None:
            rewired = next(m for m in trial if m["outgoing"] == outgoing)
            rewired["outgoing"] = incoming
            rewired["drop_cost"] = self.drop_cost(incoming)
            rewired["bar"] = (rewired["drop_cost"] + self.params.margin
                              * self.sd(rewired["incoming"], incoming, rewired["effective"]))
        late = day == self.today and self.late
        new = {"day": day, "effective": effective, "kind": kind, "incoming": incoming,
               "outgoing": outgoing, "for_next_week": late and self.next_weight > 0.0,
               "drop_cost": self.drop_cost(incoming if later is not None else outgoing)}
        new["bar"] = new["drop_cost"] + self.params.margin * self.sd(incoming, outgoing, effective)
        new["gain"] = self.delta(moves, trial + [new], effective, (incoming, outgoing), late=late)
        return trial + [new]

    def delta(self, old, new, effective, players, late=False) -> float:
        """The weighted lineup points under plan `new` minus under `old`, on the nights from
        `effective` that any of `players` plays (the only nights the two plans differ): this
        week's, and -- for a move on the week's last days -- next week's on the roster the plan
        leaves (`next_week` > 0)."""
        nights = sorted({n for p in players
                         for n in self.nights_of(p) + (self.next_nights_of(p) if late else [])
                         if n >= effective})
        total = 0.0
        for night in nights:
            total += self.weight(night, late) * (
                self.night_value(night, self.roster_at(self.roster, new, night))
                - self.night_value(night, self.roster_at(self.roster, old, night)))
        return total

    def execute(self, plan) -> list:
        """Make today's moves; the rest of the plan waits for tomorrow's replan."""
        done = []
        for m in plan:
            if m["day"] != self.today:
                continue
            incoming, outgoing = m["incoming"], m["outgoing"]
            try:
                if m["kind"] == "claim":
                    self.state.submit_claim(self.view.team_index, incoming, drop=outgoing,
                                            today=self.today)
                else:
                    self.state.add(self.view.team_index, incoming, self.today, drop=outgoing,
                                   reason="rental")
            except Exception as error:                 # noqa: BLE001 - state raises IllegalMove
                log.debug("team %d could not stream %s: %s", self.view.team_index, incoming, error)
                m["failed"] = True
                continue
            done.append({"day": self.today, "kind": "rental claim" if m["kind"] == "claim" else "rental",
                         "incoming": incoming, "outgoing": outgoing, "predicted_gain": m["gain"],
                         "for_next_week": m.get("for_next_week", False),
                         "bar": m["bar"], "drop_cost": m["drop_cost"],
                         "spot": outgoing in self.spots,
                         "reserve": streaming.reserve_today(self.view, self.params),
                         "moves_left": self.view.moves_left,
                         "incoming_games": self._games(incoming, m["effective"]),
                         "outgoing_games": self._games(outgoing, m["effective"])})
        return done

    @staticmethod
    def first_pickup(moves):
        """The player a plan adds first: its earliest move, the most valuable on that day."""
        if not moves:
            return None
        day = moves[0]["day"]
        return max((m for m in moves if m["day"] == day), key=lambda m: m["gain"] - m["bar"])["incoming"]

    def summary(self, moves, first) -> dict:
        """A plan for the window: its moves, and what the whole of it adds this week -- the
        lineup points with every move in against none (moves interact, so this is not the sum of
        their gains), and that less the drop costs."""
        players = {p for m in moves for p in (m["incoming"], m["outgoing"]) if p is not None}
        gain = self.delta([], moves, self.today, players) if moves else 0.0
        return {"first": first, "moves": [self.describe(m) for m in moves],
                "week_gain": gain, "week_edge": gain - sum(m["drop_cost"] for m in moves)}

    def _games(self, player_id, effective) -> int:
        return sum(1 for n in self.nights_of(player_id) if n >= effective)

    def describe(self, m) -> dict:
        """A planned move for the plan window: when, who, and what it was priced at."""
        return {"day": m["day"], "effective": m["effective"],
                "kind": "rental claim" if m["kind"] == "claim" else "rental",
                "incoming": m["incoming"], "outgoing": m["outgoing"],
                "gain": m["gain"], "bar": m["bar"], "drop_cost": m["drop_cost"],
                "incoming_games": self._games(m["incoming"], m["effective"]),
                "outgoing_games": self._games(m["outgoing"], m["effective"]),
                "today": m["day"] == self.today, "failed": m.get("failed", False),
                "for_next_week": m.get("for_next_week", False)}

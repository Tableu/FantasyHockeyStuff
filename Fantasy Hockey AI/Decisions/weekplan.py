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

With `next_week` > 0 (off: it did not pay, see `late_days`), a move made on the week's last day
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

For the plan window only, `TeamPlans` shows the week as TEAM SLOTS -- (day, NHL team, position),
each with the players on that team who could fill it, ranked -- valued against the chance each is
taken first, with alternative plans that leave out whole teams. It changes nothing made
(verify.py 'team plans').
"""

import logging
import math
import operator

import slots as slots_module
import streaming
import valuation

log = logging.getLogger("weekplan")

EFFECTIVE = operator.itemgetter("effective")

def long_absence(view, player_id, nights) -> bool:
    """Whether an injured player is out for most of a window (`nights`, his team's nights in it):
    expected back after its middle night -- the user's rule, 2026-09-27 (plans/injury-absence.md).
    Against skipping every injured player: 2024-25, strategy-espn-la, 32 drafts, +0.51 +/- 0.34
    pts/wk, win +0.002 +/- 0.008 -- neutral, kept as the user's rule."""
    if player_id not in view.injured:
        return False
    back = view.expected_return(player_id)
    if back is None:
        return True                     # injured with no estimate: as before, skipped
    return not nights or back > nights[len(nights) // 2]


# Goalie rentals never drop a goalie projected to start `starter_share` of his team's remaining
# games (Settings/strategy.json, streaming).

# `next_week` prices next week's nights only for a move made TODAY on the week's last `late_days`
# days with the week won (z >= `clear_win_z`, 1.2816 = P(win) >= 0.9; the z needs `gate`), and that move's
# nights this week count for nothing. It is off: every version tried lost (2024-25, live strategy,
# 8 drafts, -0.9 to -3.3 pts/wk; this one -1.51 +/- 0.40 at 0.5, -1.45 +/- 0.62 at 1.0), and none
# raised the win rate -- Monday's fresh moves make the next-week pickups anyway. Settings/README.md.


def run(view, params: streaming.StreamParams, horizon, source, slot_order, accepts, fieldable,
        z=0.0, alternatives=0):
    """Plan the week's streams and make today's. Returns (done today, plans).

    A backtest asks for no `alternatives`: plans is [the plan made]. The plan window asks for
    some, and plans are then the week's TEAM-SLOT plans (TeamPlans): plan A, whose moves today are
    the ones made, then up to `alternatives` others, each opening on a different NHL team. Each
    plan is {"first", "moves", "week_gain", "week_edge"}, and a team-slot plan carries its slots'
    options and expected value besides. Nothing a team-slot plan adds changes what is made."""
    if params.spots <= 0 or view.week is None:
        return [], []
    planner = WeekPlanner(view, params, horizon, source, slot_order, accepts, fieldable, z)
    plan = planner.plan()
    # Only the first plan is made. Making the best of 3 by week total instead measured nothing
    # (2024-25, strategy-espn-la, 32 drafts: +0.33 +/- 0.45 pts/wk; best of 5, 16: +0.27 +/- 0.73):
    # the plan is made again every day, so a better week on paper rarely survives to be played.
    # Both before today's moves change the state.
    if alternatives > 0:
        out = TeamPlans(planner).plans(plan, alternatives)
    else:
        out = [planner.summary(plan, planner.first_pickup(plan))]
    return planner.execute(plan), out


class WeekPlanner:
    def __init__(self, view, params, horizon, source, slot_order, accepts, fieldable, z):
        self.view, self.params, self.source = view, params, source
        self.slot_order, self.accepts, self.fieldable = slot_order, accepts, fieldable
        self.state = view._state
        self.eligibility = self.state.eligibility
        self.next_weight = params.next_week
        self.today = view.day
        # After the day's first puck on ESPN (the live plan's `moves_from`) today's adds take
        # effect then.
        self.moves_from = getattr(view, "moves_from", None)
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
        # An injured free agent is rented only if he is expected back by the middle of the rest
        # of the week (long_absence); before his return he counts for nothing either way.
        free = [p for p in view.free_agents() if not long_absence(view, p, view.nights_through(p, 0))
                and (self.goalies or not streaming.is_goalie(eligibility, p))]
        self.claimable = [p for p in free if view.on_waivers(p)] if params.claim else []
        self.addable = [p for p in free if not view.on_waivers(p)]
        pool = self.addable + self.claimable
        self.spots = streaming.spots(view, params, horizon, source, pool, eligibility,
                                     goalies=self.goalies)
        if self.goalies and getattr(view, "droppable", None) is None:
            # A starter is never rented away (unless the user marked him OK to drop: spots),
            # even when a free agent projects as well: a goalie
            # projected to start `starter_share` of his team's remaining games (his rest-of-season
            # rate over the league-average line; Projections/goalie_workload.py) keeps his spot.
            lines = view.goalie_projections["expected_line"]
            line = float(lines.iloc[0]) if len(lines) else None
            if line:
                self.spots = [p for p in self.spots if not streaming.is_goalie(eligibility, p)
                              or (view.ros_rate(p) or 0.0) < params.starter_share * line]
        self.after = streaming.Replacement(view, pool, horizon, source, eligibility, after_week=True)
        self.horizon = horizon

        self._nights, self._next, self._memo, self._cost, self._plays = {}, {}, {}, {}, {}
        # (the plan being extended, {night: its lineup value}): every trial in one round of
        # `plan` compares against the same plan, so its side of `delta` is priced once a night.
        self._base = None
        self._by_night, self._reserve = {}, {}
        self.week_end = view.calendar.weeks[view.week - 1].end
        # Buying next week's roster: the week's last day, the matchup won (`gate` supplies z).
        self.late = ((self.week_end - self.today).days < params.late_days and params.gate
                     and z >= params.clear_win_z)
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

    # ---------- inputs ----------

    def effective_on(self, day):
        """When a move made on `day` takes effect: that day, or for today's after the first puck on ESPN
        the day after (`moves_from`) -- both sides: the drop waits for his game tonight."""
        if day == self.today and self.moves_from is not None:
            return max(day, self.moves_from)
        return day

    def nights_of(self, player_id) -> list:
        """His team's nights from today to the end of this week -- without tonight for a player
        not on this roster who could not play tonight if acquired now (`view.closed_tonight`: his
        game has started, or his claim processes after it). A player already held keeps tonight."""
        if player_id is None:
            return []
        if player_id not in self._nights:
            closed = (player_id in getattr(self.view, "closed_tonight", ())
                      and player_id not in self.roster)
            self._nights[player_id] = [n for n in self.view.nights_through(player_id, 0)
                                       if n >= self.today and not (closed and n == self.today)]
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
        self.pool_rate = rate                      # every free agent's, for the team slots
        out = []
        for day in self.move_days:
            ahead = self.next_weight if day == self.today and self.late else 0.0
            start = self.effective_on(day)
            worth = {p: rate[p] * (sum(1 for n in self.nights_of(p) if n >= start)
                                   + ahead * len(self.next_nights_of(p)))
                        # a goalie tonight at tonight's P(start), not his usual share
                        + (self.tonight[p] - rate[p] if start == self.today and p in self.tonight
                           and self.today in self.nights_of(p) else 0.0)
                     for p in self.addable}
            best = sorted((p for p in worth if worth[p] > 0.0), key=lambda p: (-worth[p], p))[:k]
            out += [(p, day, start) for p in best if day == self.today or day in self.nights_of(p)]
        claims = {p: max(view.waiver_clears(p), self.effective_on(self.today)) for p in self.claimable}
        ahead = self.next_weight if self.late else 0.0
        worth = {p: rate[p] * (sum(1 for n in self.nights_of(p) if n >= claims[p])
                               + ahead * len(self.next_nights_of(p)))
                 for p in self.claimable}
        best = sorted((p for p in worth if worth[p] > 0.0), key=lambda p: (-worth[p], p))[:k]
        out += [(p, self.today, claims[p]) for p in best]
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
    def roster_at(base, moves, night, ordered=False) -> set:
        """The roster a plan holds on `night`: its moves applied in the order they take effect
        (stable, so moves on one day keep the plan's order). `ordered`: the moves are already in
        that order (`ordered()`), so a caller asking about several nights sorts them once."""
        held = set(base)
        for m in (moves if ordered else sorted(moves, key=EFFECTIVE)):
            if m["effective"] > night:
                break
            held.discard(m["outgoing"])
            held.add(m["incoming"])
        return held

    @staticmethod
    def ordered(moves) -> list:
        """A plan's moves in the order they take effect, for `roster_at(..., ordered=True)`."""
        return sorted(moves, key=EFFECTIVE)

    def plays_on(self, player_id) -> set:
        """The nights he plays that a plan prices (this week's, next week's when priced), and is
        not out on."""
        if player_id not in self._plays:
            self._plays[player_id] = {n for n in self.nights_of(player_id) + self.next_nights_of(player_id)
                                      if not self.view.out_on(player_id, n)}
        return self._plays[player_id]

    def night_value(self, night, held) -> float:
        if night not in self._by_night:            # everyone a plan can hold who plays that night
            self._by_night[night] = {p for p in set(self.roster) | set(self.pool_rate)
                                     if night in self.plays_on(p)}
        playing = sorted(held & self._by_night[night])
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
        if day not in self._reserve:
            week = self.view.calendar.weeks[self.view.week - 1]
            span = (week.end - week.start).days
            share = min(1.0, max(0.0, (week.end - day).days / span)) if span > 0 else 0.0
            self._reserve[day] = int(round(self.params.reserve * share))
        return self._reserve[day]

    def fits(self, spent, day, cost) -> bool:
        """Whether a move costing `cost` on `day` keeps the plan inside each day's budget: by any
        day D, the moves spent through D leave that day's reserve. The reserve falls through the
        week, so the week's last nights can spend what its first held back -- the whole budget
        by its last day. (Holding today's reserve all week planned two moves short; against it,
        2024-25, 16 drafts: -0.17 +/- 0.58 pts/wk, 147 moves a season against 143 -- neutral, kept
        so the plan shows the whole week.)"""
        if cost <= 0:
            return True
        # One pass over the days in order with a running total of what the plan spends by each --
        # the same integer comparisons as re-summing `spent` for every later day, without the
        # O(days x moves) re-sum on ~3.8M calls a season.
        spends, i, through = sorted(spent.items()), 0, 0
        for later in self.move_days:
            while i < len(spends) and spends[i][0] <= later:
                through += spends[i][1]
                i += 1
            if later >= day and through + cost > self.moves_left - self.reserve_on(later):
                return False
        return True

    def plan(self) -> list:
        """Greedy insertion, from an empty plan: of every move on any night ahead, put in the one
        with the largest survival^days x (gain - bar), until none clears its bar."""
        if self.params.lazy:
            return self._plan_lazy()
        moves, spent = [], {}
        open_spots = self.view.roster_room()
        pruned = set()                    # (incoming, day) the removal pass took out: never re-added
        while True:
            best = None
            held = self.held(moves)
            self._base = (moves, {})
            for incoming, day, effective in self.candidates:
                if incoming in held[0] or incoming in held[1] or (incoming, day) in pruned:
                    continue
                for outgoing, cost, trial in self.trials(moves, held, spent, open_spots,
                                                         incoming, day, effective):
                    gain, bar = trial[-1]["gain"], trial[-1]["bar"]
                    # A later night's pickup happens only if nobody takes him first.
                    edge = self.params.survival ** (day - self.today).days * (gain - bar)
                    if best is None or edge > best[0]:
                        best = (edge, trial, cost, outgoing is None, day)
            if best is None and self.params.prune:
                moves, spent, open_spots, pruned = self._prune(moves, pruned)
                if spent is not None:
                    continue
            if best is None:
                return sorted(moves, key=lambda m: (m["day"], m["effective"]))
            _, moves, cost, into_open, day = best
            spent[day] = spent.get(day, 0) + cost
            open_spots -= into_open

    def _plan_lazy(self) -> list:
        """`plan` with lazy evaluation (strategy `streaming.lazy`): the first round prices every
        candidate; after it, candidates are priced from the highest ceiling down -- a candidate's
        ceiling is its best edge when last priced -- and a round stops once the best edge found
        is at least the next ceiling. Not exact: a candidate's edge can rise between rounds (a
        planned pickup makes another night worth filling, or a new drop becomes legal), and then
        a different move is picked. Measured on drafts 0-2 (2026-10-05): a different move in 4.8%
        of rounds, on 49.8% of the trial prices. Ties keep the candidates' list order, as in
        `plan`."""
        moves, spent = [], {}
        open_spots = self.view.roster_room()
        ceilings = None                   # candidate index -> best edge when last priced
        pruned = set()                    # (incoming, day) the removal pass took out: never re-added
        while True:
            best = None
            held = self.held(moves)
            self._base = (moves, {})
            live = [i for i, (incoming, day, _) in enumerate(self.candidates)
                    if incoming not in held[0] and incoming not in held[1]
                    and (incoming, day) not in pruned]
            if ceilings is not None:
                live.sort(key=lambda i: (-ceilings.get(i, -math.inf), i))
            priced = {}
            for i in live:
                if ceilings is not None and best is not None and best[0] >= ceilings.get(i, -math.inf):
                    break
                incoming, day, effective = self.candidates[i]
                top = -math.inf
                for outgoing, cost, trial in self.trials(moves, held, spent, open_spots,
                                                         incoming, day, effective):
                    gain, bar = trial[-1]["gain"], trial[-1]["bar"]
                    edge = self.params.survival ** (day - self.today).days * (gain - bar)
                    top = max(top, edge)
                    if best is None or edge > best[0] or (edge == best[0] and i < best[5]):
                        best = (edge, trial, cost, outgoing is None, day, i)
                priced[i] = top
            ceilings = {**(ceilings or {}), **priced}
            if best is None and self.params.prune:
                moves, spent, open_spots, pruned = self._prune(moves, pruned)
                if spent is not None:
                    ceilings = None                   # the plan changed: price every candidate again
                    continue
            if best is None:
                return sorted(moves, key=lambda m: (m["day"], m["effective"]))
            _, moves, cost, into_open, day, _ = best
            spent[day] = spent.get(day, 0) + cost
            open_spots -= into_open

    def _prune(self, moves, pruned):
        """The removal pass (strategy streaming.prune): greedy insertion never revisits a move, so
        a pickup a later one made worthless stays in. Each planned move no later move depends on
        (its pickup is nobody's drop) is tried out of the plan; if the week's lineup points without
        it are within its bar of the points with it, it comes out, and the build resumes with the
        freed budget. Returns (moves, spent, open spots, pruned) -- spent is None when nothing came
        out, and the build ends."""
        def week_points(plan):
            ordered = self.ordered(plan)
            return sum(self.weight(n) * self.night_value(n, self.roster_at(self.roster, ordered, n,
                                                                            ordered=True))
                       for n in self.nights)
        dropped = {m["outgoing"] for m in moves if m["outgoing"] is not None}
        whole = week_points(moves)
        for m in sorted(moves, key=lambda m: (m["day"], m["effective"]), reverse=True):
            if m["incoming"] in dropped:
                continue
            rest = [x for x in moves if x is not m]
            if whole - week_points(rest) <= m["bar"]:
                pruned = pruned | {(m["incoming"], m["day"])}
                spent = {}
                for x in rest:
                    spent[x["day"]] = spent.get(x["day"], 0) + self.move_cost(x["day"], x["kind"],
                                                                              x["outgoing"])
                open_spots = self.view.roster_room() - sum(1 for x in rest if x["outgoing"] is None)
                return rest, spent, open_spots, pruned
        return moves, None, None, pruned

    @staticmethod
    def held(moves) -> tuple:
        """What a plan has already used: (players it adds, {player it drops: that move},
        {player it adds: from when})."""
        return ({m["incoming"] for m in moves},
                {m["outgoing"]: m for m in moves if m["outgoing"] is not None},
                {m["incoming"]: m["effective"] for m in moves})

    def trials(self, moves, held, spent, open_spots, incoming, day, effective, drops=None):
        """Every way to put `incoming` into the plan on `day` that clears its bar: (drop, cost,
        the plan with the move in, last). The drops are the spots, the plan's own earlier pickups
        and an open spot -- or only `drops`, when given."""
        _, dropped, picked_up = held
        kind = "claim" if incoming in self.claimable else "add"
        before = self.roster_at(self.roster, moves, effective)
        candidates = ([None] if open_spots > 0 else []) + sorted(
            p for p in before
            if p in self.spots or (p in picked_up and picked_up[p] < effective))
        for outgoing in candidates:
            if drops is not None and outgoing not in drops:
                continue
            # A claim's drop may be a player the plan only picks up before the claim clears (a
            # rental chain). The platform wants a drop you hold when the claim is entered, so the
            # live plan names who holds that spot today (Live/live.py); a pending claim's drop can
            # be changed before it processes. Requiring a drop held today, and never taking back a
            # player dropped the same day, cost the shipped system 1.74 +/- 0.65 pts/wk in the
            # realistic league (2024-25, 64 drafts, 2026-09-29) -- neither is enforced.
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
            # A move made today clears its bar by `min_gain` (strategy streaming.min_gain): the
            # moves made are the only ones that cost anything; later ones are planned again.
            margin = self.params.min_gain if day == self.today else 0.0
            if trial[-1]["gain"] > trial[-1]["bar"] and trial[-1]["gain"] - trial[-1]["bar"] >= margin:
                yield outgoing, cost, trial

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
        cache = self._base[1] if self._base is not None and old is self._base[0] else None
        total = 0.0
        # Each plan sorted once for all its nights (roster_at re-sorted it per night, ~8M times a
        # season), and the old side only when a night misses the cache.
        old_order, new_order = None, self.ordered(new)
        for night in nights:
            before = cache.get(night) if cache is not None else None
            if before is None:
                if old_order is None:
                    old_order = self.ordered(old)
                before = self.night_value(night, self.roster_at(self.roster, old_order, night,
                                                                ordered=True))
                if cache is not None:
                    cache[night] = before
            total += self.weight(night, late) * (
                self.night_value(night, self.roster_at(self.roster, new_order, night,
                                                       ordered=True)) - before)
        return total

    def execute(self, plan) -> list:
        """Make today's moves; the rest of the plan waits for tomorrow's replan."""
        done = []
        picked = {o["incoming"]: o for o in plan}
        for m in plan:
            if m["day"] != self.today:
                continue
            incoming, outgoing = m["incoming"], m["outgoing"]
            # A claim's drop may be a later rental of this plan (a chain). Who holds that spot
            # today, after today's moves -- the drop a platform needs when the claim is entered.
            holder, chain = outgoing, []
            while (holder is not None and holder not in self.roster and holder in picked
                   and picked[holder]["day"] != self.today and holder not in chain):
                chain.append(holder)
                holder = picked[holder]["outgoing"]
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
                         "incoming_out_long": long_absence(self.view, incoming, self.nights_of(incoming)),
                         "bar": m["bar"], "drop_cost": m["drop_cost"],
                         "spot": outgoing in self.spots,
                         "reserve": streaming.reserve_today(self.view, self.params),
                         "moves_left": self.view.moves_left,
                         "incoming_games": self._games(incoming, m["effective"],
                                                       self.released_on(m, plan)),
                         "outgoing_games": self._games(outgoing, m["effective"]),
                         "outgoing_today": holder,
                         "outgoing_picked_up": (picked[outgoing]["effective"] if chain else None)})
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
        return {"first": first, "moves": [self.describe(m, moves) for m in moves],
                "week_gain": gain, "week_edge": gain - sum(m["drop_cost"] for m in moves)}

    def _games(self, player_id, effective, until=None) -> int:
        """His team's games this week from `effective`, and before `until` (the night the plan
        releases him) when given."""
        return sum(1 for n in self.nights_of(player_id)
                   if n >= effective and (until is None or n < until))

    @staticmethod
    def released_on(m, moves):
        """When the plan drops the player move `m` picks up -- a later move in the same plan whose
        drop he is -- or None if he is kept through the week."""
        return min((o["effective"] for o in moves
                    if o is not m and o["outgoing"] == m["incoming"] and o["effective"] > m["effective"]),
                   default=None)

    def describe(self, m, moves=()) -> dict:
        """A planned move for the plan window: when, who, and what it was priced at. His games are
        the ones he plays while the plan holds him: a rental dropped on Thursday for the next one
        does not count his Saturday (until 2026-09-29 it did, so a one-night rental could show
        four games and a plan's total overstated its games)."""
        until = self.released_on(m, moves)
        # A slot's other options keep their own count: each was priced on its own chain of moves
        # (a claim clearing after the pick's release is still worth a move), so the pick's release
        # night does not bound them.
        options = m.get("options", [])
        return {"day": m["day"], "effective": m["effective"],
                "kind": "rental claim" if m["kind"] == "claim" else "rental",
                "incoming": m["incoming"], "outgoing": m["outgoing"],
                "gain": m["gain"], "bar": m["bar"], "drop_cost": m["drop_cost"],
                "incoming_games": self._games(m["incoming"], m["effective"], until),
                "outgoing_games": self._games(m["outgoing"], m["effective"]),
                "released": until,
                "today": m["day"] == self.today, "failed": m.get("failed", False),
                "for_next_week": m.get("for_next_week", False),
                # A team slot's (TeamPlans): its team, position, expected edge and ranked options.
                "team": m.get("team"), "group": m.get("group"), "expected": m.get("expected"),
                "options": options, "near": m.get("near", [])}


# ---------- the plan window's team-slot plans ----------

# P(a free agent is still free) 0, 1, 2, 3 and 4+ days after the plan picked him (2024-25 backtest,
# the shipped field; see the module docstring): a team slot's options are valued on it.
SURVIVAL_BY_DAYS = (1.0, 0.71, 0.59, 0.51, 0.44)
# What a team slot is filled by: any skater, or a goalie. Until 2026-09-30 a slot was one position
# (C, LW, RW, D), so "Wed PHI D" listed PHI defencemen only, even when a PHI forward could have
# taken the same opening -- the league's F and F/D slots take several positions, and each option is
# priced by solving the lineup with him in, so a position that does not fit simply gains less. A
# plan may still hold two skaters of one team on one day: a slot's key can be used again, each time
# for a player the plan does not already hold.
GROUPS = ("SKATER", "G")
OPTIONS_PER_SLOT = 8        # the players on a slot's team priced as its options
# Other teams' free agents shown under a slot (TeamPlans.near_ties): within this many of the week's
# lineup points with the pick, at most NEAR_TIES_SHOWN of them. Display only.
NEAR_TIE_POINTS = 1.0
NEAR_TIES_SHOWN = 3


def group_of(eligibility) -> str:
    """The slot group a player with these positions fills."""
    return "G" if "G" in eligibility else "SKATER"
# Slots within `chain_tie` expected points of each other count as tied, and the one whose pick is
# done playing sooner goes in: it leaves more of the week for another move.


def survival(days) -> float:
    return SURVIVAL_BY_DAYS[min(max(days, 0), len(SURVIVAL_BY_DAYS) - 1)]


def expected(edges, s) -> float:
    """A slot's expected edge when each of its options is still free with probability `s`, each
    independently of the others: the best one still there. `edges` best first."""
    total, missing = 0.0, 1.0
    for e in edges:
        total += missing * s * e
        missing *= 1.0 - s
    return total


class TeamPlans:
    """The plan window's week plans, by NHL team. A move is a team SLOT -- (day, team, position)
    -- backed by the free agents on that team who fit it, ranked by edge: any of them buys the
    same nights, so when the first is taken the next one is the move. A slot is worth its options'
    expected edge (`expected`): today, its best; a later night, more on a team three deep than on
    one with a single standout.

    Plans are built by WeekPlanner's greedy insertion, slot by slot, each slot's best option
    standing in for it on the plan's roster (a later move can drop that stand-in, as rentals
    chain). Plan A keeps today's moves as made (WeekPlanner.plan) and adds no other move today. B,
    C, ... are the fallbacks when a whole team is picked over: each opens on the best slot left and
    leaves out every earlier plan's first team (the team of its earliest move) for the whole week --
    B is the week without A's first team, C without A's and B's, and so on. (Banning an opening team only at its position
    left the plans the same after their first move.) Display only: nothing here changes what is
    made."""

    def __init__(self, planner):
        self.p = p = planner
        view, today = p.view, p.today
        pool = p.addable + p.claimable
        p.rates.update({q: p.pool_rate[q] for q in pool if q not in p.rates})
        options = {}
        for q in pool:
            team = view.nhl_team.get(q)
            positions = p.eligibility.get(q, ())
            if team is None or not positions:
                continue
            groups = [group_of(positions)]
            if q in p.claimable:
                days = [(today, max(view.waiver_clears(q), p.effective_on(today)))]
            else:
                days = [(d, p.effective_on(d)) for d in p.move_days if d == today or d in p.nights_of(q)]
            for day, effective in days:
                # WeekPlanner._shortlist's worth: rate x games from the move (a goalie tonight at
                # tonight's line), next week's too for a late move bought for next week.
                ahead = p.next_weight * len(p.next_nights_of(q)) if day == today and p.late else 0.0
                worth = p.pool_rate[q] * (sum(1 for n in p.nights_of(q) if n >= effective) + ahead)
                if effective == today and q in p.tonight and today in p.nights_of(q):
                    worth += p.tonight[q] - p.pool_rate[q]
                if worth > 0.0:
                    for g in groups:
                        options.setdefault((team, g, day), []).append((worth, q, effective))
        # Each day's best slots by their options' expected worth, `shortlist` of them.
        self.options, self.slots = {}, []
        for day in p.move_days:
            s = survival((day - today).days)
            ranked = []
            for key, found in options.items():
                if key[2] == day:
                    found.sort(key=lambda f: (-f[0], f[1]))
                    found = found[:OPTIONS_PER_SLOT]
                    self.options[key] = [(q, effective) for _, q, effective in found]
                    ranked.append((-expected([w for w, _, _ in found], s), key))
            self.slots += [key for _, key in sorted(ranked)[:p.params.shortlist]]

    # ---------- a slot ----------

    def price(self, slot, moves, held, spent, open_spots, drops=None, stand_in=None):
        """The slot put into the plan, with the drop that makes it worth most: (expected edge,
        the plan with the slot's best option in it -- or `stand_in`'s -- cost, into an open spot,
        day), the move carrying its team, position and ranked options; None if no option clears."""
        team, group, day = slot
        found = {}
        candidates = self.options.get(slot, [])
        if stand_in is not None and all(q != stand_in[0] for q, _ in candidates):
            candidates = [stand_in] + candidates
        for q, effective in candidates:
            if q in held[0] or q in held[1]:
                continue
            for outgoing, cost, trial in self.p.trials(moves, held, spent, open_spots, q, day,
                                                       effective, drops):
                found.setdefault(outgoing, []).append((trial[-1]["gain"] - trial[-1]["bar"], cost, trial))
        best, s = None, survival((day - self.p.today).days)
        for outgoing, ranked in found.items():
            ranked.sort(key=lambda f: -f[0])
            if stand_in is not None and all(f[2][-1]["incoming"] != stand_in[0] for f in ranked):
                continue
            value = expected([f[0] for f in ranked], s)
            if best is None or value > best[0]:
                best = (value, outgoing, ranked)
        if best is None:
            return None
        value, outgoing, ranked = best
        _, cost, trial = next(f for f in ranked if stand_in is None or f[2][-1]["incoming"] == stand_in[0])
        trial[-1].update(team=team, group=group, expected=value,
                         options=[self.option(f[2][-1]) for f in ranked])
        return value, trial, cost, outgoing is None, day

    def option(self, m) -> dict:
        return {"incoming": m["incoming"], "kind": "rental claim" if m["kind"] == "claim" else "rental",
                "effective": m["effective"], "gain": m["gain"], "bar": m["bar"],
                "games": self.p._games(m["incoming"], m["effective"])}

    def ends(self, priced):
        """The last night this week the slot's pick plays."""
        m = priced[1][-1]
        return max([n for n in self.p.nights_of(m["incoming"]) if n >= m["effective"]] or [m["effective"]])

    def better(self, a, b) -> bool:
        if abs(a[0] - b[0]) <= self.p.params.chain_tie:
            return (self.ends(a), -a[0]) < (self.ends(b), -b[0])
        return a[0] > b[0]

    # ---------- a plan ----------

    def build(self, moves, spent, open_spots, banned=frozenset(), today_closed=False) -> list:
        """Greedy insertion by slot from the plan given: the slot worth most goes in, until none
        clears. Never a `banned` team; with `today_closed`, no other move today."""
        moves, spent = list(moves), dict(spent)
        while True:
            best, held = None, self.p.held(moves)
            for slot in self.slots:
                if slot[0] in banned or (today_closed and slot[2] == self.p.today):
                    continue
                priced = self.price(slot, moves, held, spent, open_spots)
                if priced is not None and (best is None or self.better(priced, best)):
                    best = priced
            if best is None:
                return sorted(moves, key=lambda m: (m["day"], m["effective"]))
            _, moves, cost, into_open, day = best
            spent[day] = spent.get(day, 0) + cost
            open_spots -= into_open

    def made(self, plan):
        """Today's moves as WeekPlanner made them, each as the team slot of his group (priced on
        today's moves before it): (moves, spent, open spots)."""
        p = self.p
        moves, spent, open_spots = [], {}, p.view.roster_room()
        for m in plan:
            if m["day"] != p.today:
                continue
            team = p.view.nhl_team.get(m["incoming"])
            group = group_of(p.eligibility.get(m["incoming"], ()))
            best = self.price((team, group, p.today), moves, p.held(moves), spent, open_spots,
                              drops={m["outgoing"]}, stand_in=(m["incoming"], m["effective"]))
            if best is None:                  # priced on less of the plan, it no longer clears
                move = dict(m, team=team, expected=m["gain"] - m["bar"], group=group)
                move["options"] = [self.option(move)]
                cost = p.move_cost(p.today, m["kind"], m["outgoing"])
                best = (move["expected"], moves + [move], cost, m["outgoing"] is None, p.today)
            _, moves, cost, into_open, _ = best
            spent[p.today] = spent.get(p.today, 0) + cost
            open_spots -= into_open
        return moves, spent, open_spots

    def plans(self, plan, alternatives) -> list:
        """Plan A (today's moves as made, the rest of the week by slot), then up to `alternatives`
        others, each opened by the best slot left and without any earlier plan's first team,
        best first. Each plan says which teams it leaves out (`without`)."""
        p = self.p
        room = p.view.roster_room()
        a = self.build(*self.made(plan), today_closed=True)
        out = [(a, a[0] if a else None, ())]
        banned = [a[0]["team"]] if a else []
        held = p.held([])
        openings = [priced for priced in (self.price(slot, [], held, {}, room) for slot in self.slots)
                    if priced is not None]
        openings.sort(key=lambda o: -o[0])
        others = []
        # Each plan bans its FIRST move's team, as A does -- the fallback for "the first pickup is
        # taken". Banning the opening slot's instead (the plan's most valuable move, often late in
        # the week) left early-week teams in every plan: 12090 week 2 (2026-10-04), NSH opened
        # B-D and was never banned. An opening slot can open several plans, each without more teams.
        while len(others) < alternatives:
            opening = next((o for o in openings if o[1][-1]["team"] not in banned), None)
            if opening is None:
                break
            _, trial, cost, into_open, day = opening
            moves = self.build(trial, {day: cost}, room - into_open, banned=frozenset(banned))
            others.append((moves, moves[0], tuple(banned)))
            banned.append(moves[0]["team"])
        return ([self.summary(*o) for o in out]
                + sorted((self.summary(*o) for o in others), key=lambda s: -s["expected"]))

    def near_ties(self, moves, i) -> list:
        """Other teams' free agents nearly as good as slot i's pick (the plan window, display only):
        each shortlisted pickup of the slot's day on another team, tried in the pick's place -- a
        later move that dropped the pick drops him instead -- and kept if the week's lineup points
        come within NEAR_TIE_POINTS of the plan's. A slot's own options are its team's; these are
        the ones a team slot hides (Robertson behind a WSH slot, 2026-10-04). Best first."""
        p, m = self.p, moves[i]
        def points(plan):
            ordered = p.ordered(plan)
            return sum(p.weight(n) * p.night_value(n, p.roster_at(p.roster, ordered, n, ordered=True))
                       for n in p.nights)
        whole = points(moves)
        taken = {x["incoming"] for x in moves} | {x["outgoing"] for x in moves if x["outgoing"] is not None}
        found = []
        for incoming, day, effective in p.candidates:
            if (day != m["day"] or incoming in taken
                    or p.view.nhl_team.get(incoming) == m.get("team")):
                continue
            trial = [dict(x) for x in moves]
            trial[i].update(incoming=incoming, effective=effective)
            for j, x in enumerate(trial):
                if j != i and x["outgoing"] == m["incoming"]:
                    x["outgoing"] = incoming
            delta = points(trial) - whole
            if delta >= -NEAR_TIE_POINTS:
                found.append({"incoming": incoming, "team": p.view.nhl_team.get(incoming),
                              "delta": delta, "games": p._games(incoming, effective)})
        found.sort(key=lambda f: (-f["delta"], f["incoming"]))
        return found[:NEAR_TIES_SHOWN]

    def summary(self, moves, first, without=()) -> dict:
        """The plan for the window. A slot's options leave out the plan's other picks -- two VAN LW
        slots today are two different players, not each other's fallback -- and its expected edge
        is taken again over the options left."""
        picks = {m["incoming"] for m in moves}
        trimmed = []
        for m in moves:
            options = [o for o in m["options"] if o["incoming"] == m["incoming"] or o["incoming"] not in picks]
            value = expected([o["gain"] - o["bar"] for o in options], survival((m["day"] - self.p.today).days))
            trimmed.append(dict(m, options=options, expected=value))
        moves = trimmed
        for i, m in enumerate(moves):
            m["near"] = self.near_ties(moves, i)
        out = self.p.summary(moves, first["incoming"] if first is not None else None)
        out.update(expected=sum(m["expected"] for m in moves),
                   games=sum(m["incoming_games"] for m in out["moves"]),
                   thinnest=min((len(m["options"]) for m in moves), default=None),
                   without=list(without),
                   opening=(None if first is None else
                            {"day": first["day"], "team": first["team"], "group": first["group"]}))
        return out

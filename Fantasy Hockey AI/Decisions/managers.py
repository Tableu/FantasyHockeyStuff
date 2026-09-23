"""The section 16 ladder, as four managers behind one interface.

Each rung is defined by what it is allowed to look at, and that is the whole design:

    rung 1  autodraft, never touch the lineup     sees nothing after draft day
    rung 2  start every healthy player daily      sees the schedule and the injury report
    rung 3  schedule-maximizing streamer          + a box-score rate, and spends its 7 moves
    rung 4  full system                           + the projection stack and the distributions

Rungs 2 and 3 are the pair the whole exercise turns on. Rung 2 adds nothing but attention, and in
a daily-lock league with no games-played cap attention alone is worth a great deal; rung 3 adds
the free half of section 15 plus a capped acquisition budget, and it uses **no model at all**. If
rung 4 cannot clear rung 3, the modelling stack is not paying for itself, and that is the finding
the harness exists to produce.

`set_lineup` returns a `slots.Lineup`; `transactions` mutates state through `state.LeagueState`'s
methods, which raise rather than silently refuse. Nothing here ever sees an outcome -- a manager
only gets a `SlateView` (see `Season/view.py`).

This module imports nothing from `Season/`. It works against the view's interface -- the holdings,
tonight's slate, the schedule, the projections -- and the README lists exactly what that interface
has to offer, so a live runner can hand the same managers a view built from real data.
"""

import logging
import math

import adddrop
import orchestrator
import slots as slots_module
import streaming

log = logging.getLogger("managers")


class Manager:
    """The interface the engine drives. A rung overrides what it needs."""

    name = "base"
    rung = 0
    # Which P(start) estimate this rung may read. Declared per rung so that the naive share and
    # the fitted model cannot be confused for one another.
    p_start_column = "p_start_naive"

    def __init__(self, team_index, config, scoreset):
        self.team_index = team_index
        self.config = config
        self.scoreset = scoreset
        self.slot_order = config.slot_order()
        # Which positions each slot will take. A composite slot (`F`, `F/D`) accepts several, so
        # this cannot be inferred from the slot code.
        self.accepts = config.accepts

    def set_lineup(self, view):
        raise NotImplementedError

    def transactions(self, view) -> None:
        """Default: do nothing. Rungs 1 and 2 never transact."""
        return None

    def manage_ir(self, view) -> None:
        """Activate the recovered and stash the injured. Free in this format, so stashing is
        unconditional -- but an activation needs a roster spot, and on a full roster it FORCES a
        drop. That drop is a decision, priced by `activation_drop`.

        A player counts as recovered when his team's latest lockout report says so
        (`view.healthy_on_ir`), not when his team is merely idle tonight; and the league does not
        let him sit on IR once he is healthy, so each one is resolved today, in the cheapest way
        available: an open spot, then a swap with a newly injured player going the other way,
        and only then a drop.
        """
        state = view._state
        eligible = view.ir_eligible()
        to_stash = sorted(eligible)
        for player_id in view.healthy_on_ir():
            if view.roster_room() > 0:
                state.activate(self.team_index, player_id)
            elif to_stash:
                state.activate(self.team_index, player_id, stash=to_stash.pop(0),
                               ir_eligible=eligible)
            else:
                drop = self.activation_drop(view, player_id)
                state.activate(self.team_index, player_id, drop=drop, today=view.day)
                self.ir_log.append({"day": view.day, "returning": player_id, "dropped": drop})
        for player_id in to_stash:
            if len(state.teams[self.team_index].ir) >= self.config.ir:
                break
            state.stash(self.team_index, player_id, eligible)

    @property
    def ir_log(self) -> list:
        if not hasattr(self, "_ir_log"):
            self._ir_log = []
        return self._ir_log

    def activation_drop(self, view, returning):
        """Whom a forced activation drops, the returning player included.

        The default reads only the box score -- season-to-date points per game, shrunk toward
        last season (`view.history`), which any manager can read off the platform -- so rungs 2
        and 3 stay model-free. The cheapest player whose release leaves the roster fieldable.
        """
        return self._cheapest_safe_drop(view, returning, lambda p: view.history.get(p))

    def _cheapest_safe_drop(self, view, returning, cost):
        """The forced drop that leaves the roster able to fill as many slots as it can, and among
        those the cheapest by `cost` -- the returning player himself included.

        Safety comes first because the alternative strands the roster. With the returning player
        an unconditional candidate, a team whose second goalie came back from IR dropped HIM, was
        left with one goalie for two G slots, and -- the fieldability check then failing for every
        swap -- froze for the rest of the season (rung 7, seat 7: weeks 2-26 with no upgrade).
        """
        eligibility = view._state.eligibility
        full = list(view.roster) + [returning]
        fill = {d: self._fillable([p for p in full if p != d], eligibility) for d in full}
        best = max(fill.values())
        return min((d for d in full if fill[d] == best), key=lambda d: (cost(d), d))

    # A slot filled by a body is never worth less than an empty slot, so every startable player
    # is floored above zero. Without this the solver treats a player it values at 0.0 -- a rookie
    # with no history, a fresh waiver add -- as equivalent to leaving the slot empty, and declines
    # to start him. In a format where an idle star scores 0.00 and any regular playing tonight
    # scores about 2.9, that is the most expensive error available.
    FLOOR = 1e-3

    def _lineup_from_values(self, view, values):
        startable = {p: max(float(values.get(p, 0.0)), self.FLOOR)
                     for p in view.roster if view.available(p)}
        return slots_module.assign(self.slot_order, startable, view._state.eligibility,
                                   self.accepts)

    def _fillable(self, roster, eligibility) -> int:
        """How many active slots this roster could fill on a night when everyone plays."""
        return slots_module.matching_size(roster, self.slot_order, eligibility, self.accepts)

    def _fieldable(self, roster, eligibility, before=None) -> bool:
        """The check a transaction has to pass: it may not leave the roster less able to fill
        its slots than `before` was. With no `before`, whether it can fill every slot.

        Without a check a streamer chasing games remaining will happily drop its second goalie
        for a fourth centre and leave a G slot empty for the rest of the season. But the check
        has to be RELATIVE. As an absolute test ("could every slot be filled?") it failed for
        every swap whenever a goalie was on IR -- one goalie for two G slots -- so the team could
        make no move at all, including the one that would have fixed it (see `repair_roster`).
        """
        size = self._fillable(roster, eligibility)
        if before is None:
            return size >= len(self.slot_order)
        return size >= min(self._fillable(before, eligibility), len(self.slot_order))

    def repair_roster(self, view, value) -> list:
        """If the roster cannot fill every slot, spend moves restoring it before anything else.

        A goalie on IR, or a forced drop that left one position short, leaves an active slot
        empty every night. No upgrade rule fixes that on its own: its shortlist is ranked by
        forward value, and a replacement goalie rarely makes it. So this adds the best free
        agent (by `value`) who raises the number of fillable slots, dropping the cheapest player
        whose release keeps that gain -- or nobody, if a stash left a spot open. Costs a move
        each, like any add.
        """
        state = view._state
        eligibility = state.eligibility
        need = len(self.slot_order)
        done = []
        while view.moves_left > 0:
            roster = list(view.roster)
            have = self._fillable(roster, eligibility)
            if have >= need:
                break
            pool = sorted((p for p in view.free_agents() if not view.on_waivers(p)),
                          key=lambda p: (-value(p), p))
            move = None
            for incoming in pool:
                if self._fillable(roster + [incoming], eligibility) <= have:
                    continue
                if view.roster_room() > 0:
                    move = (incoming, None)
                    break
                drops = [d for d in roster
                         if self._fillable([x for x in roster if x != d] + [incoming],
                                           eligibility) > have]
                if drops:
                    move = (incoming, min(drops, key=lambda d: (value(d), d)))
                    break
            if move is None:
                break
            state.add(self.team_index, move[0], view.day, drop=move[1], reason="repair")
            done.append({"day": view.day, "kind": "repair", "incoming": move[0],
                         "outgoing": move[1]})
        return done


class AutodraftForget(Manager):
    """Rung 1: draft, set a lineup once, never look again. The floor.

    Section 16 calls this the floor and says any system not clearing it by a wide margin is
    broken. The lineup is frozen on the first night, so an injured or idle player keeps his slot
    and scores zero -- which is exactly the cost of inattention this rung is here to price.
    """

    name = "autodraft-forget"
    rung = 1

    def __init__(self, team_index, config, scoreset):
        super().__init__(team_index, config, scoreset)
        self._frozen = None

    def set_lineup(self, view):
        if self._frozen is None:
            # Draft order is this rung's only opinion: the earlier pick starts.
            ranking = {p: -i for i, p in enumerate(view.roster)}
            self._frozen = slots_module.assign(self.slot_order, ranking,
                                              view._state.eligibility, self.accepts)
        return self._frozen

    def manage_ir(self, view) -> None:
        """It never touches the roster, IR included -- that is what makes it the floor."""
        return None


class StartEveryone(Manager):
    """Rung 2: fill every slot every night, with whoever is playing. No projections.

    The value of a candidate is 1.0 if his team plays tonight and he is not hurt, and the
    tie-break is draft order. That is deliberate: this rung must not consult a projection, or it
    stops isolating attention from modelling. In this format that is a strong strategy on its own
    -- an idle star scores 0.00 and any rostered regular playing tonight scores about 2.9.
    """

    name = "start-everyone"
    rung = 2

    def set_lineup(self, view):
        values = {p: 1.0 + (len(view.roster) - i) * 1e-6
                  for i, p in enumerate(view.roster)}
        return self._lineup_from_values(view, values)


class ScheduleStreamer(Manager):
    """Rung 3: rung 2, plus seven moves a week spent on games remaining. Still no model.

    This is the rung that matters. Its projection is a box-score rate -- season-to-date fantasy
    points per game, shrunk toward last season (`view.naive_history`) -- and its transaction rule
    is section 15's arithmetic:

        value of a move = games that player has left this week x (his rate - the rate he displaces)

    Two consequences of the cap are built in rather than approximated. **Front-loading**: a Monday
    add on a four-game team is worth roughly four times a Saturday add on a one-game player, so
    the threshold falls as the week runs out rather than being constant. And **the drop costs its
    own forward value**: late in the week a one-game streamer can cost more in the dropped
    player's next-week games than it gains in this one, so the comparison is against the drop's
    remaining value, not against zero.
    """

    name = "schedule-streamer"
    rung = 3
    p_start_column = "p_start_naive"

    # How far ahead a swap is priced. `None` means the rest of the season. Both sides of the swap
    # use it, because both sides are permanent -- the roster spot is kept either way. Swept against
    # the season rather than assumed; see the table in the README.
    horizon_weeks = 1

    def __init__(self, team_index, config, scoreset, horizon_weeks=None):
        super().__init__(team_index, config, scoreset)
        if horizon_weeks is not None:
            self.horizon_weeks = horizon_weeks

    def set_lineup(self, view):
        view.p_start_column = self.p_start_column
        # Tonight he either dresses or he does not, so the lineup wants the rate, not the rate
        # times a dress share -- the share is for deciding who to *acquire*, not who to start.
        return self._lineup_from_values(view, view.history.rate)

    def transactions(self, view) -> None:
        view.p_start_column = self.p_start_column
        state = view._state
        if view.moves_left <= 0:
            return
        rates = view.history
        self.repair_roster(view, rates.get)

        # Two horizons, because the two sides of a swap are not symmetric. An acquisition is a
        # rental: it pays out over the games left before the reset and can be re-evaluated next
        # week. A drop is **permanent** -- he goes back to a pool eleven other managers share -- so
        # it costs his value over a forward window, not just tonight's.
        #
        # Getting this wrong is not subtle. Priced over the current week alone, every rostered
        # player is worth 0.0 once his team has finished playing, so a Saturday streamer drops a
        # star for a one-game scrub, permanently. Measured on this harness that lost 50.2% of moves
        # to dropping the better player and cost rung 3 about 23 points a week against rung 2.
        # `rates.value` is rate x dress share: expected points from one of his team's games. The
        # dress share is what stops this buying a thirteenth forward because his club plays four
        # times -- his club's games are not his games.
        def rental_value(player_id):
            return rates.value(player_id) * view.games_remaining(player_id)

        def forward_value(player_id):
            return rates.value(player_id) * view.games_through(
                player_id, weeks_ahead=self.horizon_weeks)

        eligibility = state.eligibility
        candidates = []
        for player_id in view.free_agents():
            if view.on_waivers(player_id):
                continue
            games = view.games_remaining(player_id)
            if games <= 0:
                continue
            candidates.append((rental_value(player_id), games, player_id))
        if not candidates:
            return
        candidates.sort(reverse=True)

        while view.moves_left > 0 and candidates:
            gain, games, incoming = candidates.pop(0)
            roster = [p for p in view.roster if p not in view.ir]
            if not roster:
                break

            if view.roster_room() > 0:
                # An open spot -- a stash just made one -- displaces nobody, so the add only has
                # to be worth something. This is what makes an IR stash worth anything at all.
                outgoing = None
                if forward_value(incoming) <= 0.0:
                    continue
            else:
                # The cheapest legal drop by FORWARD value, not by this week's. A swap that leaves
                # a slot unfillable is not an improvement at any value either.
                drops = sorted(roster, key=forward_value)
                outgoing = next(
                    (d for d in drops
                     if self._fieldable([p for p in roster if p != d] + [incoming], eligibility,
                                        before=roster)),
                    None)
                if outgoing is None:
                    continue
                # The stopping rule, and it compares like with like: what the incoming player is
                # worth over the same forward window the drop is priced on. Unused moves expire,
                # but a move that does not beat what it displaces is still worth not making.
                if forward_value(incoming) <= forward_value(outgoing):
                    break
            try:
                state.add(self.team_index, incoming, view.day, drop=outgoing, reason="stream")
            except Exception as error:
                log.debug("team %d could not stream %s: %s", self.team_index, incoming, error)
                continue


class FullSystem(Manager):
    """Rung 4: the projection stack, the distributions, and P(win) as the objective.

    Everything below rung 4 maximizes points. This one maximizes the chance of winning a specific
    week against a specific opponent, which is a different thing and is the reason section 5 exists.
    The mechanism, and it is not a heuristic -- it falls out of the normal approximation:

        P(win) = Phi(d / s)        d = my projected margin, s = the margin's sd

        dP = (phi/s) d(mean) - (phi z / s) d(s)  with z = d/s

    Setting that to zero gives the exchange rate between mean and spread: **d(mean) = z d(s)**. So a
    manager who is behind (z < 0) should pay mean for spread, one who is ahead should pay spread for
    mean, and the price is the z-score itself rather than a tuned risk parameter. Scoring each
    candidate at `mean - z * sd` therefore makes the lineup a *linear* objective again, which means
    `slots.assign` still solves it exactly instead of needing a search.

    Three things it reads that no lower rung may:

      * the calibrated per-player distribution (`view.moments`), rather than a point estimate;
      * the fitted P(start) (AUC 0.876 held out) rather than a box-score start share;
      * the opponent's roster, so the objective is about beating *him* and not about a total.

    Its transactions are priced the same way, over the same forward window rung 3 uses -- so the
    comparison between them is the information, not the horizon.
    """

    name = "full-system"
    rung = 4
    p_start_column = "p_start_model"

    # How far ahead an acquisition is priced, in matchup weeks. Matched to rung 3 on purpose.
    horizon_weeks = 1

    # Per-game sd/mean for a skater, measured over 465 candidates on three slates: median 0.912,
    # mean 0.990. Used only where a player has no draw tonight (see `_week_projection`).
    FALLBACK_CV = 0.9

    def _z(self, view, moments):
        """The matchup z-score: how far ahead or behind, in margins of the week's own spread.

        Both sides are projected over the games each still has this week. The opponent is assumed
        to start his best legal lineup by expected points -- not his best by P(win), because he is
        not being modelled as an adversary, and assuming he plays well is the conservative choice.
        """
        mine = self._week_projection(view, view.roster, moments)
        theirs = self._week_projection(view, view.opponent_roster(), moments)
        d = (view.my_week_points + mine[0]) - (view.opponent_week_points + theirs[0])
        s = (mine[1] + theirs[1]) ** 0.5
        if s <= 1e-9:
            return 0.0
        # Clipped because the tails of the normal approximation are not to be trusted, and a z of
        # -8 would otherwise buy any amount of variance at any cost in mean.
        return float(max(-3.0, min(3.0, d / s)))

    def _week_projection(self, view, roster, moments):
        """(mean, variance) of what a roster still scores this week.

        Rough on purpose: each player is valued at his per-game moments times the games his team has
        left, capped at the active slots available over those nights. It only has to be good enough
        to place the matchup on the right side of even, because all `_z` uses is the ratio.
        """
        mean = var = 0.0
        for player_id in roster:
            games = view.games_remaining(player_id)
            if games <= 0:
                continue
            if player_id in moments:
                mu, sd = moments[player_id]
            else:
                # He plays later this week but not tonight, so there is no draw for him. Fall back
                # to his carried projected rate -- NOT to rung 3's box-score estimator, which would
                # quietly put part of rung 4's objective on the naive number it is being compared
                # against. The spread scales off the measured per-game coefficient of variation:
                # sd/mean has a median of 0.912 across candidates (mean 0.990), so 0.9 is the
                # grounded stand-in. Zero was the obvious placeholder and it is badly wrong -- it
                # says a player who is idle tonight makes the week certain.
                mu = view.projected_rate(player_id)
                sd = mu * self.FALLBACK_CV
            mean += mu * games
            var += (sd ** 2) * games
        return mean, var

    def set_lineup(self, view):
        view.p_start_column = self.p_start_column
        candidates = [p for p in view.roster if view.available(p)]
        moments = view.moments(self.scoreset, candidates + view.opponent_roster() + view.roster)
        z = self._z(view, moments)
        values = {}
        for player_id in candidates:
            mu, sd = moments.get(player_id, (0.0, 0.0))
            # The exchange rate derived above. When level (z=0) this is exactly expected points.
            values[player_id] = mu - z * sd
        self._last_z = z
        return self._lineup_from_values(view, values)

    def transactions(self, view) -> None:
        view.p_start_column = self.p_start_column
        state = view._state
        if view.moves_left <= 0:
            return
        self.repair_roster(view, view.projected_rate)
        eligibility = state.eligibility
        roster = [p for p in view.roster if p not in view.ir]
        if not roster:
            return

        pool = [p for p in view.free_agents() if not view.on_waivers(p)]

        def forward(player_id):
            """Expected points over the forward window, with P(plays) already inside the mean.

            This is the whole of rung 4's advantage over rung 3 on transactions. Rung 3 multiplies a
            box-score rate by a *dress share* it estimates from counts; here the mean already comes
            from a chain whose first link is a fitted P(plays) at 0.969-0.991 AUC, and whose rates
            are shrunk per category by a constant measured in hours of ice time. Rung 3 shrinks an
            unproven free agent toward the league mean and so systematically prefers him to a
            rostered player with a measured low rate; this does not.
            """
            games = view.games_through(player_id, weeks_ahead=self.horizon_weeks)
            if games <= 0:
                return 0.0
            return view.projected_rate(player_id) * games

        # Ranked on the forward window rather than on tonight, so a free agent whose team is dark
        # this evening but plays four times before the reset is still visible.
        candidates = sorted(((forward(p), p) for p in pool), reverse=True)
        candidates = [(value, p) for value, p in candidates if value > 0.0]

        while view.moves_left > 0 and candidates:
            gain, incoming = candidates.pop(0)
            roster = [p for p in view.roster if p not in view.ir]
            if view.roster_room() > 0:
                outgoing = None           # an open spot (after a stash) displaces nobody
            else:
                drops = sorted(roster, key=forward)
                outgoing = next(
                    (d for d in drops
                     if self._fieldable([x for x in roster if x != d] + [incoming], eligibility,
                                        before=roster)),
                    None)
                if outgoing is None:
                    continue
                if gain <= forward(outgoing):
                    break
            try:
                state.add(self.team_index, incoming, view.day, drop=outgoing, reason="upgrade")
            except Exception as error:
                log.debug("team %d could not upgrade to %s: %s", self.team_index, incoming, error)
                continue

    # How a forced activation drop is priced: section 9's defaults. Rung 5 and up use their own
    # add/drop parameters instead, so both of a manager's drop decisions read the same numbers.
    def _drop_pricing(self):
        return 3, "per_game"

    def activation_drop(self, view, returning):
        """Price the forced drop on the roster: the player whose removal costs the fewest lineup
        points over the window, with the returning player on it. He is a candidate himself."""
        import valuation

        horizon, source = self._drop_pricing()
        eligibility = view._state.eligibility
        full = list(view.roster) + [returning]
        rates = {p: valuation.rate(view, p, source) for p in full}
        nights = valuation.RosterNights(view, full, rates, horizon, self.slot_order,
                                        eligibility, self.accepts)
        def cost(d):
            # Unknown is not worthless: a player nobody has projected is never the forced drop
            # while anyone else would do.
            if d != returning and not valuation.known(view, d, source):
                return float("inf")
            return nights.removal_cost(d)

        return self._cheapest_safe_drop(view, returning, cost)


class FullSystemAddDrop(FullSystem):
    """Rung 4's lineup, with section 9's add/drop rule in place of its transactions.

    Same projections, same distributions, same z-scored lineup; only the transaction half
    changes, so the gap to rung 4 is the value of the new rule and nothing else. See
    `adddrop.py` for the rule and `AddDropParams` for what can be varied.
    """

    name = "full-system-adddrop"
    rung = 5
    params = adddrop.AddDropParams()

    def __init__(self, team_index, config, scoreset, params=None):
        super().__init__(team_index, config, scoreset)
        if params is not None:
            self.params = params
            self.name = f"full-system-adddrop[{params.describe()}]"
        self.move_log = []

    def _drop_pricing(self):
        return self.params.horizon_weeks, self.params.rate_source

    def transactions(self, view) -> None:
        view.p_start_column = self.p_start_column
        if math.isinf(self.params.margin):
            return                                  # the hold arm: no moves at all, as rung 6
        self.move_log += self.repair_roster(view, view.projected_rate)
        self.move_log += adddrop.run(view, self.params, self.slot_order, self.accepts,
                                     self._fieldable)


class FullSystemHold(FullSystem):
    """Rung 4's lineup and nothing else: never transacts.

    The comparison arm for add/drop. Rung 2 also never transacts, but it slots by attention alone;
    this slots with the full stack, so the gap between it and rung 5 is what the moves are worth.
    """

    name = "full-system-hold"
    rung = 6

    def transactions(self, view) -> None:
        return None


LADDER = {1: AutodraftForget, 2: StartEveryone, 3: ScheduleStreamer, 4: FullSystem,
          5: FullSystemAddDrop, 6: FullSystemHold}


class Orchestrated(FullSystemAddDrop):
    """Rung 7: section 10's orchestrator -- rung 5's upgrades plus a streaming layer that spends
    only the moves the upgrades leave, run as one fixed, logged daily sequence
    (`orchestrator.DailyPlan`).

    With zero streaming spots it is rung 5 step for step: same IR, same upgrades, same lineup.
    `Season/verify.py` holds it to that, which is what makes the gap to rung 5 the value of
    streaming and nothing else.
    """

    name = "orchestrated"
    rung = 7
    stream_params = streaming.StreamParams()

    def __init__(self, team_index, config, scoreset, params=None, stream_params=None):
        super().__init__(team_index, config, scoreset, params=params)
        if stream_params is not None:
            self.stream_params = stream_params
        self.name = f"orchestrated[{self.params.describe()} {self.stream_params.describe()}]"
        self.plan = orchestrator.DailyPlan(self, self.stream_params)

    # The engine calls manage_ir -> transactions -> set_lineup. The plan owns the order, so IR
    # runs inside `transactions` (still before any move and before the engine's IR check).
    def manage_ir(self, view) -> None:
        return None

    def manage_ir_step(self, view) -> None:
        Manager.manage_ir(self, view)

    def transactions(self, view) -> None:
        self.plan.before_lock(view)

    def lineup_step(self, view):
        return FullSystem.set_lineup(self, view)

    def set_lineup(self, view):
        return self.plan.at_lock(view)


LADDER[7] = Orchestrated


def build_field(config, scoreset, rungs=(1, 2, 3, 4), clones=None, streamer_horizon=None,
                replication=0, adddrop_params=None, stream_params=None):
    """One manager per seat, rungs interleaved so seats are not blocked by strategy.

    Interleaving matters: three consecutive seats all drafting for the same rung would give that
    rung all three of the same snake positions.
    """
    rungs = [r for r in rungs if r in LADDER]
    clones = clones or (config.teams // len(rungs))
    field = []
    # The offset matters whenever the seat count is not a multiple of the rung count. Fourteen
    # seats over four rungs gives two rungs four clones and two rungs three, every time -- so the
    # assignment is rotated by replication and the extra seats move around instead of always
    # landing on the same rungs.
    for seat in range(config.teams):
        rung = rungs[(seat + replication) % len(rungs)]
        if rung == 3 and streamer_horizon is not None:
            field.append(ScheduleStreamer(seat, config, scoreset,
                                          horizon_weeks=streamer_horizon))
        elif rung == 5 and adddrop_params is not None:
            field.append(FullSystemAddDrop(seat, config, scoreset, params=adddrop_params))
        elif rung == 7:
            field.append(Orchestrated(seat, config, scoreset, params=adddrop_params,
                                      stream_params=stream_params))
        else:
            field.append(LADDER[rung](seat, config, scoreset))
    if len(field) != config.teams:
        raise ValueError(f"{len(field)} managers for {config.teams} seats")
    return field

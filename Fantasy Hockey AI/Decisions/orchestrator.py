"""Section 10's orchestrator: the full-system manager's day, as a fixed and logged sequence.

The build plan's orchestrator was once an arbiter between specialists. In practice only one
decision interacts with the others -- add/drop, which touches the move budget, the roster the IR
step leaves, and tonight's lineup at once -- so the orchestrator predicts nothing. It fixes the
order, hands each step what the step before it left, and writes down what it saw and did:

    0. ingest      lockout snapshot, refreshed projections        live runner's job (not here)
    1. matchup     the week's z: how far ahead or behind          FullSystem._z
    2. ir          activate the recovered (a forced drop on a     Manager.manage_ir
                   full roster), stash the injured
       repair      restore a roster that cannot fill every slot    Manager.repair_roster
    3. upgrade     section 9's add/drop rule                       adddrop.run
    4. stream      spend what the upgrades leave, this week only   streaming.run, or
                   (mode "week") planned over the rest of the week  weekplan.run
    5. lineup      the z-scored exact solve at the lock            FullSystem.set_lineup
    6. trades      weekly, advisory                                section 9 step 3 (not here)

The order is the design. IR runs before any move is priced, so an activation's forced drop is
settled first; upgrades run before streams, so a rental can only spend moves an upgrade did not
want; and every transaction is done before the lineup solver sees the roster. The live plan window
alone plans the week apart from the upgrades (`week_view_on`, the user's call 2026-10-07): on the
roster and moves before them, so choosing upgrades never changes the week plan; today's rentals are
then made after the upgrades, and one they left no room for is reported, not made.

`DailyPlan` works against the same view interface as every manager (Decisions/README.md), so the
live runner section 10 needs later builds a real view and calls the same two methods.
"""

import copy

import adddrop
import streaming
import weekplan


class DailyPlan:
    """One team's orchestrated day. `manager` supplies the steps; this only sequences them."""

    def __init__(self, manager, stream_params: streaming.StreamParams):
        self.manager = manager
        self.stream_params = stream_params
        self.log = []
        # The week mode's plan for the rest of the week, as of the last day planned (weekplan.py):
        # today's moves made, the later ones recommendations. Empty in the daily mode.
        self.week_plan = []
        # Alternative plans to show beside it, each opened by a different first pickup: the plan
        # window asks for some (`week_alternatives`); a backtest never does.
        self.week_alternatives = 0
        self.week_plans = []
        # The plan window keeps the week plan apart from the upgrades (the user, 2026-10-07):
        # given a state, `week_view_on` returns a view of it, and the week is planned on the
        # roster and moves as they were before today's upgrades -- choosing upgrades never
        # changes the week plan. Today's rentals are then made on the roster after the upgrades;
        # one they left no room for (its drop gone, no moves left) is not made and is listed in
        # `week_conflicts` as (rental, why). A backtest never sets it: rentals spend what the
        # upgrades leave.
        self.week_view_on = None
        self.week_conflicts = []

    def before_lock(self, view) -> None:
        """Steps 1-4: everything that changes the roster."""
        m = self.manager
        view.p_start_column = m.p_start_column
        entry = {"day": view.day, "week": view.week, "moves_left_start": view.moves_left}

        z = 0.0
        if self.stream_params.gate:
            moments = view.moments(m.scoreset, view.roster + view.opponent_roster())
            z = m._z(view, moments)
        entry["z"] = z

        forced_before = len(m.ir_log)
        m.manage_ir_step(view)
        entry["ir_forced_drops"] = len(m.ir_log) - forced_before

        repairs = m.repair_roster(view, view.projected_rate)
        m.move_log += repairs
        entry["repairs"] = len(repairs)

        apart = self.week_view_on is not None and self.stream_params.mode == "week"
        before_upgrades = copy.deepcopy(view._state) if apart else None
        upgrades = adddrop.run(view, m.params, m.slot_order, m.accepts, m._fieldable)
        m.move_log += upgrades
        entry["upgrades"] = len(upgrades)

        if self.stream_params.mode == "week":
            rentals, self.week_plans = weekplan.run(
                self.week_view_on(before_upgrades) if apart else view, self.stream_params,
                m.params.horizon_weeks, m.params.rate_source, m.slot_order, m.accepts,
                m._fieldable, z=z, alternatives=self.week_alternatives)
            self.week_plan = self.week_plans[0]["moves"] if self.week_plans else []
            if apart:
                rentals = self._make_after_upgrades(view, rentals)
        else:
            rentals = streaming.run(view, self.stream_params, m.params.horizon_weeks,
                                    m.params.rate_source, m.slot_order, m.accepts, m._fieldable,
                                    z=z)
        m.move_log += rentals
        entry["rentals"] = len(rentals)
        entry["moves_left_end"] = view.moves_left
        self.log.append(entry)

    def _make_after_upgrades(self, view, rentals) -> list:
        """Today's rentals, planned before the upgrades, made on the roster after them (the
        `week_view_on` mode): those that cannot be made are left out and kept in
        `week_conflicts` with the reason."""
        made, self.week_conflicts = [], []
        state = view._state
        team = state.teams[view.team_index]
        for r in rentals:
            if r["outgoing"] is not None and r["outgoing"] not in team.roster:
                self.week_conflicts.append((r, "an upgrade already drops him"))
                continue
            if r["incoming"] not in state.pool:
                self.week_conflicts.append((r, "an upgrade already adds him"))
                continue
            try:
                if r["kind"] == "rental claim":
                    state.submit_claim(view.team_index, r["incoming"], drop=r["outgoing"], today=view.day)
                else:
                    state.add(view.team_index, r["incoming"], view.day, drop=r["outgoing"], reason="rental")
            except Exception as error:             # noqa: BLE001 - state raises IllegalMove
                self.week_conflicts.append((r, "no moves or roster room left after them"
                                            if view.moves_left <= 0 else str(error)))
                continue
            made.append(r)
        return made

    def at_lock(self, view):
        """Step 5: the lineup."""
        return self.manager.lineup_step(view)

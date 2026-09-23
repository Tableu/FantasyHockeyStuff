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
    4. stream      spend what the upgrades leave, this week only   streaming.run
    5. lineup      the z-scored exact solve at the lock            FullSystem.set_lineup
    6. trades      weekly, advisory                                section 9 step 3 (not here)

The order is the design. IR runs before any move is priced, so an activation's forced drop is
settled first; upgrades run before streams, so a rental can only spend moves an upgrade did not
want; and every transaction is done before the lineup solver sees the roster.

`DailyPlan` works against the same view interface as every manager (Decisions/README.md), so the
live runner section 10 needs later builds a real view and calls the same two methods.
"""

import adddrop
import streaming


class DailyPlan:
    """One team's orchestrated day. `manager` supplies the steps; this only sequences them."""

    def __init__(self, manager, stream_params: streaming.StreamParams):
        self.manager = manager
        self.stream_params = stream_params
        self.log = []

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

        upgrades = adddrop.run(view, m.params, m.slot_order, m.accepts, m._fieldable)
        m.move_log += upgrades
        entry["upgrades"] = len(upgrades)

        rentals = streaming.run(view, self.stream_params, m.params.horizon_weeks,
                                m.params.rate_source, m.slot_order, m.accepts, m._fieldable, z=z)
        m.move_log += rentals
        entry["rentals"] = len(rentals)
        entry["moves_left_end"] = view.moves_left
        self.log.append(entry)

    def at_lock(self, view):
        """Step 5: the lineup."""
        return self.manager.lineup_step(view)

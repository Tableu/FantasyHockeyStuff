"""The draft as an event: seat order, the snake, and applying each pick to league state.

Which player a seat takes is a policy decision and lives in `Decisions/draft.py`
(`choose_pick`). This module is the room the draft happens in -- the part a backtest has to run
and a live draft gets from the platform.

Every rung drafts from the same board, so the ladder measures in-season strategy with one variable
moved at a time; seat order rotates across replications so no rung inherits the first pick.
"""

import logging
from collections import Counter

import pandas as pd

from decisionlayer import draft as draft_policy
from decisionlayer import slots as slots_module

log = logging.getLogger("draftroom")


def seat_order(config, replication=0, block=1) -> list:
    """The draft order for one replication: a seeded lottery, as a real league draws one.

    It used to be a rotation by replication, and `build_field` rotates which rung sits in which
    seat by replication too. The two cancelled: the rung at draft position k was
    rungs[(k + 2r) mod n], so with two rungs every replication was the SAME draft (a paired gap of
    +/- 0.0 over eight "rotations") and with four a rung only ever drafted from two position
    patterns. Removing the rotation leaves only n patterns for n rungs. A seeded shuffle makes
    every replication a different draft, reproducibly.

    `block` is the number of rungs. One lottery order is reused for `block` consecutive
    replications while `build_field` cycles the rungs through the seats, so within a block every
    draft position is taken by every rung exactly once -- a pure lottery over eight draws handed one
    of two rungs the first pick five times.
    """
    import random

    order = list(range(config.teams))
    random.Random(1000 + replication // max(block, 1)).shuffle(order)
    return order


def run(state, config, board: pd.Series, eligibility: dict, replication=0,
        boards: dict | None = None, block=1, goalie_caps: dict | None = None) -> None:
    """Draft until every roster is full: snake or linear, as the league's `draft.type` says.

    A single shared board means every team wants the same player, so the snake order is the only
    thing separating the seats -- which is the point: it isolates draft position as the one
    pre-season difference between two clones of the same rung.

    `goalie_caps` ({seat: n}) stops a seat drafting goalies once it holds n: the simulated
    opponents' rule (`Settings/field.json`), never ours.
    """
    order = seat_order(config, replication, block)
    rounds = config.roster_size

    def ranking(series):
        return {p: i for i, p in enumerate(p for p in series.index if p in state.pool)}

    # `boards` overrides the shared board for particular seats (a VOR-drafting twin rung). Each
    # seat walks only its own board, so a seat never picks a player its board does not know.
    default = ranking(board)
    by_seat = {seat: ranking(b) for seat, b in (boards or {}).items()}
    available = list(dict.fromkeys(list(default) + [p for r in by_seat.values() for p in r]))
    forced_picks = 0

    for round_number in range(rounds):
        # A snake reverses every other round; a linear draft keeps the same order each round.
        snake = config.draft["type"] == "snake"
        seats = order if (round_number % 2 == 0 or not snake) else list(reversed(order))
        for seat in seats:
            team = state.teams[seat]
            picks_left = rounds - len(team.roster)

            ranked = by_seat.get(seat, default)
            pool = [p for p in available if p in state.pool and p in ranked]
            cap = (goalie_caps or {}).get(seat)
            if cap is not None and sum("G" in eligibility.get(p, ()) for p in team.roster) >= cap:
                pool = [p for p in pool if "G" not in eligibility.get(p, ())]
            if not pool:
                break

            choice, forced = draft_policy.choose_pick(team.roster, pool, ranked, config,
                                                      eligibility, picks_left)
            forced_picks += forced
            state.draft(seat, choice)

    state.assert_legal()
    if forced_picks:
        log.info("draft: %d of %d picks were forced by positional need",
                 forced_picks, rounds * config.teams)
    log.info("draft complete: %d players over %d rounds, %d free agents remain",
             sum(len(t.roster) for t in state.teams), rounds, len(state.pool))


def verify_rosters_fieldable(state, config, eligibility) -> None:
    """Every team can fill every active slot, ignoring who plays on a given night.

    Cheap, and it catches the failure that would otherwise appear as one rung mysteriously
    leaving slots empty all season.
    """
    order = config.slot_order()
    for team in state.teams:
        values = {p: 1.0 for p in team.roster}
        lineup = slots_module.assign(order, values, eligibility, config.accepts)
        if lineup.unfilled:
            empty = Counter(order[j] for j in lineup.unfilled)
            raise AssertionError(
                f"team {team.team} cannot field a legal lineup: {dict(empty)} unfillable from "
                f"{len(team.roster)} players. The draft's positional-need rule failed.")

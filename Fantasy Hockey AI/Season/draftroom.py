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


def seat_order(config, replication=0) -> list:
    """Which rung sits in which seat, rotated by replication.

    Three clones of each rung, interleaved rather than blocked, so that a rung's three seats do
    not all draft early or all draft late.
    """
    seats = list(range(config.teams))
    shift = replication % config.teams
    return seats[shift:] + seats[:shift]


def run(state, config, board: pd.Series, eligibility: dict, replication=0) -> None:
    """Snake draft until every roster is full.

    A single shared board means every team wants the same player, so the snake order is the only
    thing separating the seats -- which is the point: it isolates draft position as the one
    pre-season difference between two clones of the same rung.
    """
    order = seat_order(config, replication)
    rounds = config.roster_size
    available = [p for p in board.index if p in state.pool]
    ranked = {p: i for i, p in enumerate(available)}
    forced_picks = 0

    for round_number in range(rounds):
        seats = order if round_number % 2 == 0 else list(reversed(order))
        for seat in seats:
            team = state.teams[seat]
            picks_left = rounds - len(team.roster)

            pool = [p for p in available if p in state.pool]
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

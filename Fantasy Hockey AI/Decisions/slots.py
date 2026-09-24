"""Filling tonight's active slots -- an assignment problem, solved exactly.

Given a roster, tonight's slate and a value per player, which players start and in which slots?
With one eligible position per player this is a sort: each slot type can only be filled from its
own pool, so taking that pool's best `n` is optimal and nothing interacts. Real eligibility
breaks that. 21.4% of the rosterable universe is eligible at more than one slot, and once a
player can fill two slots the slots compete for him. This instance is from `verify_optimal`, not
invented -- filling slots in order, each with the best eligible player left, loses 7.7 of 20.5
points, because the LW slot takes the player the RW slot had no replacement for:

    slots            LW      D      RW
    player A  C/LW          7.7          exact   LW=A  D=B  RW=C   20.5
    player B  D             3.4          greedy  LW=C  D=B  RW=--  12.8
    player C  LW/RW         9.4

Note how the failure looks from the outside: not a wrong player in a slot, but an *empty slot*
next to a benched eligible player -- which in this format is the most expensive error available
(section 15). So this is a maximum-weight bipartite matching, and `scipy.optimize.linear_sum_assignment`
solves it exactly in microseconds at this size (at most ~18 players against 12 slots). There is
no reason to approximate it, and `verify_optimal` checks the solver against brute force on small
random instances so the claim is tested rather than asserted.

**A slot is only worth filling by a player who actually plays tonight.** A rostered player whose
team is idle scores zero, so he is not a candidate at all -- which is the whole of section 15's
free half, and the reason `candidates_for` takes the slate rather than the roster.
"""

import logging
from collections import Counter

import numpy as np
from scipy.optimize import linear_sum_assignment

log = logging.getLogger("slots")

# Larger than any plausible single-player value, used to forbid an ineligible pairing. The solver
# must return min(players, slots) pairs, so a forbidden pair can still come back when there are
# more slots than eligible players; `assign` filters those out afterwards rather than trusting
# the cost to prevent them.
FORBIDDEN = 1e6


def fills(slot, eligible, accepts=None) -> bool:
    """Whether a player eligible at `eligible` may occupy `slot`.

    Set intersection, not membership: a slot may accept several positions (`F` takes any forward,
    `F/D` any skater). Passing `accepts=None` treats each slot code as its own single position,
    which is what the brute-force checks in `verify_optimal` use.
    """
    if accepts is None:
        return slot in eligible
    return bool(accepts.get(slot, frozenset({slot})) & eligible)


# Which slots an eligibility set can fill, as a boolean row over a slot order. The slot order and
# the slot-to-positions map are fixed for a league, and there are only a few dozen distinct
# eligibility sets, so each row is computed once instead of `fills` being called for every
# player-slot pair on every solve (110M calls a replication before this cache).
_SLOT_KEYS = {}            # id(accepts) -> (accepts, its content key); the reference pins the id
_ROWS = {}                 # (slots, accepts key, eligible) -> boolean row
_MATCHING = {}             # (slots, accepts key, multiset of eligibility sets) -> matching size


def _accepts_key(accepts):
    if accepts is None:
        return None
    cached = _SLOT_KEYS.get(id(accepts))
    if cached is not None and cached[0] is accepts:
        return cached[1]
    key = tuple(sorted((slot, frozenset(positions)) for slot, positions in accepts.items()))
    _SLOT_KEYS[id(accepts)] = (accepts, key)
    return key


def _row(slots_key, accepts, accepts_key, eligible):
    key = (slots_key, accepts_key, eligible)
    row = _ROWS.get(key)
    if row is None:
        row = np.array([fills(slot, eligible, accepts) for slot in slots_key], dtype=bool)
        _ROWS[key] = row
    return row


class Lineup:
    """Who starts where tonight, and who sat."""

    def __init__(self, assigned: dict, benched: list, unfilled: list):
        self.assigned = assigned          # slot index -> player_id
        self.benched = benched            # rostered, playing tonight, not started
        self.unfilled = unfilled          # slot indices left empty

    @property
    def started(self) -> list:
        return list(self.assigned.values())

    @property
    def filled(self) -> int:
        return len(self.assigned)

    def __repr__(self):
        return (f"Lineup({self.filled} filled, {len(self.unfilled)} empty, "
                f"{len(self.benched)} benched)")


def assign(slots: list, values: dict, eligibility: dict, accepts=None) -> Lineup:
    """Maximum-weight legal assignment of players to slots.

    `slots` is a flat list of slot codes (`league.LeagueConfig.slot_order()`), `values` maps
    player_id to tonight's value, and `eligibility` maps player_id to a frozenset of slot codes.
    Only players in `values` are considered -- filtering to who plays tonight happens upstream.
    """
    players = list(values)
    if not players or not slots:
        return Lineup({}, list(players), list(range(len(slots))))

    # The same matrix the per-pair loop built -- -value where the player may fill the slot,
    # FORBIDDEN elsewhere -- assembled from cached eligibility rows.
    slots_key, accepts_key = tuple(slots), _accepts_key(accepts)
    mask = np.array([_row(slots_key, accepts, accepts_key, eligibility.get(p, frozenset()))
                     for p in players])
    value = np.array([float(values[p]) for p in players], dtype="float64")
    cost = np.where(mask, -value[:, None], FORBIDDEN)

    rows, columns = linear_sum_assignment(cost)

    assigned, started = {}, set()
    for i, j in zip(rows, columns):
        # A forbidden pair can survive the solve when slots outnumber eligible players; it is not
        # a legal lineup, so it is dropped rather than started.
        if cost[i, j] < FORBIDDEN:
            assigned[int(j)] = players[i]
            started.add(players[i])

    return Lineup(assigned=assigned,
                  benched=[p for p in players if p not in started],
                  unfilled=[j for j in range(len(slots)) if j not in assigned])


def candidates_for(roster, playing_tonight: set, available: set) -> list:
    """The rostered players who can actually contribute tonight.

    Three conditions, and all three are the harness's job rather than a manager's: he is on the
    active roster, his team plays tonight, and he is not in an injury spell. A manager may still
    choose to bench him, but a player failing any of these cannot be started at all.
    """
    return [p for p in roster if p in playing_tonight and p in available]


def is_legal(lineup: Lineup, slots: list, eligibility: dict, accepts=None) -> bool:
    """Every started player is eligible for the slot he occupies, and nobody starts twice."""
    seen = set()
    for slot_index, player_id in lineup.assigned.items():
        if not fills(slots[slot_index], eligibility.get(player_id, frozenset()), accepts):
            return False
        if player_id in seen:
            return False
        seen.add(player_id)
    return True


def total_value(lineup: Lineup, values: dict) -> float:
    return float(sum(values[p] for p in lineup.assigned.values()))


def greedy(slots: list, values: dict, eligibility: dict, accepts=None) -> Lineup:
    """The obvious wrong answer, kept so the cost of getting this wrong stays measurable.

    Fills slots in order, each with the best eligible player left. Used only by
    `verify_optimal` to report how much the exact solve is worth.
    """
    remaining = dict(values)
    assigned = {}
    for j, slot in enumerate(slots):
        best, best_value = None, None
        for player_id, value in remaining.items():
            if fills(slot, eligibility.get(player_id, frozenset()), accepts):
                if best_value is None or value > best_value:
                    best, best_value = player_id, value
        if best is not None:
            assigned[j] = best
            del remaining[best]
    return Lineup(assigned, list(remaining), [j for j in range(len(slots)) if j not in assigned])


def matching_size(players, slots: list, eligibility: dict, accepts=None) -> int:
    """How many active slots this set of players can fill at once.

    Used by the draft: with composite slots, "how many centres do I still need" is not well
    defined, because a centre can fill C, F or F/D. The only sound question is how large a legal
    lineup the roster admits, which is exactly the size of the maximum matching.
    """
    # A matching's size depends only on the multiset of eligibility sets, so it is cached on that.
    key = (tuple(slots), _accepts_key(accepts),
           frozenset(Counter(eligibility.get(p, frozenset()) for p in players).items()))
    size = _MATCHING.get(key)
    if size is None:
        size = assign(slots, {p: 1.0 for p in players}, eligibility, accepts).filled
        _MATCHING[key] = size
    return size


def verify_optimal(slot_positions: dict, trials=400, seed=17) -> dict:
    """Check the solver against brute force, and price the greedy alternative.

    `slot_positions` maps each slot code to the positions it accepts -- the league's
    `SLOT_POSITIONS`, passed in because this folder reads no league config of its own.

    Small random instances only -- brute force is factorial -- but the property being checked
    (the matching is maximum weight) does not depend on size, and a bug here silently costs every
    manager in the field points in a way no season-level number would attribute correctly.
    """
    from itertools import permutations

    rng = np.random.default_rng(seed)
    # Composite slots included on purpose: they are where greedy fails hardest and where an
    # off-by-one in the eligibility test would otherwise go unnoticed.
    all_slots = ["C", "LW", "RW", "D", "G", "F", "F/D"]
    combos = [frozenset({"C"}), frozenset({"LW"}), frozenset({"RW"}), frozenset({"D"}),
              frozenset({"G"}), frozenset({"LW", "RW"}), frozenset({"C", "LW"}),
              frozenset({"C", "RW"}), frozenset({"C", "LW", "RW"})]
    mismatches, greedy_losses, greedy_loss_total = 0, 0, 0.0

    for _ in range(trials):
        n_slots = int(rng.integers(2, 6))
        n_players = int(rng.integers(1, 7))
        slots = list(rng.choice(all_slots, size=n_slots))
        accepts = {s: slot_positions[s] for s in set(slots)}
        eligibility = {i: combos[int(rng.integers(0, len(combos)))] for i in range(n_players)}
        values = {i: float(round(rng.uniform(0, 10), 2)) for i in range(n_players)}

        solved = assign(slots, values, eligibility, accepts)
        if not is_legal(solved, slots, eligibility, accepts):
            raise AssertionError(f"illegal assignment on {slots} / {eligibility}")

        # Brute force: every way of seating a subset of players into the slots.
        best = 0.0
        players = list(values)
        for size in range(min(len(players), len(slots)) + 1):
            for chosen in permutations(players, size):
                for seats in permutations(range(len(slots)), size):
                    if all(fills(slots[seat], eligibility[p], accepts)
                           for p, seat in zip(chosen, seats)):
                        best = max(best, sum(values[p] for p in chosen))
        if abs(total_value(solved, values) - best) > 1e-9:
            mismatches += 1

        loss = best - total_value(greedy(slots, values, eligibility, accepts), values)
        if loss > 1e-9:
            greedy_losses += 1
            greedy_loss_total += loss

    if mismatches:
        raise AssertionError(f"solver was sub-optimal on {mismatches} of {trials} instances")
    return {
        "trials": trials,
        "solver_optimal": True,
        "greedy_suboptimal_instances": greedy_losses,
        "greedy_mean_loss_when_wrong": (greedy_loss_total / greedy_losses) if greedy_losses else 0.0,
    }

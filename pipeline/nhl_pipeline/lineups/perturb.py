"""Structured noise for an actual opening lineup, so it can stand in for the pre-game line
chart the live pipeline will see (docs/fantasy-ai/data-sources.md, "noise injection").

A pre-game chart is wrong in specific ways, not uniformly at random: a late scratch whose
slot goes to the healthy extra, a winger swapped onto the next line, a flipped D pair, a
PP2 player promoted, a surprise goalie start. Each is applied here as its own edit at its
own rate, with everything downstream re-derived (a unit short of bodies stays short, exactly
as an 11-forward night does). Invariants checked on every call: the skater count is
unchanged unless no extra was available, nobody appears twice, and no injured player is ever
dressed. `healthy_extras` must be built from lockout-knowable information only -- players
who dressed in recent games and aren't in an injury spell -- never from a later game.

Rates come from calibration.perturb_rates() (game-to-game churn, an over-estimate of chart
error) until the live snapshot job can measure the real thing; see that module.
"""

import random

from nhl_pipeline.lineups.store import PlayerLineup, TeamGame

# The goalie rate is a judgment call, not a measurement: consecutive-game starter churn
# (~60%) is rotation, not chart error. A pre-game chart names the wrong starter far less
# often than that, so calibration.perturb_rates() leaves this at a conservative default.
DEFAULT_GOALIE_SWITCH_RATE = 0.10


def _poisson(rng: random.Random, lam: float) -> int:
    n, p, threshold = 0, 1.0, pow(2.718281828459045, -lam)
    while True:
        p *= rng.random()
        if p <= threshold:
            return n
        n += 1


def _unit_map(players: dict, attr: str) -> dict:
    """{rank: [player ids]} for the given unit attribute."""
    units: dict = {}
    for p, pl in players.items():
        rank = getattr(pl, attr)
        if rank is not None:
            units.setdefault(rank, []).append(p)
    return units


def _swap_units(players: dict, attr: str, a: int, b: int) -> None:
    ra, rb = getattr(players[a], attr), getattr(players[b], attr)
    setattr(players[a], attr, rb)
    setattr(players[b], attr, ra)


def _scratch(players: dict, victim: int, extras: dict, rng: random.Random) -> bool:
    """Remove `victim` from the lineup; a same-position-group extra (if any) takes the lowest
    unit slot and one player from that slot moves up into the vacated one -- the usual
    "everyone bumps" reshuffle collapsed to a single promotion. Returns whether the slot
    could be filled."""
    pl = players.pop(victim)
    if pl.is_forward:
        attr, same_group = "line", lambda pos: pos in ("C", "L", "R")
    elif pl.is_defence:
        attr, same_group = "pair", lambda pos: pos == "D"
    else:
        attr, same_group = None, lambda pos: False
    candidates = [p for p, pos in extras.items() if p not in players and same_group(pos)]

    # The vacated special-teams slot goes to a random unit-less skater of the same group.
    for st_attr in ("pp", "pk"):
        rank = getattr(pl, st_attr)
        if rank is not None:
            pool = [p for p, q in players.items() if not q.is_goalie and getattr(q, st_attr) is None]
            if pool:
                setattr(players[rng.choice(pool)], st_attr, rank)

    if attr is None or not candidates:
        return False
    newcomer = rng.choice(candidates)
    units = _unit_map(players, attr)
    lowest = max(units) if units else None
    vacated = getattr(pl, attr)
    players[newcomer] = PlayerLineup(extras[newcomer], True, None, None, None, None, False)
    if vacated is None:
        return True
    if lowest is None or vacated >= lowest:
        setattr(players[newcomer], attr, vacated)
    else:
        promoted = rng.choice(units[lowest])
        setattr(players[promoted], attr, vacated)
        setattr(players[newcomer], attr, lowest)
    return True


def perturb(team_game: TeamGame, healthy_extras: dict, rates: dict, rng: random.Random) -> TeamGame:
    """healthy_extras: {PlayerID: PositionCode} of lockout-knowable, uninjured players not in
    this lineup. rates: calibration.perturb_rates() shape. Returns a new TeamGame."""
    players = {p: pl.copy() for p, pl in team_game.players.items()}
    out = TeamGame(team_game.game_id, team_game.nhl_game_id, team_game.game_date, team_game.season_id,
                   team_game.team_id, players, team_game.had_pp, team_game.had_pk)
    extras = dict(healthy_extras)
    original_skaters = sum(1 for pl in players.values() if pl.dressed and not pl.is_goalie)

    # 1. Late scratches.
    unfilled = 0
    for p in [p for p, pl in players.items() if pl.dressed and not pl.is_goalie]:
        if rng.random() < rates.get("scratch_per_skater", 0):
            unfilled += not _scratch(players, p, extras, rng)

    # 2. Forward swaps between lines (adjacent most often), D partner swaps between pairs.
    for attr, key, moves_key in (("line", "forward_swaps_per_game", "forward_line_move_distribution"),
                                 ("pair", "defence_swaps_per_game", None)):
        for _ in range(_poisson(rng, rates.get(key, 0))):
            units = _unit_map(players, attr)
            if len(units) < 2:
                break
            a_rank = rng.choice(sorted(units))
            distribution = rates.get(moves_key) or {"1": 1.0}
            steps = [int(k) for k in distribution]
            delta = rng.choices(steps, weights=[distribution[str(s)] for s in steps])[0]
            b_rank = a_rank + rng.choice((-delta, delta))
            if b_rank not in units:
                b_rank = a_rank + 1 if a_rank + 1 in units else a_rank - 1
            if b_rank not in units:
                continue
            _swap_units(players, attr, rng.choice(units[a_rank]), rng.choice(units[b_rank]))

    # 3. Whole-unit reorders (the rank noise a chart also carries).
    for attr, key in (("line", "forward_unit_reorders_per_game"), ("pair", "defence_unit_reorders_per_game")):
        for _ in range(_poisson(rng, rates.get(key, 0))):
            units = _unit_map(players, attr)
            ranks = sorted(units)
            if len(ranks) < 2:
                break
            i = rng.randrange(len(ranks) - 1)
            a, b = ranks[i], ranks[i + 1]
            for p in units[a]:
                setattr(players[p], attr, b)
            for p in units[b]:
                setattr(players[p], attr, a)

    # 4. Special teams: a unit-2 player promoted over a unit-1 player (they swap).
    for attr, key in (("pp", "pp_promotions_per_game"), ("pk", "pk_promotions_per_game")):
        for _ in range(_poisson(rng, rates.get(key, 0))):
            units = _unit_map(players, attr)
            if 1 in units and 2 in units:
                _swap_units(players, attr, rng.choice(units[1]), rng.choice(units[2]))

    # 4b. Special teams: a unit member replaced by a unit-less skater of the same group.
    for attr, key in (("pp", "pp_substitutions_per_game"), ("pk", "pk_substitutions_per_game")):
        for _ in range(_poisson(rng, rates.get(key, 0))):
            on_unit = [p for p, pl in players.items() if getattr(pl, attr) is not None]
            if not on_unit:
                break
            out_player = rng.choice(on_unit)
            same_group = players[out_player].is_forward
            bench = [p for p, pl in players.items() if not pl.is_goalie and getattr(pl, attr) is None and pl.is_forward == same_group]
            if bench:
                _swap_units(players, attr, out_player, rng.choice(bench))

    # 5. Surprise goalie start.
    goalies = [p for p, pl in players.items() if pl.is_goalie and pl.dressed]
    if len(goalies) == 2 and rng.random() < rates.get("goalie_switch", DEFAULT_GOALIE_SWITCH_RATE):
        for p in goalies:
            players[p].starting_goalie = not players[p].starting_goalie

    skaters_now = sum(1 for pl in players.values() if pl.dressed and not pl.is_goalie)
    assert skaters_now == original_skaters - unfilled, "skater count drifted"
    assert set(players) <= set(team_game.players) | set(healthy_extras), "player from outside the lineup or the extras pool"
    return out

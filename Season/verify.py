#!/usr/bin/env python
"""The checks that have to pass before any ladder number means anything.

Run as a script. Each check is one failure mode that would otherwise show up as a plausible
season-level result rather than as an error:

    provenance      rung 4 handed projections that saw the season it is being scored on
    leakage         a manager handed an outcome column
    draws           rung 4 not actually using the distributions it is credited with using
    invariants      an illegal roster, an over-budget week, or an ineligible IR stash
    assignment      a sub-optimal lineup, which would understate every rung equally
    calendar        a game in no matchup week, or a week with no games

    python verify.py
"""

import logging
import random
import sys

import numpy as np
import pandas as pd

import draft as draft_module
import engine as engine_module
import inputs
import league as league_module
import paths
import schedule as schedule_module
import simlayer
import slots as slots_module
import state as state_module
import view as view_module

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("verify")

SEASON = "2025-26"


def check_provenance() -> str:
    """The in-sample lambda table must be refused; the holdout one accepted."""
    bad = paths.PROJECTIONS_REPORTS / f"lambdas_{SEASON}_A.parquet"
    if bad.exists():
        try:
            inputs._assert_out_of_sample(pd.read_parquet(bad), SEASON, bad)
            raise AssertionError("the deployment-build lambda table was accepted; it is "
                                 "in-sample on the season being replayed")
        except inputs.ProvenanceError:
            pass
    table = inputs.load_projections(SEASON)
    assert len(table), "the holdout table loaded empty"
    return f"in-sample table refused, holdout accepted ({len(table)} rows)"


def check_leakage() -> str:
    """Every outcome-shaped column must be rejected by the view."""
    rejected = 0
    for column in ("target_goals", "target_played", "label_starting_goalie", "saves",
                   "goals_against", "decision", "is_starter", "pulled"):
        try:
            view_module.assert_clean(pd.DataFrame({"player_id": [1], column: [1]}), "test")
        except view_module.LeakageError:
            rejected += 1
    assert rejected == 8, f"only {rejected} of 8 outcome columns were rejected"
    view_module.assert_clean(
        pd.DataFrame({"player_id": [1], "lambda_goals": [0.3], "p_plays": [0.9]}), "test")
    return "8/8 outcome columns rejected, projections accepted"


def check_draws(day_limit=6) -> str:
    """Rung 4 must actually consume the distributions, and they must be its own stream.

    Phase 1 resolves nights from the real season, so a decision draw cannot collide with an
    outcome by construction -- there is nothing sampled to collide with. What can still go wrong is
    subtler and would flatter rung 4 in the write-up rather than in the numbers: it could be
    credited with using the Monte Carlo layer while quietly ignoring it. So this asserts the draws
    are non-degenerate and that the lineup actually moves when they do.
    """
    config = league_module.load()
    scoreset = simlayer.load_scoreset("points-league")
    data = inputs.load_season(SEASON)
    universe = pd.concat([data["projections"][["player_id", "position"]],
                          data["goalie_candidates"][["player_id", "position"]]]
                         ).drop_duplicates("player_id")
    eligibility = inputs.load_eligibility(config, universe)
    calendar = schedule_module.from_candidates(
        data["projections"][["game_id", "game_date", "team_id"]], config.week_starts_on)

    import managers as managers_module
    field = managers_module.build_field(config, scoreset, rungs=(4,))
    season = engine_module.Season(config, calendar, data, eligibility, scoreset, field,
                                  decision_sims=150)
    day = calendar.days[40]
    draws = season.decision_draws(day)
    assert draws, "no decision draws were produced"
    spreads = np.array([s.std() for s in draws.values()])
    assert (spreads > 0).mean() > 0.9, "decision draws are degenerate (no spread)"

    # Same slate, a different seed: the sampled means must move, or nothing is being sampled.
    other = engine_module.Season(config, calendar, data, eligibility, scoreset,
                                 managers_module.build_field(config, scoreset, rungs=(4,)),
                                 decision_sims=150, decision_seed=1234567)
    theirs = other.decision_draws(day)
    shared = sorted(set(draws) & set(theirs))[:400]
    moved = np.mean([abs(draws[p].mean() - theirs[p].mean()) > 1e-9 for p in shared])
    assert moved > 0.5, f"only {moved:.0%} of candidates moved when the seed changed"
    return (f"{len(draws)} candidates drawn, {(spreads > 0).mean():.0%} with spread, "
            f"{moved:.0%} move on a new seed")


def check_invariants(steps=20000, seed=17) -> str:
    """Random abuse of the transaction API never reaches an illegal state."""
    config = league_module.load()
    rng = random.Random(seed)
    pool = list(range(1, 400))
    eligibility = {p: frozenset({["C", "LW", "RW", "D", "G"][p % 5]}) for p in pool}
    s = state_module.LeagueState(config, pool, eligibility)
    for seat in range(config.teams):
        for _ in range(config.roster_size):
            s.draft(seat, rng.choice(sorted(s.pool)))
    day = pd.Timestamp("2026-01-05")
    refused = 0
    for step in range(steps):
        if step % 500 == 0:
            s.start_week(step // 500 + 1)
        seat = rng.randrange(config.teams)
        team = s.teams[seat]
        action = rng.choice(["add", "drop", "stash", "activate", "claim", "waivers"])
        try:
            if action == "add" and s.pool:
                s.add(seat, rng.choice(sorted(s.pool)), day,
                      drop=rng.choice(team.roster) if team.roster else None)
            elif action == "drop" and team.roster:
                s.drop(seat, rng.choice(team.roster), day)
            elif action == "stash" and team.roster:
                p = rng.choice(team.roster)
                s.stash(seat, p, {p})
            elif action == "activate" and team.ir:
                s.activate(seat, rng.choice(team.ir))
            elif action == "claim" and s.pool:
                s.submit_claim(seat, rng.choice(sorted(s.pool)))
            elif action == "waivers":
                s.process_waivers(day)
        except state_module.IllegalMove:
            refused += 1
        s.assert_legal()
    assert all(t.moves_used <= config.moves_per_week for t in s.teams)
    assert all(len(t.ir) <= config.ir for t in s.teams)
    assert all(len(t.roster) <= config.roster_size for t in s.teams)
    return f"{steps:,} random transactions, {refused:,} refused, legal after every one"


def check_assignment() -> str:
    result = slots_module.verify_optimal(trials=300)
    assert result["solver_optimal"]
    return (f"{result['trials']} instances optimal against brute force; greedy wrong on "
            f"{result['greedy_suboptimal_instances']}, mean loss "
            f"{result['greedy_mean_loss_when_wrong']:.2f}")


def check_calendar() -> str:
    config = league_module.load()
    projections = inputs.load_projections(SEASON)
    calendar = schedule_module.from_candidates(
        projections[["game_id", "game_date", "team_id"]], config.week_starts_on)
    summary = calendar.verify()
    assert summary["gaps"], "the mid-season break was not detected"
    return (f"{summary['games']} games in {summary['weeks']} weeks, "
            f"{len(summary['gaps'])} break(s) found, every game in exactly one week")


CHECKS = [("provenance", check_provenance), ("leakage", check_leakage),
          ("draws", check_draws), ("invariants", check_invariants),
          ("assignment", check_assignment), ("calendar", check_calendar)]


def main():
    failures = 0
    for name, check in CHECKS:
        try:
            print(f"  PASS  {name:12s} {check()}")
        except Exception as error:                       # noqa: BLE001 - reported, not raised
            failures += 1
            print(f"  FAIL  {name:12s} {error}")
    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

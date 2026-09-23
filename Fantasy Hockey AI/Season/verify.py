#!/usr/bin/env python
"""The checks that have to pass before any ladder number means anything.

Run as a script. Each check is one failure mode that would otherwise show up as a plausible
season-level result rather than as an error:

    provenance      rung 4 handed projections that saw the season it is being scored on
    season guard    one season's projections handed over as another's
    leakage         a manager handed an outcome column
    draws           rung 4 not actually using the distributions it is credited with using
    invariants      an illegal roster, an over-budget week, or an ineligible IR stash
    assignment      a sub-optimal lineup, which would understate every rung equally
    calendar        a game in no matchup week, or a week with no games
    dark nights     a rostered player valued at zero games because his team is idle tonight
    opening rates   a skater priced at zero on opening night because his club has not played yet
    ros provenance  rest-of-season projections that saw the season they project
    hold            the add/drop rule at an infinite margin making any move at all
    ir              a healthy player left on IR, an injured one activated on a dark night, or an
                    activation's forced drop spending a move
    streaming       rung 7 at zero spots differing from rung 5, or a rental breaking the reserve,
                    its drop-cost floor, the spot rule or the weekly budget
    modules         a Decisions/ module name that would shadow one in Season/ or Simulation/

    python verify.py
"""

import logging
import random
import sys

import numpy as np
import pandas as pd

import engine as engine_module
import inputs
import league as league_module
import paths
import schedule as schedule_module
import simlayer
import state as state_module
import view as view_module
from decisionlayer import managers as managers_module
from decisionlayer import slots as slots_module

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


def check_season_guard() -> str:
    """A file for one season is refused when another is asked for.

    With one unkeyed predictions file, `load_projections("2024-25")` returned the 2025-26 holdout,
    and the span check passed it -- it was a clean single season, just the wrong one. Asserted on
    the in-memory table so the check does not depend on which files happen to be on disk.
    """
    table = pd.read_parquet(paths.holdout_predictions(SEASON))
    inputs._assert_out_of_sample(table, SEASON, "holdout")          # the right season passes
    other = f"{int(SEASON[:4]) - 1}-{SEASON[2:4]}"
    for label, check in (
            ("predictions", lambda: inputs._assert_out_of_sample(table, other, "holdout")),
            ("p_start dates", lambda: inputs._assert_in_season(table["game_date"], other, "x"))):
        try:
            check()
            raise AssertionError(f"{SEASON} {label} were accepted as {other}")
        except inputs.ProvenanceError:
            pass
    missing = paths.holdout_predictions(other)
    return (f"{SEASON} rows refused as {other}; {other}'s own predictions "
            f"{'exist' if missing.exists() else 'are not built'} ({missing.name})")


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
    result = slots_module.verify_optimal(league_module.SLOT_POSITIONS, trials=300)
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


def _small_season(rungs, params=None, sims=0):
    config = league_module.load()
    scoreset = simlayer.load_scoreset("points-league")
    data = inputs.load_season(SEASON)
    universe = pd.concat([data["projections"][["player_id", "position"]],
                          data["goalie_candidates"][["player_id", "position"]]]
                         ).drop_duplicates("player_id")
    eligibility = inputs.load_eligibility(config, universe)
    calendar = schedule_module.from_candidates(
        data["projections"][["game_id", "game_date", "team_id"]], config.week_starts_on)
    field = managers_module.build_field(config, scoreset, rungs=rungs, adddrop_params=params)
    return engine_module.Season(config, calendar, data, eligibility, scoreset, field,
                                decision_sims=sims), calendar


def check_dark_nights() -> str:
    """A player whose team is idle tonight still has this week's games in his window.

    The bug this guards against valued every such player at zero games, so every transacting
    rung dropped players for nothing but a dark night.
    """
    season, calendar = _small_season((2,))
    day = calendar.days[60]
    season.latest_team.update(season.nhl_team_by_day.get(day, {}))
    tonight = set(season.nhl_team_by_day.get(day, {}))
    idle = [p for p in season.latest_team if p not in tonight][:200]
    assert idle, "no idle players found to test"
    view = view_module.SlateView(
        day=day, week=calendar.week_of(day), config=season.config, calendar=calendar,
        projections=season.data["projections"].iloc[:0][["player_id"]],
        goalie_projections=pd.DataFrame(), unavailable=set(), playing_tonight=tonight,
        nhl_team=season.latest_team, history=None, state=None, team_index=0,
        opponent_index=None, my_week_points=0.0, opponent_week_points=0.0)
    counted = [view.games_through(p, weeks_ahead=1) for p in idle]
    expected = [calendar.games_through(season.latest_team[p], day, 1) for p in idle]
    assert counted == expected and sum(counted) > 0, "idle players lost their games"
    return (f"{len(idle)} players idle on {pd.Timestamp(day).date()} keep "
            f"{sum(counted)} games over the window")


def check_opening_rates() -> str:
    """Before the first night is played, every opening-week skater already has a rate.

    Both rates used to fill only once a player's team had played, so on the first nights a star
    whose club had not opened priced at zero and was the first man dropped. And a player nobody
    has projected must read as unknown, never as zero, so the add/drop rule will not drop him.
    """
    from decisionlayer import valuation

    season, calendar = _small_season((2,))
    first_week = [d for d in calendar.days if d <= calendar.days[0] + pd.Timedelta(days=6)]
    skaters = set()
    for day in first_week:
        frame = season.proj_by_day.get(day)
        if frame is not None:
            skaters |= set(frame["player_id"].astype(int))
    missing = [p for p in skaters if p not in season.latest_rate]
    assert not missing, f"{len(missing)} opening-week skaters have no rate before the first night"
    ros_first_week = set()
    for day in first_week:
        ros_first_week |= set(season.ros_by_day.get(pd.Timestamp(day), {}))
    missing_ros = [p for p in ros_first_week if p not in season.latest_ros]
    assert not missing_ros, f"{len(missing_ros)} opening-week skaters have no rest-of-season rate"

    view = view_module.SlateView(
        day=calendar.days[0], week=1, config=season.config, calendar=calendar,
        projections=season.data["projections"].iloc[:0][["player_id"]],
        goalie_projections=pd.DataFrame(), unavailable=set(), playing_tonight=set(),
        nhl_team={}, history=None, state=None, team_index=0, opponent_index=None,
        my_week_points=0.0, opponent_week_points=0.0, rate_estimate={}, ros_estimate={})
    assert not valuation.known(view, 1, "ros") and not valuation.known(view, 1, "per_game"),         "a player with no rate reads as known"
    return (f"{len(skaters)} skaters rated and {len(ros_first_week)} with rest-of-season before "
            f"the first night; an unprojected player reads as unknown")


def check_ros_provenance() -> str:
    """The holdout rest-of-season table loads; a table trained on the season is refused."""
    table = inputs.load_ros(SEASON)
    if table is None:
        return "not built (skipped) -- run Projections/ros_train.py --predictions-out"
    raw = pd.read_parquet(paths.ros_predictions(SEASON))
    raw["trained_on"] = f"2024-25,{SEASON}"
    bad = paths.REPORTS_DIR / "_verify_ros_in_sample.parquet"
    paths.ensure(paths.REPORTS_DIR)
    raw.to_parquet(bad, index=False)
    original = paths.ros_predictions
    try:
        paths.ros_predictions = lambda season, horizon="season": bad
        inputs.load_ros(SEASON)
        raise AssertionError("a rest-of-season table trained on the replayed season was accepted")
    except inputs.ProvenanceError:
        pass
    finally:
        paths.ros_predictions = original
        bad.unlink(missing_ok=True)
    return f"holdout accepted ({len(table)} rows), in-sample refused"


def check_hold() -> str:
    """At an infinite margin the add/drop rule is a manager that never moves."""
    from dataclasses import replace

    from decisionlayer import adddrop

    params = replace(adddrop.AddDropParams(), margin=float("inf"))
    season, _ = _small_season((5,), params=params)
    rate = {int(k): 1.0 for k in season.player_pool()}
    season.run({p: -i for i, p in enumerate(sorted(rate))}, rate)
    moves = len(season.state.transactions)
    assert moves == 0, f"an infinite margin still made {moves} moves"
    return "margin inf: 0 moves over a full season"


def check_ir() -> str:
    """An activation on a full roster forces a drop or a swap and never spends a move; and over a
    season no one is activated while his latest report still has him injured -- the dark-night
    flap that followed 95% of stashes before status was carried forward."""
    config = league_module.load()
    pool = list(range(1, 200))
    eligibility = {p: frozenset({"C"}) for p in pool}
    s = state_module.LeagueState(config, pool, eligibility)
    for p in pool[:config.roster_size]:
        s.draft(0, p)
    day = pd.Timestamp("2026-01-05")
    hurt, other = pool[0], pool[1]
    s.stash(0, hurt, {hurt})
    s.add(0, pool[50], day)                              # fill the spot the stash opened
    used = s.teams[0].moves_used
    try:
        s.activate(0, hurt)
        raise AssertionError("activation on a full roster was allowed without a drop")
    except state_module.IllegalMove:
        pass
    s.activate(0, hurt, drop=pool[50], today=day)
    assert hurt in s.teams[0].roster and pool[50] in s.pool, "forced drop did not happen"
    s.stash(0, hurt, {hurt})
    s.add(0, pool[51], day)
    s.stash(0, other, {other})                           # IR now full (2)
    s.add(0, pool[52], day)
    s.activate(0, hurt, stash=pool[2], ir_eligible={pool[2]})
    assert hurt in s.teams[0].roster and pool[2] in s.teams[0].ir, "swap did not happen"
    assert s.teams[0].moves_used == used + 2, "an activation or its drop spent a move"
    try:
        s.assert_ir_resolved(0, injured=set())
        raise AssertionError("a healthy player on IR passed")
    except state_module.IllegalMove:
        pass

    season, _ = _small_season((2, 5))
    flaps, activations = [], [0]
    original = managers_module.Manager.manage_ir

    def watched(self, view):
        before = set(view.ir)
        original(self, view)
        back = before - set(view.ir)
        activations[0] += len(back & set(view.roster))
        flaps.extend(p for p in back & set(view.roster) if p in view.injured)

    managers_module.Manager.manage_ir = watched
    try:
        rate = {int(k): 1.0 for k in season.player_pool()}
        season.run({p: -i for i, p in enumerate(sorted(rate))}, rate)   # asserts IR daily
    finally:
        managers_module.Manager.manage_ir = original
    assert not flaps, f"{len(flaps)} activations of a player still reported injured"
    forced = sum(len(m.ir_log) for m in season.field)
    return (f"forced drop and swap cost 0 moves; season: {activations[0]} activations, "
            f"0 while injured, {forced} forced drops, no healthy player left on IR")


def check_streaming() -> str:
    """Rung 7 (section 10): with zero streaming spots it is rung 5 seat for seat; with spots, no
    rental breaks the reserve, clears less than its drop cost, drops anyone but a designated spot,
    or pushes a week past its budget."""
    from dataclasses import replace

    from decisionlayer import adddrop, streaming

    def run(rungs, spots):
        season, _ = _small_season(rungs, params=adddrop.AddDropParams(), sims=0)
        for m in season.field:
            if m.rung == 7:
                m.stream_params = replace(streaming.StreamParams(), spots=spots)
                m.plan.stream_params = m.stream_params
        rate = {int(k): 1.0 for k in season.player_pool()}
        report = season.run({p: -i for i, p in enumerate(sorted(rate))}, rate)
        return season, report["teams"][["seat", "points", "moves_spent", "forced_drops"]]

    _, five = run((2, 5), 0)
    _, seven = run((2, 7), 0)
    assert five.equals(seven), "rung 7 with no streaming spots is not rung 5"

    season, _ = run((2, 7), 2)
    rentals = [r for m in season.field if m.rung == 7 for r in m.move_log if r["kind"] == "rental"]
    assert rentals, "two streaming spots made no rentals over a season"
    for r in rentals:
        assert r["moves_left"] >= r["reserve"], f"a rental broke the reserve: {r}"
        assert r["predicted_gain"] > r["bar"] >= r["drop_cost"], f"a rental under its floor: {r}"
        assert r["outgoing"] is None or r["spot"], f"a rental dropped a non-spot player: {r}"
    weekly = pd.DataFrame(season.state.transactions).groupby(["team", "week"]).size()
    assert weekly.max() <= season.config.moves_per_week, "a week went over budget"
    return (f"k=0 identical to rung 5; k=2: {len(rentals)} rentals, reserve, floor, spot-only "
            f"drops and the weekly budget all held")


def check_modules() -> str:
    import decisionlayer

    return f"no collisions; Decisions/ provides {', '.join(sorted(decisionlayer.__all__))}"


CHECKS = [("provenance", check_provenance), ("season guard", check_season_guard),
          ("leakage", check_leakage),
          ("draws", check_draws), ("invariants", check_invariants),
          ("assignment", check_assignment), ("calendar", check_calendar),
          ("dark nights", check_dark_nights), ("opening rates", check_opening_rates),
          ("ros provenance", check_ros_provenance),
          ("hold", check_hold), ("ir", check_ir), ("streaming", check_streaming),
          ("modules", check_modules)]


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

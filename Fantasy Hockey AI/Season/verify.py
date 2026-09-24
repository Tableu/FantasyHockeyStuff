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
    frozen rosters  a transacting team left short of its slots with a fix available (a goalie on IR)
    vor board       a draft board that read past draft day, or a VOR draft that leaves a roster short
    draft lottery   replications that repeat one draft, or give a rung more early picks
    claims          a waiver claim resolved early, awarded out of priority, or failing silently
    league rules    an unsupported league rule accepted, or a move cost charged wrongly
    no clobber      a scored projection build overwriting the deployment boosters in models/
    modules         a Decisions/ module name that would shadow one in Season/ or Simulation/

    python verify.py
"""

import logging
from pathlib import Path
import random
import sys

import numpy as np
import pandas as pd

import decisionlayer
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


def _strategy():
    """The committed strategy file -- what every ladder run reads unless told otherwise."""
    return decisionlayer.load_strategy()


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

    field = managers_module.build_field(config, scoreset, _strategy(), rungs=(4,))
    season = engine_module.Season(config, calendar, data, eligibility, scoreset, field,
                                  decision_sims=150)
    day = calendar.days[40]
    draws = season.decision_draws(day)
    assert draws, "no decision draws were produced"
    # Spread is only owed by candidates who might play. Since the variant-A holdout was rebuilt,
    # P(plays) is ~0 for the ~12% flagged injured at the lockout (who by construction did not
    # play), and a certain scratch rightly draws all zeros.
    frame = season.proj_by_day[day]
    live = set(frame.loc[frame["p_plays"] >= 0.01, "player_id"].astype(int))
    spreads = np.array([s.std() for p, s in draws.items() if p in live])
    assert len(spreads) and (spreads > 0).mean() > 0.9, "decision draws are degenerate (no spread)"

    # Same slate, a different seed: the sampled means must move, or nothing is being sampled.
    other = engine_module.Season(config, calendar, data, eligibility, scoreset,
                                 managers_module.build_field(config, scoreset, _strategy(), rungs=(4,)),
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
    result = slots_module.verify_optimal(league_module.load().accepts, trials=300)
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


def _small_season(rungs, strategy=None, sims=0):
    config = league_module.load()
    scoreset = simlayer.load_scoreset("points-league")
    data = inputs.load_season(SEASON)
    universe = pd.concat([data["projections"][["player_id", "position"]],
                          data["goalie_candidates"][["player_id", "position"]]]
                         ).drop_duplicates("player_id")
    eligibility = inputs.load_eligibility(config, universe)
    calendar = schedule_module.from_candidates(
        data["projections"][["game_id", "game_date", "team_id"]], config.week_starts_on)
    field = managers_module.build_field(config, scoreset, strategy or _strategy(), rungs=rungs)
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

    strategy = _strategy()
    strategy = replace(strategy, adddrop=replace(strategy.adddrop, margin=float("inf")))
    season, _ = _small_season((5,), strategy=strategy)
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

    def run(rungs, spots):
        strategy = _strategy()
        strategy = replace(strategy, streaming=replace(strategy.streaming, spots=spots))
        season, _ = _small_season(rungs, strategy=strategy, sims=0)
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


def check_frozen_rosters() -> str:
    """No transacting team is left unable to fill its slots while it has moves and a free agent
    who could fix it.

    The bug: the fieldability test was absolute, so a goalie on IR made every swap illegal, and a
    forced activation drop could release the returning goalie -- after which the team made no move
    for the rest of the season with a G slot empty every night (banger, rung 7, seat 7: weeks
    2-26). Replayed under banger scoring, where it surfaced.
    """
    config = league_module.load()
    scoreset = simlayer.load_scoreset("banger-league")
    data = inputs.load_season(SEASON)
    universe = pd.concat([data["projections"][["player_id", "position"]],
                          data["goalie_candidates"][["player_id", "position"]]]
                         ).drop_duplicates("player_id")
    eligibility = inputs.load_eligibility(config, universe)
    calendar = schedule_module.from_candidates(
        data["projections"][["game_id", "game_date", "team_id"]], config.week_starts_on)
    field = managers_module.build_field(config, scoreset, _strategy(), rungs=(2, 5, 7))
    stuck, checked = [], [0]
    for manager in field:
        if manager.rung < 3:
            continue

        def watched(view, _orig=manager.transactions, _m=manager):
            _orig(view)
            checked[0] += 1
            roster, elig = list(view.roster), view._state.eligibility
            have = _m._fillable(roster, elig)
            if have < len(_m.slot_order) and view.moves_left > 0:
                fixable = any(_m._fillable(roster + [p], elig) > have
                              for p in view.free_agents() if not view.on_waivers(p))
                if fixable:
                    stuck.append((_m.team_index, view.day.date(), have))
        manager.transactions = watched
    season = engine_module.Season(config, calendar, data, eligibility, scoreset, field)
    rate = {int(k): 1.0 for k in season.player_pool()}
    season.run({p: -i for i, p in enumerate(sorted(rate))}, rate)
    assert not stuck, f"{len(stuck)} team-days left unfillable with a fix available: {stuck[:3]}"
    return f"{checked[0]:,} transaction steps, none left a fixable roster short of its slots"


def check_vor_board() -> str:
    """The value-over-replacement draft board is built from draft-day knowledge only, against a
    real replacement level, and drafting by it leaves every roster fieldable."""
    import draftroom
    import ladder

    from decisionlayer import draft as draft_module

    config = league_module.load()
    scoreset = simlayer.load_scoreset("points-league")
    data = inputs.load_season(SEASON)
    universe = pd.concat([data["projections"][["player_id", "position"]],
                          data["goalie_candidates"][["player_id", "position"]]]
                         ).drop_duplicates("player_id")
    eligibility = inputs.load_eligibility(config, universe)
    assert eligibility[240] == frozenset({"C"}), "a forward/defence name collision was kept"
    strategy = _strategy()
    data["prior"] = {scoreset.name: ladder.prior_season("2024-25", scoreset, strategy)}
    board = ladder.vor_board(data, "2024-25", scoreset, config, eligibility, strategy)

    # Draft-day knowledge only: inflating every rest-of-season row after opening week must not
    # move the board at all.
    later = data["ros"].copy()
    cut = later["game_date"].min() + pd.Timedelta(days=7)
    for column in [c for c in later.columns if c.startswith("proj_")]:
        later.loc[later["game_date"] >= cut, column] *= 100.0
    poisoned = ladder.vor_board({**data, "ros": later}, "2024-25", scoreset, config, eligibility,
                               strategy)
    assert board.equals(poisoned), "the VOR board read rest-of-season rows from after draft day"

    values = draft_module.preseason_values(data["ros"], data["prior"][scoreset.name][0],
                                           inputs.load_goalie_starts("2024-25"), scoreset,
                                           opening_days=strategy.opening_days)
    levels = draft_module.replacement_levels(values[[p in eligibility for p in values.index]],
                                             config, eligibility)
    assert all(v > 0 for v in levels.values()), f"a position has no replacement level: {levels}"

    state = state_module.LeagueState(config, sorted(eligibility), eligibility)
    draftroom.run(state, config, board, eligibility,
                  boards={seat: board for seat in range(config.teams)})
    draftroom.verify_rosters_fieldable(state, config, eligibility)
    return (f"{len(board)} players valued from opening-week rows only; replacement "
            f"{ {k: round(v) for k, v in levels.items()} }; an all-VOR draft is fieldable")


def check_draft_lottery(replications=8) -> str:
    """Every replication is a different draft, and every rung takes each early pick equally often.

    The rotation this replaced cancelled against build_field's own rotation: with two rungs all
    eight "rotations" were the same draft, which is how a paired gap came out +/- 0.0.
    """
    import collections

    import draftroom

    config = league_module.load()
    scoreset = simlayer.load_scoreset("points-league")
    notes = []
    for rungs in ((2, 12), (2, 5, 6, 7)):
        drafts, picks = set(), collections.Counter()
        for rep in range(replications):
            field = managers_module.build_field(config, scoreset, _strategy(), rungs=rungs,
                                                replication=rep)
            order = draftroom.seat_order(config, rep, len(rungs))
            drafts.add(tuple(field[s].rung for s in order))
            for k in range(3):
                picks[(field[order[k]].rung, k)] += 1
        assert len(drafts) == replications, f"{rungs}: only {len(drafts)} distinct drafts"
        counts = {picks[(r, k)] for r in rungs for k in range(3)}
        assert len(counts) == 1, f"{rungs}: early picks unbalanced across rungs {dict(picks)}"
        notes.append(f"{len(rungs)} rungs: {len(drafts)} drafts, picks 1-3 x{counts.pop()} each")
    return "; ".join(notes)


def check_claims() -> str:
    """Waiver claims resolve when the player clears, to the best-priority claimant who can take
    him, cost that claimant one move and his place in the queue -- and fail loudly, not silently.

    Claims used to be processed the next game day through add(), which refuses a player still on
    waivers: in a whole 2025-26 replay one claim of two succeeded and the other vanished at DEBUG.
    """
    config = league_module.load()
    pool = list(range(1, 400))
    eligibility = {p: frozenset({["C", "LW", "RW", "D", "G"][p % 5]}) for p in pool}
    s = state_module.LeagueState(config, pool, eligibility)
    for seat in range(3):
        for p in pool[seat * config.roster_size:(seat + 1) * config.roster_size]:
            s.draft(seat, p)
    s.start_week(1)
    monday = pd.Timestamp("2026-01-05")
    roster = {t: list(s.teams[t].roster) for t in range(3)}

    try:
        s.submit_claim(0, pool[-1], drop=roster[0][0], today=monday)
        raise AssertionError("a claim on a player who is not on waivers was accepted")
    except state_module.IllegalMove:
        pass

    # Team 2 drops X; teams 1 and 0 claim him. Team 0 has the better priority.
    x = roster[2][0]
    s.drop(2, x, monday)
    clears = s.waived[x]
    s.submit_claim(1, x, drop=roster[1][0], today=monday + pd.Timedelta(days=1))
    s.submit_claim(0, x, drop=roster[0][0], today=monday + pd.Timedelta(days=1))
    s.process_waivers(monday + pd.Timedelta(days=1))
    assert x in s.pool and x in s.pending_claims, "a claim was processed before the player cleared"

    moves_before = {t: s.teams[t].moves_used for t in range(3)}
    priority_before = {t: s.teams[t].waiver_priority for t in range(3)}
    awarded = s.process_waivers(clears)
    assert awarded == [(0, x)], f"the best-priority claimant did not win: {awarded}"
    assert x in s.teams[0].roster and roster[0][0] in s.pool, "the award or its drop did not happen"
    assert s.teams[0].moves_used == moves_before[0] + 1, "the award did not cost one move"
    assert s.teams[1].moves_used == moves_before[1], "the losing claimant spent a move"
    assert roster[1][0] in s.teams[1].roster, "the losing claimant's drop happened anyway"
    assert s.teams[0].waiver_priority == max(t.waiver_priority for t in s.teams), \
        "the winner did not go to the back of the queue"
    assert s.teams[1].waiver_priority == priority_before[1] - 1, "the queue did not move up"

    # A stale drop is re-chosen through the hook; without a replacement the claim fails loudly.
    y = roster[2][1]
    s.drop(2, y, clears)
    s.submit_claim(1, y, drop=roster[1][1], today=clears)
    s.drop(1, roster[1][1], clears)                      # the chosen drop leaves the roster
    s.add(1, pool[-2], clears)                           # and his spot is filled again
    replacement = roster[1][2]
    awarded = s.process_waivers(s.waived[y], redrop=lambda team, player: replacement)
    assert (1, y) in awarded and replacement in s.pool, "a stale drop was not re-chosen"

    z = roster[2][2]
    s.drop(2, z, clears)
    s.submit_claim(1, z, drop=roster[1][3], today=clears)
    s.drop(1, roster[1][3], clears)
    s.add(1, pool[-3], clears)
    failures_before = len(s.failed_claims)
    s.process_waivers(s.waived[z], redrop=lambda team, player: None)
    assert z not in s.teams[1].roster, "a claim with no legal drop was awarded"
    assert len(s.failed_claims) == failures_before + 1 and "drop" in s.failed_claims[-1]["reason"], \
        "a failed claim was not recorded with its reason"
    s.assert_legal()
    return (f"awarded at the clear date to the best priority; {s.claims_submitted[1]} claims by one "
            f"team, {len(s.failed_claims)} recorded failure(s); stale drop re-chosen")


def check_league_rules() -> str:
    """The league's rules come from its settings file: an unsupported rule is refused at load, and
    move costs are charged as configured -- checked before anything changes, so a refused action
    spends nothing."""
    import copy
    import json

    base = json.loads(paths.league_config("league").read_text(encoding="utf-8"))
    for label, mutate in (("FAAB waivers", lambda c: c["rules"].update(waivers="faab")),
                          ("an undefined slot", lambda c: c["slot_positions"].pop("F")),
                          ("a goalie/skater slot", lambda c: c["slot_positions"].update({"F/D": ["C", "G"]}))):
        config = copy.deepcopy(base)
        mutate(config)
        try:
            league_module.LeagueConfig(**config)
            raise AssertionError(f"a league with {label} was accepted")
        except ValueError:
            pass

    config = copy.deepcopy(base)
    config["rules"]["move_cost"].update(drop=1, ir_stash=1)
    config = league_module.LeagueConfig(**config)
    pool = list(range(1, 200))
    eligibility = {p: frozenset({"C"}) for p in pool}
    s = state_module.LeagueState(config, pool, eligibility)
    for p in pool[:config.roster_size]:
        s.draft(0, p)
    s.start_week(1)
    day = pd.Timestamp("2026-01-05")
    team = s.teams[0]
    s.add(0, pool[100], day, drop=pool[0])
    assert team.moves_used == 2, f"an add with a drop costing 1 spent {team.moves_used}, not 2"
    s.stash(0, pool[1], {pool[1]})
    assert team.moves_used == 3, "a stash costing 1 was not charged"
    team.moves_used = config.moves_per_week
    roster_before = list(team.roster)
    try:
        s.add(0, pool[101], day, drop=pool[2])
        raise AssertionError("an add was allowed with no moves left")
    except state_module.IllegalMove:
        pass
    assert team.roster == roster_before, "a refused add still changed the roster"
    return "unsupported rules refused at load; configured costs charged (add+drop 2, stash 1); a refused add spends nothing"


def check_settings() -> str:
    """Ties, draft, schedule and playoffs are league settings, and strategy parameters are their own
    file: an unsupported or missing value is refused at load, a tie is scored by the league's rule,
    a linear draft keeps its order, and a strategy file missing a parameter is an error rather
    than a default."""
    import copy
    import json

    import draftroom

    base = json.loads(paths.league_config("league").read_text(encoding="utf-8"))
    for label, mutate in (("an unknown tie rule", lambda c: c.update(ties="coin")),
                          ("keepers", lambda c: c["draft"].update(keepers=2)),
                          ("an auction draft", lambda c: c["draft"].update(type="auction")),
                          ("a missing schedule length", lambda c: c["schedule"].pop(
                              "regular_season_weeks")),
                          ("both a length and an end date", lambda c: c["schedule"].update(
                              regular_season_end="03-14")),
                          ("miscounted byes", lambda c: c["playoffs"].update(byes=1)),
                          ("a 4-team bracket over 3 rounds", lambda c: c["playoffs"].update(
                              teams=4, byes=4)),
                          ("a loose playoff_teams", lambda c: c.update(playoff_teams=8))):
        config = copy.deepcopy(base)
        mutate(config)
        try:
            league_module.LeagueConfig(**config)
            raise AssertionError(f"a league with {label} was accepted")
        except (ValueError, TypeError):
            pass

    config = league_module.LeagueConfig(**base)
    assert config.tie_share() == 0.5 and config.bracket_order() == [1, 8, 4, 5, 2, 7, 3, 6],         "the committed league's tie rule or bracket moved"
    loss = league_module.LeagueConfig(**{**base, "ties": "loss"})
    assert loss.tie_share() == 0.0, "a tie under the loss rule still scored"

    linear = league_module.LeagueConfig(**{**copy.deepcopy(base),
                                           "draft": {**base["draft"], "type": "linear"}})
    pool = list(range(1, 400))
    eligibility = {p: frozenset({["C", "LW", "RW", "D", "G"][p % 5]}) for p in pool}
    board = pd.Series({p: float(-p) for p in pool})
    rounds = {}
    for label, league in (("snake", config), ("linear", linear)):
        state = state_module.LeagueState(league, pool, eligibility)
        draftroom.run(state, league, board, eligibility)
        # Second-round pick of the first-round first picker: his second-best player.
        first = min(range(league.teams), key=lambda t: min(state.teams[t].roster))
        rounds[label] = sorted(state.teams[first].roster)[1]
    assert rounds["linear"] < rounds["snake"], \
        f"the linear draft did not keep the first seat first in round two: {rounds}"

    payload = json.loads(paths.STRATEGY_CONFIG.read_text(encoding="utf-8"))
    for section, key in (("adddrop", "margin"), ("streaming", "spots"), ("priors", "opening_days")):
        broken = copy.deepcopy(payload)
        broken[section].pop(key)
        try:
            decisionlayer.strategy.from_dict(broken, name="broken")
            raise AssertionError(f"a strategy missing {section}.{key} was accepted")
        except ValueError:
            pass
    return (f"unsupported ties/draft/schedule/playoffs refused; tie = {config.tie_share():g} win "
            f"each; linear draft keeps order; strategy {_strategy().name!r} requires every value")


def check_playoffs() -> str:
    """The bracket after the regular season: the top seeds by record (season points breaking
    ties), byes for the top seeds, a fixed bracket, one champion who won every round he played,
    and regular-season metrics that stop at the regular season's last week."""
    season, calendar = _small_season((2,), sims=0)
    config = season.config
    rate = {int(k): 1.0 for k in season.player_pool()}
    report = season.run({p: -i for i, p in enumerate(sorted(rate))}, rate)
    teams, games = report["teams"], report["playoffs"]
    regular = config.regular_season_weeks_in(calendar)

    record = sorted(range(config.teams), key=lambda t: (-season.state.teams[t].matchup_wins,
                                                        -season._season_points(t), t))
    seeds = {t: i + 1 for i, t in enumerate(record[:config.playoff_teams])}
    assert season.seeds == seeds, f"seeding is not record then points: {season.seeds} vs {seeds}"
    assert (teams["weeks"] == regular).all(), "a playoff week counted as a regular-season week"

    byes = {t for t, s in seeds.items() if s <= config.playoffs["byes"]}
    first = games[games["round"] == 1]
    assert not byes & (set(first["team_a"]) | set(first["team_b"])), "a bye seed played round one"
    assert sorted(map(sorted, zip(first["seed_a"], first["seed_b"]))) ==         sorted(map(sorted, [(4, 5), (3, 6)] if config.playoff_teams == 6 else
                   zip(config.bracket_order()[::2], config.bracket_order()[1::2]))),         f"round one is not the fixed bracket: {first[['seed_a', 'seed_b']].values.tolist()}"
    assert len(games) == config.playoff_teams - 1, f"{len(games)} playoff games, not one fewer than the field"
    champion = season.champion
    assert int(teams["champion"].sum()) == 1 and champion in seeds, "not exactly one champion"
    for g in games.itertuples():
        loser = g.team_b if g.winner == g.team_a else g.team_a
        winner_points = g.points_a if g.winner == g.team_a else g.points_b
        loser_points = g.points_b if g.winner == g.team_a else g.points_a
        assert winner_points > loser_points or (winner_points == loser_points and
            season._season_points(g.winner) >= season._season_points(loser)),             f"round {g.round}: the lower score advanced without the tiebreak"
        later = games[(games["round"] > g.round)]
        assert loser not in set(later["team_a"]) | set(later["team_b"]), "an eliminated team played on"
    rounds_won = int(teams.loc[teams["seat"] == champion, "playoff_wins"].iloc[0])
    played = config.playoff_rounds - (1 if seeds[champion] <= config.playoffs["byes"] else 0)
    assert rounds_won == played, f"the champion won {rounds_won} of {played} rounds"
    return (f"{regular} regular weeks + {config.playoff_weeks} playoff; seeds by record then points, "
            f"byes to seeds 1-{config.playoffs['byes']}; {len(games)} games; champion seed "
            f"{seeds[champion]} won {rounds_won} round(s)")


def check_no_clobber() -> str:
    """A scored build aimed elsewhere leaves every file under Projections/models/ untouched.

    Every train.py run used to save its boosters into models/ whatever its holdout, so building a
    2024-25 holdout would have replaced the deployment build predict.py ships. Run as a real,
    small scored build in a subprocess -- Projections' own `paths` would shadow this folder's.
    """
    import hashlib
    import subprocess
    import tempfile

    models = paths.PROJECTIONS_DIR / "models"

    def manifest():
        return {str(p.relative_to(models)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(models.rglob("*")) if p.is_file()}

    before = manifest()
    with tempfile.TemporaryDirectory() as scratch:
        result = subprocess.run(
            [sys.executable, "goalie_starts.py", "--train", "--save", "--rounds", "20",
             "--season", SEASON, "--models-dir", scratch],
            cwd=paths.PROJECTIONS_DIR, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr[-400:]
        wrote = sorted(p.name for p in Path(scratch).iterdir())
    after = manifest()
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    assert not changed, f"a scored build changed models/: {changed[:5]}"
    assert "goalie_start.txt" in wrote, f"the build wrote nothing to its own directory: {wrote}"
    return f"scored build wrote {len(wrote)} files to its own directory; {len(before)} files in models/ unchanged"


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
          ("frozen rosters", check_frozen_rosters),
          ("vor board", check_vor_board), ("draft lottery", check_draft_lottery), ("claims", check_claims), ("league rules", check_league_rules),
          ("settings", check_settings), ("playoffs", check_playoffs),
          ("no clobber", check_no_clobber),
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

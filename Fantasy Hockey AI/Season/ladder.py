#!/usr/bin/env python
"""Run the section 16 ladder and report what it measured.

    python ladder.py --season 2025-26 --weights points-league
    python ladder.py --season 2025-26 --weights points-league --weights banger-league

One mixed twelve-team league, three clones of each rung, double round robin. The rungs share one
free-agent pool, so they interfere -- which is realistic, and which is why the rung-3 clones can
take players from each other.

Phase 1 runs rungs 1 to 3 on the real 2025-26 outcomes: one deterministic replay, no Monte Carlo
noise. Rung 4 needs the distributions and arrives with phase 2.
"""

import argparse
import json
import logging
import sys

import pandas as pd

import engine as engine_module
import field as field_module
import inputs
import league as league_module
import paths
import report as report_module
import schedule as schedule_module

import simlayer
from decisionlayer import draft as draft_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ladder")


def parse_args():
    parser = argparse.ArgumentParser(description="Run the section 16 baseline ladder")
    parser.add_argument("--season", default="2025-26", help="Season to replay")
    parser.add_argument("--prior-season", default="2024-25",
                        help="Season whose points make the draft board")
    parser.add_argument("--weights", action="append", default=None,
                        help="Scoring file; repeat to score one run under several leagues")
    parser.add_argument("--rung", action="append", type=int, default=None,
                        help="Rungs to seat (default 1 2 3 4)")
    parser.add_argument("--replications", type=int, default=1,
                        help="Seat rotations; outcomes are deterministic in phase 1, so this "
                             "varies draft position only")
    parser.add_argument("--league", default=None, help="League config: a name in Settings/rosters/, or a path")
    parser.add_argument("--decision-sims", type=int, default=200,
                        help="Monte Carlo draws per slate for rung 4's decisions. Drawn on a "
                             "stream independent of anything that resolves a night.")
    parser.add_argument("--verbose-weeks", action="store_true")
    parser.add_argument("--strategy", default=None,
                        help="Strategy settings: a name in Settings/, or a path "
                             "(default strategy.json). The flags below override single values.")
    # Section 9's add/drop rule, seated as rung 5. Unset flags keep the strategy file's values.
    parser.add_argument("--margin", type=float, default=None,
                        help="Rung 5: sds of the gain a move must clear ('inf' never moves)")
    parser.add_argument("--horizon-weeks", default=None,
                        help="Rung 5: weeks past the current one a swap is priced over, or 'season'")
    parser.add_argument("--rate-source", choices=("ros", "per_game"), default=None,
                        help="Rung 5: rest-of-season (holdout build) or per-game carried rate")
    parser.add_argument("--claim-premium", type=float, default=None,
                        help="Rung 5: extra points a waiver claim must clear ('inf' never claims)")
    # Section 10's orchestrator, seated as rung 7: rung 5's upgrades plus streaming.
    parser.add_argument("--streams", type=int, default=None,
                        help="Rung 7: streaming spots (0 makes it rung 5)")
    parser.add_argument("--stream-reserve", type=int, default=None,
                        help="Rung 7: moves held for upgrades on a week's first day, falling to 0")
    parser.add_argument("--stream-lambda", type=float, default=None,
                        help="Rung 7: points a stream must clear early in the week, falling to 0")
    parser.add_argument("--stream-margin", type=float, default=None,
                        help="Rung 7: sds of the week's gain a stream must also clear")
    parser.add_argument("--stream-gate", action="store_true",
                        help="Rung 7: scale a stream's gain by phi(z)/phi(0) of the matchup")
    parser.add_argument("--no-stream-claim", action="store_true",
                        help="Rung 7: stop rentals claiming players off waivers (on by default)")
    parser.add_argument("--stream-flat", action="store_true",
                        help="Rung 7: hold the rental bar at lam/2 all week instead of letting it fall")
    parser.add_argument("--playoff-eliminated", choices=("hold", "continue"), default=None,
                        help="Playoffs: whether an eliminated team keeps transacting")
    parser.add_argument("--playoff-weight", choices=("p_advance", "flat"), default=None,
                        help="Playoffs: weight later rounds by P(reaching them), or count them flat")
    parser.add_argument("--z-source", choices=("closed_form", "sampled"), default=None,
                        help="Rung 4+: the matchup z from closed-form moments or sampled week totals")
    parser.add_argument("--field", default=None,
                        help="Opponent settings: a name in Settings/, or a path (default field.json)")
    parser.add_argument("--opponent-board", choices=field_module.BOARDS, default=None,
                        help="How non-VOR seats draft (field opponent_board)")
    parser.add_argument("--opponent-sources", default=None,
                        help="Sources each opponent reads, e.g. 2 or 1-3 (field sources_per_opponent)")
    parser.add_argument("--vor-values", choices=("own_model", "consensus"), default=None,
                        help="What the VOR board values players on (strategy draft.vor_values)")
    parser.add_argument("--workers", type=int, default=None,
                        help="Processes to run replications in (default: one per replication, at "
                             "most the logical cores less 4). 1 runs them in this process, one "
                             "after another")
    parser.add_argument("--tag", default=None,
                        help="Suffix for the report and doc names, so an experiment does not "
                             "overwrite the committed ladder")
    return parser.parse_args()


def load_strategy(args):
    """The strategy file, with any command-line overrides applied on top."""
    from dataclasses import replace

    from decisionlayer import load_strategy as load

    strategy = load(args.strategy)
    strategy = replace(strategy, adddrop=adddrop_params(args, strategy.adddrop),
                       streaming=stream_params(args, strategy.streaming))
    if args.z_source:
        strategy = replace(strategy, z_source=args.z_source)
    if args.playoff_eliminated:
        strategy = replace(strategy, playoff_eliminated=args.playoff_eliminated)
    if args.playoff_weight:
        strategy = replace(strategy, playoff_week_weight=args.playoff_weight)
    if args.vor_values:
        strategy = replace(strategy, vor_values=args.vor_values)
    return strategy


def adddrop_params(args, base):
    """Rung 5's parameters: the strategy file's, with command-line overrides."""
    from dataclasses import replace

    changes = {}
    if args.margin is not None:
        changes["margin"] = args.margin
    if args.horizon_weeks is not None:
        changes["horizon_weeks"] = (None if args.horizon_weeks == "season"
                                    else int(args.horizon_weeks))
    if args.rate_source is not None:
        changes["rate_source"] = args.rate_source
    if args.claim_premium is not None:
        changes["claim_premium"] = args.claim_premium
    return replace(base, **changes)


def stream_params(args, base):
    """Rung 7's streaming parameters: the strategy file's, with command-line overrides."""
    from dataclasses import replace

    changes = {}
    for flag, field in (("streams", "spots"), ("stream_reserve", "reserve"),
                        ("stream_lambda", "lam"), ("stream_margin", "margin")):
        if getattr(args, flag) is not None:
            changes[field] = getattr(args, flag)
    if args.stream_gate:
        changes["gate"] = True
    if args.stream_flat:
        changes["flat"] = True
    if args.no_stream_claim:
        changes["claim"] = False
    return replace(base, **changes)


def prior_season(prior_season_name, scoreset, strategy):
    """Last season's totals (the shared draft board), rates per game played (rung 3's prior), and
    rates per team game (the fallback for anyone the projections have not reached yet)."""
    actuals = inputs.load_actuals(prior_season_name)
    goalies = inputs.load_goalie_starts(prior_season_name)
    return (draft_module.prior_season_board(actuals, goalies, scoreset),
            draft_module.prior_season_rate(actuals, goalies, scoreset,
                                           strategy.prior_rate_shrink_games),
            draft_module.prior_season_team_game_rate(actuals, goalies, scoreset,
                                                     strategy.prior_rate_shrink_games))


def team_openers(data) -> dict:
    """Each team's first game of the season -- the schedule, knowable before the draft."""
    games = data["projections"][["team_id", "game_date"]]
    return pd.to_datetime(games["game_date"]).groupby(games["team_id"]).min().to_dict()


def vor_board(data, prior_season_name, scoreset, config, eligibility, strategy):
    """Section 9 step 2's draft board: value over replacement on `board_values`, with each
    position's replacement fixed before the draft."""
    values = board_values(data, prior_season_name, scoreset, strategy)
    return draft_module.vor_board(values[[p in eligibility for p in values.index]], config,
                                  eligibility)


def board_values(data, prior_season_name, scoreset, strategy):
    """The season values our VOR draft is built on, from what is knowable on draft day.

    They follow strategy draft.vor_values: `consensus`, the external sources alone, read
    through `inputs.load_external_projections` and so only if published before the opener; or
    `own_model`, our opening-week rest-of-season rows, a backtest reference only.
    """
    board = data["prior"][scoreset.name][0]
    board.index = board.index.astype(int)
    if strategy.vor_values == "consensus":
        opener = data["projections"]["game_date"].min()
        external = inputs.load_external_projections(data["season"], opener,
                                                    strategy.undated_sources)
        values = draft_module.values_for("consensus", scoreset, board, external=external,
                                         min_sources=strategy.vor_min_sources)
    else:
        if data.get("ros") is None:
            raise SystemExit("the own_model VOR board needs rest-of-season projections -- run "
                             "Projections/ros_train.py --horizon season --predictions-out")
        values = draft_module.values_for(
            "own_model", scoreset, board, ros=data["ros"],
            prior_goalie_lines=inputs.load_goalie_starts(prior_season_name),
            opening_days=strategy.opening_days, team_openers=team_openers(data))
    return values


# A simulated leaguemate's board, per (season, scoring, format, sources). Subsets repeat across
# seats and replications, and building one runs a league draft for its replacement levels.
_OPPONENT_BOARDS = {}


def prepare_field(data, field_config, strategy) -> None:
    """Put the opponents' settings, and the sources they read, where every worker finds them."""
    data["field"] = field_config
    if field_config.opponent_board == "source_subsets" and "external" not in data:
        opener = data["projections"]["game_date"].min()
        data["external"] = inputs.load_external_projections(data["season"], opener,
                                                            strategy.undated_sources)


def opponent_board(data, scoreset, config, eligibility, replication, seat) -> pd.Series:
    """The VOR board of the leaguemate in `seat`: the consensus of the 1-3 sources he reads."""
    external = data["external"]
    sources = field_module.draw_sources(external["source"].unique(),
                                        data["field"].sources_per_opponent, replication, seat)
    key = (data["season"], scoreset.name, config.name, sources)
    board = _OPPONENT_BOARDS.get(key)
    if board is None:
        last = data["prior"][scoreset.name][0]
        last.index = last.index.astype(int)
        values = draft_module.source_board(external, sources, last, scoreset)
        board = draft_module.vor_board(values[[p in eligibility for p in values.index]], config,
                                       eligibility)
        _OPPONENT_BOARDS[key] = board
    return board


def run_one(config, calendar, data, eligibility, scoreset, rungs, replication, verbose_weeks,
            decision_sims, strategy, candidate=None):
    from decisionlayer import managers as managers_module

    field = managers_module.build_field(config, scoreset, strategy, rungs=rungs,
                                        replication=replication, candidate=candidate)
    base = [m.rung % managers_module.VOR_TWIN for m in field]
    if any(r in (5, 7) for r in base) and strategy.adddrop.rate_source == "ros"             and data.get("ros") is None:
        raise SystemExit("rung 5 reads rest-of-season projections and none are built -- run "
                         "Projections/ros_train.py --horizon season --predictions-out")
    # Draws are only paid for if a rung on the board actually uses them.
    sims = decision_sims if any(r in (4, 5, 6, 7) for r in base) else 0
    season = engine_module.Season(config, calendar, data, eligibility, scoreset, field,
                                 replication=replication, log_every_week=verbose_weeks,
                                 decision_sims=sims)
    board, rate, forward = data["prior"][scoreset.name]
    vor = data.get("vor", {}).get(scoreset.name)
    boards = {m.team_index: vor for m in field if m.draft_board == "vor"}
    if boards and vor is None:
        raise SystemExit("a VOR-drafting rung is seated but no VOR board was built")
    caps = {}
    fielded = data.get("field")
    if fielded is not None and fielded.opponent_board == "source_subsets":
        for m in field:
            if m.draft_board != "vor":
                boards[m.team_index] = opponent_board(data, scoreset, config, eligibility,
                                                      replication, m.team_index)
                caps[m.team_index] = fielded.max_goalies
    return season.run({int(k): float(v) for k, v in board.items()},
                      {int(k): float(v) for k, v in rate.items()},
                      {int(k): float(v) for k, v in forward.items()},
                      boards={s: {int(k): float(v) for k, v in b.items()}
                              for s, b in boards.items()},
                      goalie_caps=caps or None)


# One replication per task, in a pool of processes. Each worker receives the season's inputs once,
# at start-up, and every replication seeds its own streams, so the result is the same as running
# them one after another -- `verify.py` holds the ladder to that under two hash seeds.
_WORKER = {}


def _init_worker(payload):
    _WORKER.update(payload)


def _run_replication(replication):
    w = _WORKER
    return run_one(w["config"], w["calendar"], w["data"], w["eligibility"], w["scoreset"],
                   w["rungs"], replication, w["verbose_weeks"], w["decision_sims"], w["strategy"],
                   w.get("candidate"))


def default_workers(replications) -> int:
    """One process per replication, up to the logical cores less four (8 on a 12-core machine):
    an 8-draft run then finishes in one wave -- 52 s against 85 s at the old fixed 6 -- and the
    spare cores keep the machine usable. Results do not depend on it."""
    import os

    return max(1, min(replications, (os.cpu_count() or 2) - 4))


def run_replications(args, config, calendar, data, eligibility, scoreset, rungs, strategy,
                     candidate=None):
    workers = args.workers or default_workers(args.replications)
    if workers <= 1 or args.replications <= 1:
        return [run_one(config, calendar, data, eligibility, scoreset, rungs, r,
                        args.verbose_weeks, args.decision_sims, strategy, candidate)
                for r in range(args.replications)]
    import sys
    from concurrent.futures import ProcessPoolExecutor

    # A spawned worker starts from this process's sys.path, where simlayer has put Simulation/
    # first -- so its `import paths` would find Simulation's paths module, not this folder's.
    # Season first, as it is when a run starts; simlayer puts Simulation back in front of the
    # rest once `paths` is imported, exactly as here.
    if sys.path[0] != str(paths.PROJECT_ROOT):
        sys.path.insert(0, str(paths.PROJECT_ROOT))
    payload = {"config": config, "calendar": calendar, "data": data, "eligibility": eligibility,
               "scoreset": scoreset, "rungs": rungs, "verbose_weeks": args.verbose_weeks,
               "decision_sims": args.decision_sims, "strategy": strategy,
               "candidate": candidate}
    log.info("running %d replications in %d processes", args.replications, workers)
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                             initargs=(payload,)) as pool:
        return list(pool.map(_run_replication, range(args.replications)))


def summarize(per_replication, scoreset_name):
    """Collapse seats into rungs. Three clones a rung, so a rung's number is their mean."""
    teams = pd.concat([r["teams"].assign(replication=i)
                       for i, r in enumerate(per_replication)], ignore_index=True)
    by_rung = (teams.groupby(["rung", "strategy"])
               .agg(seats=("seat", "count"),
                    matchup_wins=("matchup_wins", "mean"),
                    weeks=("weeks", "mean"),
                    points=("points", "mean"),
                    games_started_rate=("games_started_rate", "mean"),
                    empty_slot_nights=("empty_slot_nights", "mean"),
                    wasted_slot_nights=("wasted_slot_nights", "mean"),
                    decision_efficiency=("decision_efficiency", "mean"),
                    moves_spent=("moves_spent", "mean"),
                    forced_drops=("forced_drops", "mean"),
                    claims_submitted=("claims_submitted", "mean"),
                    claims_awarded=("claims_awarded", "mean"),
                    claims_failed=("claims_failed", "mean"),
                    move_hit_rate=("move_hit_rate", "mean"),
                    realized_gain_per_move=("realized_gain_per_move", "mean"),
                    rentals=("rentals", "mean"),
                    rental_hit_rate=("rental_hit_rate", "mean"),
                    rental_gain=("rental_gain", "mean"),
                    rental_drop_next_week=("rental_drop_next_week", "mean"),
                    playoff_rate=("made_playoffs", "mean"),
                    playoff_wins=("playoff_wins", "mean"),
                    title_rate=("champion", "mean"))
               .reset_index())
    by_rung["win_rate"] = by_rung["matchup_wins"] / by_rung["weeks"]
    by_rung["points_per_week"] = by_rung["points"] / by_rung["weeks"]
    by_rung["scoreset"] = scoreset_name
    return by_rung.sort_values("rung"), teams


def main():
    args = parse_args()
    config = league_module.load(args.league)
    rungs = tuple(args.rung) if args.rung else (1, 2, 3, 4)
    weights = args.weights or ["points-league"]

    log.info("%s", config.describe())
    data = inputs.load_season(args.season)
    data["prior_season"] = args.prior_season

    universe = pd.concat([
        data["projections"][["player_id", "position"]],
        data["goalie_candidates"][["player_id", "position"]],
    ]).drop_duplicates("player_id")
    eligibility = inputs.load_eligibility(config, universe)
    calendar = schedule_module.from_candidates(
        data["projections"][["game_id", "game_date", "team_id"]], config.week_starts_on,
        config.min_first_week_games)
    log.info("calendar: %s", json.dumps(calendar.verify()))

    strategy = load_strategy(args)
    params, streams = strategy.adddrop, strategy.streaming
    log.info("strategy: %s", strategy.name)
    field_config = field_module.with_overrides(field_module.load(args.field), args.opponent_board,
                                               args.opponent_sources)
    prepare_field(data, field_config, strategy)
    log.info("field: %s", field_config.describe())
    report = {"season": args.season, "league": config.name, "strategy": strategy.name,
              "rungs": list(rungs), "replications": args.replications, "results": {},
              "vor_values": strategy.vor_values, "field": field_config.describe()}
    if 5 in rungs:
        report["adddrop"] = params.describe()
        log.info("rung 5 add/drop: %s", params.describe())
    if 7 in rungs:
        report["adddrop"] = params.describe()
        report["streaming"] = streams.describe()
        log.info("rung 7 orchestrator: %s | %s", params.describe(), streams.describe())

    data["prior"] = {}
    data["vor"] = {}
    pwin_frames = {}
    for name in weights:
        scoreset = simlayer.load_scoreset(name)
        data["prior"][scoreset.name] = prior_season(args.prior_season, scoreset, strategy)
        if any(r > 10 for r in rungs):
            data["vor"][scoreset.name] = vor_board(data, args.prior_season, scoreset, config,
                                                   eligibility, strategy)
        log.info("=== %s === skaters score %s | goalies score %s (unpriced: %s)",
                 scoreset.name, scoreset.scored("skaters"), scoreset.scored("goalies"),
                 scoreset.missing("goalies") or "none")
        runs = run_replications(args, config, calendar, data, eligibility, scoreset, rungs,
                                strategy)
        table, teams = summarize(runs, scoreset.name)
        logged = [r["pwin"].assign(replication=i) for i, r in enumerate(runs)
                  if len(r.get("pwin", []))]
        # Rungs below 4 compute no P(win), so a field of them logs nothing.
        pwin = pd.concat(logged, ignore_index=True) if logged else pd.DataFrame()
        if len(pwin):
            pwin_frames[scoreset.name] = pwin
            for column in ("p_closed", "p_sampled"):
                if column in pwin:
                    rows = pwin.dropna(subset=[column])
                    brier = float(((rows[column] - rows["won"]) ** 2).mean())
                    log.info("P(win) calibration, %s: Brier %.4f over %d manager-days", column,
                             brier, len(rows))
        print(f"\n{scoreset.name}")
        print(table[["rung", "strategy", "win_rate", "points_per_week", "games_started_rate",
                     "decision_efficiency", "empty_slot_nights", "wasted_slot_nights",
                     "moves_spent", "forced_drops", "claims_awarded", "claims_failed", "move_hit_rate",
                     "realized_gain_per_move",
                     "rentals", "rental_hit_rate", "rental_gain",
                     "playoff_rate", "title_rate"]]
              .to_string(index=False, float_format=lambda v: f"{v:.3f}"))
        games = [r["playoffs"].assign(replication=i) for i, r in enumerate(runs)
                 if len(r.get("playoffs", []))]
        report["results"][scoreset.name] = {
            "by_rung": table.to_dict("records"),
            "by_seat": teams.to_dict("records"),
            # Every playoff game, for paired game-level comparisons between arms.
            "playoff_games": (pd.concat(games, ignore_index=True).to_dict("records")
                              if games else []),
        }

    paths.ensure(paths.REPORTS_DIR)
    stem = (config.source.stem if config.source else config.name)
    if args.tag:
        stem = f"{stem}_{args.tag}"
    out = paths.ladder_report(args.season, stem)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    log.info("-> %s", out)
    for name, frame in pwin_frames.items():
        path = out.with_name(f"{out.stem}_pwin_{name}.parquet")
        frame.to_parquet(path, index=False)
        log.info("-> %s", path)
    report_module.write(report, args.season, config.regular_season_weeks_in(calendar),
                        path=paths.ensure(paths.DOCS_DIR) / f"ladder-{stem}.md")


if __name__ == "__main__":
    main()

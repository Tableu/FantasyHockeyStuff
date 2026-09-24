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
    parser.add_argument("--league", default=None, help="League config: a name in LeagueSettings/rosters/, or a path")
    parser.add_argument("--decision-sims", type=int, default=200,
                        help="Monte Carlo draws per slate for rung 4's decisions. Drawn on a "
                             "stream independent of anything that resolves a night.")
    parser.add_argument("--verbose-weeks", action="store_true")
    # Section 9's add/drop rule, seated as rung 5. Unset flags keep AddDropParams' defaults.
    parser.add_argument("--margin", type=float, default=None,
                        help="Rung 5: sds of the gain a move must clear ('inf' never moves)")
    parser.add_argument("--horizon-weeks", default=None,
                        help="Rung 5: weeks past the current one a swap is priced over, or 'season'")
    parser.add_argument("--rate-source", choices=("ros", "per_game"), default=None,
                        help="Rung 5: rest-of-season (holdout build) or per-game carried rate")
    parser.add_argument("--claim-premium", type=float, default=None,
                        help="Rung 5: extra points a waiver claim must clear (default 0; 'inf' never claims)")
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
    parser.add_argument("--tag", default=None,
                        help="Suffix for the report and doc names, so an experiment does not "
                             "overwrite the committed ladder")
    return parser.parse_args()


def adddrop_params(args):
    """Rung 5's parameters from the command line, over AddDropParams' defaults."""
    from dataclasses import replace

    from decisionlayer import adddrop

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
    return replace(adddrop.AddDropParams(), **changes)


def stream_params(args):
    """Rung 7's streaming parameters from the command line, over StreamParams' defaults."""
    from dataclasses import replace

    from decisionlayer import streaming

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
    return replace(streaming.StreamParams(), **changes)


def prior_season(prior_season_name, scoreset):
    """Last season's totals (the shared draft board), rates per game played (rung 3's prior), and
    rates per team game (the fallback for anyone the projections have not reached yet)."""
    actuals = inputs.load_actuals(prior_season_name)
    goalies = inputs.load_goalie_starts(prior_season_name)
    return (draft_module.prior_season_board(actuals, goalies, scoreset),
            draft_module.prior_season_rate(actuals, goalies, scoreset),
            draft_module.prior_season_team_game_rate(actuals, goalies, scoreset))


def vor_board(data, prior_season_name, scoreset, config, eligibility):
    """Section 9 step 2's draft board: value over replacement, from what is knowable on draft day
    (opening-week rest-of-season rows from the holdout build, last season's goalie starts)."""
    if data.get("ros") is None:
        raise SystemExit("the VOR board needs rest-of-season projections -- run "
                         "Projections/ros_train.py --horizon season --predictions-out")
    board = data["prior"][scoreset.name][0]
    board.index = board.index.astype(int)
    values = draft_module.preseason_values(data["ros"], board,
                                           inputs.load_goalie_starts(prior_season_name), scoreset)
    return draft_module.vor_board(values[[p in eligibility for p in values.index]], config,
                                  eligibility)


def run_one(config, calendar, data, eligibility, scoreset, rungs, replication, verbose_weeks,
            decision_sims=0, params=None, streams=None):
    from decisionlayer import managers as managers_module

    field = managers_module.build_field(config, scoreset, rungs=rungs,
                                        replication=replication, adddrop_params=params,
                                        stream_params=streams)
    base = [m.rung % managers_module.VOR_TWIN for m in field]
    if any(r in (5, 7) for r in base) and params is not None and params.rate_source == "ros"             and data.get("ros") is None:
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
    return season.run({int(k): float(v) for k, v in board.items()},
                      {int(k): float(v) for k, v in rate.items()},
                      {int(k): float(v) for k, v in forward.items()},
                      boards={s: {int(k): float(v) for k, v in b.items()}
                              for s, b in boards.items()})


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
                    rental_drop_next_week=("rental_drop_next_week", "mean"))
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
        data["projections"][["game_id", "game_date", "team_id"]], config.week_starts_on)
    log.info("calendar: %s", json.dumps(calendar.verify()))

    params = adddrop_params(args)
    streams = stream_params(args)
    report = {"season": args.season, "league": config.name, "rungs": list(rungs),
              "replications": args.replications, "results": {}}
    if 5 in rungs:
        report["adddrop"] = params.describe()
        log.info("rung 5 add/drop: %s", params.describe())
    if 7 in rungs:
        report["adddrop"] = params.describe()
        report["streaming"] = streams.describe()
        log.info("rung 7 orchestrator: %s | %s", params.describe(), streams.describe())

    data["prior"] = {}
    data["vor"] = {}
    for name in weights:
        scoreset = simlayer.load_scoreset(name)
        data["prior"][scoreset.name] = prior_season(args.prior_season, scoreset)
        if any(r > 10 for r in rungs):
            data["vor"][scoreset.name] = vor_board(data, args.prior_season, scoreset, config,
                                                   eligibility)
        log.info("=== %s === skaters score %s | goalies score %s (unpriced: %s)",
                 scoreset.name, scoreset.scored("skaters"), scoreset.scored("goalies"),
                 scoreset.missing("goalies") or "none")
        runs = [run_one(config, calendar, data, eligibility, scoreset, rungs, r,
                        args.verbose_weeks, args.decision_sims, params, streams)
                for r in range(args.replications)]
        table, teams = summarize(runs, scoreset.name)
        print(f"\n{scoreset.name}")
        print(table[["rung", "strategy", "win_rate", "points_per_week", "games_started_rate",
                     "decision_efficiency", "empty_slot_nights", "wasted_slot_nights",
                     "moves_spent", "forced_drops", "claims_awarded", "claims_failed", "move_hit_rate",
                     "realized_gain_per_move",
                     "rentals", "rental_hit_rate", "rental_gain"]]
              .to_string(index=False, float_format=lambda v: f"{v:.3f}"))
        report["results"][scoreset.name] = {
            "by_rung": table.to_dict("records"),
            "by_seat": teams.to_dict("records"),
        }

    paths.ensure(paths.REPORTS_DIR)
    stem = (config.source.stem if config.source else config.name)
    if args.tag:
        stem = f"{stem}_{args.tag}"
    out = paths.ladder_report(args.season, stem)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    log.info("-> %s", out)
    report_module.write(report, args.season, config.regular_season_weeks,
                        path=paths.ensure(paths.DOCS_DIR) / f"ladder-{stem}.md")


if __name__ == "__main__":
    main()

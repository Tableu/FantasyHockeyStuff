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
                        help="Rung 5: extra points a waiver claim must clear ('inf' never claims)")
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


def prior_season(prior_season_name, scoreset):
    """Last season's totals (the shared draft board), rates per game played (rung 3's prior), and
    rates per team game (the fallback for anyone the projections have not reached yet)."""
    actuals = inputs.load_actuals(prior_season_name)
    goalies = inputs.load_goalie_starts(prior_season_name)
    return (draft_module.prior_season_board(actuals, goalies, scoreset),
            draft_module.prior_season_rate(actuals, goalies, scoreset),
            draft_module.prior_season_team_game_rate(actuals, goalies, scoreset))


def run_one(config, calendar, data, eligibility, scoreset, rungs, replication, verbose_weeks,
            decision_sims=0, params=None):
    from decisionlayer import managers as managers_module

    field = managers_module.build_field(config, scoreset, rungs=rungs,
                                        replication=replication, adddrop_params=params)
    if any(m.rung == 5 for m in field) and params is not None and params.rate_source == "ros"             and data.get("ros") is None:
        raise SystemExit("rung 5 reads rest-of-season projections and none are built -- run "
                         "Projections/ros_train.py --horizon season --predictions-out")
    # Draws are only paid for if a rung on the board actually uses them.
    sims = decision_sims if any(m.rung in (4, 5, 6) for m in field) else 0
    season = engine_module.Season(config, calendar, data, eligibility, scoreset, field,
                                 replication=replication, log_every_week=verbose_weeks,
                                 decision_sims=sims)
    board, rate, forward = data["prior"][scoreset.name]
    return season.run({int(k): float(v) for k, v in board.items()},
                      {int(k): float(v) for k, v in rate.items()},
                      {int(k): float(v) for k, v in forward.items()})


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
                    move_hit_rate=("move_hit_rate", "mean"),
                    realized_gain_per_move=("realized_gain_per_move", "mean"))
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
    report = {"season": args.season, "league": config.name, "rungs": list(rungs),
              "replications": args.replications, "results": {}}
    if 5 in rungs:
        report["adddrop"] = params.describe()
        log.info("rung 5 add/drop: %s", params.describe())

    data["prior"] = {}
    for name in weights:
        scoreset = simlayer.load_scoreset(name)
        data["prior"][scoreset.name] = prior_season(args.prior_season, scoreset)
        log.info("=== %s === skaters score %s | goalies score %s (unpriced: %s)",
                 scoreset.name, scoreset.scored("skaters"), scoreset.scored("goalies"),
                 scoreset.missing("goalies") or "none")
        runs = [run_one(config, calendar, data, eligibility, scoreset, rungs, r,
                        args.verbose_weeks, args.decision_sims, params)
                for r in range(args.replications)]
        table, teams = summarize(runs, scoreset.name)
        print(f"\n{scoreset.name}")
        print(table[["rung", "strategy", "win_rate", "points_per_week", "games_started_rate",
                     "decision_efficiency", "empty_slot_nights", "wasted_slot_nights",
                     "moves_spent", "move_hit_rate", "realized_gain_per_move"]]
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

#!/usr/bin/env python
"""Turns a lambda table into distributions -- the CLI for the whole layer.

    Projections/predict.py   lambda per player-game        "he averages 2.6 shots"
    Simulation/simulate.py   a distribution per player-game "he is 27% to be held off the
                                                             sheet and 8% to go for three"

One row out per row in, carrying each category's mean, spread and quantiles, and -- when a
scoring file is supplied -- the same for fantasy points, plus the floor, the ceiling and the
chance of a zero. Several scoring files can be passed at once; they share one set of draws,
so comparing formats costs nothing extra.

Games are drawn whole, in chunks. The correlation structure lives inside a game, so a chunk
boundary must never fall through one, and nothing but a chunk is ever held in memory: a full
season at 1,000 draws is 62M samples per category, which is not something to materialize.

Usage:
    python simulate.py --season 2025-26 --variant A --sims 2000
    python simulate.py --lambdas path/to/lambdas.parquet --sims 5000 \
        --weights points-league --weights banger-league
    python simulate.py --season 2025-26 --date 2026-03-14 --sims 20000 --draws-out day.npz
"""

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

import copula as copula_module
import correlations as correlations_module
import paths
import sampler
import scoring as scoring_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("simulate")

QUANTILES = [0.05, 0.25, 0.5, 0.75, 0.95]
CHUNK_ROWS = 4000


def parse_args():
    parser = argparse.ArgumentParser(description="Simulate a lambda table")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--season", help="Season whose lambda table to simulate")
    source.add_argument("--lambdas", type=Path, help="A lambda parquet to simulate directly")
    parser.add_argument("--variant", choices=("A", "B"), default="A")
    parser.add_argument("--sims", type=int, default=2000)
    parser.add_argument("--date", help="Only this game date (YYYY-MM-DD)")
    parser.add_argument("--from-date", dest="from_date", help="Earliest game date")
    parser.add_argument("--to-date", dest="to_date", help="Latest game date")
    parser.add_argument("--weights", action="append", default=None, metavar="FILE",
                        help="Scoring file to summarize points under; repeat for several")
    parser.add_argument("--list-scoresets", action="store_true")
    parser.add_argument("--independent", action="store_true",
                        help="Sample players independently -- the plan's baseline, for "
                             "contrast only; it understates a stacked roster badly")
    parser.add_argument("--chunk-rows", type=int, default=CHUNK_ROWS)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--draws-out", type=Path, default=None,
                        help="Also write the raw [rows, sims] draws as .npz (one slate at a "
                             "time -- this is not something to do to a season)")
    return parser.parse_args()


def build_simulator(independent=False, seed=17):
    """The calibrated simulator: dispersion from Projections, structure from correlations.py."""
    dispersion = correlations_module.load_dispersion()
    correlations = paths.correlations_path()
    if not correlations.exists():
        raise FileNotFoundError(f"{correlations} is missing -- run correlations.py first")
    payload = json.loads(correlations.read_text(encoding="utf-8"))
    structure = (copula_module.independent() if independent
                 else copula_module.load(correlations))
    penalties = payload["penalty_incidents"]
    return sampler.Simulator(dispersion, structure, penalties["weights"],
                             penalties.get("latent_variance", 0.0), seed=seed)


def load_table(args):
    path = args.lambdas or paths.lambda_table(args.season, args.variant)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing -- build it with Projections/predict.py "
            f"--season {args.season or 'YYYY-YY'} --variant {args.variant}")
    table = pd.read_parquet(path)
    table["game_date"] = pd.to_datetime(table["game_date"])
    if args.date:
        table = table[table["game_date"] == pd.Timestamp(args.date)]
    if args.from_date:
        table = table[table["game_date"] >= pd.Timestamp(args.from_date)]
    if args.to_date:
        table = table[table["game_date"] <= pd.Timestamp(args.to_date)]
    if table.empty:
        raise ValueError("no rows left after the date filters")
    return table.sort_values(["game_date", "game_id", "team_id", "player_id"]).reset_index(drop=True)


def chunks(table, chunk_rows):
    """Row blocks that never split a game, since a game is the unit of correlation."""
    sizes = table.groupby("game_id", sort=False).size()
    current, count = [], 0
    for game, size in sizes.items():
        if current and count + size > chunk_rows:
            yield table[table["game_id"].isin(current)]
            current, count = [], 0
        current.append(game)
        count += size
    if current:
        yield table[table["game_id"].isin(current)]


def summarize(draws, scoresets):
    """One row per player-game: each category's spread, then points per scoring system."""
    out = draws.keys.copy()
    out["p_plays_realized"] = draws.played.mean(axis=1)
    for category in draws.categories():
        values = draws[category].astype("float32")
        out[f"{category}_mean"] = values.mean(axis=1)
        out[f"{category}_sd"] = values.std(axis=1)
        for quantile, column in zip(QUANTILES, np.quantile(values, QUANTILES, axis=1)):
            out[f"{category}_p{int(quantile * 100):02d}"] = column
    for scoreset in scoresets:
        points = scoreset.score_draws(draws)
        name = scoreset.name
        out[f"{name}_mean"] = points.mean(axis=1)
        out[f"{name}_sd"] = points.std(axis=1)
        for quantile, column in zip(QUANTILES, np.quantile(points, QUANTILES, axis=1)):
            out[f"{name}_p{int(quantile * 100):02d}"] = column
        out[f"{name}_p_zero"] = (points <= 0).mean(axis=1)
        # "Boom" is deliberately relative to the player's own projection rather than to a
        # league-wide number: a ceiling game for a fourth-liner is not a ceiling game for a
        # first-line winger, and a start/sit decision is made between specific players.
        out[f"{name}_p_double"] = (points >= 2 * points.mean(axis=1, keepdims=True)).mean(axis=1)
    return out


def run(args):
    table = load_table(args)
    scoresets = [scoring_module.load(w) for w in (args.weights or [])]
    simulator = build_simulator(args.independent, args.seed)
    log.info("simulating %d player-games x %d draws%s", len(table), args.sims,
             (" under " + ", ".join(s.name for s in scoresets)) if scoresets else "")

    pieces, saved = [], None
    for block in chunks(table, args.chunk_rows):
        draws = simulator.draw(block, args.sims)
        pieces.append(summarize(draws, scoresets))
        if args.draws_out is not None and saved is None:
            saved = draws
    summary = pd.concat(pieces, ignore_index=True)

    label = args.season or Path(args.lambdas).stem
    destination = args.out or paths.ensure(paths.REPORTS_DIR) / f"simulated_{label}.parquet"
    summary.to_parquet(destination, index=False)
    log.info("wrote %s: %d rows x %d columns", destination, len(summary), summary.shape[1])

    if args.draws_out is not None:
        if len(pieces) > 1:
            log.warning("--draws-out keeps only the first chunk (%d rows); narrow the date "
                        "filter to get a whole slate in one", saved.n_rows)
        np.savez_compressed(args.draws_out, played=saved.played,
                            **{c: saved[c] for c in saved.categories()},
                            player_id=saved.keys["player_id"].to_numpy(),
                            game_id=saved.keys["game_id"].to_numpy())
        log.info("wrote %s", args.draws_out)
    return summary


def main():
    args = parse_args()
    if args.list_scoresets:
        for path in scoring_module.available():
            scoreset = scoring_module.load(path)
            print(f"{scoreset.name:16s} {scoreset.description}")
            print(f"{'':16s} {scoreset.describe()}")
        return
    run(args)


if __name__ == "__main__":
    main()

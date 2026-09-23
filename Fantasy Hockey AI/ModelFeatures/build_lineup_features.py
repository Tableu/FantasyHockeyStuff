#!/usr/bin/env python
"""Builds the lockout-time lineup feature table (lineups/features.py) for the
projection models, in variant A (previous game's lineup) or B (this game's lineup with
calibrated noise, lineups/perturb.py), from Lineups.GameLineups. Writes
data/lineups/features_{variant}_{season}.parquet (gitignored; regenerate whenever the
derivation or the feature set changes).

Variant B's noise rates come from measured game-to-game churn (lineups/
calibration.py) unless --rates points at a JSON file -- which is where the measured
Daily Faceoff discrepancy goes once the live snapshot job has produced it. --calibrate
alone just measures and writes data/lineups/churn_{season}.json.

Usage:
    python build_lineup_features.py --season 2025-26 --calibrate
    python build_lineup_features.py --season 2025-26 --variant A
    python build_lineup_features.py --season 2025-26 --variant B --copies 3 [--seed 0] [--rates data/lineups/dfo_rates.json]
"""

import argparse
import json
import logging

import nhlstats_db
import paths
from lineups import calibration, features

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_lineup_features")

DATA_DIR = paths.LINEUPS_DIR


def parse_args():
    parser = argparse.ArgumentParser(description="Build lineup feature variants")
    parser.add_argument("--season", action="append", required=True, metavar="YYYY-YY", help="Season(s) to build (repeatable)")
    parser.add_argument("--variant", choices=["A", "B"], default=None)
    parser.add_argument("--copies", type=int, default=1, help="Noisy draws per game (variant B)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--rates", default=None, help="JSON of perturb rates; default: measured churn")
    parser.add_argument("--calibrate", action="store_true", help="Measure churn and write churn_{season}.json")
    return parser.parse_args()


def main():
    args = parse_args()
    conn = nhlstats_db.connect()
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT SeasonID, DisplayName FROM Reference.Seasons WHERE DisplayName IN ({','.join('?' * len(args.season))})",
        *args.season,
    )
    seasons = {r.DisplayName: r.SeasonID for r in cursor.fetchall()}
    missing = set(args.season) - set(seasons)
    if missing:
        raise SystemExit(f"Unknown season(s): {sorted(missing)}")
    season_ids = list(seasons.values())
    tag = "_".join(sorted(seasons))
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    rates = None
    if args.calibrate or (args.variant == "B" and not args.rates):
        churn = calibration.measure_churn(cursor, season_ids)
        rates = calibration.perturb_rates(churn)
        out = DATA_DIR / f"churn_{tag}.json"
        out.write_text(json.dumps({"churn": churn, "perturb_rates": rates}, indent=2), encoding="utf-8")
        log.info("Churn over %d team-game pairs written to %s", churn["team_game_pairs"], out)
        for k, v in churn.items():
            if k not in ("counts", "forward_line_move_distribution"):
                log.info("  %-32s %s", k, v)
    if args.rates:
        rates = json.loads(open(args.rates, encoding="utf-8").read())
        rates = rates.get("perturb_rates", rates)

    if args.variant:
        frame = features.build_lineup_features(cursor, season_ids, args.variant, rates=rates, copies=args.copies, seed=args.seed)
        out = DATA_DIR / f"features_{args.variant}_{tag}.parquet"
        frame.to_parquet(out, index=False)
        log.info("Variant %s: %d rows x %d columns -> %s", args.variant, len(frame), frame.shape[1], out)


if __name__ == "__main__":
    main()

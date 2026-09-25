#!/usr/bin/env python
"""Builds tonight's feature rows from the live reports (tonight.py) and writes them for
Projections/project_tonight.py:

    data/live/tonight_{date}_skaters.parquet   the assembled skater table, variant-B shaped
    data/live/tonight_{date}_goalies.parquet   goalie candidate rows (for P(start))
    data/live/tonight_{date}_context.parquet   per team-game: lineup source, chart, goalie report
    data/live/tonight_{date}_questionable.json {PlayerID: "DTD" | "GTD"}
    data/live/tonight_{date}_status.parquet    every reported player's merged status at --at

Rebuilt before each lineup window, so each run overwrites the date's files; the durable record
is the Live schema itself. Read-only like the rest of ModelFeatures.

Usage:
    python build_tonight.py                                   # today, as of now
    python build_tonight.py --date 2026-10-01
    python build_tonight.py --date 2026-01-15 --at "2026-01-15 17:00"   # replay (UTC)
"""

import argparse
import datetime as dt
import json
import logging

import nhlstats_db
import paths
import tonight

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_tonight")

LIVE_DIR = paths.DATA_DIR / "live"


def output_paths(game_date: dt.date) -> dict:
    stem = f"tonight_{game_date.isoformat()}"
    return {"skaters": LIVE_DIR / f"{stem}_skaters.parquet", "goalies": LIVE_DIR / f"{stem}_goalies.parquet",
            "context": LIVE_DIR / f"{stem}_context.parquet", "questionable": LIVE_DIR / f"{stem}_questionable.json",
            "status": LIVE_DIR / f"{stem}_status.parquet"}


def main():
    parser = argparse.ArgumentParser(description="Build tonight's feature rows from the live reports")
    parser.add_argument("--date", default=None, help="game date (default: today on this PC)")
    parser.add_argument("--at", default=None, help="UTC moment the reports are read as of (default: now)")
    args = parser.parse_args()
    game_date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    at = (dt.datetime.fromisoformat(args.at) if args.at
          else dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0))

    cursor = nhlstats_db.connect().cursor()
    out = tonight.build(cursor, game_date, at)
    if not out:
        return
    paths.ensure(LIVE_DIR)
    files = output_paths(game_date)
    out["skaters"].to_parquet(files["skaters"], index=False)
    out["goalies"].to_parquet(files["goalies"], index=False)
    out["context"].assign(built_at=at).to_parquet(files["context"], index=False)
    out["status"].to_parquet(files["status"], index=False)
    files["questionable"].write_text(json.dumps({str(k): v for k, v in out["questionable"].items()}),
                                     encoding="utf-8")
    log.info("wrote %s", ", ".join(p.name for p in files.values()))


if __name__ == "__main__":
    main()

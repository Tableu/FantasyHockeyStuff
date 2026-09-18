#!/usr/bin/env python
"""Derives Lineups.GameLineups (see nhl_pipeline/calc/lineups.py) for games already in the
database -- the LINEUPS stage only runs inside run_daily's per-game pipeline, so games
ingested before it existed, or a change to the derivation, need this pass. Everything is
computed from Game.Shifts / Game.Plays / Stats.PlayerGameStats already on hand: no API calls.

Rerunnable: each game's rows are replaced.

Usage:
    python backfill_lineups.py                       # every game in the database
    python backfill_lineups.py --season 2025-26      # one season
    python backfill_lineups.py --missing-only        # only games with no lineup rows yet
"""

import argparse
import logging

from nhl_pipeline import db
from nhl_pipeline.calc import lineups, situation_resolver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_lineups")


def parse_args():
    parser = argparse.ArgumentParser(description="Derive lineups for ingested games")
    parser.add_argument("--season", default=None, metavar="YYYY-YY", help="Only this season's games")
    parser.add_argument("--missing-only", action="store_true", help="Skip games that already have lineup rows")
    return parser.parse_args()


def main():
    args = parse_args()
    conn = db.connect()
    cursor = conn.cursor()

    sql = "SELECT g.GameID, g.NHLGameID FROM Game.Games g JOIN Reference.Seasons s ON s.SeasonID = g.SeasonID WHERE 1 = 1"
    params = []
    if args.season:
        sql += " AND s.DisplayName = ?"
        params.append(args.season)
    if args.missing_only:
        sql += " AND NOT EXISTS (SELECT 1 FROM Lineups.GameLineups l WHERE l.GameID = g.GameID)"
    cursor.execute(sql + " ORDER BY g.GameDate, g.NHLGameID", params)
    games = cursor.fetchall()
    log.info("%d game(s) to derive", len(games))

    code_map = situation_resolver.load_situation_code_map(cursor)
    failures = 0
    for i, game in enumerate(games, start=1):
        try:
            lineups.compute_and_store(cursor, game.GameID, code_map)
            conn.commit()
        except Exception:
            conn.rollback()
            failures += 1
            log.exception("FAILED game %s", game.NHLGameID)
        if i % 100 == 0:
            log.info("  %d / %d", i, len(games))

    log.info("Done: %d game(s), %d failure(s)", len(games), failures)


if __name__ == "__main__":
    main()

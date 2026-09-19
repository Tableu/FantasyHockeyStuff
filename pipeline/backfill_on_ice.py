#!/usr/bin/env python
"""Re-derives Game.PlayOnIcePlayers and the analytics that depend on it, for games already
in the database.

Needed after a change to the shifts-to-plays interval join in ingest/on_ice.py: that join
used to close both ends of the shift interval, which credited the player leaving the ice and
the one arriving whenever an event landed exactly on a shift boundary (6.104 players per
shot event per team, 6.65% of them above 6). The half-open version measures 5.887 / 0.02%,
and the inflated attributions had flowed into Analytics.PlayerGameOnIceStats.

Everything is recomputed from Game.Shifts / Game.Plays / Game.Shots already on hand: no API
calls. Only the on-ice chain needs it -- goalie_stats reads Game.Shots and
Analytics.ShotExpectedGoals, and strength_toi / calc.lineups read Game.Shifts directly, so
none of those are re-run. individual_stats is, because IndividualCorsiForPct is defined
against the player's own on-ice CorsiFor.

Rerunnable, and safe to interrupt: each game is committed on its own.

Usage:
    python backfill_on_ice.py                     # every game in the database
    python backfill_on_ice.py --season 2025-26    # one season
    python backfill_on_ice.py --dry-run           # report the games it would touch
"""

import argparse
import logging

from nhl_pipeline import db
from nhl_pipeline.calc import individual_stats, on_ice_stats
from nhl_pipeline.ingest import on_ice

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_on_ice")


def parse_args():
    parser = argparse.ArgumentParser(description="Re-derive on-ice players and the analytics above them")
    parser.add_argument("--season", default=None, metavar="YYYY-YY", help="Only this season's games")
    parser.add_argument("--dry-run", action="store_true", help="List the games, write nothing")
    return parser.parse_args()


def main():
    args = parse_args()
    conn = db.connect()
    cursor = conn.cursor()

    sql = ("SELECT g.GameID, g.NHLGameID FROM Game.Games g "
           "JOIN Reference.Seasons s ON s.SeasonID = g.SeasonID WHERE 1 = 1")
    params = []
    if args.season:
        sql += " AND s.DisplayName = ?"
        params.append(args.season)
    cursor.execute(sql + " ORDER BY g.GameDate, g.NHLGameID", params)
    games = cursor.fetchall()
    log.info("%d game(s) to re-derive", len(games))

    if args.dry_run:
        for game in games[:10]:
            log.info("  would re-derive %s", game.NHLGameID)
        log.info("  (%d total)", len(games))
        return

    corsi_version_id = db.fetch_scalar(
        cursor, "SELECT CalculationVersionID FROM Analytics.CalculationVersions WHERE MetricCode = 'CORSI' AND IsActive = 1"
    )
    xg_version_id = db.fetch_scalar(
        cursor, "SELECT CalculationVersionID FROM Analytics.CalculationVersions WHERE MetricCode = 'XG' AND IsActive = 1"
    )

    failures = 0
    for i, game in enumerate(games, start=1):
        try:
            unmapped: set = set()
            on_ice.derive_on_ice_players(cursor, game.GameID)
            on_ice_stats.compute_and_store(cursor, game.GameID, corsi_version_id, xg_version_id, unmapped)
            individual_stats.compute_and_store(cursor, game.GameID, corsi_version_id, xg_version_id, unmapped)
            conn.commit()
        except Exception:
            conn.rollback()
            failures += 1
            log.exception("FAILED game %s", game.NHLGameID)
        if i % 200 == 0:
            log.info("  %d / %d", i, len(games))

    log.info("Done: %d game(s), %d failure(s)", len(games), failures)


if __name__ == "__main__":
    main()

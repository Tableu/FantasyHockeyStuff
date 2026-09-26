#!/usr/bin/env python
"""The team each player is signed with, into Reference.PlayerTeamHistory (see
nhl_pipeline/ingest/player_teams.py; plan ~/.claude/plans/nhl-roster-sync.md).

Two steps, each its own transaction:

1. backfill_unresolved_players: every name a source could not match is looked up in the NHL
   player search and added with its real NHL id -- how a signing who has not played yet gets a
   row (see nhl_pipeline/ingest/player_backfill.py);
2. the team check: each projected or league-pool player's NHL page, about 1,500 players and 12
   minutes.

Run daily at 05:00 by Task Scheduler (FantasyHockey-PlayerTeams, run_player_teams.cmd, which
then re-exports ModelFeatures/build_players.py).

Usage:
    python import_player_teams.py
    python import_player_teams.py --dry-run --limit 50     # the same run on 50 players, rolled back
    python import_player_teams.py --skip-backfill
"""

import argparse
import logging
import sys
from datetime import date

from nhl_pipeline import config, db
from nhl_pipeline.ingest import player_backfill, player_teams
from nhl_pipeline.ingest.season import ensure_season

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_player_teams")

# The live injury and line-chart snapshots queue their misses in Injuries.UnresolvedPlayerNames.
UNRESOLVED_TABLES = ["Fantasy.UnresolvedPlayerNames", "Projections.UnresolvedPlayerNames",
                     "Injuries.UnresolvedPlayerNames"]


def main():
    parser = argparse.ArgumentParser(description="Import each player's current NHL team")
    parser.add_argument("--dry-run", action="store_true", help="Run everything, then roll back")
    parser.add_argument("--limit", type=int, default=None, help="Check only the first N players")
    parser.add_argument("--skip-backfill", action="store_true",
                        help="Skip adding unresolved names from the NHL player search")
    parser.add_argument("--as-of", default=None, help="Stint date, YYYY-MM-DD (default today)")
    args = parser.parse_args()

    conn = db.connect()
    cursor = conn.cursor()
    season_id = ensure_season(cursor, config.load_season_config())
    conn.commit()
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()

    failures = 0
    backfilled = set()
    if not args.skip_backfill:
        try:
            for table in UNRESOLVED_TABLES:
                backfilled |= player_backfill.backfill_unresolved_names(cursor, table)["player_ids"]
            conn.rollback() if args.dry_run else conn.commit()
        except Exception:
            conn.rollback()
            failures += 1
            log.exception("backfill of unresolved names failed -- continuing with the team check")

    if args.dry_run:
        backfilled = set()      # rolled back above: those rows are not there to check
    counts = player_teams.sync_player_teams(cursor, season_id, as_of, limit=args.limit,
                                            also=backfilled)
    if args.dry_run:
        conn.rollback()
        log.info("Dry run: rolled back, nothing written.")
    else:
        conn.commit()
        log.info("Done.")
    sys.exit(1 if failures or counts["failed"] else 0)


if __name__ == "__main__":
    main()

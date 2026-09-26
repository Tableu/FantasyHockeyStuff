#!/usr/bin/env python
"""Imports every player's regular-season totals from the NHL.com stats reports into
Stats.SeasonTotals (see nhl_pipeline/ingest/season_totals.py; plan
~/.claude/plans/boom-bust-odds.md). Players the database has never seen are added to
Reference.Players under their NHL id.

Each season is its own transaction and replaces that season's rows, so a failed season can be
re-run alone. About 5 requests per season.

Usage:
    python import_season_totals.py                          # 2000-01 .. 2025-26
    python import_season_totals.py --first 2010-11 --last 2012-13
    python import_season_totals.py --dry-run                # the same run, rolled back
"""

import argparse
import logging
import sys

from nhl_pipeline import db
from nhl_pipeline.ingest import season_totals
from nhl_pipeline.ingest.season import ensure_season

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_season_totals")

LOCKOUT_SEASONS = {20042005}


def season_ids(first: str, last: str) -> list:
    start, end = int(first[:4]), int(last[:4])
    ids = [year * 10000 + year + 1 for year in range(start, end + 1)]
    return [s for s in ids if s not in LOCKOUT_SEASONS]


def display(nhl_season_id: int) -> str:
    start = nhl_season_id // 10000
    return f"{start}-{str(start + 1)[2:]}"


def main():
    parser = argparse.ArgumentParser(description="Import NHL season totals")
    parser.add_argument("--first", default="2000-01", help="First season, e.g. 2000-01")
    parser.add_argument("--last", default="2025-26", help="Last season, e.g. 2025-26")
    parser.add_argument("--dry-run", action="store_true", help="Run everything, then roll back")
    args = parser.parse_args()

    conn = db.connect()
    cursor = conn.cursor()

    failures = 0
    for nhl_season_id in season_ids(args.first, args.last):
        name = display(nhl_season_id)
        try:
            season_totals.ensure_table(cursor)      # inside the season: a dry run rolls it back
            season_id = ensure_season(cursor, {"SeasonID_NHL": nhl_season_id, "DisplayName": name})
            counts = season_totals.sync_season(cursor, season_id, nhl_season_id)
            conn.rollback() if args.dry_run else conn.commit()
            log.info("%s: %d skaters, %d goalies, %d players added to Reference.Players, "
                     "%d birth dates filled", name, counts["skaters"], counts["goalies"],
                     len(counts["players_added"]), counts["birth_dates_filled"])
        except Exception:
            conn.rollback()
            failures += 1
            log.exception("%s failed -- rolled back", name)
    if args.dry_run:
        log.info("Dry run: rolled back, nothing written.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

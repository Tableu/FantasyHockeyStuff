#!/usr/bin/env python
"""Imports position eligibility into Fantasy.PlayerPositions from a real public NHL league
on Fleaflicker (see nhl_pipeline/ingest/fantasy_fleaflicker.py for why a specific league,
FLEAFLICKER_PROXY_LEAGUE_ID -- Fleaflicker has no platform-wide player pool the way ESPN
does). ADP is deliberately not pulled from here: a single league's draft data would be a far
worse stand-in for "Fleaflicker's ADP" than one league's position eligibility is for
"Fleaflicker's positions" (eligibility is close to just the player's real position; ADP is
inherently about aggregate draft behavior, which one small league can't represent).

Rerunnable: each run fully replaces this platform's PlayerPositions rows for the season (not
a pure upsert -- see fantasy_fleaflicker.py's docstring for why).

It also records each resolved player's Fleaflicker id in Fantasy.PlatformPlayerIDs, which the
draft assistant uses to name a pick exactly.

Usage:
    python import_fantasy_fleaflicker.py                    # league 12090, the user's league
    python import_fantasy_fleaflicker.py --league-id 100    # the old proxy league
    python import_fantasy_fleaflicker.py --dry-run          # the same run, rolled back
"""

import argparse
import logging

from nhl_pipeline import db
from nhl_pipeline.ingest import fantasy_fleaflicker
from nhl_pipeline.ingest.season import ensure_season

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_fantasy_fleaflicker")

# Same 2026-27 season used by import_projections.py / import_fantasy_espn.py.
SEASON_CFG = {"SeasonID_NHL": 20262027, "DisplayName": "2026-27"}


def main():
    parser = argparse.ArgumentParser(description="Import Fleaflicker positions and player ids")
    parser.add_argument("--league-id", type=int, default=fantasy_fleaflicker.FLEAFLICKER_LEAGUE_ID)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = db.connect()
    cursor = conn.cursor()

    season_id = ensure_season(cursor, SEASON_CFG)
    conn.commit()

    log.info("Fetching Fleaflicker league %d player positions...", args.league_id)
    fantasy_fleaflicker.sync_fleaflicker(cursor, season_id, league_id=args.league_id)
    if args.dry_run:
        conn.rollback()
        log.info("Dry run: rolled back, nothing written.")
    else:
        conn.commit()
        log.info("Done.")


if __name__ == "__main__":
    main()

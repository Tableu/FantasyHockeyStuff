#!/usr/bin/env python
"""Imports Old Time Hockey's ADP (roldtimehockey.com/adp, the r/OldTimeHockey Fleaflicker leagues'
drafts) into Fantasy.PlayerADP under the platform "OldTimeHockey" (see
nhl_pipeline/ingest/fantasy_oldtimehockey.py). ADP only -- the leagues' positions are Fleaflicker's.

Players are matched by their Fleaflicker id, so run import_fantasy_fleaflicker.py for the season
first. Rerunnable: each run replaces the season's rows, so run it again as more leagues draft.
Then export it for the draft tools:

    python import_fantasy_oldtimehockey.py
    python import_fantasy_oldtimehockey.py --dry-run     # the same run, rolled back
    (ModelFeatures) python build_fantasy_positions.py --platform OldTimeHockey --season 2026-27 --adp-only
"""

import argparse
import logging

from nhl_pipeline import db
from nhl_pipeline.ingest import fantasy_oldtimehockey
from nhl_pipeline.ingest.season import ensure_season

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_fantasy_oldtimehockey")

# Same 2026-27 season used by import_projections.py/import_fantasy_espn.py.
SEASON_CFG = {"SeasonID_NHL": 20262027, "DisplayName": "2026-27"}


def main():
    parser = argparse.ArgumentParser(description="Import Old Time Hockey ADP")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = db.connect()
    cursor = conn.cursor()

    season_id = ensure_season(cursor, SEASON_CFG)
    conn.commit()

    fantasy_oldtimehockey.sync_oldtimehockey(cursor, season_id, SEASON_CFG["SeasonID_NHL"] // 10000)
    if args.dry_run:
        conn.rollback()
        log.info("Dry run: rolled back, nothing written.")
    else:
        conn.commit()
        log.info("Done.")


if __name__ == "__main__":
    main()

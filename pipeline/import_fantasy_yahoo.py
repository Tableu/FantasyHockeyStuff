#!/usr/bin/env python
"""Imports Yahoo's fantasy position eligibility + ADP (from Sheets/dailyfaceoff yahoo.csv)
into Fantasy.PlayerADP / Fantasy.PlayerPositions (see nhl_pipeline/ingest/fantasy_yahoo.py).

Rerunnable: everything is upserted, so this can be run periodically as the sheet refreshes.

Usage:
    python import_fantasy_yahoo.py
"""

import logging

from nhl_pipeline import db
from nhl_pipeline.ingest import fantasy_yahoo
from nhl_pipeline.ingest.season import ensure_season

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_fantasy_yahoo")

# Same 2026-27 season used by import_projections.py/import_fantasy_espn.py.
SEASON_CFG = {"SeasonID_NHL": 20262027, "DisplayName": "2026-27"}


def main():
    conn = db.connect()
    cursor = conn.cursor()

    season_id = ensure_season(cursor, SEASON_CFG)
    conn.commit()

    fantasy_yahoo.sync_yahoo(cursor, season_id)
    conn.commit()

    log.info("Done.")


if __name__ == "__main__":
    main()

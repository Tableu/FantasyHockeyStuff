#!/usr/bin/env python
"""Imports Fantrax's fantasy position eligibility + ADP, pulled live from Fantrax's public
getAdp API, into Fantasy.PlayerADP / Fantasy.PlayerPositions (see
nhl_pipeline/ingest/fantasy_fantrax.py).

Rerunnable: each run fully replaces this platform's PlayerADP/PlayerPositions rows for the
season (not a pure upsert -- see fantasy_fantrax.py's docstring for why), so this can be run
periodically as Fantrax's own ADP data updates.

Usage:
    python import_fantasy_fantrax.py
"""

import logging

from nhl_pipeline import db
from nhl_pipeline.ingest import fantasy_fantrax
from nhl_pipeline.ingest.season import ensure_season

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_fantasy_fantrax")

# Same 2026-27 season used by import_projections.py/import_fantasy_espn.py.
SEASON_CFG = {"SeasonID_NHL": 20262027, "DisplayName": "2026-27"}


def main():
    conn = db.connect()
    cursor = conn.cursor()

    season_id = ensure_season(cursor, SEASON_CFG)
    conn.commit()

    fantasy_fantrax.sync_fantrax(cursor, season_id)
    conn.commit()

    log.info("Done.")


if __name__ == "__main__":
    main()

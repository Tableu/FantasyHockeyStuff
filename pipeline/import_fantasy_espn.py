#!/usr/bin/env python
"""Imports ESPN's public NHL fantasy player pool into Fantasy.PlayerADP and
Fantasy.PlayerPositions (see nhl_pipeline/ingest/fantasy_espn.py). No credentials needed --
unlike Yahoo (OAuth app registration) or Fantrax (login cookie), ESPN's player-pool endpoint
is fully public. Fleaflicker is skipped entirely: it has no platform-wide ADP endpoint, only
per-league data.

Rerunnable: everything is upserted, so this can be run periodically as ADP shifts through
the draft season.

Usage:
    python import_fantasy_espn.py [year]
    (year defaults to 2026, ESPN's fantasy-season year for the upcoming 2026-27 season)
"""

import logging
import sys

from nhl_pipeline import db
from nhl_pipeline.ingest import fantasy_espn
from nhl_pipeline.ingest.season import ensure_season

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_fantasy_espn")

DEFAULT_YEAR = 2026

# Same 2026-27 season used by import_projections.py -- see that script for why it's ensured
# here rather than coming from season_config.json (that file tracks the season currently
# being ingested by the live pipeline, not next season).
SEASON_CFG = {"SeasonID_NHL": 20262027, "DisplayName": "2026-27"}


def main():
    year = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_YEAR

    conn = db.connect()
    cursor = conn.cursor()

    season_id = ensure_season(cursor, SEASON_CFG)
    conn.commit()

    log.info("Fetching ESPN %d fantasy hockey player pool...", year)
    fantasy_espn.sync_espn(cursor, year, season_id)
    conn.commit()

    log.info("Done.")


if __name__ == "__main__":
    main()

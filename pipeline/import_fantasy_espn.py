#!/usr/bin/env python
"""Imports ESPN's public NHL fantasy player pool into Fantasy.PlayerADP and
Fantasy.PlayerPositions (see nhl_pipeline/ingest/fantasy_espn.py). No credentials needed --
same as Yahoo's pub-api-ro endpoint, but unlike Fantrax (no public ADP source at all, reads a
workbook instead). Fleaflicker is skipped entirely: it has no platform-wide ADP endpoint, only
per-league data.

Rerunnable: each run fully replaces this platform's PlayerADP/PlayerPositions rows for the
season (not a pure upsert -- see fantasy_espn.py's docstring for why), so this can be run
periodically as ADP shifts through the draft season.

Usage:
    python import_fantasy_espn.py [year]
    (year defaults to 2027 -- ESPN's fantasy API numbers a season by the year it ENDS, so
    the upcoming 2026-27 season is seasons/2027, confirmed by watching the live network calls
    fantasy.espn.com's own Live Draft Trends page makes)
"""

import logging
import sys

from nhl_pipeline import db
from nhl_pipeline.ingest import fantasy_espn
from nhl_pipeline.ingest.season import ensure_season

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_fantasy_espn")

DEFAULT_YEAR = 2027

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

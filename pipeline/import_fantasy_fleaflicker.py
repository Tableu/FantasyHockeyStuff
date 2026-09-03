#!/usr/bin/env python
"""Imports position eligibility into Fantasy.PlayerPositions from a real public NHL league
on Fleaflicker (see nhl_pipeline/ingest/fantasy_fleaflicker.py for why a specific league,
FLEAFLICKER_PROXY_LEAGUE_ID -- Fleaflicker has no platform-wide player pool the way ESPN
does). ADP is deliberately not pulled from here: a single league's draft data would be a far
worse stand-in for "Fleaflicker's ADP" than one league's position eligibility is for
"Fleaflicker's positions" (eligibility is close to just the player's real position; ADP is
inherently about aggregate draft behavior, which one small league can't represent).

Rerunnable: everything is upserted.

Usage:
    python import_fantasy_fleaflicker.py
"""

import logging

from nhl_pipeline import db
from nhl_pipeline.ingest import fantasy_fleaflicker
from nhl_pipeline.ingest.season import ensure_season

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_fantasy_fleaflicker")

# Same 2026-27 season used by import_projections.py / import_fantasy_espn.py.
SEASON_CFG = {"SeasonID_NHL": 20262027, "DisplayName": "2026-27"}


def main():
    conn = db.connect()
    cursor = conn.cursor()

    season_id = ensure_season(cursor, SEASON_CFG)
    conn.commit()

    log.info("Fetching Fleaflicker league %d player positions...", fantasy_fleaflicker.FLEAFLICKER_PROXY_LEAGUE_ID)
    fantasy_fleaflicker.sync_fleaflicker(cursor, season_id)
    conn.commit()

    log.info("Done.")


if __name__ == "__main__":
    main()

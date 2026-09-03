#!/usr/bin/env python
"""Imports the full NHL schedule for a season -- including games that haven't been played
yet -- into Reference.Schedule (see nhl_pipeline/ingest/schedule.py). Every game type
(preseason/regular/playoffs) the schedule endpoint returns is kept; filter by GameType at
query time if only one is wanted.

Rerunnable: everything is upserted, so re-running as the season progresses (scores/states
change, e.g. postponements) just refreshes rows -- particularly useful for the parts of the
season that were still in the future on an earlier run.

Usage:
    python import_schedule.py [start_date] [end_date]
    (both YYYY-MM-DD; default to the 2026-27 season, matching import_projections.py /
    import_fantasy_espn.py / import_fantasy_fleaflicker.py)
"""

import logging
import sys

from nhl_pipeline import db
from nhl_pipeline.ingest import schedule
from nhl_pipeline.ingest.season import ensure_season

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_schedule")

SEASON_CFG = {"SeasonID_NHL": 20262027, "DisplayName": "2026-27"}
# Regular season opens 2026-09-29; started a few weeks earlier to also pick up preseason
# games (kept, not filtered here -- see module docstring) rather than risk clipping the
# actual opener again.
DEFAULT_START_DATE = "2026-09-01"
DEFAULT_END_DATE = "2027-06-30"


def main():
    start_date = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_START_DATE
    end_date = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_END_DATE

    conn = db.connect()
    cursor = conn.cursor()

    season_id = ensure_season(cursor, SEASON_CFG)
    conn.commit()

    log.info("Fetching schedule from %s to %s...", start_date, end_date)
    counts = schedule.sync_schedule(cursor, start_date, end_date, season_id)
    conn.commit()

    log.info("Imported %d game(s)", counts["games"])


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Adds a draft class to Reference.Players. The draft-picks endpoint alone doesn't include a
permanent NHL player ID, so each pick is resolved through api.player_search first (see
nhl_pipeline/ingest/draft.py) -- using the real ID, not a placeholder, so a prospect who
later plays a real NHL game gets upserted onto this same row by the normal ingestion
pipeline rather than creating a duplicate. Also clears out a chunk of
Projections.UnresolvedPlayerNames (see import_projections.py): several 2026 draft picks show
up in this season's projection sheets before they'd otherwise ever get a PlayerID.

Rerunnable: everything is upserted.

Usage:
    python import_draft_class.py [year]
    (year defaults to 2026, the most recently completed draft)
"""

import logging
import sys

from nhl_pipeline import db
from nhl_pipeline.ingest import draft

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_draft_class")

DEFAULT_YEAR = 2026


def main():
    year = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_YEAR

    conn = db.connect()
    cursor = conn.cursor()

    log.info("Fetching %d draft class...", year)
    counts = draft.sync_draft_class(cursor, year)
    conn.commit()

    log.info("Added/updated %d player(s), %d unresolved", counts["added"], counts["unresolved"])


if __name__ == "__main__":
    main()

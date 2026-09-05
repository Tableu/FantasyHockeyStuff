#!/usr/bin/env python
"""Backfills Reference.Players for names that every source's normal resolution turned up
zero local candidates for (Fantasy.UnresolvedPlayerNames / Projections.UnresolvedPlayerNames,
CandidatePlayerIDs IS NULL) -- typically a real, currently-rostered NHL player who simply
hasn't appeared in any game this pipeline has ingested yet (e.g. injured all of a tracked
season), same "has an ID, hasn't dressed" gap import_draft_class.py already solves for draft
picks. See nhl_pipeline/ingest/player_backfill.py.

This only adds rows to Reference.Players -- it does NOT re-run any fantasy/projections
import, so run the relevant import_*.py again afterward to actually pick up ADP/positions
for anyone newly added here.

Rerunnable: everything is upserted.

Usage:
    python backfill_unresolved_players.py
"""

import logging

from nhl_pipeline import db
from nhl_pipeline.ingest import player_backfill

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_unresolved_players")

UNRESOLVED_TABLES = ["Fantasy.UnresolvedPlayerNames", "Projections.UnresolvedPlayerNames"]


def main():
    conn = db.connect()
    cursor = conn.cursor()

    for table in UNRESOLVED_TABLES:
        player_backfill.backfill_unresolved_names(cursor, table)
        conn.commit()

    log.info("Done.")


if __name__ == "__main__":
    main()

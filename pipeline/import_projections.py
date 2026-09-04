#!/usr/bin/env python
"""Imports every fantasy-projection source in Sheets/ into Projections.SkaterProjections /
Projections.GoalieProjections (see nhl_pipeline/projections/). Rerunnable: sources, name
aliases, and projection rows are all upserted, so re-running after a sheet is refreshed just
updates the numbers. Names that don't resolve to exactly one player accumulate in
Projections.UnresolvedPlayerNames for manual review -- see that table's comment in
nhl_database_schema.sql, and nhl_pipeline/projections/name_resolver.py for how resolution
works. Resolving one means adding the correct row to Projections.PlayerNameAliases and
re-running this script.

Usage:
    python import_projections.py
"""

import logging

from nhl_pipeline import config, db
from nhl_pipeline.ingest.season import ensure_season
from nhl_pipeline.projections import importer
from nhl_pipeline.projections.sources import (
    apples_ginos_blake, apples_ginos_nate, dailyfaceoff, dtz, fantrax, lineup_experts,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_projections")

# AggregateWorkbook/Sheets/, not a bare "Sheets/" off the repo root -- that's where every
# source file actually lives (see AggregateWorkbook/Sheets' explicit .gitignore entry).
SHEETS_DIR = config.PROJECT_ROOT / "AggregateWorkbook" / "Sheets"

# All sheets in Sheets/ are 2026-27 projections, a season that hasn't started yet and so
# isn't in Reference.Seasons via the normal ingestion path (season_config.json/ensure_season
# only ever run for the season currently being ingested). Ensured here instead.
PROJECTIONS_SEASON_CFG = {"SeasonID_NHL": 20262027, "DisplayName": "2026-27"}

SOURCES = [
    ("DtZ", dtz, "DtZ 2026-2027 NHL Fantasy Projections"),
    ("Dailyfaceoff", dailyfaceoff, "dailyfaceoff espn.csv"),
    ("Lineup Experts", lineup_experts, "Lineup Experts Hockey Fantasy Draft Cheat Sheet"),
    ("Dom", fantrax, "Fantrax 2026-27 Fantasy Projections, 'The List' sheet"),
    ("Apples & Ginos - Blake", apples_ginos_blake, "Apples & Ginos 2026-27 NHL Skater Projections - Blake"),
    ("Apples & Ginos - Nate", apples_ginos_nate, "Apples & Ginos 2026-27 NHL Skater Projections - Nate"),
]


def main():
    conn = db.connect()
    cursor = conn.cursor()

    season_id = ensure_season(cursor, PROJECTIONS_SEASON_CFG)
    conn.commit()

    for source_name, module, description in SOURCES:
        log.info("Importing %s...", source_name)
        try:
            importer.import_rows(cursor, source_name, season_id, module.rows(SHEETS_DIR), description)
        except FileNotFoundError as exc:
            # Fantrax's own source file is retired (see ACTIVE_SOURCES in
            # build_aggregate_workbook.py) -- not present in Sheets/ until it's manually
            # added back, so a missing file here shouldn't abort every other source's import.
            log.warning("Skipping %s: %s", source_name, exc)
            continue
        conn.commit()

    log.info("Done.")


if __name__ == "__main__":
    main()

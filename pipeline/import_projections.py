#!/usr/bin/env python
"""Imports every fantasy-projection source in ProjectionSheets/ into Projections.SkaterProjections /
Projections.GoalieProjections (see nhl_pipeline/projections/). Rerunnable: sources, name
aliases, and projection rows are all upserted, so re-running after a sheet is refreshed just
updates the numbers. Names that don't resolve to exactly one player accumulate in
Projections.UnresolvedPlayerNames for manual review -- see that table's comment in
nhl_database_schema.sql, and nhl_pipeline/projections/name_resolver.py for how resolution
works. Resolving one means adding the correct row to Projections.PlayerNameAliases and
re-running this script.

Usage:
    python import_projections.py                        # the 2026-27 sheets in ProjectionSheets/
    python import_projections.py --season 2025-26       # the Crome workbook in ProjectionSheets/2025-26/
    python import_projections.py --season 2025-26 --dry-run   # the same run, rolled back

2025-26 is last season's Crome aggregate workbook, one sheet per source
(nhl_pipeline/projections/sources/crome_workbook.py). Its sources are new (source, season) rows
beside the 2026-27 ones -- nothing written for 2025-26 touches a 2026-27 row -- and each is seeded
with its 2026-27 namesake's name aliases and dated from the workbook's SourceCheck tab.
"""

import argparse

import logging

from nhl_pipeline import config, db
from nhl_pipeline.ingest.season import ensure_season
from nhl_pipeline.projections import importer
from nhl_pipeline.projections.sources import (
    apples_ginos_blake, apples_ginos_nate, dailyfaceoff, dtz, dom, kubota_hockey,
    lineup_experts, scott_cullen, steve_laidlaw,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_projections")

# The downloaded source files live in ProjectionSheets/ at the pipeline root (kept out of
# git by pipeline/.gitignore).
SHEETS_DIR = config.PROJECT_ROOT / "ProjectionSheets"

# All sheets in ProjectionSheets/ are 2026-27 projections, a season that hasn't started yet and so
# isn't in Reference.Seasons via the normal ingestion path (season_config.json/ensure_season
# only ever run for the season currently being ingested). Ensured here instead.
PROJECTIONS_SEASON_CFG = {"SeasonID_NHL": 20262027, "DisplayName": "2026-27"}

SOURCES = [
    ("DtZ", dtz, "DtZ 2026-2027 NHL Fantasy Projections"),
    ("Dailyfaceoff", dailyfaceoff, "dailyfaceoff espn.csv"),
    ("Lineup Experts", lineup_experts, "Lineup Experts Hockey Fantasy Draft Cheat Sheet"),
    ("Dom", dom, "Dom's Fantrax 2026-27 Fantasy Projections, 'The List' sheet"),
    ("Apples & Ginos - Blake", apples_ginos_blake, "Apples & Ginos 2026-27 NHL Skater Projections - Blake"),
    ("Apples & Ginos - Nate", apples_ginos_nate, "Apples & Ginos 2026-27 NHL Skater Projections - Nate"),
    ("Scott Cullen", scott_cullen, "Scott Cullen Projections"),
    ("Kubota Hockey", kubota_hockey, "Kubota Hockey 2026-27 Projections"),
    ("Steve Laidlaw", steve_laidlaw, "Steve Laidlaw Fantasy Hockey Rankings"),
]


WORKBOOK_SEASON_CFG = {"SeasonID_NHL": 20252026, "DisplayName": "2025-26"}


def parse_args():
    parser = argparse.ArgumentParser(description="Import fantasy projection sources")
    parser.add_argument("--season", choices=("2026-27", "2025-26"), default="2026-27")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run every read and resolution, then roll back instead of committing")
    return parser.parse_args()


def import_workbook(conn, cursor, dry_run: bool):
    from nhl_pipeline.projections.sources import crome_workbook

    season_id = ensure_season(cursor, WORKBOOK_SEASON_CFG)
    later_id = ensure_season(cursor, PROJECTIONS_SEASON_CFG)
    workbook = crome_workbook.open_workbook(SHEETS_DIR / "2025-26")
    published = crome_workbook.published_dates(workbook)
    totals = []
    for sheet, (source_name, dated_as, _) in crome_workbook.SHEETS.items():
        counts = importer.import_workbook_rows(
            cursor, source_name, season_id, crome_workbook.rows(workbook, sheet),
            description=f"Crome Aggregate Projections 2025-26, '{sheet}' tab",
            published_on=published.get(dated_as), seed_from_season_id=later_id,
            confirmed_aliases={**crome_workbook.CONFIRMED_ALIASES,
                               **{raw: pid for (src, raw), pid in crome_workbook.SOURCE_ALIASES.items()
                                  if src == source_name}})
        sheet_summary = crome_workbook.summary(workbook, sheet)
        totals.append((source_name, sheet_summary, counts, published.get(dated_as)))
        if not dry_run:
            conn.commit()
    print()
    print(f"{'source':24s} {'sheet rows':>10s} {'Crome matched':>13s} {'skaters':>8s} "
          f"{'goalies':>8s} {'by raw':>7s} {'by Crome':>8s} {'unresolved':>10s} published")
    for name, sheet_summary, c, date in totals:
        print(f"{name:24s} {sheet_summary['players'] or 0:10d} "
              f"{(sheet_summary['matched'] or 0) + (sheet_summary['fixed'] or 0):13d} "
              f"{c['skaters']:8d} {c['goalies']:8d} {c['by_raw']:7d} {c['by_crome']:8d} "
              f"{c['unresolved']:10d} {date}")
        if c["unresolved_names"]:
            print(f"    unresolved: {', '.join(c['unresolved_names'][:25])}"
                  f"{' ...' if len(c['unresolved_names']) > 25 else ''}")
    if dry_run:
        conn.rollback()
        log.info("Dry run: rolled back, nothing written.")


def main():
    args = parse_args()
    conn = db.connect()
    cursor = conn.cursor()

    if args.season == "2025-26":
        import_workbook(conn, cursor, args.dry_run)
        log.info("Done.")
        return

    season_id = ensure_season(cursor, PROJECTIONS_SEASON_CFG)
    conn.commit()

    for source_name, module, description in SOURCES:
        log.info("Importing %s...", source_name)
        try:
            importer.import_rows(cursor, source_name, season_id, module.rows(SHEETS_DIR), description)
        except FileNotFoundError as exc:
            # Fantrax's own source file is retired (see ACTIVE_SOURCES in
            # build_aggregate_workbook.py) -- not present in ProjectionSheets/ until it's manually
            # added back, so a missing file here shouldn't abort every other source's import.
            log.warning("Skipping %s: %s", source_name, exc)
            continue
        conn.commit()

    log.info("Done.")


if __name__ == "__main__":
    main()

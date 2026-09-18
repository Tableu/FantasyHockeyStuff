#!/usr/bin/env python
"""Imports the NHL Injury Viz injury database (every regular-season injury spell since
2000-01) into Injuries.Spells -- see nhl_pipeline/ingest/injury_history.py for how team game
numbers become dates, how 25 seasons of player names get resolved, and why the same run also
fills Reference.Schedule (and the defunct franchises in Reference.Teams) for every season it
touches. Needs `pip install tableauhyperapi` to read the workbook's .hyper extract.

Rerunnable: each season's spells are replaced wholesale, everything else is upserted, and
name aliases persist so the NHL player-search fallback only ever runs once per name. Names it
couldn't settle are left in Injuries.UnresolvedPlayerNames -- add the correct row to
Injuries.PlayerNameAliases and re-run that season to fill in the PlayerID.

Usage:
    python import_injury_history.py                       # download the workbook, import every season
    python import_injury_history.py --season 2025-26      # one (or more) season(s) only
    python import_injury_history.py --file path/to/NHLinjurydatabase.twbx   # or a .hyper; skips the download
"""

import argparse
import logging
from pathlib import Path

from nhl_pipeline import db
from nhl_pipeline.api import nhl_injury_viz
from nhl_pipeline.config import PROJECT_ROOT
from nhl_pipeline.ingest import injury_history

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_injury_history")

DATA_DIR = PROJECT_ROOT / "data" / "injuries"


def parse_args():
    parser = argparse.ArgumentParser(description="Import NHL Injury Viz injury history")
    parser.add_argument("--file", type=Path, default=None, help="Local .twbx or .hyper instead of downloading")
    parser.add_argument("--season", action="append", default=None, metavar="YYYY-YY",
                        help="Only import this season (repeatable), e.g. 2025-26")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.file is None:
        hyper_path = nhl_injury_viz.download_workbook(DATA_DIR)
    elif args.file.suffix.lower() == ".hyper":
        hyper_path = args.file
    else:
        hyper_path = nhl_injury_viz.extract_hyper(args.file)

    log.info("Reading %s ...", hyper_path)
    rows = nhl_injury_viz.read_rows(hyper_path)
    log.info("%d row(s) in the extract", len(rows))

    conn = db.connect()
    cursor = conn.cursor()

    def commit_season(season_display):
        conn.commit()
        log.info("Committed %s", season_display)

    counts = injury_history.sync_injury_history(cursor, rows, seasons=args.season, on_season_done=commit_season)
    conn.commit()

    log.info(
        "Wrote %d spell(s) (%d playoff row(s) skipped, %d filtered out, %d left undated); "
        "players resolved locally %d / via NHL search %d / unresolved %d; %d schedule row(s) upserted",
        counts["spells"], counts["playoffs_skipped"], counts["filtered_out"], counts["undated"],
        counts["resolved_local"], counts["resolved_search"], counts["unresolved"], counts["schedule_rows"],
    )
    if counts["unresolved"]:
        log.warning("Review Injuries.UnresolvedPlayerNames, add aliases, and re-run the affected season(s)")


if __name__ == "__main__":
    main()

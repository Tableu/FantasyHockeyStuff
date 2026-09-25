#!/usr/bin/env python
"""Imports Dobber's Band-Aid Boys (DobberHockey's yearly injury-risk list) into Injuries.RiskLists
-- see nhl_pipeline/api/dobber_bandaid.py for the page and its three groups (Certified, Trainee,
Goalie) and nhl_pipeline/ingest/injury_risk.py for name resolution.

Rerunnable: the season's rows are replaced wholesale. A name that could not be settled is left in
Injuries.UnresolvedPlayerNames -- add the right row to Injuries.PlayerNameAliases and re-run.

Usage:
    python import_injury_risk.py                          # 2026-27, the 2026 article
    python import_injury_risk.py --season 2027-28 --url https://dobberhockey.com/...
    python import_injury_risk.py --dry-run                # the same run, rolled back
"""

import argparse
import logging

from nhl_pipeline import db
from nhl_pipeline.api import dobber_bandaid
from nhl_pipeline.ingest import injury_risk
from nhl_pipeline.ingest.season import ensure_season

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_injury_risk")

SOURCE_NAME = "Dobber Band-Aid Boys"


def main():
    parser = argparse.ArgumentParser(description="Import Dobber's Band-Aid Boys")
    parser.add_argument("--season", default="2026-27")
    parser.add_argument("--url", default=None, help="The season's article (default: the known one)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = dobber_bandaid.fetch(args.season, args.url)
    by_tier = {}
    for r in rows:
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
    log.info("%d players on the page: %s", len(rows), by_tier)

    first = int(args.season[:4])
    conn = db.connect()
    cursor = conn.cursor()
    season_id = ensure_season(cursor, {"SeasonID_NHL": first * 10000 + first + 1,
                                       "DisplayName": args.season})
    injury_risk.sync_risk_list(
        cursor, season_id, SOURCE_NAME,
        "DobberHockey's yearly Band-Aid Boys article: Certified, Trainee and Goalie groups",
        rows)
    if args.dry_run:
        conn.rollback()
        log.info("Dry run: rolled back, nothing written.")
    else:
        conn.commit()
        log.info("Done.")


if __name__ == "__main__":
    main()

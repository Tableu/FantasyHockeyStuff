#!/usr/bin/env python
"""Adds the platform ids the preseason imports missed (call-ups), so a live league's rosters name
every player -- see nhl_pipeline/ingest/platform_ids.py. Add-only: nothing already imported is
removed or changed. Runs daily in the player-teams job (Server/scheduled.py), before
build_players.py exports platform_ids.parquet.

Usage:
    python import_platform_ids.py              # this season: ESPN's pool + every active Fleaflicker league
    python import_platform_ids.py --dry-run    # the same run, rolled back
"""

import argparse
import datetime as dt
import json
import logging

from nhl_pipeline import db, fantasy_leagues
from nhl_pipeline.ingest import platform_ids
from nhl_pipeline.ingest.season import ensure_season

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_platform_ids")


def fleaflicker_leagues() -> list:
    """Every active Fleaflicker league in the registry with an id."""
    out = []
    for path in sorted(fantasy_leagues.LEAGUES_DIR.glob("*.json")):
        league = json.loads(path.read_text(encoding="utf-8"))
        if league.get("platform") == "fleaflicker" and league.get("active") and league.get("league_id") is not None:
            out.append(int(league["league_id"]))
    return out


def main():
    parser = argparse.ArgumentParser(description="Add missing ESPN / Fleaflicker player ids")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    today = dt.date.today()
    start = today.year if today.month >= 7 else today.year - 1
    conn = db.connect()
    cursor = conn.cursor()
    season_id = ensure_season(cursor, {"SeasonID_NHL": int(f"{start}{start + 1}"),
                                       "DisplayName": f"{start}-{(start + 1) % 100:02d}"})
    conn.commit()

    platform_ids.add_espn(cursor, start + 1, season_id)       # ESPN numbers a season by the year it ends
    leagues = fleaflicker_leagues()
    if leagues:
        platform_ids.add_fleaflicker(cursor, season_id, leagues)
    if args.dry_run:
        conn.rollback()
        log.info("Dry run: rolled back, nothing written.")
    else:
        conn.commit()
        log.info("Done.")


if __name__ == "__main__":
    main()

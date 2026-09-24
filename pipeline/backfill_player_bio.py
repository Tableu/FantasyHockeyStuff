#!/usr/bin/env python
"""Backfills Reference.Players' bio columns -- BirthDate, HeightInches, WeightLbs, Shoots -- from
the NHL player landing endpoint, for every player missing a birth date.

They were empty for all 4,032 players: nothing in the pipeline ever called that endpoint, which
blocked an age feature downstream (ModelFeatures, and the rest-of-season aging curve).
Players who have appeared in an ingested game go first. See nhl_pipeline/ingest/player_bio.py.

Rerunnable: a player already fetched is skipped (even if the endpoint had no birth date for
him), so a second run makes no API calls; --refetch overrides that.

Usage:
    python backfill_player_bio.py [--limit N] [--refetch]
"""

import argparse
import logging

from nhl_pipeline import db
from nhl_pipeline.ingest import player_bio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_player_bio")

COMMIT_EVERY = 50


def coverage(cursor) -> str:
    cursor.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN p.BirthDate IS NOT NULL THEN 1 ELSE 0 END),
               SUM(CASE WHEN g.PlayerID IS NOT NULL THEN 1 ELSE 0 END),
               SUM(CASE WHEN g.PlayerID IS NOT NULL AND p.BirthDate IS NOT NULL THEN 1 ELSE 0 END)
        FROM Reference.Players p
        LEFT JOIN (SELECT DISTINCT PlayerID FROM Stats.PlayerGameStats) g ON g.PlayerID = p.PlayerID""")
    total, born, played, played_born = cursor.fetchone()
    return (f"BirthDate for {born}/{total} players ({born / max(total, 1):.1%}); "
            f"{played_born}/{played} of those who played an ingested game ({played_born / max(played, 1):.1%})")


def main():
    parser = argparse.ArgumentParser(description="Backfill player bio columns")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--refetch", action="store_true")
    args = parser.parse_args()

    conn = db.connect()
    cursor = conn.cursor()
    todo = player_bio.players_to_fetch(cursor, refetch=args.refetch)
    if args.limit:
        todo = todo[:args.limit]
    log.info("%d player(s) to fetch; before: %s", len(todo), coverage(cursor))

    missing_page, no_birth = [], []
    for i, (player_id, nhl_player_id) in enumerate(todo, start=1):
        fields = player_bio.sync_player_bio(cursor, player_id, nhl_player_id)
        if fields is None:
            missing_page.append(nhl_player_id)
        elif "BirthDate" not in fields:
            no_birth.append(nhl_player_id)
        if i % COMMIT_EVERY == 0:
            conn.commit()
        if i % 250 == 0:
            log.info("%d / %d", i, len(todo))
    conn.commit()

    log.info("after: %s", coverage(cursor))
    if missing_page:
        log.warning("%d player(s) have no landing page: %s", len(missing_page), missing_page[:20])
    if no_birth:
        log.warning("%d player(s) returned no birth date: %s", len(no_birth), no_birth[:20])


if __name__ == "__main__":
    main()

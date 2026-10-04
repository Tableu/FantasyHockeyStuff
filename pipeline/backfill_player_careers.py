#!/usr/bin/env python
"""Backfills every player's career lines -- each season in every league (AHL, CHL, NCAA, Europe,
the NHL), regular season and playoffs -- into his archived landing page
(Ingestion.RawPlayerResponses, EndpointType PLAYER_LANDING, key `seasonTotals`).

The landing fetch kept only the bio and draft keys until 2026-10-03; it now keeps the career lines
too (field_map.PLAYER_LANDING_KEYS / career_rows), so the daily player-teams run and run_daily's
newcomers keep them current. This fills them in for everyone fetched before that. They are the
rookie prior's input: a player with little NHL history has a minor-league or junior one
(Fantasy Hockey AI/Projections, the rest-of-season model).

Players who have appeared in an ingested game go first. Rerunnable: a player whose archive
already has career lines is skipped, so a second run makes no API calls. Each fetch also
re-applies the bio columns, which only ever fills a missing value (player_bio.apply_bio).

Usage:
    python backfill_player_careers.py [--limit N]
"""

import argparse
import logging

from nhl_pipeline import db
from nhl_pipeline.ingest import player_bio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_player_careers")

COMMIT_EVERY = 50


def players_to_fetch(cursor) -> list:
    """(PlayerID, NHLPlayerID) of players whose archived landing page has no career lines yet,
    those who have played an ingested game first."""
    cursor.execute(f"""
        SELECT p.PlayerID, p.NHLPlayerID,
               CASE WHEN EXISTS (SELECT 1 FROM Stats.PlayerGameStats s WHERE s.PlayerID = p.PlayerID)
                    THEN 0 ELSE 1 END AS Priority
        FROM Reference.Players p
        WHERE p.NHLPlayerID IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM Ingestion.RawPlayerResponses r
                          WHERE r.NHLPlayerID = p.NHLPlayerID
                            AND r.EndpointType = '{player_bio.ENDPOINT}'
                            AND r.RawJSON LIKE '%"seasonTotals"%')
        ORDER BY Priority, p.PlayerID""")
    return [(r.PlayerID, r.NHLPlayerID) for r in cursor.fetchall()]


def coverage(cursor) -> str:
    cursor.execute(f"""
        SELECT COUNT(*),
               SUM(CASE WHEN r.RawJSON LIKE '%"seasonTotals"%' THEN 1 ELSE 0 END)
        FROM Reference.Players p
        LEFT JOIN Ingestion.RawPlayerResponses r
               ON r.NHLPlayerID = p.NHLPlayerID AND r.EndpointType = '{player_bio.ENDPOINT}'
        WHERE p.NHLPlayerID IS NOT NULL""")
    total, with_careers = cursor.fetchone()
    with_careers = with_careers or 0
    return f"career lines for {with_careers}/{total} players ({with_careers / max(total, 1):.1%})"


def main():
    parser = argparse.ArgumentParser(description="Backfill players' career lines")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    conn = db.connect()
    cursor = conn.cursor()
    todo = players_to_fetch(cursor)
    if args.limit:
        todo = todo[:args.limit]
    log.info("%d player(s) to fetch; before: %s", len(todo), coverage(cursor))

    missing_page, failed = [], []
    for i, (player_id, nhl_player_id) in enumerate(todo, start=1):
        try:
            kept = player_bio.fetch_landing(cursor, player_id, nhl_player_id)
        except Exception as error:  # noqa: BLE001 -- one bad fetch skips this player only
            failed.append(nhl_player_id)
            log.warning("NHL player %s: fetch failed: %s", nhl_player_id, error)
            continue
        if kept is None:
            missing_page.append(nhl_player_id)
        else:
            player_bio.apply_bio(cursor, player_id, kept)
        if i % COMMIT_EVERY == 0:
            conn.commit()
        if i % 250 == 0:
            log.info("%d / %d", i, len(todo))
    conn.commit()

    log.info("after: %s", coverage(cursor))
    if missing_page:
        log.warning("%d player(s) have no landing page: %s", len(missing_page), missing_page[:20])
    if failed:
        log.warning("%d fetch(es) failed (rerun to retry): %s", len(failed), failed[:20])


if __name__ == "__main__":
    main()

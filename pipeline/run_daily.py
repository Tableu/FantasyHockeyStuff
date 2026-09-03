#!/usr/bin/env python
"""Daily NHL ingestion entry point -- the one command to wire into a scheduler.

Usage:
    python run_daily.py                      # incremental: lookback window, skips already-ingested games
    python run_daily.py --backfill           # force full-season reload (safe: every write is an upsert)
    python run_daily.py --lookback-days 14   # override the default 7-day lookback
    python run_daily.py --game-id 2025020740 # ingest exactly one game, by NHL game id
    python run_daily.py --dry-run            # discovery only, no writes

Exits with status 1 if any game failed, 0 otherwise, so a scheduler can alert on failure.
"""

import argparse
import logging
import sys

from nhl_pipeline import config, db
from nhl_pipeline.api import field_map
from nhl_pipeline.api import play_by_play as api_play_by_play
from nhl_pipeline.ingest import official_stats
from nhl_pipeline.ingest import season as ingest_season
from nhl_pipeline.orchestration import discovery, pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("run_daily")


def parse_args():
    parser = argparse.ArgumentParser(description="Daily NHL ingestion")
    parser.add_argument("--backfill", action="store_true", help="Force full-season reload")
    parser.add_argument("--lookback-days", type=int, default=discovery.DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--game-id", type=int, default=None, help="Ingest exactly one NHL game id")
    parser.add_argument("--dry-run", action="store_true", help="Discovery only, no writes")
    return parser.parse_args()


def main():
    args = parse_args()
    season_cfg = config.load_season_config()
    conn = db.connect()
    cursor = conn.cursor()

    season_id = ingest_season.ensure_season(cursor, season_cfg)
    conn.commit()

    if args.game_id:
        pbp = api_play_by_play.get_play_by_play(args.game_id)
        schedule_game = field_map.schedule_game_from_play_by_play(pbp)
        games_to_run = [(schedule_game, pbp["gameDate"])]
    else:
        games_to_run = discovery.discover_games(
            cursor, season_cfg, season_id, lookback_days=args.lookback_days, backfill=args.backfill
        )

    log.info("Discovered %d game(s) to ingest", len(games_to_run))

    if args.dry_run:
        for game, date_str in games_to_run:
            log.info("  would ingest %s (%s) on %s", game["id"], game.get("gameState"), date_str)
        return

    failures = 0
    for game, date_str in games_to_run:
        nhl_game_id = game["id"]
        log.info("Ingesting game %s (%s)", nhl_game_id, date_str)
        try:
            pipeline.run_game(conn, game, date_str, season_id)
            log.info("  OK")
        except Exception:
            failures += 1
            log.exception("  FAILED game %s -- continuing with next game", nhl_game_id)
            continue

    log.info("Recomputing season totals")
    official_stats.sync_player_season_stats(cursor, season_id)
    conn.commit()

    log.info("Done: %d game(s) processed, %d failure(s)", len(games_to_run), failures)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

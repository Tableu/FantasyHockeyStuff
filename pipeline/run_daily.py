#!/usr/bin/env python
"""Daily NHL ingestion entry point -- the one command to wire into a scheduler.

Each run first refreshes Reference.Schedule from the start of the lookback window through
the end of the season (so postponements rescheduled weeks out are picked up), then ingests
any newly-final games.

Usage:
    python run_daily.py                      # incremental: lookback window, skips already-ingested games
    python run_daily.py --backfill           # force full-season reload (safe: every write is an upsert)
    python run_daily.py --lookback-days 14   # override the default 7-day lookback
    python run_daily.py --game-id 2025020740 # ingest exactly one game, by NHL game id (no schedule refresh)
    python run_daily.py --dry-run            # discovery only, no writes (no schedule refresh)
    python run_daily.py --season 20242025    # backfill a past season instead of season_config.json's
                                             # (date range from Reference.Schedule; no schedule refresh)

Exits with status 1 if any game (or the schedule refresh) failed, 0 otherwise, so a
scheduler can alert on failure.
"""

import argparse
import logging
import sys
from datetime import date, datetime, timedelta

from nhl_pipeline import config, db
from nhl_pipeline.api import field_map
from nhl_pipeline.api import play_by_play as api_play_by_play
from nhl_pipeline.ingest import official_stats
from nhl_pipeline.ingest import schedule as ingest_schedule
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
    parser.add_argument("--season", type=int, default=None, metavar="YYYYYYYY",
                        help="Backfill this past season (NHL season id, e.g. 20242025) instead of season_config.json")
    return parser.parse_args()


def season_config_from_schedule(cursor, nhl_season_id: int) -> dict:
    """A season_config.json-shaped dict for a past season, bounded by that season's
    regular-season games in Reference.Schedule (filled for every season back to 2000-01 by
    import_injury_history.py) so nothing has to be hand-written per season."""
    cursor.execute(
        "SELECT MIN(sc.GameDate), MAX(sc.GameDate) FROM Reference.Schedule sc "
        "JOIN Reference.Seasons s ON s.SeasonID = sc.SeasonID WHERE s.NHLSeasonID = ? AND sc.GameType = '2'",
        nhl_season_id,
    )
    start, end = cursor.fetchone()
    if start is None:
        raise SystemExit(f"Reference.Schedule has no regular-season games for {nhl_season_id} -- import the schedule first")
    start_year = str(nhl_season_id)[:4]
    return {
        "SeasonID_NHL": nhl_season_id,
        "DisplayName": f"{start_year}-{str(nhl_season_id)[6:]}",
        "StartDate": start.isoformat(),
        "EndDate": end.isoformat(),
        "IncludeGameTypes": [2],
    }


def schedule_refresh_window(season_cfg: dict, lookback_days: int, backfill: bool) -> tuple:
    start_cfg = datetime.strptime(season_cfg["StartDate"], "%Y-%m-%d").date()
    start = start_cfg if backfill else max(start_cfg, date.today() - timedelta(days=lookback_days))
    return start.isoformat(), season_cfg["EndDate"]


def main():
    args = parse_args()
    conn = db.connect()
    cursor = conn.cursor()

    if args.season:
        # A past season is always a full reload, and its schedule is already on hand.
        season_cfg = season_config_from_schedule(cursor, args.season)
        args.backfill = True
    else:
        season_cfg = config.load_season_config()

    season_id = ingest_season.ensure_season(cursor, season_cfg)
    conn.commit()

    failures = 0

    if not args.game_id and not args.dry_run and not args.season:
        start_date, end_date = schedule_refresh_window(season_cfg, args.lookback_days, args.backfill)
        log.info("Refreshing schedule from %s to %s", start_date, end_date)
        try:
            counts = ingest_schedule.sync_schedule(cursor, start_date, end_date, season_id)
            conn.commit()
            log.info("  %d schedule game(s) refreshed", counts["games"])
        except Exception:
            conn.rollback()
            failures += 1
            log.exception("  FAILED schedule refresh -- continuing with game ingestion")

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

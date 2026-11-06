"""Decides which games to ingest today: full-season backfill on the very first run for a
season, otherwise a rolling lookback window (to absorb postponed games, API data lag and stat
corrections). Only games that are officially final (gameState == "OFF") are taken; one already
ingested is taken again only if the NHL has since changed its players' stat lines, so this is
always safe to re-run.
"""

import json
import logging
from datetime import date, datetime, timedelta

from nhl_pipeline.api import boxscore, schedule
from nhl_pipeline.orchestration.logging_utils import needs_ingestion
from nhl_pipeline.orchestration.pipeline import TERMINAL_STAGE

FINAL_STATE = "OFF"
DEFAULT_LOOKBACK_DAYS = 7

log = logging.getLogger("discovery")


def stats_corrected(cursor, nhl_game_id: int) -> bool:
    """Whether the NHL's box score now gives other player stat lines than the one last ingested:
    a stat correction, hours or days after the final (2026-11-02: Sherwood's assist moved to Misa).
    A failed read is no change; the next run asks again."""
    cursor.execute(
        """
        SELECT r.RawJSON FROM Ingestion.RawApiResponses r
        JOIN Game.Games g ON g.GameID = r.GameID
        WHERE g.NHLGameID = ? AND r.EndpointType = 'BOXSCORE'
        """,
        nhl_game_id,
    )
    row = cursor.fetchone()
    if row is None:
        return True
    try:
        now = boxscore.get_boxscore(nhl_game_id)
    except Exception:
        log.exception("Could not check game %s for stat corrections", nhl_game_id)
        return False
    changed = now.get("playerByGameStats") != json.loads(row[0]).get("playerByGameStats")
    if changed:
        log.info("Game %s: stat lines changed since ingestion", nhl_game_id)
    return changed


def _date_range(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _season_has_any_games(cursor, season_id: int) -> bool:
    cursor.execute("SELECT TOP 1 GameID FROM Game.Games WHERE SeasonID = ?", season_id)
    return cursor.fetchone() is not None


def discover_games(cursor, season_cfg: dict, season_id: int, lookback_days: int = DEFAULT_LOOKBACK_DAYS, backfill: bool = False):
    """Returns a list of (schedule_game_dict, date_str) tuples ready for pipeline.run_game."""
    today = date.today()
    start_cfg = datetime.strptime(season_cfg["StartDate"], "%Y-%m-%d").date()
    end_cfg = datetime.strptime(season_cfg["EndDate"], "%Y-%m-%d").date()

    if backfill or not _season_has_any_games(cursor, season_id):
        start = start_cfg
        end = min(today, end_cfg)
    else:
        start = max(start_cfg, today - timedelta(days=lookback_days))
        end = min(today, end_cfg)

    include_game_types = set(season_cfg.get("IncludeGameTypes", [2]))

    candidates = []
    for day in _date_range(start, end):
        date_str = day.isoformat()
        for game in schedule.get_games_for_date(date_str):
            if game.get("gameState") != FINAL_STATE:
                continue
            if game.get("gameType") not in include_game_types:
                continue
            candidates.append((game, date_str))

    if backfill:
        # Reprocess everything in range unconditionally -- this is how a fix to the
        # parsing/calc logic gets applied to already-ingested games, safe because every
        # write downstream is an upsert.
        return candidates

    return [
        (game, date_str)
        for game, date_str in candidates
        if needs_ingestion(cursor, game["id"], [TERMINAL_STAGE]) or stats_corrected(cursor, game["id"])
    ]

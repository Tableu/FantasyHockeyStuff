#!/usr/bin/env python
"""One-off/rerunnable patcher: fills Stats.PlayerGameStats.PowerPlayTOISeconds/
ShortHandedTOISeconds for every already-ingested game using calc.strength_toi (added after
those games were first ingested), then resyncs Stats.PlayerSeasonStats so the rollup picks
up the new per-game values.

Usage:
    python backfill_strength_toi.py
"""

import logging

from nhl_pipeline import db
from nhl_pipeline.calc import situation_resolver, strength_toi
from nhl_pipeline.ingest import official_stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_strength_toi")


def main():
    conn = db.connect()
    cursor = conn.cursor()

    code_map = situation_resolver.load_situation_code_map(cursor)

    cursor.execute("SELECT DISTINCT GameID FROM Game.Shifts")
    game_ids = [row.GameID for row in cursor.fetchall()]
    log.info("Found %d game(s) with shift data", len(game_ids))

    for i, game_id in enumerate(game_ids, 1):
        strength_toi.apply_toi_by_strength(cursor, game_id, code_map)
        conn.commit()
        if i % 100 == 0:
            log.info("  ...%d/%d games done", i, len(game_ids))

    log.info("Patched PP/SH TOI for %d game(s)", len(game_ids))

    cursor.execute("SELECT SeasonID FROM Reference.Seasons")
    season_ids = [r.SeasonID for r in cursor.fetchall()]
    for season_id in season_ids:
        official_stats.sync_player_season_stats(cursor, season_id)
    conn.commit()
    log.info("Resynced PlayerSeasonStats for season(s): %s", season_ids)

    log.info("Done.")


if __name__ == "__main__":
    main()

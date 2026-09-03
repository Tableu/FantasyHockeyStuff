#!/usr/bin/env python
"""One-off/rerunnable patcher: fills Game.Goals.HighlightClipID / DiscreteClipID /
ClipSharingURL from the play-by-play JSON already archived in Ingestion.RawApiResponses --
no NHL API calls needed. Useful whenever a new field is added to goal parsing after games
have already been ingested (this is how the clip-id columns got backfilled the first time,
since the running full-season backfill had already loaded the old ingest/goals.py into
memory before those columns were added).

Usage:
    python backfill_clip_metadata.py
"""

import json
import logging

from nhl_pipeline import db
from nhl_pipeline.api import field_map

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_clip_metadata")


def main():
    conn = db.connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT GameID FROM Game.Goals WHERE HighlightClipID IS NULL AND DiscreteClipID IS NULL "
        "GROUP BY GameID"
    )
    game_ids = [row.GameID for row in cursor.fetchall()]
    log.info("Found %d game(s) with unpatched goal clip metadata", len(game_ids))

    patched_games = 0
    patched_goals = 0
    for game_id in game_ids:
        cursor.execute(
            "SELECT RawJSON FROM Ingestion.RawApiResponses WHERE GameID = ? AND EndpointType = 'PLAY_BY_PLAY'",
            game_id,
        )
        row = cursor.fetchone()
        if row is None:
            log.warning("  GameID %s: no archived PLAY_BY_PLAY response, skipping", game_id)
            continue

        pbp = json.loads(row.RawJSON)
        goal_plays_by_nhl_id = {
            play["eventId"]: field_map.goal_fields(play)
            for play in pbp.get("plays", [])
            if play["typeDescKey"] == "goal" and not field_map.is_shootout_play(play)
        }
        if not goal_plays_by_nhl_id:
            continue

        cursor.execute(
            "SELECT go.GoalID, p.NHLPlayID FROM Game.Goals go "
            "JOIN Game.Plays p ON p.PlayID = go.PlayID WHERE go.GameID = ?",
            game_id,
        )
        for goal_id, nhl_play_id in cursor.fetchall():
            f = goal_plays_by_nhl_id.get(nhl_play_id)
            if f is None:
                continue
            cursor.execute(
                "UPDATE Game.Goals SET HighlightClipID = ?, DiscreteClipID = ?, ClipSharingURL = ? WHERE GoalID = ?",
                f["highlight_clip_id"], f["discrete_clip_id"], f["clip_sharing_url"], goal_id,
            )
            patched_goals += 1
        conn.commit()
        patched_games += 1

    log.info("Done: patched %d goal(s) across %d game(s)", patched_goals, patched_games)


if __name__ == "__main__":
    main()

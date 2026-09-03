#!/usr/bin/env python
"""One-off/rerunnable patcher: recomputes Game.Goals.IsPowerPlay/IsShortHanded with the
fixed goalie-pull-adjusted classify_strength() (see calc/situation_resolver.py), then
re-derives every downstream value that was built from the old, buggy flags:
Stats.PlayerGameStats.{PowerPlayAssists,ShortHandedGoals,ShortHandedAssists} via
official_stats.apply_pp_sh_goals_assists(), and Stats.PlayerSeasonStats via
official_stats.sync_player_season_stats(). PlayerSeasonStats.PowerPlayGoals is untouched --
it's sourced from the official boxscore, not from Game.Goals.

Root cause: the old classify_strength() compared raw skater counts, so a pulled goalie
(replaced on the ice by an extra attacker) looked like a real penalty-driven manpower edge.
Reported symptom: Adrian Kempe showed 2 shorthanded goals that were actually empty-net goals
scored at even strength.

Usage:
    python backfill_strength_classification.py
"""

import logging

from nhl_pipeline import db
from nhl_pipeline.calc import situation_resolver
from nhl_pipeline.ingest import official_stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_strength_classification")


def main():
    conn = db.connect()
    cursor = conn.cursor()

    code_map = situation_resolver.load_situation_code_map(cursor)

    cursor.execute(
        """
        SELECT g.GoalID, g.GameID, g.TeamID, g.StrengthCode, g.IsPowerPlay, g.IsShortHanded, gm.HomeTeamID
        FROM Game.Goals g
        JOIN Game.Games gm ON g.GameID = gm.GameID
        """
    )
    rows = cursor.fetchall()
    log.info("Checking %d goal(s)", len(rows))

    changed_goal_ids = []
    affected_game_ids = set()
    for r in rows:
        team_is_home = r.TeamID == r.HomeTeamID
        new_pp, new_sh = situation_resolver.classify_strength(code_map, r.StrengthCode, team_is_home)
        if bool(r.IsPowerPlay) != new_pp or bool(r.IsShortHanded) != new_sh:
            cursor.execute(
                "UPDATE Game.Goals SET IsPowerPlay = ?, IsShortHanded = ? WHERE GoalID = ?",
                new_pp, new_sh, r.GoalID,
            )
            changed_goal_ids.append(r.GoalID)
            affected_game_ids.add(r.GameID)

    conn.commit()
    log.info("Corrected %d goal(s) across %d game(s)", len(changed_goal_ids), len(affected_game_ids))

    for game_id in affected_game_ids:
        official_stats.apply_pp_sh_goals_assists(cursor, game_id)
    conn.commit()
    log.info("Re-derived PlayerGameStats PP/SH assist and SH goal counts for %d game(s)", len(affected_game_ids))

    if affected_game_ids:
        placeholders = ",".join("?" for _ in affected_game_ids)
        cursor.execute(
            f"SELECT DISTINCT SeasonID FROM Game.Games WHERE GameID IN ({placeholders})",
            *affected_game_ids,
        )
        season_ids = [r.SeasonID for r in cursor.fetchall()]
        for season_id in season_ids:
            official_stats.sync_player_season_stats(cursor, season_id)
        conn.commit()
        log.info("Resynced PlayerSeasonStats for season(s): %s", season_ids)

    log.info("Done.")


if __name__ == "__main__":
    main()

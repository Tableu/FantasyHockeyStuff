#!/usr/bin/env python
"""Rewrites Game.Shifts for already-ingested games with the merged, non-overlapping
intervals that ingest/shifts.py now produces, and recomputes everything derived from
them.

Why: the shift-charts payload duplicates and nests a player's shift rows, and is not
unique on (player, period, start) the way UQ_Shifts assumed -- so stored ice time was
double-counted for 129 player-games and partly lost for 27 (see ingest/shifts.py).
The merge fixes both, but only for games ingested after it existed; this pass applies
it to the rest.

No API calls: every game's raw shift payload is already in
Ingestion.RawApiResponses (EndpointType = 'SHIFT_CHARTS'), which is also what the
HTML-report fallback wrote for the games the JSON feed dropped. Games whose merged
intervals match what is already stored are skipped, so only the affected games pay for
the downstream recompute (on-ice players -> strength TOI -> lineups -> on-ice /
individual / goalie analytics; the xG model doesn't depend on shifts).

Rerunnable, and safe to interrupt: each game is committed on its own.

Usage:
    python backfill_shift_merge.py --dry-run           # report what would change, write nothing
    python backfill_shift_merge.py                     # every game in the database
    python backfill_shift_merge.py --season 2024-25     # one season
"""

import argparse
import json
import logging

from nhl_pipeline import db
from nhl_pipeline.calc import goalie_stats, individual_stats, lineups, on_ice_stats, situation_resolver, strength_toi
from nhl_pipeline.ingest import on_ice, shifts as ingest_shifts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_shift_merge")


def parse_args():
    parser = argparse.ArgumentParser(description="Re-derive Game.Shifts as merged intervals")
    parser.add_argument("--season", default=None, metavar="YYYY-YY", help="Only this season's games")
    parser.add_argument("--dry-run", action="store_true", help="Report the differences, write nothing")
    return parser.parse_args()


def id_maps(cursor) -> tuple:
    cursor.execute("SELECT NHLTeamID, TeamID FROM Reference.Teams WHERE NHLTeamID IS NOT NULL")
    teams = {row[0]: row[1] for row in cursor.fetchall()}
    cursor.execute("SELECT NHLPlayerID, PlayerID FROM Reference.Players WHERE NHLPlayerID IS NOT NULL")
    players = {row[0]: row[1] for row in cursor.fetchall()}
    return teams, players


def stored_rows(cursor, game_id: int) -> set:
    cursor.execute(
        """SELECT PlayerID, TeamID, PeriodNumber, ShiftStartSeconds, ShiftEndSeconds, DurationSeconds
           FROM Game.Shifts WHERE GameID = ?""",
        game_id,
    )
    return {tuple(row) for row in cursor.fetchall()}


def merged_rows(payload: dict, teams: dict, players: dict) -> set:
    return {
        (r["PlayerID"], r["TeamID"], r["PeriodNumber"], r["ShiftStartSeconds"],
         r["ShiftEndSeconds"], r["DurationSeconds"])
        for r in ingest_shifts.merge_shift_rows(payload.get("data", []), teams, players)
    }


def recompute_dependents(cursor, game_id: int, code_map: dict, corsi_version_id: int, xg_version_id: int) -> None:
    unmapped: set = set()
    on_ice.derive_on_ice_players(cursor, game_id)
    strength_toi.apply_toi_by_strength(cursor, game_id, code_map)
    lineups.compute_and_store(cursor, game_id, code_map)
    on_ice_stats.compute_and_store(cursor, game_id, corsi_version_id, xg_version_id, unmapped)
    individual_stats.compute_and_store(cursor, game_id, corsi_version_id, xg_version_id, unmapped)
    goalie_stats.compute_and_store(cursor, game_id, xg_version_id, unmapped)


def main():
    args = parse_args()
    conn = db.connect()
    cursor = conn.cursor()

    sql = """
        SELECT g.GameID, g.NHLGameID, r.RawJSON
        FROM Game.Games g
        JOIN Ingestion.RawApiResponses r ON r.GameID = g.GameID AND r.EndpointType = 'SHIFT_CHARTS'
        JOIN Reference.Seasons s ON s.SeasonID = g.SeasonID
    """
    params = []
    if args.season:
        sql += " WHERE s.DisplayName = ?"
        params.append(args.season)
    cursor.execute(sql + " ORDER BY g.GameDate, g.NHLGameID", params)
    games = cursor.fetchall()
    log.info("%d game(s) with a stored shift payload", len(games))

    teams, players = id_maps(cursor)
    code_map = situation_resolver.load_situation_code_map(cursor)
    corsi_version_id = db.fetch_scalar(
        cursor, "SELECT CalculationVersionID FROM Analytics.CalculationVersions WHERE MetricCode = 'CORSI' AND IsActive = 1"
    )
    xg_version_id = db.fetch_scalar(
        cursor, "SELECT CalculationVersionID FROM Analytics.CalculationVersions WHERE MetricCode = 'XG' AND IsActive = 1"
    )

    changed = failures = seconds_before = seconds_after = 0
    for i, game in enumerate(games, start=1):
        try:
            wanted = merged_rows(json.loads(game.RawJSON), teams, players)
            current = stored_rows(cursor, game.GameID)
            if wanted == current or not wanted:
                continue

            changed += 1
            seconds_before += sum(r[5] for r in current)
            seconds_after += sum(r[5] for r in wanted)
            log.info("game %s: %d row(s) -> %d, %+d second(s) of ice time",
                     game.NHLGameID, len(current), len(wanted),
                     sum(r[5] for r in wanted) - sum(r[5] for r in current))

            if args.dry_run:
                continue

            ingest_shifts.sync_shifts(cursor, game.GameID, json.loads(game.RawJSON)["data"], teams, players)
            recompute_dependents(cursor, game.GameID, code_map, corsi_version_id, xg_version_id)
            conn.commit()
        except Exception:
            conn.rollback()
            failures += 1
            log.exception("FAILED game %s", game.NHLGameID)
        finally:
            if i % 250 == 0:
                log.info("  scanned %d / %d", i, len(games))

    log.info("Done: %d game(s) scanned, %d changed, %d failure(s); ice time %d -> %d seconds in the changed games%s",
             len(games), changed, failures, seconds_before, seconds_after, " (dry run)" if args.dry_run else "")


if __name__ == "__main__":
    main()

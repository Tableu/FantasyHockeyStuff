#!/usr/bin/env python
"""Standalone single-game verification/debug runner.

Usage:
    python verify_one_game.py --game-id 2025020740             # just report on data already ingested
    python verify_one_game.py --game-id 2025020740 --run        # ingest the game first, then report
    python verify_one_game.py --game-id 2025020740 --run --twice # run twice, to sanity-check idempotency

Prints row counts, the goal-sum-vs-final-score check, IngestionRuns status, and the goal /
situation breakdown, per the plan's verification steps.
"""

import argparse
import subprocess
import sys

from nhl_pipeline import db

TABLES = [
    "Game.Games", "Game.Plays", "Game.Shots", "Game.Goals", "Game.Shifts", "Game.PlayOnIcePlayers",
    "Stats.PlayerGameStats", "Analytics.ShotExpectedGoals", "Analytics.PlayerGameAdvancedStats",
    "Analytics.PlayerGameOnIceStats", "Analytics.GoalieGameAdvancedStats",
    "Ingestion.RawApiResponses", "Ingestion.IngestionRuns",
]


def run_ingestion(nhl_game_id: int):
    subprocess.run([sys.executable, "run_daily.py", "--game-id", str(nhl_game_id)], check=True)


def report(nhl_game_id: int):
    conn = db.connect()
    cursor = conn.cursor()

    cursor.execute("SELECT GameID, HomeScore, AwayScore FROM Game.Games WHERE NHLGameID = ?", nhl_game_id)
    row = cursor.fetchone()
    if row is None:
        print(f"No Game.Games row for NHLGameID={nhl_game_id} -- nothing ingested yet.")
        return
    game_id, home_score, away_score = row

    print(f"=== Game {nhl_game_id} (internal GameID={game_id}) ===\n")

    print("-- Row counts --")
    for table in TABLES:
        if table == "Game.PlayOnIcePlayers":
            # No GameID column here -- scoped via its parent Play instead.
            cursor.execute(
                "SELECT COUNT(*) FROM Game.PlayOnIcePlayers oip "
                "JOIN Game.Plays p ON p.PlayID = oip.PlayID WHERE p.GameID = ?",
                game_id,
            )
        elif table == "Analytics.ShotExpectedGoals":
            # No GameID column here either -- scoped via its parent Shot.
            cursor.execute(
                "SELECT COUNT(*) FROM Analytics.ShotExpectedGoals xg "
                "JOIN Game.Shots s ON s.ShotID = xg.ShotID WHERE s.GameID = ?",
                game_id,
            )
        else:
            cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE GameID = ?", game_id)
        print(f"  {table:<38} {cursor.fetchone()[0]}")

    print("\n-- Goal count vs. final score --")
    cursor.execute("SELECT COUNT(*) FROM Game.Goals WHERE GameID = ?", game_id)
    goal_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM Game.Plays WHERE GameID = ? AND PeriodNumber = 5", game_id)
    went_to_shootout = cursor.fetchone()[0] > 0
    expected = (home_score or 0) + (away_score or 0)
    if went_to_shootout:
        # The NHL awards the shootout winner a bonus goal in the official score, but it
        # isn't a real hockey play (no shooter/situation to record), so it's intentionally
        # excluded from Game.Goals -- expect exactly one fewer row than the final score.
        expected -= 1
    status = "OK" if goal_count == expected else "MISMATCH"
    print(f"  Game.Goals rows = {goal_count}, HomeScore+AwayScore = {expected}  [{status}]")

    print("\n-- Ingestion run stages --")
    cursor.execute(
        "SELECT Stage, Status, ErrorMessage FROM Ingestion.IngestionRuns WHERE GameID = ? ORDER BY StartedAt",
        game_id,
    )
    for stage, status, error in cursor.fetchall():
        line = f"  {stage:<28} {status}"
        if error:
            line += f"  -- {error[:120]}"
        print(line)

    print("\n-- Raw API responses archived --")
    cursor.execute("SELECT EndpointType, RetrievedAt FROM Ingestion.RawApiResponses WHERE GameID = ?", game_id)
    for endpoint, retrieved_at in cursor.fetchall():
        print(f"  {endpoint:<16} {retrieved_at}")

    print("\n-- Goals: strength classification --")
    cursor.execute(
        """
        SELECT TeamID, StrengthCode, IsPowerPlay, IsShortHanded, IsEmptyNet
        FROM Game.Goals WHERE GameID = ? ORDER BY PeriodNumber, PeriodTimeSeconds
        """,
        game_id,
    )
    for team_id, strength_code, is_pp, is_sh, is_en in cursor.fetchall():
        print(f"  TeamID={team_id} StrengthCode={strength_code} PP={bool(is_pp)} SH={bool(is_sh)} EN={bool(is_en)}")

    print("\n-- Unmapped situationCode warnings --")
    cursor.execute(
        "SELECT ErrorMessage FROM Ingestion.IngestionRuns "
        "WHERE GameID = ? AND Stage = 'SITUATION_RESOLVE_UNMAPPED'",
        game_id,
    )
    rows = cursor.fetchall()
    if not rows:
        print("  none")
    for (msg,) in rows:
        print(f"  {msg}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-id", type=int, required=True)
    parser.add_argument("--run", action="store_true", help="Run ingestion before reporting")
    parser.add_argument("--twice", action="store_true", help="Run ingestion twice (idempotency check)")
    args = parser.parse_args()

    if args.run:
        run_ingestion(args.game_id)
        if args.twice:
            run_ingestion(args.game_id)

    report(args.game_id)


if __name__ == "__main__":
    main()

"""Analytics.PlayerGameAdvancedStats -- per-shooter individual Corsi/Fenwick/xG.

Must run AFTER calc.on_ice_stats for the same game: IndividualCorsiForPct is defined as
"share of the player's own on-ice CorsiFor that they personally took" (the schema gives no
natural single-player definition of Corsi-against), which reads PlayerGameOnIceStats.
"""

from nhl_pipeline import db
from nhl_pipeline.calc import situation_resolver


def compute_and_store(cursor, game_id: int, corsi_calc_version_id: int, xg_calc_version_id: int, unmapped_codes: set) -> None:
    cursor.execute("SELECT HomeTeamID, AwayTeamID FROM Game.Games WHERE GameID = ?", game_id)
    home_team_id, _away_team_id = cursor.fetchone()

    code_map = situation_resolver.load_situation_code_map(cursor)
    situations_by_strength = situation_resolver.load_situations_by_strength(cursor)
    all_situation_id = situation_resolver.get_all_situation_id(cursor)

    cursor.execute(
        """
        SELECT s.ShotID, s.TeamID, s.ShooterPlayerID, s.IsGoal, s.IsBlocked, s.IsMissed, s.StrengthCode,
               ISNULL(xg.ExpectedGoals, 0) AS ExpectedGoals
        FROM Game.Shots s
        LEFT JOIN Analytics.ShotExpectedGoals xg
          ON xg.ShotID = s.ShotID AND xg.CalculationVersionID = ?
        WHERE s.GameID = ? AND s.ShooterPlayerID IS NOT NULL
        """,
        xg_calc_version_id, game_id,
    )
    shots = cursor.fetchall()

    accumulators: dict = {}
    for shot in shots:
        team_is_home = shot.TeamID == home_team_id
        situation_id = situation_resolver.resolve_situation_id(
            code_map, situations_by_strength, all_situation_id,
            shot.StrengthCode, team_is_home, unmapped_codes,
        )
        for bucket in {situation_id, all_situation_id}:
            key = (shot.ShooterPlayerID, bucket)
            acc = accumulators.setdefault(
                key, {"team_id": shot.TeamID, "corsi": 0, "fenwick": 0, "shots": 0, "goals": 0, "xg": 0.0}
            )
            acc["corsi"] += 1
            if not shot.IsBlocked:
                acc["fenwick"] += 1
                acc["xg"] += float(shot.ExpectedGoals or 0)
                if not shot.IsMissed:
                    acc["shots"] += 1
                if shot.IsGoal:
                    acc["goals"] += 1

    cursor.execute(
        "SELECT PlayerID, SituationID, CorsiFor FROM Analytics.PlayerGameOnIceStats "
        "WHERE GameID = ? AND CalculationVersionID = ?",
        game_id, corsi_calc_version_id,
    )
    on_ice_corsi_for = {(r.PlayerID, r.SituationID): r.CorsiFor for r in cursor.fetchall()}

    for (player_id, situation_id), acc in accumulators.items():
        shooting_pct = round(acc["goals"] / acc["shots"], 4) if acc["shots"] else None
        team_corsi_for = on_ice_corsi_for.get((player_id, situation_id))
        corsi_pct = round(acc["corsi"] / team_corsi_for, 4) if team_corsi_for else None

        db.upsert(
            cursor, "Analytics.PlayerGameAdvancedStats",
            {
                "GameID": game_id, "PlayerID": player_id, "TeamID": acc["team_id"],
                "SituationID": situation_id, "CalculationVersionID": corsi_calc_version_id,
            },
            {
                "IndividualCorsiFor": acc["corsi"], "IndividualFenwickFor": acc["fenwick"],
                "IndividualShots": acc["shots"], "IndividualGoals": acc["goals"],
                "IndividualExpectedGoals": round(acc["xg"], 4),
                "IndividualCorsiForPct": corsi_pct, "ShootingPercentage": shooting_pct,
            },
        )

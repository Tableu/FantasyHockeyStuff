"""Analytics.GoalieGameAdvancedStats -- SV%, xGA, GSAx per goalie per situation."""

from nhl_pipeline import db
from nhl_pipeline.calc import situation_resolver


def compute_and_store(cursor, game_id: int, xg_calc_version_id: int, unmapped_codes: set) -> None:
    cursor.execute("SELECT HomeTeamID, AwayTeamID FROM Game.Games WHERE GameID = ?", game_id)
    home_team_id, away_team_id = cursor.fetchone()

    code_map = situation_resolver.load_situation_code_map(cursor)
    situations_by_strength = situation_resolver.load_situations_by_strength(cursor)
    all_situation_id = situation_resolver.get_all_situation_id(cursor)

    cursor.execute(
        """
        SELECT s.ShotID, s.TeamID, s.GoaliePlayerID, s.IsGoal, s.StrengthCode,
               ISNULL(xg.ExpectedGoals, 0) AS ExpectedGoals
        FROM Game.Shots s
        LEFT JOIN Analytics.ShotExpectedGoals xg
          ON xg.ShotID = s.ShotID AND xg.CalculationVersionID = ?
        WHERE s.GameID = ? AND s.GoaliePlayerID IS NOT NULL AND s.IsBlocked = 0
        """,
        xg_calc_version_id, game_id,
    )
    shots = cursor.fetchall()

    accumulators: dict = {}
    for shot in shots:
        # The goalie belongs to whichever team is NOT credited with the shot.
        goalie_team_is_home = shot.TeamID != home_team_id
        situation_id = situation_resolver.resolve_situation_id(
            code_map, situations_by_strength, all_situation_id,
            shot.StrengthCode, goalie_team_is_home, unmapped_codes,
        )
        goalie_team_id = home_team_id if goalie_team_is_home else away_team_id

        for bucket in {situation_id, all_situation_id}:
            key = (shot.GoaliePlayerID, bucket)
            acc = accumulators.setdefault(
                key, {"team_id": goalie_team_id, "shots_against": 0, "saves": 0, "goals_against": 0, "xga": 0.0}
            )
            acc["shots_against"] += 1
            if shot.IsGoal:
                acc["goals_against"] += 1
            else:
                acc["saves"] += 1
            acc["xga"] += float(shot.ExpectedGoals or 0)

    for (goalie_id, situation_id), acc in accumulators.items():
        shots_against = acc["shots_against"]
        save_pct = round(acc["saves"] / shots_against, 5) if shots_against else None
        xga = round(acc["xga"], 4)
        exp_save_pct = round(1 - xga / shots_against, 5) if shots_against else None
        gsax = round(xga - acc["goals_against"], 4)

        db.upsert(
            cursor, "Analytics.GoalieGameAdvancedStats",
            {
                "GameID": game_id, "GoaliePlayerID": goalie_id, "TeamID": acc["team_id"],
                "SituationID": situation_id, "CalculationVersionID": xg_calc_version_id,
            },
            {
                "ShotsAgainst": shots_against, "Saves": acc["saves"], "GoalsAgainst": acc["goals_against"],
                "SavePercentage": save_pct, "ExpectedGoalsAgainst": xga,
                "GoalsSavedAboveExpected": gsax, "ExpectedSavePercentage": exp_save_pct,
            },
        )

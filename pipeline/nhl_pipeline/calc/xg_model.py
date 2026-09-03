"""CalculationVersion XG/naive_v1: a flat probability lookup by shot type + distance
bucket. Not fit to any real data -- a deliberate placeholder per the agreed V1 scope,
tagged with its own CalculationVersionID so it's trivial to replace later without
touching historical rows (see Analytics.CalculationVersions).
"""

from nhl_pipeline import db

_DISTANCE_BUCKETS = ((10, 0.18), (20, 0.12), (30, 0.07), (45, 0.04), (float("inf"), 0.02))

_TYPE_MULTIPLIER = {
    "tip-in": 1.4, "deflected": 1.4, "wrist": 1.0, "snap": 1.0,
    "slap": 0.85, "backhand": 0.9, "wrap-around": 0.8,
}


def naive_expected_goals(shot_type, distance_feet) -> float:
    if distance_feet is None:
        distance_feet = 30.0
    base = next(prob for threshold, prob in _DISTANCE_BUCKETS if distance_feet < threshold)
    multiplier = _TYPE_MULTIPLIER.get(shot_type, 1.0)
    return round(min(base * multiplier, 0.95), 8)


def run_xg_for_game(cursor, game_id: int, calc_version_id: int) -> None:
    cursor.execute(
        "SELECT ShotID, ShotType, DistanceFeet FROM Game.Shots WHERE GameID = ? AND IsBlocked = 0",
        game_id,
    )
    for row in cursor.fetchall():
        xg = naive_expected_goals(row.ShotType, row.DistanceFeet)
        db.upsert(
            cursor, "Analytics.ShotExpectedGoals",
            {"ShotID": row.ShotID, "CalculationVersionID": calc_version_id},
            {"ExpectedGoals": xg},
        )

"""Analytics.PlayerGameOnIceStats -- Corsi/Fenwick/xG for every skater who was on the ice
for a shot attempt, per situation bucket (including the 'ALL' aggregate bucket).

Done in Python rather than a single T-SQL query: situation resolution is team-relative
(see calc.situation_resolver) and reusing that one implementation here avoids a second,
divergence-prone copy of the same logic in SQL. Game-scale data (~60-70 shot attempts,
~30 skaters) keeps this cheap even done in-memory once per game.
"""

from nhl_pipeline import db
from nhl_pipeline.calc import situation_resolver


def _new_accumulator(team_id):
    return {
        "team_id": team_id,
        "corsi_for": 0, "corsi_against": 0,
        "fenwick_for": 0, "fenwick_against": 0,
        "shots_for": 0, "shots_against": 0,
        "goals_for": 0, "goals_against": 0,
        "xg_for": 0.0, "xg_against": 0.0,
    }


def _accumulate(acc, shot, is_for):
    side = "for" if is_for else "against"
    acc[f"corsi_{side}"] += 1
    if not shot.IsBlocked:
        acc[f"fenwick_{side}"] += 1
        acc[f"xg_{side}"] += float(shot.ExpectedGoals or 0)
        if not shot.IsMissed:
            acc[f"shots_{side}"] += 1
        if shot.IsGoal:
            acc[f"goals_{side}"] += 1


def _pct(for_, against):
    total = for_ + against
    return round(for_ / total, 4) if total else None


def _finalize(acc):
    on_ice_sh_pct = round(acc["goals_for"] / acc["shots_for"], 4) if acc["shots_for"] else None
    on_ice_sv_pct = round(1 - acc["goals_against"] / acc["shots_against"], 4) if acc["shots_against"] else None
    pdo = round(on_ice_sh_pct + on_ice_sv_pct, 4) if (on_ice_sh_pct is not None and on_ice_sv_pct is not None) else None
    return {
        "CorsiFor": acc["corsi_for"], "CorsiAgainst": acc["corsi_against"],
        "CorsiForPct": _pct(acc["corsi_for"], acc["corsi_against"]),
        "FenwickFor": acc["fenwick_for"], "FenwickAgainst": acc["fenwick_against"],
        "FenwickForPct": _pct(acc["fenwick_for"], acc["fenwick_against"]),
        "ShotsFor": acc["shots_for"], "ShotsAgainst": acc["shots_against"],
        "GoalsFor": acc["goals_for"], "GoalsAgainst": acc["goals_against"],
        "ExpectedGoalsFor": round(acc["xg_for"], 4), "ExpectedGoalsAgainst": round(acc["xg_against"], 4),
        "ExpectedGoalsPct": _pct(acc["xg_for"], acc["xg_against"]),
        "PDO": pdo, "OnIceShootingPct": on_ice_sh_pct, "OnIceSavePct": on_ice_sv_pct,
    }


def compute_and_store(cursor, game_id: int, corsi_calc_version_id: int, xg_calc_version_id: int, unmapped_codes: set) -> None:
    cursor.execute("SELECT HomeTeamID, AwayTeamID FROM Game.Games WHERE GameID = ?", game_id)
    home_team_id, _away_team_id = cursor.fetchone()

    code_map = situation_resolver.load_situation_code_map(cursor)
    situations_by_strength = situation_resolver.load_situations_by_strength(cursor)
    all_situation_id = situation_resolver.get_all_situation_id(cursor)

    cursor.execute(
        """
        SELECT s.ShotID, s.TeamID, s.IsGoal, s.IsBlocked, s.IsMissed, s.StrengthCode,
               ISNULL(xg.ExpectedGoals, 0) AS ExpectedGoals
        FROM Game.Shots s
        LEFT JOIN Analytics.ShotExpectedGoals xg
          ON xg.ShotID = s.ShotID AND xg.CalculationVersionID = ?
        WHERE s.GameID = ?
        """,
        xg_calc_version_id, game_id,
    )
    shots_by_id = {r.ShotID: r for r in cursor.fetchall()}

    cursor.execute(
        """
        SELECT sh.ShotID, oip.PlayerID, oip.TeamID AS PlayerTeamID
        FROM Game.Shots sh
        JOIN Game.PlayOnIcePlayers oip ON oip.PlayID = sh.PlayID
        WHERE sh.GameID = ? AND oip.RoleCode = 'SKATER'
        """,
        game_id,
    )
    on_ice_rows = cursor.fetchall()

    accumulators: dict = {}
    for row in on_ice_rows:
        shot = shots_by_id.get(row.ShotID)
        if shot is None:
            continue
        player_team_is_home = row.PlayerTeamID == home_team_id
        situation_id = situation_resolver.resolve_situation_id(
            code_map, situations_by_strength, all_situation_id,
            shot.StrengthCode, player_team_is_home, unmapped_codes,
        )
        is_for = shot.TeamID == row.PlayerTeamID

        for bucket in {situation_id, all_situation_id}:
            key = (row.PlayerID, bucket)
            acc = accumulators.setdefault(key, _new_accumulator(row.PlayerTeamID))
            _accumulate(acc, shot, is_for)

    cursor.execute(
        "SELECT PlayerID, SUM(DurationSeconds) AS ToiSeconds FROM Game.Shifts WHERE GameID = ? GROUP BY PlayerID",
        game_id,
    )
    toi_all = {r.PlayerID: r.ToiSeconds for r in cursor.fetchall()}

    for (player_id, situation_id), acc in accumulators.items():
        values = _finalize(acc)
        toi = toi_all.get(player_id) if situation_id == all_situation_id else None
        db.upsert(
            cursor, "Analytics.PlayerGameOnIceStats",
            {
                "GameID": game_id, "PlayerID": player_id, "TeamID": acc["team_id"],
                "SituationID": situation_id, "CalculationVersionID": corsi_calc_version_id,
            },
            {**values, "TimeOnIceSeconds": toi},
        )

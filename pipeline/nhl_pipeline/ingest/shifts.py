from nhl_pipeline import db
from nhl_pipeline.api import field_map


def sync_shifts(cursor, game_id: int, shift_rows: list, team_id_by_nhl: dict, player_id_by_nhl: dict) -> None:
    for row in shift_rows:
        f = field_map.shift_fields(row)
        if f["shift_start_seconds"] is None or f["shift_end_seconds"] is None:
            continue

        player_id = player_id_by_nhl.get(f["nhl_player_id"])
        team_id = team_id_by_nhl.get(f["nhl_team_id"])
        if player_id is None or team_id is None:
            continue

        duration = f["duration_seconds"]
        if duration is None:
            duration = max(f["shift_end_seconds"] - f["shift_start_seconds"], 0)

        db.upsert(
            cursor, "Game.Shifts",
            {
                "GameID": game_id,
                "PlayerID": player_id,
                "PeriodNumber": f["period_number"],
                "ShiftStartSeconds": f["shift_start_seconds"],
            },
            {"TeamID": team_id, "ShiftEndSeconds": f["shift_end_seconds"], "DurationSeconds": duration},
        )

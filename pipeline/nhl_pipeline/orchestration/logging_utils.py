"""Ingestion.IngestionRuns logging. run_stage() wraps a stage's execution: SUCCESS/FAILED
is always recorded, and failures re-raise so pipeline.run_game aborts the rest of *this*
game's stages while the caller (run_daily's game loop) still moves on to the next game.
"""


def log_run(cursor, game_id, season_id, stage: str, status: str, error_message: str | None = None) -> None:
    cursor.execute(
        """
        INSERT INTO Ingestion.IngestionRuns (GameID, SeasonID, Stage, Status, ErrorMessage, StartedAt, CompletedAt)
        VALUES (?, ?, ?, ?, ?, SYSUTCDATETIME(), SYSUTCDATETIME())
        """,
        game_id, season_id, stage, status, error_message,
    )


def run_stage(cursor, _run_game_id, _run_season_id, _run_stage_name: str, fn, *args, **kwargs):
    """Names are prefixed (_run_*) so they can never collide with a same-named kwarg meant
    for `fn` (e.g. games.ensure_game's own `season_id=` argument)."""
    try:
        result = fn(*args, **kwargs)
        log_run(cursor, _run_game_id, _run_season_id, _run_stage_name, "SUCCESS")
        return result
    except Exception as exc:
        log_run(cursor, _run_game_id, _run_season_id, _run_stage_name, "FAILED", str(exc)[:4000])
        raise


def log_unmapped_situation_codes(cursor, game_id, codes: set) -> None:
    if codes:
        log_run(
            cursor, game_id, None, "SITUATION_RESOLVE_UNMAPPED", "FAILED",
            "Unmapped codes (bucketed into ALL, extend Reference.SituationCodeMap): " + ", ".join(sorted(codes)),
        )


def needs_ingestion(cursor, nhl_game_id: int, required_stages: list) -> bool:
    cursor.execute("SELECT GameID FROM Game.Games WHERE NHLGameID = ?", nhl_game_id)
    row = cursor.fetchone()
    if row is None:
        return True
    game_id = row[0]
    cursor.execute(
        "SELECT DISTINCT Stage FROM Ingestion.IngestionRuns WHERE GameID = ? AND Status = 'SUCCESS'",
        game_id,
    )
    succeeded = {r[0] for r in cursor.fetchall()}
    return not set(required_stages).issubset(succeeded)

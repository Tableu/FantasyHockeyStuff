"""Stats.PlayerGameStats.PowerPlayTOISeconds / ShortHandedTOISeconds.

Neither the boxscore nor the shift-chart endpoint reports per-strength ice time directly
(see ingest.official_stats' module docstring for the other fields in the same situation).
This reconstructs it instead: Game.Plays.StrengthCode is recorded on every play, so
consecutive plays within a period mark out a timeline of strength-state segments (a new
segment starts wherever the code changes from the previous play). Each player's Game.Shifts
intervals are then overlapped against that timeline and bucketed into PP/SH seconds via
situation_resolver.classify_strength(). This is only as precise as the nearest play event --
a strength change with no play near it shifts the segment boundary slightly -- which is the
standard approach used across public hockey analytics when a real per-shift strength feed
isn't available.
"""

from nhl_pipeline.calc import situation_resolver

_PERIOD_END_SENTINEL = 32767  # SMALLINT max; the final segment of a period just needs to outlast every shift in it


def _build_period_segments(plays: list) -> dict:
    """plays: rows with PeriodNumber, PeriodTimeSeconds, StrengthCode, sorted by
    (PeriodNumber, PeriodTimeSeconds, PlayID). Returns {PeriodNumber: [(start, end, raw_code), ...]}."""
    by_period: dict = {}
    for p in plays:
        by_period.setdefault(p.PeriodNumber, []).append(p)

    segments_by_period = {}
    for period, period_plays in by_period.items():
        segments = []
        prev_code, prev_time = None, 0
        for p in period_plays:
            if p.StrengthCode != prev_code:
                if prev_code is not None:
                    segments.append((prev_time, p.PeriodTimeSeconds, prev_code))
                prev_code, prev_time = p.StrengthCode, p.PeriodTimeSeconds
        segments.append((prev_time, _PERIOD_END_SENTINEL, prev_code))
        segments_by_period[period] = segments
    return segments_by_period


def _overlap_seconds(a_start, a_end, b_start, b_end) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def compute_toi_by_strength(cursor, game_id: int, code_map: dict) -> dict:
    """Returns {PlayerID: (PowerPlayTOISeconds, ShortHandedTOISeconds)} for every player
    with at least one shift recorded in the game."""
    cursor.execute("SELECT HomeTeamID FROM Game.Games WHERE GameID = ?", game_id)
    home_team_id = cursor.fetchone()[0]

    cursor.execute(
        "SELECT PeriodNumber, PeriodTimeSeconds, StrengthCode FROM Game.Plays "
        "WHERE GameID = ? ORDER BY PeriodNumber, PeriodTimeSeconds, PlayID",
        game_id,
    )
    segments_by_period = _build_period_segments(cursor.fetchall())

    cursor.execute(
        "SELECT PlayerID, TeamID, PeriodNumber, ShiftStartSeconds, ShiftEndSeconds "
        "FROM Game.Shifts WHERE GameID = ?",
        game_id,
    )
    shifts = cursor.fetchall()

    toi: dict = {}
    for s in shifts:
        pp_sh = toi.setdefault(s.PlayerID, [0, 0])
        segments = segments_by_period.get(s.PeriodNumber, [])
        team_is_home = s.TeamID == home_team_id
        for seg_start, seg_end, raw_code in segments:
            overlap = _overlap_seconds(s.ShiftStartSeconds, s.ShiftEndSeconds, seg_start, seg_end)
            if overlap == 0:
                continue
            is_pp, is_sh = situation_resolver.classify_strength(code_map, raw_code, team_is_home)
            if is_pp:
                pp_sh[0] += overlap
            elif is_sh:
                pp_sh[1] += overlap

    return {player_id: (pp, sh) for player_id, (pp, sh) in toi.items()}


def apply_toi_by_strength(cursor, game_id: int, code_map: dict) -> None:
    for player_id, (pp_seconds, sh_seconds) in compute_toi_by_strength(cursor, game_id, code_map).items():
        cursor.execute(
            "UPDATE Stats.PlayerGameStats SET PowerPlayTOISeconds = ?, ShortHandedTOISeconds = ? "
            "WHERE GameID = ? AND PlayerID = ?",
            pp_seconds, sh_seconds, game_id, player_id,
        )

"""Game.Shifts from the shift-charts payload (the JSON endpoint, or the HTML report
fallback in api/html_shift_report.py -- both arrive in the same shape).

The payload needs cleaning before it can be stored as ice time. Three defects, all
measured across the 2,624 ingested games (2026-09-18):

  * Goal markers (typeCode 505) are mixed in with the shift rows (517). They carry
    start == end and no duration, so they are not shifts at all.
  * A player's shift rows can duplicate or nest: identical intervals under different
    shiftNumbers, or a period-long row plus a second row inside it (Binnington,
    2024020677: P1 0-1200 *and* P1 122-1200). Summing durations double-counts those --
    129 player-games' TOI came out over the boxscore's for this reason alone.
  * The rows are not unique on (player, period, start), which UQ_Shifts assumes: a
    goalie's real P3 0-1200 row can share a key with a glitch row like P3 0-517 dur=63
    (Shesterkin, 2024020750), and the upsert let the second overwrite the first -- 27
    player-games were missing up to 19 minutes of ice time.

So each (player, period)'s intervals are merged into a non-overlapping set, with
duration taken as end - start rather than the payload's own duration field (which
disagrees with the interval on the glitch rows). Merging before the write also makes
the unique key safe, since the merged intervals have distinct starts by construction.
A merged set can hold fewer rows than the payload, so the game's existing rows are
deleted first: an upsert alone would leave orphans behind.

What remains after this is genuine source disagreement -- 123 player-games where even
the merged intervals exceed the official boxscore TOI (median 21s, max 324s). The
boxscore is the authority there, and Stats.PlayerGameStats.TimeOnIceSeconds already
comes from it.
"""

import collections

from nhl_pipeline import db
from nhl_pipeline.api import field_map

_SHIFT_TYPE_CODE = 517


def merge_shift_rows(shift_rows: list, team_id_by_nhl: dict, player_id_by_nhl: dict) -> list:
    """Payload rows -> one dict per merged interval, keyed for Game.Shifts. Rows for
    unknown players/teams, non-shift type codes and zero-length intervals are dropped.

    `team_id_by_nhl` must contain *only the two teams in this game*: it is what keeps a mixed
    payload out of the table. The endpoint occasionally folds another game's shifts into a
    response under the requested game id -- game 2025020565 (NJD-BUF) returned 2,179 rows
    including 340 VGK and 336 SJS -- and those rows are only rejected because their team is
    not in the map. A caller that passes a league-wide map will store them.
    """
    intervals = collections.defaultdict(list)
    for row in shift_rows:
        if row.get("typeCode") is not None and row["typeCode"] != _SHIFT_TYPE_CODE:
            continue

        f = field_map.shift_fields(row)
        if f["shift_start_seconds"] is None or f["shift_end_seconds"] is None:
            continue
        if f["shift_end_seconds"] <= f["shift_start_seconds"]:
            continue

        player_id = player_id_by_nhl.get(f["nhl_player_id"])
        team_id = team_id_by_nhl.get(f["nhl_team_id"])
        if player_id is None or team_id is None:
            continue

        key = (player_id, team_id, f["period_number"])
        intervals[key].append((f["shift_start_seconds"], f["shift_end_seconds"]))

    merged = []
    for (player_id, team_id, period), spans in intervals.items():
        start = end = None
        for span_start, span_end in sorted(spans):
            if end is None or span_start > end:
                if end is not None:
                    merged.append((player_id, team_id, period, start, end))
                start, end = span_start, span_end
            else:
                end = max(end, span_end)
        if end is not None:
            merged.append((player_id, team_id, period, start, end))

    return [
        {
            "PlayerID": player_id,
            "TeamID": team_id,
            "PeriodNumber": period,
            "ShiftStartSeconds": start,
            "ShiftEndSeconds": end,
            "DurationSeconds": end - start,
        }
        for player_id, team_id, period, start, end in merged
    ]


def sync_shifts(cursor, game_id: int, shift_rows: list, team_id_by_nhl: dict, player_id_by_nhl: dict) -> None:
    merged = merge_shift_rows(shift_rows, team_id_by_nhl, player_id_by_nhl)
    if not merged:
        return

    cursor.execute("DELETE FROM Game.Shifts WHERE GameID = ?", game_id)
    for row in merged:
        db.upsert(
            cursor, "Game.Shifts",
            {
                "GameID": game_id,
                "PlayerID": row["PlayerID"],
                "PeriodNumber": row["PeriodNumber"],
                "ShiftStartSeconds": row["ShiftStartSeconds"],
            },
            {
                "TeamID": row["TeamID"],
                "ShiftEndSeconds": row["ShiftEndSeconds"],
                "DurationSeconds": row["DurationSeconds"],
            },
        )

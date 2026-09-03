"""Reference.Schedule -- the full season schedule, including games that haven't been played
yet (see that table's comment in nhl_database_schema.sql for why this is separate from
Game.Games). The schedule/{date} endpoint always returns a full week regardless of which
date in it is requested (api.schedule.get_schedule_week, already used for daily discovery),
and hands back the week's own nextStartDate -- walked here to cover a date range without
needing to guess week boundaries, stopping once nextStartDate runs past end_date or the
endpoint stops advancing (repeats the same date, e.g. at the end of its known schedule).
"""

import logging
from datetime import datetime

from nhl_pipeline import db
from nhl_pipeline.api import field_map
from nhl_pipeline.api import schedule as api_schedule

log = logging.getLogger("ingest.schedule")


def _load_team_index(cursor) -> dict:
    cursor.execute("SELECT NHLTeamID, TeamID FROM Reference.Teams")
    return {row.NHLTeamID: row.TeamID for row in cursor.fetchall()}


def sync_schedule(cursor, start_date: str, end_date: str, season_id: int) -> dict:
    team_index = _load_team_index(cursor)

    counts = {"games": 0, "skipped_unknown_team": 0}
    seen_dates: set = set()
    week_start = start_date

    while week_start <= end_date:
        week = api_schedule.get_schedule_week(week_start)

        for day in week.get("gameWeek", []):
            day_date = day.get("date")
            if not day_date or day_date < start_date or day_date > end_date or day_date in seen_dates:
                continue
            seen_dates.add(day_date)

            for game in day.get("games", []):
                f = field_map.schedule_row_fields(game, day_date)
                home_team_id = team_index.get(f["home_nhl_team_id"])
                away_team_id = team_index.get(f["away_nhl_team_id"])
                if home_team_id is None or away_team_id is None:
                    counts["skipped_unknown_team"] += 1
                    continue

                db.upsert(
                    cursor, "Reference.Schedule",
                    {"NHLGameID": f["nhl_game_id"]},
                    {
                        "SeasonID": season_id,
                        "GameType": str(f["game_type"]) if f["game_type"] is not None else None,
                        "GameDate": datetime.strptime(f["game_date"], "%Y-%m-%d").date(),
                        "StartTimeUTC": datetime.fromisoformat(f["start_time_utc"]) if f["start_time_utc"] else None,
                        "HomeTeamID": home_team_id,
                        "AwayTeamID": away_team_id,
                        "GameState": f["game_state"],
                    },
                )
                counts["games"] += 1

        next_start = week.get("nextStartDate")
        if not next_start or next_start <= week_start:
            break
        week_start = next_start

    if counts["skipped_unknown_team"]:
        log.warning(
            "Skipped %d game(s) referencing a team not yet in Reference.Teams", counts["skipped_unknown_team"]
        )
    return counts

"""Every raw NHL API field name the pipeline depends on lives here, isolated from the
parsing/ingestion logic, so an API change is a one-file fix. Field names below were
confirmed by curling live endpoints for gameId=2025020740 (MTL @ BUF, 2026-01-15) during
planning -- see the plan doc for the raw samples.
"""

# Event types (Plays.EventType / typeDescKey) that represent a shot attempt.
SHOT_ATTEMPT_EVENT_TYPES = {"shot-on-goal", "missed-shot", "blocked-shot", "goal"}


def is_shootout_play(play: dict) -> bool:
    """Shootout attempts are individual 1-on-1 gimmick shots, not real game play -- they
    must not become Shots/Goals rows (they'd inflate Goals beyond the actual final score
    and pollute Corsi/Fenwick/xG). Plays.EventType still records them for a complete raw
    event log; only the Shots/Goals/analytics tables exclude them."""
    return (play.get("periodDescriptor") or {}).get("periodType") == "SO"


def parse_clock(value) -> int | None:
    """'MM:SS' -> elapsed seconds. Used for timeInPeriod, shift start/end/duration, TOI."""
    if not value:
        return None
    parts = str(value).split(":")
    if len(parts) != 2:
        return None
    minutes, seconds = parts
    return int(minutes) * 60 + int(seconds)


# ---------------------------------------------------------------------------
# Schedule: gameWeek[].games[]
# ---------------------------------------------------------------------------

def schedule_game_summary(game: dict) -> dict:
    return {
        "nhl_game_id": game["id"],
        "game_type": game["gameType"],
        "game_state": game["gameState"],
    }


def schedule_game_from_play_by_play(pbp: dict) -> dict:
    """Play-by-play's top-level id/gameType/gameState/homeTeam/awayTeam match the
    schedule endpoint's game-object shape exactly (verified live) -- used by --game-id
    manual runs so they don't need a separate schedule lookup to find the game's date."""
    return {
        "id": pbp["id"],
        "gameType": pbp["gameType"],
        "gameState": pbp["gameState"],
        "homeTeam": pbp["homeTeam"],
        "awayTeam": pbp["awayTeam"],
    }


def team_from_schedule_side(side: dict) -> dict:
    return {
        "nhl_team_id": side["id"],
        "abbreviation": side["abbrev"],
        "team_name": (side.get("commonName") or {}).get("default") or side["abbrev"],
        "location": (side.get("placeName") or {}).get("default"),
    }


# ---------------------------------------------------------------------------
# Play-by-play: rosterSpots[]
# ---------------------------------------------------------------------------

def player_from_roster_spot(spot: dict) -> dict:
    first = (spot.get("firstName") or {}).get("default")
    last = (spot.get("lastName") or {}).get("default")
    full_name = " ".join(p for p in (first, last) if p) or str(spot["playerId"])
    return {
        "nhl_player_id": spot["playerId"],
        "nhl_team_id": spot["teamId"],
        "first_name": first,
        "last_name": last,
        "full_name": full_name,
        "position_code": spot.get("positionCode"),
    }


# ---------------------------------------------------------------------------
# Play-by-play: plays[]
# ---------------------------------------------------------------------------

def _primary_player_id(event_type: str, details: dict):
    if event_type == "goal":
        return details.get("scoringPlayerId")
    if event_type in ("shot-on-goal", "missed-shot", "blocked-shot"):
        return details.get("shootingPlayerId")
    if event_type == "faceoff":
        return details.get("winningPlayerId")
    if event_type == "penalty":
        return details.get("committedByPlayerId")
    if event_type in ("giveaway", "takeaway"):
        return details.get("playerId")
    return None


def _secondary_player_id(event_type: str, details: dict):
    if event_type == "goal":
        return details.get("assist1PlayerId")
    if event_type == "blocked-shot":
        return details.get("blockingPlayerId")
    if event_type == "faceoff":
        return details.get("losingPlayerId")
    if event_type == "penalty":
        return details.get("drawnByPlayerId")
    return None


def play_fields(play: dict) -> dict:
    details = play.get("details") or {}
    event_type = play["typeDescKey"]
    return {
        "nhl_play_id": play["eventId"],
        "period_number": play["periodDescriptor"]["number"],
        "period_time_seconds": parse_clock(play.get("timeInPeriod")),
        "period_time_remaining": parse_clock(play.get("timeRemaining")),
        "event_type": event_type,
        "team_nhl_id": details.get("eventOwnerTeamId"),
        "player_nhl_id": _primary_player_id(event_type, details),
        "secondary_player_nhl_id": _secondary_player_id(event_type, details),
        "x_coord": details.get("xCoord"),
        "y_coord": details.get("yCoord"),
        "home_score": details.get("homeScore"),
        "away_score": details.get("awayScore"),
        "strength_code": play.get("situationCode"),
        "home_defending_side": play.get("homeTeamDefendingSide"),
    }


def shot_fields(play: dict) -> dict:
    details = play.get("details") or {}
    event_type = play["typeDescKey"]
    is_goal = event_type == "goal"
    shooter_id = details.get("scoringPlayerId") if is_goal else details.get("shootingPlayerId")
    return {
        "team_nhl_id": details.get("eventOwnerTeamId"),
        "shooter_nhl_player_id": shooter_id,
        "goalie_nhl_player_id": details.get("goalieInNetId"),
        "period_number": play["periodDescriptor"]["number"],
        "period_time_seconds": parse_clock(play.get("timeInPeriod")),
        "shot_event_type": event_type,
        "shot_type": details.get("shotType"),
        "x_coord": details.get("xCoord"),
        "y_coord": details.get("yCoord"),
        "is_goal": is_goal,
        "is_blocked": event_type == "blocked-shot",
        "is_missed": event_type == "missed-shot",
        "strength_code": play.get("situationCode"),
        "home_defending_side": play.get("homeTeamDefendingSide"),
        "home_score": details.get("homeScore"),
        "away_score": details.get("awayScore"),
    }


def goal_fields(play: dict) -> dict:
    details = play.get("details") or {}
    return {
        "team_nhl_id": details.get("eventOwnerTeamId"),
        "scoring_nhl_player_id": details.get("scoringPlayerId"),
        "assist1_nhl_player_id": details.get("assist1PlayerId"),
        "assist2_nhl_player_id": details.get("assist2PlayerId"),
        "goalie_nhl_player_id": details.get("goalieInNetId"),
        "period_number": play["periodDescriptor"]["number"],
        "period_time_seconds": parse_clock(play.get("timeInPeriod")),
        "strength_code": play.get("situationCode"),
        "home_score": details.get("homeScore"),
        "away_score": details.get("awayScore"),
        "highlight_clip_id": details.get("highlightClip"),
        "discrete_clip_id": details.get("discreteClip"),
        "clip_sharing_url": details.get("highlightClipSharingUrl"),
    }


# ---------------------------------------------------------------------------
# Boxscore: playerByGameStats.{homeTeam,awayTeam}.{forwards,defense,goalies}[]
# ---------------------------------------------------------------------------

def skater_boxscore_fields(row: dict) -> dict:
    return {
        "nhl_player_id": row["playerId"],
        "position_code": row.get("position"),
        "goals": row.get("goals"),
        "assists": row.get("assists"),
        "points": row.get("points"),
        "shots": row.get("sog"),
        "hits": row.get("hits"),
        "blocks": row.get("blockedShots"),
        "giveaways": row.get("giveaways"),
        "takeaways": row.get("takeaways"),
        "penalty_minutes": row.get("pim"),
        "time_on_ice_seconds": parse_clock(row.get("toi")),
        "power_play_goals": row.get("powerPlayGoals"),
    }


def goalie_boxscore_fields(row: dict) -> dict:
    return {
        "nhl_player_id": row["playerId"],
        "position_code": row.get("position") or "G",
        "shots_against": row.get("shotsAgainst"),
        "saves": row.get("saves"),
        "goals_against": row.get("goalsAgainst"),
        "penalty_minutes": row.get("pim"),
        "time_on_ice_seconds": parse_clock(row.get("toi")),
    }


# ---------------------------------------------------------------------------
# Shift charts: data[]
# ---------------------------------------------------------------------------

def shift_fields(row: dict) -> dict:
    return {
        "nhl_player_id": row["playerId"],
        "nhl_team_id": row["teamId"],
        "period_number": row["period"],
        "shift_start_seconds": parse_clock(row.get("startTime")),
        "shift_end_seconds": parse_clock(row.get("endTime")),
        "duration_seconds": parse_clock(row.get("duration")),
    }


# ---------------------------------------------------------------------------
# Draft: draft/picks/{year}/all -> picks[]
# ---------------------------------------------------------------------------

def draft_pick_fields(pick: dict) -> dict:
    first = (pick.get("firstName") or {}).get("default")
    last = (pick.get("lastName") or {}).get("default")
    full_name = " ".join(p for p in (first, last) if p) or None
    return {
        "first_name": first,
        "last_name": last,
        "full_name": full_name,
        "position_code": pick.get("positionCode"),
        "height_inches": pick.get("height"),
        "weight_lbs": pick.get("weight"),
        "team_abbrev": pick.get("teamAbbrev"),
        "overall_pick": pick.get("overallPick"),
        "round_number": pick.get("round"),
    }


# ---------------------------------------------------------------------------
# Player search: search.d3.nhle.com/api/v1/search/player -> [ {...}, ... ]
# ---------------------------------------------------------------------------

def player_search_result_fields(result: dict) -> dict:
    return {
        "nhl_player_id": int(result["playerId"]),
        "name": result.get("name"),
        "team_abbrev": result.get("teamAbbrev"),
        "height_inches": result.get("heightInInches"),
        "weight_lbs": result.get("weightInPounds"),
    }


# ---------------------------------------------------------------------------
# ESPN fantasy: games/fhl/seasons/{year}/players -> [ {...}, ... ]
# ---------------------------------------------------------------------------

# ESPN's roster "lineup slot" IDs (eligibleSlots), NOT the same enum as defaultPositionId --
# verified live: only the concrete positions are meaningful eligibility facts, the rest
# (3=flex Forward, 6=UTIL, 7=Bench, 8=IR, ...) are roster-management slots implied by having
# one of the concrete positions, not separate eligibility information.
ESPN_SLOT_POSITION = {0: "C", 1: "LW", 2: "RW", 4: "D", 5: "G"}


def espn_player_fields(player: dict) -> dict:
    return {
        "espn_player_id": player["id"],
        "full_name": player.get("fullName"),
        "average_draft_position": (player.get("ownership") or {}).get("averageDraftPosition"),
        "position_codes": [ESPN_SLOT_POSITION[s] for s in player.get("eligibleSlots", []) if s in ESPN_SLOT_POSITION],
    }


# ---------------------------------------------------------------------------
# Fleaflicker: FetchPlayerListing -> players[] (players[].proPlayer)
# ---------------------------------------------------------------------------

def fleaflicker_player_fields(player: dict) -> dict:
    pro_player = player.get("proPlayer") or {}
    return {
        "fleaflicker_player_id": pro_player.get("id"),
        "full_name": pro_player.get("nameFull"),
        "position_codes": pro_player.get("positionEligibility") or [],
    }


# ---------------------------------------------------------------------------
# Schedule: schedule/{date} -> gameWeek[].games[]
# ---------------------------------------------------------------------------

def schedule_row_fields(game: dict, game_date: str) -> dict:
    return {
        "nhl_game_id": game["id"],
        "game_type": game.get("gameType"),
        "game_date": game_date,
        "start_time_utc": game.get("startTimeUTC"),
        "home_nhl_team_id": game["homeTeam"]["id"],
        "away_nhl_team_id": game["awayTeam"]["id"],
        "game_state": game.get("gameState"),
    }

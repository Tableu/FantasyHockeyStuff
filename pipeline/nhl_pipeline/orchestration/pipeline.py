"""Runs the full ingestion workflow for one game, in dependency order:
fetch -> teams/players -> game -> raw responses -> plays -> shots -> goals -> shifts ->
on-ice players -> official stats -> xG -> on-ice analytics -> individual analytics ->
goalie analytics. Every stage is wrapped by logging_utils.run_stage, which records
SUCCESS/FAILED to Ingestion.IngestionRuns and re-raises on failure so run_daily's outer
loop can skip to the next game without losing the failure record.

on_ice_stats runs before individual_stats (a reordering from the source report's listed
step numbers) because IndividualCorsiForPct is defined relative to the player's own on-ice
CorsiFor, which on_ice_stats produces.
"""

from nhl_pipeline import db
from nhl_pipeline.api import boxscore as api_boxscore
from nhl_pipeline.api import play_by_play as api_play_by_play
from nhl_pipeline.api import shift_charts as api_shift_charts
from nhl_pipeline.calc import goalie_stats, individual_stats, on_ice_stats, situation_resolver, strength_toi, xg_model
from nhl_pipeline.ingest import games, goals, official_stats, on_ice, raw_responses
from nhl_pipeline.ingest import plays as ingest_plays
from nhl_pipeline.ingest import shifts as ingest_shifts
from nhl_pipeline.ingest import shots as ingest_shots
from nhl_pipeline.ingest import teams_players
from nhl_pipeline.orchestration import logging_utils

TERMINAL_STAGE = "GOALIE_STATS"


def run_game(conn, schedule_game: dict, game_date: str, season_id: int) -> None:
    nhl_game_id = schedule_game["id"]
    cursor = conn.cursor()

    try:
        pbp = logging_utils.run_stage(cursor, None, season_id, "FETCH_PLAY_BY_PLAY", api_play_by_play.get_play_by_play, nhl_game_id)
        box = logging_utils.run_stage(cursor, None, season_id, "FETCH_BOXSCORE", api_boxscore.get_boxscore, nhl_game_id)
        shift_data = logging_utils.run_stage(cursor, None, season_id, "FETCH_SHIFT_CHARTS", api_shift_charts.get_shift_charts, nhl_game_id)
        conn.commit()

        team_id_by_nhl = logging_utils.run_stage(
            cursor, None, season_id, "TEAMS",
            teams_players.sync_teams, cursor, schedule_game["homeTeam"], schedule_game["awayTeam"],
        )
        conn.commit()

        home_team_id = team_id_by_nhl[schedule_game["homeTeam"]["id"]]
        away_team_id = team_id_by_nhl[schedule_game["awayTeam"]["id"]]

        game_id = logging_utils.run_stage(
            cursor, None, season_id, "GAME", games.ensure_game, cursor,
            nhl_game_id=nhl_game_id, season_id=season_id, game_type=schedule_game["gameType"],
            game_date=game_date, home_team_id=home_team_id, away_team_id=away_team_id,
            home_score=schedule_game["homeTeam"].get("score"), away_score=schedule_game["awayTeam"].get("score"),
            game_state=schedule_game["gameState"],
        )
        conn.commit()

        def stage(name, fn, *a, **kw):
            return logging_utils.run_stage(cursor, game_id, season_id, name, fn, *a, **kw)

        stage("RAW_PLAY_BY_PLAY", raw_responses.save_raw_response, cursor, game_id, "PLAY_BY_PLAY", pbp)
        stage("RAW_BOXSCORE", raw_responses.save_raw_response, cursor, game_id, "BOXSCORE", box)
        stage("RAW_SHIFT_CHARTS", raw_responses.save_raw_response, cursor, game_id, "SHIFT_CHARTS", shift_data)
        conn.commit()

        player_id_by_nhl = stage("PLAYERS", teams_players.sync_players, cursor, pbp.get("rosterSpots", []))
        conn.commit()

        plays_raw = pbp.get("plays", [])

        play_id_by_nhl = stage(
            "PLAYS", ingest_plays.sync_plays, cursor, game_id, plays_raw, team_id_by_nhl, player_id_by_nhl
        )
        conn.commit()

        shot_id_by_nhl = stage(
            "SHOTS", ingest_shots.sync_shots, cursor, game_id, plays_raw,
            play_id_by_nhl, team_id_by_nhl, player_id_by_nhl, home_team_id,
        )
        conn.commit()

        code_map = situation_resolver.load_situation_code_map(cursor)
        stage(
            "GOALS", goals.sync_goals, cursor, game_id, plays_raw,
            play_id_by_nhl, shot_id_by_nhl, team_id_by_nhl, player_id_by_nhl, code_map, home_team_id,
        )
        conn.commit()

        stage(
            "SHIFTS", ingest_shifts.sync_shifts, cursor, game_id,
            shift_data.get("data", []), team_id_by_nhl, player_id_by_nhl,
        )
        conn.commit()

        stage("ON_ICE_PLAYERS", on_ice.derive_on_ice_players, cursor, game_id)
        conn.commit()

        faceoff_counts = official_stats.count_faceoffs(plays_raw)
        stage(
            "OFFICIAL_STATS", official_stats.sync_player_game_stats,
            cursor, game_id, box, team_id_by_nhl, player_id_by_nhl, faceoff_counts,
        )
        stage("OFFICIAL_STATS_PP_SH", official_stats.apply_pp_sh_goals_assists, cursor, game_id)
        stage("STRENGTH_TOI", strength_toi.apply_toi_by_strength, cursor, game_id, code_map)
        conn.commit()

        xg_version_id = db.fetch_scalar(
            cursor, "SELECT CalculationVersionID FROM Analytics.CalculationVersions WHERE MetricCode = 'XG' AND IsActive = 1"
        )
        corsi_version_id = db.fetch_scalar(
            cursor, "SELECT CalculationVersionID FROM Analytics.CalculationVersions WHERE MetricCode = 'CORSI' AND IsActive = 1"
        )

        stage("XG_MODEL", xg_model.run_xg_for_game, cursor, game_id, xg_version_id)
        conn.commit()

        unmapped_codes: set = set()
        stage("ON_ICE_STATS", on_ice_stats.compute_and_store, cursor, game_id, corsi_version_id, xg_version_id, unmapped_codes)
        stage("INDIVIDUAL_STATS", individual_stats.compute_and_store, cursor, game_id, corsi_version_id, xg_version_id, unmapped_codes)
        stage(TERMINAL_STAGE, goalie_stats.compute_and_store, cursor, game_id, xg_version_id, unmapped_codes)
        conn.commit()

        logging_utils.log_unmapped_situation_codes(cursor, game_id, unmapped_codes)
        conn.commit()

    except Exception:
        # Commit (not rollback) so the FAILED row run_stage() just logged to
        # Ingestion.IngestionRuns survives -- rolling back would erase the failure record
        # along with it. Any partial writes from the failed stage are harmless: every
        # table is upserted idempotently, so a retry simply overwrites them.
        conn.commit()
        raise

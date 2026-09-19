"""One query per source, season-scoped, each returning a tidy DataFrame. Nothing here
derives a feature -- it only pulls the raw per-game facts that rolling/context/injuries turn
into features, so every SQL statement stays readable and every join key is explicit.

Read-only by construction (see nhlstats_db.py): all SELECTs.

Notes on the sources, measured 2026-09-18:

  * TOI comes from Stats.PlayerGameStats, which is boxscore-sourced and authoritative.
    Shift-derived seconds (Lineups.GameLineups.Period1EVSeconds, the PP/SH splits) are good
    to a few seconds per game but not exact: 129 of 99,986 player-games still exceed the
    boxscore total, a feed artefact upstream of us.
  * The Analytics tables only carry a row where the player had events in that situation --
    83,597 rows at 5v5 against 23,292 at 5v4 and 4,966 at 4v5. A missing row therefore means
    "no events", not "unknown", whenever the player had TOI in that situation; base.py
    coalesces accordingly.
  * Game.Games.StartTimeUTC and Venue are NULL/empty for every game, so start time comes
    from Reference.Schedule (populated for all 35,901 rows) and the arena is the home team.
"""

import pandas as pd

SITUATION_ALL = 1
SITUATION_5V5 = 2
SITUATION_5V4 = 3
SITUATION_4V5 = 4

SHOT_EVENTS = ("shot-on-goal", "missed-shot", "goal")


def _frame(cursor, sql: str, season_ids: list) -> pd.DataFrame:
    """Runs one season-scoped query. `{seasons}` may appear more than once (a CTE and its
    consumer each filtering by season), so the parameters are repeated to match."""
    placeholders = ",".join("?" * len(season_ids))
    repeats = sql.count("{seasons}")
    cursor.execute(sql.format(seasons=placeholders), *(list(season_ids) * repeats))
    columns = [d[0] for d in cursor.description]
    return pd.DataFrame.from_records(cursor.fetchall(), columns=columns)


def games(cursor, season_ids: list) -> pd.DataFrame:
    """One row per game: teams, date, and the start time from Reference.Schedule."""
    return _frame(cursor, """
        SELECT g.GameID AS game_id, g.SeasonID AS season_id, g.NHLGameID AS nhl_game_id,
               g.GameDate AS game_date, g.HomeTeamID AS home_team_id, g.AwayTeamID AS away_team_id,
               g.HomeScore AS home_score, g.AwayScore AS away_score,
               sc.StartTimeUTC AS start_time_utc
        FROM Game.Games g
        LEFT JOIN Reference.Schedule sc ON sc.NHLGameID = g.NHLGameID
        WHERE g.SeasonID IN ({seasons})
        ORDER BY g.GameDate, g.GameID
    """, season_ids)


def player_games(cursor, season_ids: list) -> pd.DataFrame:
    """One row per (game, team, player) who played, with the boxscore counting stats and the
    shift-derived deployment columns from Lineups.GameLineups."""
    return _frame(cursor, """
        SELECT g.SeasonID AS season_id, pgs.GameID AS game_id, g.GameDate AS game_date,
               pgs.TeamID AS team_id, pgs.PlayerID AS player_id, pgs.PositionCode AS position,
               pgs.Goals AS goals, pgs.Assists AS assists, pgs.Points AS points,
               pgs.Shots AS shots, pgs.Hits AS hits, pgs.Blocks AS blocks,
               pgs.Giveaways AS giveaways, pgs.Takeaways AS takeaways,
               pgs.PenaltyMinutes AS pim,
               pgs.FaceoffWins AS faceoff_wins, pgs.FaceoffLosses AS faceoff_losses,
               pgs.TimeOnIceSeconds AS toi,
               pgs.PowerPlayTOISeconds AS pp_toi, pgs.ShortHandedTOISeconds AS sh_toi,
               -- NULL, not 0, for a player with no points at all: COALESCE or 65% of
               -- played rows arrive as NULL and only scorers reach the PP/SH models.
               COALESCE(pgs.PowerPlayGoals, 0) + COALESCE(pgs.PowerPlayAssists, 0) AS ppp,
               COALESCE(pgs.ShortHandedGoals, 0) + COALESCE(pgs.ShortHandedAssists, 0) AS shp,
               l.Period1EVSeconds AS p1_ev_seconds,
               l.ForwardLine AS actual_line, l.DefensePair AS actual_pair,
               l.PowerPlayUnit AS actual_pp, l.PenaltyKillUnit AS actual_pk,
               l.Dressed AS dressed
        FROM Stats.PlayerGameStats pgs
        JOIN Game.Games g ON g.GameID = pgs.GameID
        LEFT JOIN Lineups.GameLineups l ON l.GameID = pgs.GameID AND l.PlayerID = pgs.PlayerID
        WHERE g.SeasonID IN ({seasons})
        ORDER BY pgs.PlayerID, g.GameDate, pgs.GameID
    """, season_ids)


def player_individual_analytics(cursor, season_ids: list) -> pd.DataFrame:
    """Individual Corsi/Fenwick/xG per (game, player, situation), long form."""
    return _frame(cursor, f"""
        SELECT a.GameID AS game_id, a.PlayerID AS player_id, a.SituationID AS situation_id,
               a.IndividualCorsiFor AS icf, a.IndividualFenwickFor AS iff,
               a.IndividualExpectedGoals AS ixg, a.IndividualScoringChances AS iscf,
               a.IndividualHighDangerChances AS ihdcf
        FROM Analytics.PlayerGameAdvancedStats a
        JOIN Game.Games g ON g.GameID = a.GameID
        WHERE g.SeasonID IN ({{seasons}})
          AND a.SituationID IN ({SITUATION_ALL}, {SITUATION_5V5}, {SITUATION_5V4}, {SITUATION_4V5})
    """, season_ids)


def player_onice_analytics(cursor, season_ids: list) -> pd.DataFrame:
    """On-ice rate stats per (game, player, situation), long form. Derived from
    Game.PlayOnIcePlayers, whose interval join was corrected on 2026-09-18."""
    return _frame(cursor, f"""
        SELECT o.GameID AS game_id, o.PlayerID AS player_id, o.SituationID AS situation_id,
               o.TimeOnIceSeconds AS oi_toi, o.CorsiForPct AS oi_cf_pct,
               o.ExpectedGoalsPct AS oi_xgf_pct, o.OnIceShootingPct AS oi_sh_pct,
               o.PDO AS oi_pdo, o.CorsiFor AS oi_cf, o.CorsiAgainst AS oi_ca,
               o.ExpectedGoalsFor AS oi_xgf, o.ExpectedGoalsAgainst AS oi_xga
        FROM Analytics.PlayerGameOnIceStats o
        JOIN Game.Games g ON g.GameID = o.GameID
        WHERE g.SeasonID IN ({{seasons}})
          AND o.SituationID IN ({SITUATION_ALL}, {SITUATION_5V5})
    """, season_ids)


def team_games(cursor, season_ids: list) -> pd.DataFrame:
    """One row per (game, team): shots/xG for and against, the counting stats its players
    recorded, PP opportunities (= penalties taken by the opponent) and penalties taken."""
    return _frame(cursor, f"""
        WITH shots AS (
            SELECT s.GameID, s.TeamID,
                   COUNT(*) AS attempts,
                   SUM(CASE WHEN s.IsGoal = 1 THEN 1 ELSE 0 END) AS goals,
                   SUM(ISNULL(xg.ExpectedGoals, 0)) AS xg
            FROM Game.Shots s
            LEFT JOIN Analytics.ShotExpectedGoals xg ON xg.ShotID = s.ShotID
            WHERE s.ShotEventType IN ({",".join("'" + e + "'" for e in SHOT_EVENTS)})
            GROUP BY s.GameID, s.TeamID
        ),
        skaters AS (
            SELECT pgs.GameID, pgs.TeamID,
                   SUM(pgs.Hits) AS hits, SUM(pgs.Blocks) AS blocks,
                   SUM(pgs.PenaltyMinutes) AS pim,
                   SUM(pgs.PowerPlayGoals) AS pp_goals,
                   SUM(pgs.TimeOnIceSeconds) AS toi
            FROM Stats.PlayerGameStats pgs
            GROUP BY pgs.GameID, pgs.TeamID
        ),
        penalties AS (
            SELECT p.GameID, p.TeamID, COUNT(*) AS penalties_taken
            FROM Game.Plays p
            WHERE p.EventType = 'penalty' AND p.TeamID IS NOT NULL
            GROUP BY p.GameID, p.TeamID
        )
        SELECT g.SeasonID AS season_id, g.GameID AS game_id, g.GameDate AS game_date,
               t.TeamID AS team_id, o.TeamID AS opp_team_id,
               CASE WHEN t.TeamID = g.HomeTeamID THEN 1 ELSE 0 END AS is_home,
               ISNULL(sf.attempts, 0) AS shots_for, ISNULL(sa.attempts, 0) AS shots_against,
               ISNULL(sf.goals, 0) AS goals_for, ISNULL(sa.goals, 0) AS goals_against,
               ISNULL(sf.xg, 0) AS xgf, ISNULL(sa.xg, 0) AS xga,
               ISNULL(kf.hits, 0) AS hits_for, ISNULL(ka.hits, 0) AS hits_against,
               ISNULL(kf.blocks, 0) AS blocks_for, ISNULL(ka.blocks, 0) AS blocks_against,
               ISNULL(kf.pim, 0) AS pim_for, ISNULL(kf.pp_goals, 0) AS pp_goals,
               ISNULL(kf.toi, 0) AS skater_toi,
               ISNULL(pa.penalties_taken, 0) AS pp_opportunities,
               ISNULL(pf.penalties_taken, 0) AS penalties_taken
        FROM Game.Games g
        CROSS APPLY (VALUES (g.HomeTeamID, g.AwayTeamID), (g.AwayTeamID, g.HomeTeamID)) AS v(TeamID, OppTeamID)
        CROSS APPLY (SELECT v.TeamID AS TeamID) AS t
        CROSS APPLY (SELECT v.OppTeamID AS TeamID) AS o
        LEFT JOIN shots sf ON sf.GameID = g.GameID AND sf.TeamID = t.TeamID
        LEFT JOIN shots sa ON sa.GameID = g.GameID AND sa.TeamID = o.TeamID
        LEFT JOIN skaters kf ON kf.GameID = g.GameID AND kf.TeamID = t.TeamID
        LEFT JOIN skaters ka ON ka.GameID = g.GameID AND ka.TeamID = o.TeamID
        LEFT JOIN penalties pf ON pf.GameID = g.GameID AND pf.TeamID = t.TeamID
        LEFT JOIN penalties pa ON pa.GameID = g.GameID AND pa.TeamID = o.TeamID
        WHERE g.SeasonID IN ({{seasons}})
        ORDER BY t.TeamID, g.GameDate, g.GameID
    """, season_ids)


def zone_starts(cursor, season_ids: list) -> pd.DataFrame:
    """Offensive / defensive / neutral zone faceoffs each player was on the ice for.

    Two mechanics, both validated on the full database (2026-09-18):

    Attacking side is exact rather than inferred from a heuristic: Game.Shots.XCoordinate is
    normalised attacking-right while Game.Plays.XCoordinate is the raw coordinate, so the
    sign of their product, summed over a team's shots in a period, gives the direction that
    team was attacking. Over 16,769 team-periods none came out undetermined, the two teams
    never agreed on a direction in the same period, and no team ever failed to flip ends
    between consecutive periods. Blocked shots are excluded because their event team is the
    blocking side, not the shooting side.

    "On the ice for the faceoff" uses a half-open interval (start <= t < end), the same
    convention as the corrected Game.PlayOnIcePlayers derivation: 5.73 players per
    team-faceoff against 8.42 for a both-ends-inclusive join, which counts the line leaving
    the ice as well as the one arriving.

    Faceoff x is populated for all 147,792 faceoffs and clusters at 0 (centre), +/-20
    (neutral-zone dots) and +/-69 (end-zone dots), so +/-25 separates the zones cleanly.
    """
    return _frame(cursor, f"""
        WITH side AS (
            SELECT sh.GameID, sh.TeamID, sh.PeriodNumber,
                   SIGN(SUM(p.XCoordinate * sh.XCoordinate)) AS attack_sign
            FROM Game.Shots sh
            JOIN Game.Plays p ON p.PlayID = sh.PlayID
            JOIN Game.Games g ON g.GameID = sh.GameID
            WHERE g.SeasonID IN ({{seasons}})
              AND sh.ShotEventType IN ({",".join("'" + e + "'" for e in SHOT_EVENTS)})
              AND p.XCoordinate IS NOT NULL AND sh.XCoordinate IS NOT NULL AND sh.XCoordinate <> 0
            GROUP BY sh.GameID, sh.TeamID, sh.PeriodNumber
        ),
        -- A team with no shots of its own in a period (short overtimes) still needs a side:
        -- take the opponent's and negate it.
        side_filled AS (
            SELECT s.GameID, s.TeamID, s.PeriodNumber, s.attack_sign FROM side s
            UNION ALL
            SELECT o.GameID, v.TeamID, o.PeriodNumber, -o.attack_sign
            FROM side o
            JOIN Game.Games g ON g.GameID = o.GameID
            CROSS APPLY (VALUES (CASE WHEN o.TeamID = g.HomeTeamID THEN g.AwayTeamID ELSE g.HomeTeamID END)) AS v(TeamID)
            WHERE NOT EXISTS (SELECT 1 FROM side s2
                              WHERE s2.GameID = o.GameID AND s2.TeamID = v.TeamID AND s2.PeriodNumber = o.PeriodNumber)
        ),
        faceoffs AS (
            SELECT p.PlayID, p.GameID, p.PeriodNumber, p.PeriodTimeSeconds, p.XCoordinate
            FROM Game.Plays p
            JOIN Game.Games g ON g.GameID = p.GameID
            WHERE g.SeasonID IN ({{seasons}}) AND p.EventType = 'faceoff' AND p.XCoordinate IS NOT NULL
        )
        SELECT sh.GameID AS game_id, sh.PlayerID AS player_id, sh.TeamID AS team_id,
               SUM(CASE WHEN sd.attack_sign * f.XCoordinate >  25 THEN 1 ELSE 0 END) AS oz_starts,
               SUM(CASE WHEN sd.attack_sign * f.XCoordinate < -25 THEN 1 ELSE 0 END) AS dz_starts,
               SUM(CASE WHEN ABS(f.XCoordinate) <= 25 THEN 1 ELSE 0 END) AS nz_starts
        FROM faceoffs f
        JOIN Game.Shifts sh
          ON sh.GameID = f.GameID AND sh.PeriodNumber = f.PeriodNumber
         AND sh.ShiftStartSeconds <= f.PeriodTimeSeconds AND sh.ShiftEndSeconds > f.PeriodTimeSeconds
        JOIN side_filled sd
          ON sd.GameID = f.GameID AND sd.TeamID = sh.TeamID AND sd.PeriodNumber = f.PeriodNumber
        GROUP BY sh.GameID, sh.PlayerID, sh.TeamID
    """, season_ids)


def goalie_games(cursor, season_ids: list) -> pd.DataFrame:
    """One row per (game, goalie) at all situations, for the opposing-goalie feature."""
    return _frame(cursor, f"""
        SELECT a.GameID AS game_id, g.GameDate AS game_date, g.SeasonID AS season_id,
               a.GoaliePlayerID AS player_id, a.TeamID AS team_id,
               a.ShotsAgainst AS shots_against, a.Saves AS saves, a.GoalsAgainst AS goals_against,
               a.ExpectedGoalsAgainst AS xga, a.GoalsSavedAboveExpected AS gsax
        FROM Analytics.GoalieGameAdvancedStats a
        JOIN Game.Games g ON g.GameID = a.GameID
        WHERE g.SeasonID IN ({{seasons}}) AND a.SituationID = {SITUATION_ALL}
        ORDER BY a.GoaliePlayerID, g.GameDate, a.GameID
    """, season_ids)


def season_ids_for(cursor, display_names: list) -> dict:
    """{DisplayName: SeasonID} -- the CLI takes '2025-26', everything else keys on SeasonID."""
    placeholders = ",".join("?" * len(display_names))
    cursor.execute(
        f"SELECT DisplayName, SeasonID FROM Reference.Seasons WHERE DisplayName IN ({placeholders})",
        *display_names,
    )
    found = {r.DisplayName: r.SeasonID for r in cursor.fetchall()}
    missing = set(display_names) - set(found)
    if missing:
        raise SystemExit(f"Unknown season(s): {', '.join(sorted(missing))}")
    return found


def prior_season_ids(cursor, season_ids: list) -> dict:
    """{SeasonID: prior SeasonID or None} for the prev-season family.

    A prior season only counts if it is actually *ingested* -- Reference.Seasons holds a row
    for every season back to 2000-01 (import_injury_history.py fills them), but only 2024-25
    and 2025-26 have games. So 2025-26 gets a real prior season and 2024-25 gets None, which
    is why every prev_* column is NULL on 2024-25 rows."""
    cursor.execute("""
        SELECT s.SeasonID, s.NHLSeasonID,
               CASE WHEN EXISTS (SELECT 1 FROM Game.Games g WHERE g.SeasonID = s.SeasonID) THEN 1 ELSE 0 END AS ingested
        FROM Reference.Seasons s
    """)
    rows = cursor.fetchall()
    by_nhl = {r.NHLSeasonID: r.SeasonID for r in rows if r.ingested}
    nhl_by_id = {r.SeasonID: r.NHLSeasonID for r in rows}
    out = {}
    for season_id in season_ids:
        start = int(str(nhl_by_id[season_id])[:4]) - 1
        out[season_id] = by_nhl.get(int(f"{start}{start + 1}"))
    return out

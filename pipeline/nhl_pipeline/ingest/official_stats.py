"""Stats.PlayerGameStats / PlayerSeasonStats.

The boxscore endpoint doesn't provide FaceoffWins/Losses as raw counts (only a
percentage), and provides no PowerPlayTOISeconds/ShortHandedTOISeconds/PowerPlayAssists/
ShortHandedGoals/ShortHandedAssists at all (confirmed live during planning). Resolution:
  - FaceoffWins/Losses are counted from play-by-play faceoff events instead (more accurate
    than back-solving a percentage, and consistent with "events are source of truth").
  - PowerPlayAssists/ShortHandedGoals/ShortHandedAssists are derived from our own
    Game.Goals rows (IsPowerPlay/IsShortHanded), via apply_pp_sh_goals_assists() below.
  - PowerPlayTOISeconds/ShortHandedTOISeconds are populated by calc.strength_toi, which
    reconstructs strength-state segments from Game.Plays.StrengthCode and overlaps them with
    Game.Shifts -- necessary because no endpoint reports per-strength ice time directly, and
    it's only as precise as the nearest play event (see that module's docstring).

PlayerSeasonStats carries the same FaceoffWins/FaceoffLosses/PowerPlayAssists/
PowerPlayTOISeconds/ShortHandedGoals/ShortHandedAssists/ShortHandedTOISeconds columns,
populated by sync_player_season_stats() below as a per-player-per-season SUM() over
PlayerGameStats -- so any correction applied at the game level (e.g. an
apply_pp_sh_goals_assists() or strength_toi rerun) only reaches PlayerSeasonStats once
sync_player_season_stats() is rerun for the affected season(s).
PlayerSeasonStats.AverageTOIMinutes is a separate, persisted computed column defined in the
schema directly (TimeOnIceSeconds / 60 / GamesPlayed) rather than written by this module.
"""

from nhl_pipeline import db
from nhl_pipeline.api import field_map


def count_faceoffs(plays_raw: list) -> dict:
    """Returns {NHLPlayerID: (wins, losses)}."""
    counts: dict = {}
    for play in plays_raw:
        if play["typeDescKey"] != "faceoff":
            continue
        details = play.get("details") or {}
        winner = details.get("winningPlayerId")
        loser = details.get("losingPlayerId")
        if winner is not None:
            w, l = counts.get(winner, (0, 0))
            counts[winner] = (w + 1, l)
        if loser is not None:
            w, l = counts.get(loser, (0, 0))
            counts[loser] = (w, l + 1)
    return counts


def sync_player_game_stats(
    cursor, game_id: int, boxscore: dict, team_id_by_nhl: dict, player_id_by_nhl: dict, faceoff_counts: dict,
) -> None:
    stats_block = boxscore["playerByGameStats"]
    sides = (("homeTeam", boxscore["homeTeam"]["id"]), ("awayTeam", boxscore["awayTeam"]["id"]))

    for side_key, team_nhl_id in sides:
        side = stats_block[side_key]
        team_id = team_id_by_nhl.get(team_nhl_id)
        if team_id is None:
            continue

        for group in ("forwards", "defense"):
            for row in side.get(group, []):
                f = field_map.skater_boxscore_fields(row)
                player_id = player_id_by_nhl.get(f["nhl_player_id"])
                if player_id is None:
                    continue
                wins, losses = faceoff_counts.get(f["nhl_player_id"], (0, 0))
                db.upsert(
                    cursor, "Stats.PlayerGameStats",
                    {"GameID": game_id, "PlayerID": player_id, "TeamID": team_id},
                    {
                        "PositionCode": f["position_code"],
                        "Goals": f["goals"], "Assists": f["assists"], "Points": f["points"],
                        "Shots": f["shots"], "Hits": f["hits"], "Blocks": f["blocks"],
                        "Giveaways": f["giveaways"], "Takeaways": f["takeaways"],
                        "PenaltyMinutes": f["penalty_minutes"],
                        "FaceoffWins": wins, "FaceoffLosses": losses,
                        "TimeOnIceSeconds": f["time_on_ice_seconds"],
                        "PowerPlayGoals": f["power_play_goals"],
                    },
                )

        for row in side.get("goalies", []):
            f = field_map.goalie_boxscore_fields(row)
            player_id = player_id_by_nhl.get(f["nhl_player_id"])
            if player_id is None:
                continue
            db.upsert(
                cursor, "Stats.PlayerGameStats",
                {"GameID": game_id, "PlayerID": player_id, "TeamID": team_id},
                {
                    "PositionCode": f["position_code"],
                    "PenaltyMinutes": f["penalty_minutes"],
                    "TimeOnIceSeconds": f["time_on_ice_seconds"],
                },
            )


def apply_pp_sh_goals_assists(cursor, game_id: int) -> None:
    cursor.execute(
        """
        UPDATE pgs SET PowerPlayAssists = agg.PPAssists, ShortHandedGoals = agg.SHGoals, ShortHandedAssists = agg.SHAssists
        FROM Stats.PlayerGameStats pgs
        JOIN (
            SELECT PlayerID, GameID,
                   SUM(CASE WHEN IsAssist = 1 AND IsPowerPlay = 1 THEN 1 ELSE 0 END) AS PPAssists,
                   SUM(CASE WHEN IsAssist = 0 AND IsShortHanded = 1 THEN 1 ELSE 0 END) AS SHGoals,
                   SUM(CASE WHEN IsAssist = 1 AND IsShortHanded = 1 THEN 1 ELSE 0 END) AS SHAssists
            FROM (
                SELECT ScoringPlayerID AS PlayerID, GameID, IsPowerPlay, IsShortHanded, 0 AS IsAssist
                FROM Game.Goals WHERE GameID = ?
                UNION ALL
                SELECT Assist1PlayerID, GameID, IsPowerPlay, IsShortHanded, 1
                FROM Game.Goals WHERE GameID = ? AND Assist1PlayerID IS NOT NULL
                UNION ALL
                SELECT Assist2PlayerID, GameID, IsPowerPlay, IsShortHanded, 1
                FROM Game.Goals WHERE GameID = ? AND Assist2PlayerID IS NOT NULL
            ) x
            GROUP BY PlayerID, GameID
        ) agg ON agg.PlayerID = pgs.PlayerID AND agg.GameID = pgs.GameID
        WHERE pgs.GameID = ?
        """,
        game_id, game_id, game_id, game_id,
    )


def sync_player_season_stats(cursor, season_id: int) -> None:
    cursor.execute(
        """
        SELECT pgs.PlayerID, pgs.TeamID,
               COUNT(*) AS GamesPlayed, SUM(pgs.Goals) AS Goals, SUM(pgs.Assists) AS Assists,
               SUM(pgs.Points) AS Points, SUM(pgs.Shots) AS Shots, SUM(pgs.Hits) AS Hits,
               SUM(pgs.Blocks) AS Blocks, SUM(pgs.Giveaways) AS Giveaways, SUM(pgs.Takeaways) AS Takeaways,
               SUM(pgs.PenaltyMinutes) AS PenaltyMinutes,
               SUM(pgs.FaceoffWins) AS FaceoffWins, SUM(pgs.FaceoffLosses) AS FaceoffLosses,
               SUM(pgs.TimeOnIceSeconds) AS TimeOnIceSeconds,
               SUM(pgs.PowerPlayTOISeconds) AS PowerPlayTOISeconds,
               SUM(pgs.ShortHandedTOISeconds) AS ShortHandedTOISeconds,
               SUM(pgs.PowerPlayGoals) AS PowerPlayGoals, SUM(pgs.PowerPlayAssists) AS PowerPlayAssists,
               SUM(pgs.ShortHandedGoals) AS ShortHandedGoals, SUM(pgs.ShortHandedAssists) AS ShortHandedAssists
        FROM Stats.PlayerGameStats pgs
        JOIN Game.Games g ON g.GameID = pgs.GameID
        WHERE g.SeasonID = ?
        GROUP BY pgs.PlayerID, pgs.TeamID
        """,
        season_id,
    )
    for r in cursor.fetchall():
        db.upsert(
            cursor, "Stats.PlayerSeasonStats",
            {"SeasonID": season_id, "PlayerID": r.PlayerID, "TeamID": r.TeamID},
            {
                "GamesPlayed": r.GamesPlayed, "Goals": r.Goals, "Assists": r.Assists, "Points": r.Points,
                "Shots": r.Shots, "Hits": r.Hits, "Blocks": r.Blocks, "Giveaways": r.Giveaways,
                "Takeaways": r.Takeaways, "PenaltyMinutes": r.PenaltyMinutes,
                "FaceoffWins": r.FaceoffWins, "FaceoffLosses": r.FaceoffLosses,
                "TimeOnIceSeconds": r.TimeOnIceSeconds,
                "PowerPlayTOISeconds": r.PowerPlayTOISeconds, "ShortHandedTOISeconds": r.ShortHandedTOISeconds,
                "PowerPlayGoals": r.PowerPlayGoals, "PowerPlayAssists": r.PowerPlayAssists,
                "ShortHandedGoals": r.ShortHandedGoals, "ShortHandedAssists": r.ShortHandedAssists,
            },
        )

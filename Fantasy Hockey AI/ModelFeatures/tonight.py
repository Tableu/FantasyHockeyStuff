"""Tonight's feature rows: the same skater table the models train on, for games not played yet.

Nothing here is a second feature path. Tonight's games go in as placeholder rows with empty
stats, so every shift-by-one window in features/ computes "form entering tonight" with the
code that built the training rows (base.build_base's `tonight` argument), and the lineup rows
come from lineups/features.target_rows -- the function the season build calls per game. What is
live is only the inputs, read from the Live schema the pipeline's snapshot job fills
(pipeline/snapshot_live.py), as of a moment `at`:

- who is out: Live.PlayerStatus (the pipeline's merge of Fleaflicker, ESPN and Daily Faceoff),
  OUT or SUSP at `at`. It is overlaid on the injury spells as a spell covering tonight, known
  before the lock -- so `injured_at_lockout` and the team injury counts see it.
- tonight's lineup: each team's latest Daily Faceoff chart (Live.LineCharts), as the lineup the
  variant-B columns read (the models were trained on B: an opening lineup with chart-like
  noise). A chart older than the team's last game is stale: that team falls back to variant A,
  its previous opening lineup. Opening night has no previous game, so the chart is used as is.
- the starter: Daily Faceoff's goalie report for the game if it names one, else the chart's
  first goalie. Its strength (Confirmed / Likely) is kept for the post-model adjustment.

Returned alongside: each candidate's questionable status (DTD, or GTD on his team's chart),
which the models never saw, for the caps in Settings/live.json; and every reported player's
merged status, which the live runner reads for IR (a player on a roster may be idle tonight).

Replaying a past date (`--date 2026-01-15 --at ...`) sees only games before it, which is what
verify_tonight compares against the batch build.
"""

import datetime as dt
import logging
from collections import defaultdict

import pandas as pd

from features import assemble as assemble_module
from features import base as base_module
from lineups import features as lineup_features
from lineups import store

log = logging.getLogger("tonight")

SKATER_POSITIONS = ("C", "L", "R", "D")
INJURED = ("OUT", "SUSP")
_DFO_POSITION = {"c": "C", "lw": "L", "rw": "R", "ld": "D", "rd": "D", "g": "G"}
_TEAM_STAT_COLUMNS = ["shots_for", "shots_against", "goals_for", "goals_against", "xgf", "xga",
                      "hits_for", "hits_against", "blocks_for", "blocks_against", "pim_for", "pp_goals",
                      "skater_toi", "pp_opportunities", "penalties_taken"]


# ---------- live inputs ----------

def schedule(cursor, game_date: dt.date) -> pd.DataFrame:
    cursor.execute("""
        SELECT s.SeasonID, s.NHLGameID, s.GameDate, s.StartTimeUTC, s.HomeTeamID, s.AwayTeamID
        FROM Reference.Schedule s WHERE s.GameDate = ? AND s.GameType = '2'
        ORDER BY s.StartTimeUTC, s.NHLGameID""", game_date)
    return pd.DataFrame.from_records([tuple(r) for r in cursor.fetchall()],
                                     columns=["season_id", "nhl_game_id", "game_date", "start_time_utc",
                                              "home_team_id", "away_team_id"])


def player_status(cursor, at: dt.datetime) -> dict:
    """{PlayerID: (Status, TeamID, GameTimeDecision)} -- the merged report as of `at`."""
    cursor.execute("""
        SELECT PlayerID, Status, TeamID, GameTimeDecision
        FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY PlayerID ORDER BY ChangedAt DESC, PlayerStatusID DESC) AS rn
              FROM Live.PlayerStatus WHERE ChangedAt <= ?) x
        WHERE rn = 1""", at)
    return {r.PlayerID: (r.Status, r.TeamID, bool(r.GameTimeDecision)) for r in cursor.fetchall()}


def line_charts(cursor, at: dt.datetime) -> dict:
    """{TeamID: {"updated_at", "label", "rows": [(PlayerID, group, position)]}} -- each team's
    latest Daily Faceoff chart as of `at`."""
    cursor.execute("""
        SELECT c.TeamID, c.SourceUpdatedAt, c.SourceLabel, r.SnapshotAt, p.PlayerID, p.GroupIdentifier, p.Position
        FROM (SELECT c.*, ROW_NUMBER() OVER (PARTITION BY c.TeamID ORDER BY c.LineChartID DESC) AS rn
              FROM Live.LineCharts c JOIN Live.SnapshotRuns r0 ON r0.SnapshotRunID = c.SnapshotRunID
              WHERE r0.SnapshotAt <= ?) c
        JOIN Live.SnapshotRuns r ON r.SnapshotRunID = c.SnapshotRunID
        JOIN Live.LineChartPlayers p ON p.LineChartID = c.LineChartID
        WHERE c.rn = 1 AND p.PlayerID IS NOT NULL""", at)
    charts: dict = {}
    for r in cursor.fetchall():
        chart = charts.setdefault(r.TeamID, {"updated_at": r.SourceUpdatedAt or r.SnapshotAt,
                                             "label": r.SourceLabel, "rows": []})
        chart["rows"].append((r.PlayerID, r.GroupIdentifier, r.Position))
    return charts


def goalie_reports(cursor, game_date: dt.date, at: dt.datetime) -> dict:
    """{TeamID: (PlayerID, Strength)} -- the latest Daily Faceoff report per team for the date."""
    cursor.execute("""
        SELECT TeamID, PlayerID, Strength
        FROM (SELECT g.*, ROW_NUMBER() OVER (PARTITION BY g.TeamID ORDER BY r.SnapshotAt DESC, r.SnapshotRunID DESC) AS rn
              FROM Live.GoalieReports g JOIN Live.SnapshotRuns r ON r.SnapshotRunID = g.SnapshotRunID
              WHERE g.GameDate = ? AND r.SnapshotAt <= ?) x
        WHERE rn = 1""", game_date, at)
    return {r.TeamID: (r.PlayerID, r.Strength) for r in cursor.fetchall()}


# ---------- tonight's lineup from a chart ----------

def chart_lineup(chart: dict, game_id: int, nhl_game_id: int, game_date: dt.date, season_id: int,
                 team_id: int, injured: set, starter) -> store.TeamGame:
    """A Daily Faceoff chart as a TeamGame: lines f1-f4, pairs d1-d3, units pp1/pp2 and pk1/pk2,
    goalies g. A reported-out player is not dressed; the IR group is left out entirely."""
    game = store.TeamGame(game_id, nhl_game_id, game_date, season_id, team_id)
    positions = {}
    for player_id, group, position in chart["rows"]:
        code = _DFO_POSITION.get((position or "").lower().rstrip("0123456789"))
        if code and group != "ir":
            positions.setdefault(player_id, code)
    for player_id, group, _ in chart["rows"]:
        if group == "ir" or player_id not in positions:
            continue
        pl = game.players.get(player_id) or store.PlayerLineup(positions[player_id], player_id not in injured,
                                                                None, None, None, None, False)
        kind, rank = group.rstrip("0123456789"), group[len(group.rstrip("0123456789")):]
        rank = int(rank) if rank else None
        if kind == "f":
            pl.line = rank
        elif kind == "d":
            pl.pair = rank
        elif kind == "pp":
            pl.pp = rank
            game.had_pp = True
        elif kind == "pk":
            pl.pk = rank
            game.had_pk = True
        game.players[player_id] = pl
    goalies = [p for p, pl in game.players.items() if pl.is_goalie and pl.dressed]
    if starter is not None and starter not in game.players and starter not in injured:
        game.players[starter] = store.PlayerLineup("G", True, None, None, None, None, False)
        goalies.insert(0, starter)
    chosen = starter if starter in goalies else (goalies[0] if goalies else None)
    if chosen is not None:
        game.players[chosen].starting_goalie = True
    return game


# ---------- the build ----------

def build(cursor, game_date: dt.date, at: dt.datetime) -> dict:
    """{"skaters": assembled feature rows, "goalies": goalie candidate rows, "context": per
    team-game notes (lineup source, goalie report), "questionable": {PlayerID: status}}."""
    games = schedule(cursor, game_date)
    if games.empty:
        log.info("no regular-season games on %s", game_date)
        return {}
    season_id = int(games["season_id"].iloc[0])
    status = player_status(cursor, at)
    charts = line_charts(cursor, at)
    reports = goalie_reports(cursor, game_date, at)

    team_games = store.load_team_games(cursor, [season_id])
    static_positions = store.load_player_positions(cursor)
    spells = store.load_injury_spells(cursor, [season_id])
    injured_by_team = defaultdict(set)
    for player_id, (state, team_id, _) in status.items():
        if state in INJURED and team_id is not None:
            injured_by_team[team_id].add(player_id)
    overlay = {(team_id, p): [(game_date, game_date, True)]
               for team_id, players in injured_by_team.items() for p in players}
    spells = store.merge_spells(spells, overlay)

    lineup_rows, context_rows, placeholder_team_games = [], [], []
    for g in games.itertuples():
        game_id = -int(g.nhl_game_id)   # not in Game.Games yet; negative so it can never collide
        for team_id, opp_id, is_home in ((g.home_team_id, g.away_team_id, 1), (g.away_team_id, g.home_team_id, 0)):
            played = [tg for tg in team_games.get(team_id, [])
                      if tg.season_id == season_id and tg.game_date < game_date]
            history = played[-lineup_features.CANDIDATE_LOOKBACK:]
            chart = charts.get(team_id)
            report = reports.get(team_id)
            starter = report[0] if report and report[0] is not None else None
            last_game = history[-1].game_date if history else None
            stale = chart is None or (last_game is not None and chart["updated_at"].date() <= last_game)
            target = (chart_lineup(chart, game_id, g.nhl_game_id, game_date, season_id, team_id,
                                   injured_by_team[team_id], starter) if chart
                      else store.TeamGame(game_id, g.nhl_game_id, game_date, season_id, team_id))
            if chart is not None and (not stale or not history):
                variant, source = "B", target
            elif history:
                variant, source = "A", None
            else:
                log.warning("team %s: no chart and no game this season -- no rows", team_id)
                continue
            lineup_rows.extend(lineup_features.target_rows(team_id, history, target, variant, spells,
                                                           static_positions, source=source))
            chosen = next((p for p, pl in (source or history[-1]).players.items() if pl.starting_goalie), None)
            context_rows.append({"game_id": game_id, "nhl_game_id": g.nhl_game_id, "team_id": team_id,
                                 "start_time_utc": g.start_time_utc, "lineup_source": variant,
                                 "chart_label": chart["label"] if chart else None,
                                 "chart_updated_at": chart["updated_at"] if chart else None,
                                 "expected_starter": chosen,
                                 "report_goalie": starter, "report_strength": report[1] if report else None})
            placeholder_team_games.append({"season_id": season_id, "game_id": game_id, "game_date": game_date,
                                           "team_id": team_id, "opp_team_id": opp_id, "is_home": is_home,
                                           **{c: float("nan") for c in _TEAM_STAT_COLUMNS},
                                           "is_placeholder": True})

    lineup = lineup_features._typed(pd.DataFrame(lineup_rows))
    tonight = {
        "date": game_date,
        "team_games": pd.DataFrame(placeholder_team_games),
        "games": pd.DataFrame({"game_id": -games["nhl_game_id"].astype(int), "season_id": games["season_id"],
                               "nhl_game_id": games["nhl_game_id"], "game_date": games["game_date"],
                               "home_team_id": games["home_team_id"], "away_team_id": games["away_team_id"],
                               "home_score": float("nan"), "away_score": float("nan"),
                               "start_time_utc": games["start_time_utc"]}),
        "spells": overlay,
    }
    candidates = (lineup.loc[lineup["position"].isin(SKATER_POSITIONS),
                             ["season_id", "game_id", "game_date", "team_id", "player_id", "position"]]
                  .drop_duplicates().reset_index(drop=True))
    skaters, goalie_form = base_module.build_base(cursor, [season_id], candidates, tonight)
    table = assemble_module.assemble(skaters, goalie_form, lineup)

    questionable = {}
    for player_id, (state, _, gtd) in status.items():
        if gtd:
            questionable[player_id] = "GTD"
        elif state == "DTD":
            questionable[player_id] = "DTD"
    goalie_rows = lineup[lineup["position"] == "G"].copy()
    log.info("%s as of %s: %d games, %d skater rows, %d goalie rows, lineup source %s",
             game_date, at, len(games), len(table), len(goalie_rows),
             dict(pd.Series([c["lineup_source"] for c in context_rows]).value_counts()))
    status_frame = pd.DataFrame([(p, st, t, g) for p, (st, t, g) in status.items()],
                                columns=["player_id", "status", "team_id", "game_time_decision"])
    return {"skaters": table, "goalies": goalie_rows, "context": pd.DataFrame(context_rows),
            "questionable": questionable, "status": status_frame}

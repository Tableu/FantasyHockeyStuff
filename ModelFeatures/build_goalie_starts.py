#!/usr/bin/env python
"""The per-start goalie line, exported to parquet -- the piece section 6 was blocked on.

`pipeline/` has stored the goalie half of the boxscore since the goalie-boxscore ingest went
in (shots against, saves, goals against, the W/L/O decision and the official starter flag),
and the build log records that it was kept on purpose after the goalie *models* were deleted:
it is data-layer correctness, and a season simulator that scores a goaltending line needs it
even though nothing projects goalie quality. This is the export that hands it over.

One row per **dressed** goalie per game, not per start. A backup who never faced a shot scores
zero and that zero is real -- a manager who started him lost the slot -- so he belongs in the
table with zeros rather than being filtered out. `is_starter` and `appeared` separate the three
states (started / relieved / dressed and unused).

The scorable columns (`wins`, `losses`, `ot_losses`, `shutouts`, `saves`, `goals_against`) are
named to match the `goalies` block of a scoring file, so `Simulation/scoring.py` scores this
frame directly with no rename in between.

What is deliberately NOT here: any projection. Per-start goalie fantasy points measured at
R2 -0.8% and save percentage does not carry from one start to the next (r = +0.026), so the
standing treatment is P(start) x league average. This file is the *actual* line, for replaying
a real season and for calibrating the sampler against.

Usage:
    python build_goalie_starts.py --season 2025-26
    python build_goalie_starts.py --season 2023-24 --season 2024-25 --season 2025-26
"""

import argparse
import logging

import pandas as pd

import nhlstats_db
import paths
from features import extract

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("goalie-starts")

SCORABLE = ["wins", "losses", "ot_losses", "shutouts", "saves", "goals_against"]


def parse_args():
    parser = argparse.ArgumentParser(description="Export the per-start goalie line")
    parser.add_argument("--season", action="append", required=True,
                        help="Season display name; repeat for several")
    return parser.parse_args()


def fetch(cursor, season_ids: list) -> pd.DataFrame:
    """Every dressed goalie's line, with the game context the decision depends on.

    `Stats.PlayerGameStats` is the only source for `Decision` and `IsStarter` -- nothing else
    in the database records who got the win or who officially started -- and
    `Game.Games.LastPeriodType` is what separates a regulation loss from an overtime one, which
    most formats price differently.
    """
    placeholders = ",".join("?" * len(season_ids))
    cursor.execute(f"""
        SELECT g.SeasonID AS season_id, pgs.GameID AS game_id, g.GameDate AS game_date,
               pgs.TeamID AS team_id, pgs.PlayerID AS player_id,
               CASE WHEN pgs.TeamID = g.HomeTeamID THEN g.AwayTeamID ELSE g.HomeTeamID END
                   AS opp_team_id,
               CASE WHEN pgs.TeamID = g.HomeTeamID THEN 1 ELSE 0 END AS is_home,
               CASE WHEN pgs.TeamID = g.HomeTeamID THEN g.HomeScore ELSE g.AwayScore END
                   AS team_score,
               CASE WHEN pgs.TeamID = g.HomeTeamID THEN g.AwayScore ELSE g.HomeScore END
                   AS opp_score,
               g.LastPeriodType AS last_period_type,
               COALESCE(pgs.IsStarter, 0) AS is_starter,
               pgs.TimeOnIceSeconds AS toi_seconds,
               pgs.ShotsAgainst AS shots_against, pgs.Saves AS saves,
               pgs.GoalsAgainst AS goals_against, pgs.Decision AS decision
        FROM Stats.PlayerGameStats pgs
        JOIN Game.Games g ON g.GameID = pgs.GameID
        WHERE g.SeasonID IN ({placeholders}) AND pgs.PositionCode = 'G'
        ORDER BY g.GameDate, pgs.GameID, pgs.TeamID, pgs.PlayerID
    """, *season_ids)
    columns = [d[0] for d in cursor.description]
    return pd.DataFrame.from_records(cursor.fetchall(), columns=columns)


def derive(table: pd.DataFrame) -> pd.DataFrame:
    """Add the scorable indicators and the two states a raw boxscore row does not name."""
    out = table.copy()
    out["game_date"] = pd.to_datetime(out["game_date"])
    for column in ("shots_against", "saves", "goals_against", "toi_seconds"):
        out[column] = out[column].fillna(0).astype("int32")
    out["is_starter"] = out["is_starter"].astype(bool)
    out["is_home"] = out["is_home"].astype(bool)

    # Dressed but unused is the third state, and it is the one a fantasy manager pays for: he
    # occupied an active slot and returned nothing. Relief appearances are rare but real.
    out["appeared"] = (out["toi_seconds"] > 0) | (out["shots_against"] > 0)
    used = (out.loc[out["appeared"]]
            .groupby(["game_id", "team_id"])["player_id"].transform("size"))
    out["goalies_used"] = used.reindex(out.index).fillna(0).astype("int8")
    # A starter who was not his team's only appearance was pulled (or hurt) -- the explicit
    # left-tail component section 5 asks for, measured rather than assumed.
    out["pulled"] = out["is_starter"] & out["appeared"] & (out["goalies_used"] > 1)

    decision = out["decision"].fillna("")
    out["wins"] = (decision == "W").astype("int8")
    out["losses"] = (decision == "L").astype("int8")
    out["ot_losses"] = (decision == "O").astype("int8")
    # The NHL definition, and the one every fantasy platform uses: no goals allowed *and* the
    # whole game was his. A 0-GA half-game in relief is not a shutout.
    out["shutouts"] = ((out["goals_against"] == 0) & (out["wins"] == 1)
                       & (out["goalies_used"] == 1) & out["appeared"]).astype("int8")
    return out


def verify(table: pd.DataFrame, season: str) -> None:
    """Checks that have to fail loudly, since everything downstream consumes this file."""
    games = table["game_id"].nunique()
    starters = int(table["is_starter"].sum())
    team_games = table.groupby(["game_id", "team_id"]).ngroups

    if starters != team_games:
        raise SystemExit(f"{season}: {starters} official starters for {team_games} team-games "
                         f"-- exactly one per team-game is expected")
    wins = int(table["wins"].sum())
    if wins != games:
        raise SystemExit(f"{season}: {wins} wins over {games} games")
    decisions = int(table[["wins", "losses", "ot_losses"]].to_numpy().sum())
    if decisions != 2 * games:
        raise SystemExit(f"{season}: {decisions} decisions over {games} games "
                         f"(expected {2 * games} -- one per side)")

    # Reported, never patched. A handful of rows have saves + GA = SA + 1, the same
    # scorekeeping class as the holdout's goals recorded with no shot on goal: the boxscore is
    # the source of truth, and a derived "fix" would only hide the feed's own inconsistency.
    identity = table["saves"] + table["goals_against"] - table["shots_against"]
    exceptions = identity[identity != 0]
    log.info("%s: %d games, %d dressed goalie rows, %d appearances, %d starters",
             season, games, len(table), int(table["appeared"].sum()), starters)
    log.info("%s: saves + GA = SA on %d of %d rows; %d exceptions %s",
             season, len(table) - len(exceptions), len(table), len(exceptions),
             dict(sorted(exceptions.value_counts().items())) if len(exceptions) else {})
    save_pct = table["saves"].sum() / max(int(table["shots_against"].sum()), 1)
    started = table.loc[table["is_starter"]]
    log.info("%s: league SV%% %.4f, %d pulls, %d shutouts, %.2f SA and %.2f GA per start",
             season, save_pct, int(table["pulled"].sum()), int(table["shutouts"].sum()),
             started["shots_against"].mean(), started["goals_against"].mean())


def main():
    args = parse_args()
    seasons = list(dict.fromkeys(args.season))
    conn = nhlstats_db.connect()
    cursor = conn.cursor()
    season_ids = extract.season_ids_for(cursor, seasons)
    paths.ensure(paths.FEATURES_DIR)

    for season in seasons:
        table = derive(fetch(cursor, [season_ids[season]]))
        verify(table, season)
        out = paths.FEATURES_DIR / f"goalie_starts_{season}.parquet"
        table.to_parquet(out, index=False)
        log.info("%s -> %s", season, out)


if __name__ == "__main__":
    main()

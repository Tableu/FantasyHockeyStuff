#!/usr/bin/env python
"""External preseason projections, exported to parquet -- one row per (source, player).

`pipeline/import_projections.py` loads each source's season projections into
`Projections.SkaterProjections` / `GoalieProjections`, one source per `Projections.Sources` row
(2025-26: eleven sources from the Crome aggregate workbook; 2026-27: nine files). This exports one
season, with the stat line named the way the scoring files name it, so `Season/` and `Decisions/`
can score it without knowing the database's column names.

A stat a source does not project stays NULL. Laidlaw has no PIM, Lineup Experts no PPP, and most
sources no SHP: a consumer must treat those as missing, not as zero.

    python build_external_projections.py --season 2025-26
    python build_external_projections.py --season 2026-27
"""

import argparse
import logging

import pandas as pd

import nhlstats_db
import paths
from features import verify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("external-projections")

SKATER_SQL = """
    SELECT s.SourceName AS source, s.PublishedOn AS published_on, k.PlayerID AS player_id,
           k.TeamID AS team_id, k.GamesPlayed AS games, k.Goals AS goals, k.Assists AS assists,
           k.Points AS points, k.PowerPlayPoints AS ppp, k.ShortHandedPoints AS shp,
           k.Shots AS shots, k.Hits AS hits, k.Blocks AS blocks, k.PenaltyMinutes AS pim,
           k.AverageTOIMinutes AS toi_minutes
    FROM Projections.SkaterProjections k
    JOIN Projections.Sources s ON s.SourceID = k.SourceID
    JOIN Reference.Seasons r ON r.SeasonID = s.SeasonID
    WHERE r.DisplayName = ?
"""
GOALIE_SQL = """
    SELECT s.SourceName AS source, s.PublishedOn AS published_on, g.PlayerID AS player_id,
           g.TeamID AS team_id, g.GamesPlayed AS games, g.GamesStarted AS games_started,
           g.Wins AS wins, g.Losses AS losses, g.OvertimeLosses AS ot_losses,
           g.Shutouts AS shutouts, g.Saves AS saves, g.GoalsAgainst AS goals_against,
           g.ShotsAgainst AS shots_against, g.SavePercentage AS save_pct,
           g.GoalsAgainstAverage AS gaa
    FROM Projections.GoalieProjections g
    JOIN Projections.Sources s ON s.SourceID = g.SourceID
    JOIN Reference.Seasons r ON r.SeasonID = s.SeasonID
    WHERE r.DisplayName = ?
"""


def fetch(cursor, sql, season) -> pd.DataFrame:
    cursor.execute(sql, season)
    columns = [d[0] for d in cursor.description]
    frame = pd.DataFrame.from_records(cursor.fetchall(), columns=columns)
    for column in frame.columns:
        if column not in ("source", "published_on"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def main():
    parser = argparse.ArgumentParser(description="Export external season projections")
    parser.add_argument("--season", required=True, help="Season display name, e.g. 2025-26")
    args = parser.parse_args()

    cursor = nhlstats_db.connect().cursor()
    skaters = fetch(cursor, SKATER_SQL, args.season).assign(is_goalie=False)
    goalies = fetch(cursor, GOALIE_SQL, args.season).assign(is_goalie=True)
    if skaters.empty and goalies.empty:
        raise SystemExit(f"no projections for {args.season}; run pipeline/import_projections.py")
    table = pd.concat([skaters, goalies], ignore_index=True)
    table["player_id"] = table["player_id"].astype("int64")
    table["published_on"] = pd.to_datetime(table["published_on"])
    duplicates = table.duplicated(["source", "player_id", "is_goalie"]).sum()
    if duplicates:
        raise SystemExit(f"{duplicates} duplicate (source, player) rows")

    by_source = table.groupby("source").agg(skaters=("is_goalie", lambda g: int((~g).sum())),
                                            goalies=("is_goalie", "sum"),
                                            published=("published_on", "first"))
    log.info("%s: %d rows over %d sources\n%s", args.season, len(table), len(by_source),
             by_source.to_string())
    if not verify.external_projections(cursor, table, args.season):
        raise SystemExit("export does not match the database; nothing written")
    paths.ensure(paths.FEATURES_DIR)
    out = paths.FEATURES_DIR / f"external_projections_{args.season}.parquet"
    table.to_parquet(out, index=False)
    log.info("-> %s", out)


if __name__ == "__main__":
    main()

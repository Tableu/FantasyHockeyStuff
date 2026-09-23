#!/usr/bin/env python
"""Fantasy positional eligibility, exported to parquet.

A fantasy platform's idea of what a player is eligible at is its own, and it is not the NHL's
`PositionCode`. The NHL says a player is a left wing; Yahoo says he is LW *and* RW because he
has taken enough shifts on the right. That difference is the whole reason a lineup slot is a
choice: with one position per player the nightly assignment decomposes -- the C slots cannot
take a player the D slots wanted -- and filling each position's slots with its own best `n`
players is optimal by construction. With real eligibility it is a genuine assignment problem,
and a greedy fill leaves points on the bench.

`pipeline/import_fantasy_*.py` load `Fantasy.PlayerPositions`, one row per eligible position
rather than a delimited list, per platform and season. This exports one platform-season.

Measured on Yahoo 2026-27: 1,786 eligibilities over 1,536 players, **15% of them
multi-position** -- LW/RW most often, then C/LW and C/RW, with 19 players eligible at all three
forward spots. That is a modest but real widening of the legal-lineup space.

    python build_fantasy_positions.py --platform Yahoo --season 2026-27
    python build_fantasy_positions.py --platform ESPN --season 2026-27 --adp
"""

import argparse
import logging

import pandas as pd

import nhlstats_db
import paths

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fantasy-positions")

# How a platform's slot names map onto the NHL PositionCode the feature tables carry, used only
# as the fallback for a player the platform has never listed.
NHL_TO_FANTASY = {"C": "C", "L": "LW", "R": "RW", "D": "D", "G": "G"}


def parse_args():
    parser = argparse.ArgumentParser(description="Export fantasy positional eligibility")
    parser.add_argument("--platform", default="Yahoo",
                        help="Platform name as Fantasy.Platforms spells it (default Yahoo)")
    parser.add_argument("--season", required=True, help="Season display name, e.g. 2026-27")
    parser.add_argument("--adp", action="store_true",
                        help="Also export Fantasy.PlayerADP for the same platform-season")
    return parser.parse_args()


def fetch_positions(cursor, platform: str, season: str) -> pd.DataFrame:
    cursor.execute("""
        SELECT pp.PlayerID AS player_id, pp.PositionCode AS fantasy_position
        FROM Fantasy.PlayerPositions pp
        JOIN Fantasy.Platforms p ON p.FantasyPlatformID = pp.FantasyPlatformID
        JOIN Reference.Seasons s ON s.SeasonID = pp.SeasonID
        WHERE p.PlatformName = ? AND s.DisplayName = ?
        ORDER BY pp.PlayerID, pp.PositionCode
    """, platform, season)
    columns = [d[0] for d in cursor.description]
    return pd.DataFrame.from_records(cursor.fetchall(), columns=columns)


def fetch_adp(cursor, platform: str, season: str) -> pd.DataFrame:
    cursor.execute("""
        SELECT a.PlayerID AS player_id, a.ADP AS adp
        FROM Fantasy.PlayerADP a
        JOIN Fantasy.Platforms p ON p.FantasyPlatformID = a.FantasyPlatformID
        JOIN Reference.Seasons s ON s.SeasonID = a.SeasonID
        WHERE p.PlatformName = ? AND s.DisplayName = ?
        ORDER BY a.ADP
    """, platform, season)
    columns = [d[0] for d in cursor.description]
    return pd.DataFrame.from_records(cursor.fetchall(), columns=columns)


def report(table: pd.DataFrame, platform: str, season: str) -> None:
    if table.empty:
        raise SystemExit(f"no eligibility rows for {platform} {season}; check "
                         f"Fantasy.Platforms for the exact platform name")
    per_player = table.groupby("player_id").size()
    combos = (table.groupby("player_id")["fantasy_position"]
              .apply(lambda s: "/".join(sorted(s))).value_counts())
    log.info("%s %s: %d eligibilities over %d players, %.1f%% multi-position",
             platform, season, len(table), len(per_player), 100 * (per_player > 1).mean())
    log.info("%s %s: positions %s", platform, season,
             dict(table["fantasy_position"].value_counts()))
    log.info("%s %s: most common multi-position combinations %s", platform, season,
             dict(combos[combos.index.str.contains("/")].head(6)))


def main():
    args = parse_args()
    conn = nhlstats_db.connect()
    cursor = conn.cursor()
    paths.ensure(paths.FEATURES_DIR)

    positions = fetch_positions(cursor, args.platform, args.season)
    report(positions, args.platform, args.season)
    out = paths.FEATURES_DIR / f"fantasy_positions_{args.platform.lower()}_{args.season}.parquet"
    positions.to_parquet(out, index=False)
    log.info("-> %s", out)

    if args.adp:
        adp = fetch_adp(cursor, args.platform, args.season)
        out = paths.FEATURES_DIR / f"fantasy_adp_{args.platform.lower()}_{args.season}.parquet"
        adp.to_parquet(out, index=False)
        log.info("%s %s: %d ADP rows, %.2f..%.2f -> %s", args.platform, args.season,
                 len(adp), adp["adp"].min(), adp["adp"].max(), out)


if __name__ == "__main__":
    main()

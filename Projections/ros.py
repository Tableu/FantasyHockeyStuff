"""Rest-of-season targets: what a player produces *after* a given date.

Section 4 of the build plan. The per-game stack answers "what will he do tonight"; this
answers "what will he produce over the games that remain", which is a different question with
a different bias. Tonight's model leans on recent form because recent form really does
predict tonight. Over a horizon, a hot streak should regress toward an established level
instead of extrapolating, so the useful signals shift toward season-to-date and multi-season
evidence, and the fitted answer has to be shrunk by how much evidence there is.

**The decomposition this module targets**, which mirrors the per-game chain rather than
inventing a new shape:

    production over the horizon  =  team games in it          (schedule: known in advance)
                                 x  availability              (share of them he dresses for)
                                 x  ice time per game played
                                 x  rate per 60

Each factor stabilizes at a different speed, and separating them is what lets the slow ones
carry the fast ones. Lumping them into a single "points per game" throws that away.

**Windows are measured in days, not games.** A game-count window has to decide whose games to
count, which breaks the moment a player is traded; a date window does not, and the
opportunity term -- how many games his team plays in the next six weeks -- stays exactly what
a manager can look up in advance. Schedule density therefore falls out of the target rather
than being bolted on.

**A window that runs past the end of a season is dropped, not truncated**, because a partial
window looks like a player who stopped producing.

Usage:
    python ros.py --season 2025-26 --horizon 42
"""

import argparse
import logging

import numpy as np
import pandas as pd

import paths

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ros")

# What gets accumulated over the horizon. `toi` is carried because ice time is the opportunity
# term in the decomposition above, not because anyone scores it.
STATS = ["toi", "shots", "hits", "blocks", "assists", "goals", "pim", "ppp", "shp", "points"]

DEFAULT_HORIZON_DAYS = 42


def parse_args():
    parser = argparse.ArgumentParser(description="Build rest-of-season targets")
    parser.add_argument("--season", required=True)
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON_DAYS,
                        help="Forward window in days (default 42, about six weeks)")
    parser.add_argument("--features-dir", default=None)
    parser.add_argument("--out", default=None)
    return parser.parse_args()


def base_table(season, features_dir=None):
    """The lineup-independent feature table: one row per lockout-knowable candidate."""
    directory = features_dir or paths.FEATURES_DIR
    path = directory / f"base_{season}.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing -- build it with ModelFeatures/build_feature_table.py "
            f"--season {season}")
    table = pd.read_parquet(path)
    table["game_date"] = pd.to_datetime(table["game_date"])
    return table


def team_schedule(table):
    """Every team-game in the season, from the candidate rows themselves.

    The candidate universe covers every team-game, so the schedule can be recovered without
    opening a database -- which keeps this folder's no-connection rule intact.
    """
    return (table[["team_id", "game_id", "game_date"]]
            .drop_duplicates()
            .sort_values(["team_id", "game_date"])
            .reset_index(drop=True))


def team_games_in_window(schedule, horizon_days):
    """For each team-game, how many further games that team plays inside the window.

    This is the opportunity term, and it is knowable at the time: the schedule is published
    in advance. A player's own availability is a separate factor.
    """
    counts = []
    for team_id, games in schedule.groupby("team_id", sort=False):
        dates = games["game_date"].to_numpy("datetime64[ns]")
        horizon = dates + np.timedelta64(horizon_days, "D")
        # Games strictly after this one, up to and including the horizon date.
        upper = np.searchsorted(dates, horizon, side="right")
        lower = np.arange(len(dates)) + 1
        counts.append(pd.DataFrame({
            "team_id": team_id,
            "game_id": games["game_id"].to_numpy(),
            "window_team_games": upper - lower,
            "window_end": horizon,
            "season_end": dates[-1],
        }))
    return pd.concat(counts, ignore_index=True)


def forward_totals(table, horizon_days):
    """Per (player, as-of game): his production over the following `horizon_days`.

    Accumulated per player across whatever team he plays for, so a trade moves his games with
    him. Only games he actually dressed for contribute, which is what makes `window_played`
    an availability measure rather than a schedule measure.
    """
    played = table[table["target_played"].astype(bool)].copy()
    columns = ["player_id", "game_date"] + [f"target_{s}" for s in STATS]
    played = played[columns].sort_values(["player_id", "game_date"]).reset_index(drop=True)

    pieces = []
    for player_id, games in played.groupby("player_id", sort=False):
        dates = games["game_date"].to_numpy("datetime64[ns]")
        values = games[[f"target_{s}" for s in STATS]].to_numpy("float64")
        # Prefix sums let every window be two lookups instead of a scan.
        cumulative = np.vstack([np.zeros(len(STATS)), np.cumsum(values, axis=0)])
        counts = np.arange(len(dates) + 1)

        asof = table.loc[table["player_id"] == player_id, ["game_id", "game_date"]]
        asof = asof.drop_duplicates().sort_values("game_date")
        start = np.searchsorted(dates, asof["game_date"].to_numpy("datetime64[ns]"),
                                side="right")
        end = np.searchsorted(dates,
                              asof["game_date"].to_numpy("datetime64[ns]")
                              + np.timedelta64(horizon_days, "D"), side="right")
        totals = cumulative[end] - cumulative[start]
        frame = pd.DataFrame(totals, columns=[f"window_{s}" for s in STATS])
        frame["player_id"] = player_id
        frame["game_id"] = asof["game_id"].to_numpy()
        frame["window_played"] = counts[end] - counts[start]
        pieces.append(frame)
    return pd.concat(pieces, ignore_index=True)


def build(season, horizon_days=DEFAULT_HORIZON_DAYS, features_dir=None):
    """The rest-of-season training frame: as-of features plus forward-looking targets."""
    table = base_table(season, features_dir)
    log.info("%s: %d candidate rows", season, len(table))

    schedule = team_schedule(table)
    windows = team_games_in_window(schedule, horizon_days)
    forward = forward_totals(table, horizon_days)

    frame = table.merge(windows, on=["team_id", "game_id"], how="left")
    frame = frame.merge(forward, on=["player_id", "game_id"], how="left")
    for stat in STATS:
        frame[f"window_{stat}"] = frame[f"window_{stat}"].fillna(0.0)
    frame["window_played"] = frame["window_played"].fillna(0.0)

    # A window that runs off the end of the season is not a short window, it is a wrong one:
    # the player looks like he stopped producing. Drop those rows rather than truncate them.
    frame["window_complete"] = frame["window_end"] <= frame["season_end"]
    complete = frame[frame["window_complete"] & (frame["window_team_games"] > 0)].copy()

    # The three factors, each on its own scale.
    complete["ros_availability"] = (complete["window_played"]
                                    / complete["window_team_games"]).clip(0, 1)
    played = complete["window_played"].replace(0, np.nan)
    complete["ros_toi_per_game"] = complete["window_toi"] / played
    for stat in STATS:
        if stat == "toi":
            continue
        complete[f"ros_{stat}_per_game"] = complete[f"window_{stat}"] / played
        complete[f"ros_{stat}_p60"] = (complete[f"window_{stat}"]
                                       / (complete["window_toi"] / 3600.0).replace(0, np.nan))

    log.info("%d rows with a complete %d-day window (%.1f%% of candidates); "
             "median team games in window %.0f, mean availability %.3f",
             len(complete), horizon_days, 100 * len(complete) / len(table),
             complete["window_team_games"].median(), complete["ros_availability"].mean())
    return complete


def main():
    args = parse_args()
    features = paths.FEATURES_DIR if args.features_dir is None else args.features_dir
    frame = build(args.season, args.horizon, features)
    destination = (args.out or paths.ensure(paths.REPORTS_DIR)
                   / f"ros_{args.season}_{args.horizon}d.parquet")
    frame.to_parquet(destination, index=False)
    log.info("wrote %s: %d rows x %d columns", destination, len(frame), frame.shape[1])


if __name__ == "__main__":
    main()

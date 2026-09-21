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
opportunity term -- how many games his team plays in the window -- stays exactly what a
manager can look up in advance. Schedule density therefore falls out of the target rather
than being bolted on.

**Two horizon modes.** A fixed window of N days is the right shape for a trade or a streaming
decision, and it keeps every row comparable. `--horizon season` instead runs each window to
the end of the season, which is what a draft or a keep-or-cut decision actually asks about
and what "rest of season" means literally. The variable mode costs almost nothing precisely
because of the decomposition above: availability, ice time and rate per 60 do not depend on
how long the window is, so only the games-remaining multiplier changes with the date. What
does change is the noise -- a window with four games left is a far weaker label than one with
forty -- which is why rows are weighted by the window behind them downstream, and why
`--min-window-games` drops the degenerate tail.

**A fixed window that runs past the end of a season is dropped, not truncated**, because a
partial window looks like a player who stopped producing. A season-mode window ends there by
definition, so nothing is dropped for that reason.

Usage:
    python ros.py --season 2025-26 --horizon 42
    python ros.py --season 2025-26 --horizon season --min-window-games 5
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
    parser.add_argument("--horizon", default=str(DEFAULT_HORIZON_DAYS),
                        help="Forward window in days, or 'season' to run to the season's "
                             "end (default 42, about six weeks)")
    parser.add_argument("--min-window-games", type=int, default=1,
                        help="Drop rows whose window holds fewer team games than this; "
                             "worth raising in season mode, where the tail of the schedule "
                             "produces windows of one or two games (default 1)")
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


def parse_horizon(value):
    """Either a number of days, or None meaning 'run to the end of the season'."""
    if isinstance(value, str) and value.lower() == "season":
        return None
    return int(value)


def team_games_in_window(schedule, horizon_days):
    """For each team-game, how many further games that team plays inside the window.

    This is the opportunity term, and it is knowable at the time: the schedule is published
    in advance. A player's own availability is a separate factor.
    """
    counts = []
    for team_id, games in schedule.groupby("team_id", sort=False):
        dates = games["game_date"].to_numpy("datetime64[ns]")
        # The season ends when *this team* stops playing, not when the league does. Using the
        # league-wide last date instead silently loosens the completeness test below, because
        # a team finishing early would have windows that overrun its own schedule counted as
        # complete.
        team_end = dates[-1]
        if horizon_days is None:
            # Every window ends with the season, so the opportunity term is simply however
            # many games the team has left -- which is what shrinks as the season runs out.
            horizon = np.full(len(dates), team_end)
        else:
            horizon = dates + np.timedelta64(horizon_days, "D")
        # Games strictly after this one, up to and including the horizon date.
        upper = np.searchsorted(dates, horizon, side="right")
        lower = np.arange(len(dates)) + 1
        counts.append(pd.DataFrame({
            "team_id": team_id,
            "game_id": games["game_id"].to_numpy(),
            "window_team_games": upper - lower,
            "window_end": horizon,
            "season_end": team_end,
        }))
    return pd.concat(counts, ignore_index=True)


def forward_totals(table, window_ends):
    """Per (player, as-of game): his production between that game and the window's end.

    Accumulated per player across whatever team he plays for, so a trade moves his games with
    him. Only games he actually dressed for contribute, which is what makes `window_played`
    an availability measure rather than a schedule measure. `window_ends` carries one end
    date per (game_id, team_id), so a fixed horizon and a run-to-season-end horizon go
    through exactly the same code.
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

        asof = table.loc[table["player_id"] == player_id,
                         ["game_id", "game_date", "window_end"]]
        asof = asof.drop_duplicates(subset=["game_id"]).sort_values("game_date")
        start = np.searchsorted(dates, asof["game_date"].to_numpy("datetime64[ns]"),
                                side="right")
        end = np.searchsorted(dates, asof["window_end"].to_numpy("datetime64[ns]"),
                              side="right")
        totals = cumulative[end] - cumulative[start]
        frame = pd.DataFrame(totals, columns=[f"window_{s}" for s in STATS])
        frame["player_id"] = player_id
        frame["game_id"] = asof["game_id"].to_numpy()
        frame["window_played"] = counts[end] - counts[start]
        pieces.append(frame)
    return pd.concat(pieces, ignore_index=True)


def build(season, horizon_days=DEFAULT_HORIZON_DAYS, features_dir=None,
          min_window_games=1):
    """The rest-of-season training frame: as-of features plus forward-looking targets."""
    table = base_table(season, features_dir)
    log.info("%s: %d candidate rows", season, len(table))

    schedule = team_schedule(table)
    windows = team_games_in_window(schedule, horizon_days)
    table = table.merge(windows, on=["team_id", "game_id"], how="left")
    forward = forward_totals(table, windows)

    frame = table.merge(forward, on=["player_id", "game_id"], how="left")
    for stat in STATS:
        frame[f"window_{stat}"] = frame[f"window_{stat}"].fillna(0.0)
    frame["window_played"] = frame["window_played"].fillna(0.0)

    # A fixed window that runs off the end of the season is not a short window, it is a
    # wrong one: the player looks like he stopped producing. Drop those rows rather than
    # truncate them. In season mode every window ends with the season, so this is vacuous
    # and the minimum-games filter does the work instead.
    frame["window_complete"] = frame["window_end"] <= frame["season_end"]
    complete = frame[frame["window_complete"]
                     & (frame["window_team_games"] >= max(min_window_games, 1))].copy()

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

    label = "to season end" if horizon_days is None else f"{horizon_days}-day"
    log.info("%d rows with a complete %s window (%.1f%% of candidates); team games in "
             "window: median %.0f, range %.0f-%.0f; mean availability %.3f",
             len(complete), label, 100 * len(complete) / len(table),
             complete["window_team_games"].median(), complete["window_team_games"].min(),
             complete["window_team_games"].max(), complete["ros_availability"].mean())
    return complete


def suffix(horizon):
    """How a horizon is spelled in a filename."""
    return "season" if parse_horizon(horizon) is None else f"{parse_horizon(horizon)}d"


def main():
    args = parse_args()
    features = paths.FEATURES_DIR if args.features_dir is None else args.features_dir
    horizon = parse_horizon(args.horizon)
    frame = build(args.season, horizon, features, args.min_window_games)
    destination = (args.out or paths.ensure(paths.REPORTS_DIR)
                   / f"ros_{args.season}_{suffix(args.horizon)}.parquet")
    frame.to_parquet(destination, index=False)
    log.info("wrote %s: %d rows x %d columns", destination, len(frame), frame.shape[1])


if __name__ == "__main__":
    main()

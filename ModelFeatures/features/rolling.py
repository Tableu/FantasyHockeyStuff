"""Leak-free rolling windows.

A window must never contain the game it describes. There are two ways to guarantee that, and
which one applies depends on how the feature is attached to a row:

  * `shift=1` (the default) for features joined onto the row's own game -- team form, the
    opposing goalie -- where the row exists in the source frame and the shift excludes it.
  * `shift=0` for the player family, whose rows are attached by an as-of join on the date in
    base.py. The candidate universe includes players who did *not* play the target game, so
    they have no row to shift against; there the as-of join (`allow_exact_matches=False`)
    does the excluding, and the window must include the player's own last game for his
    history to be complete as of that date.

Either way the exclusion happens in one place -- `_shifted_groups()` or the as-of join -- and
base.py asserts it on real data by carrying the source game's date alongside the features and
checking it is strictly earlier than the target game's.

Grouping always includes the season, so a window never reaches back across a summer; the
prior season is a separate family (`prior_season_aggregate`) precisely so that early-season
rows are explicitly "new season, little history" rather than silently continuous.

Windows: `l5 / l10 / l20` are the previous N games the player actually played, and `std` is
season-to-date (an expanding window). `gp_{window}` records how many games each window
actually had, so a model can tell a 2-game average from a 20-game one.
"""

import pandas as pd

WINDOWS = (5, 10, 20)
SEASON_TO_DATE = "std"


def _sorted(df: pd.DataFrame, keys: list, order: list) -> pd.DataFrame:
    return df.sort_values(keys + order, kind="mergesort").reset_index(drop=True)


def _shifted_groups(df: pd.DataFrame, keys: list, cols: list, shift: int) -> tuple:
    """The per-group shift that makes a window leak-free, plus a dense group id to roll over.

    shift=1 is for features joined onto the row's own game: the row then sees only earlier
    games. shift=0 includes the row's own game and is for features joined *as of a date* to
    rows that may not appear in this frame at all -- a candidate who did not play the target
    game has no row for it, so his history is attached with an as-of join on the date
    (see base.py), and it is that join, not a shift, that excludes the target game.
    """
    grouped = df.groupby(keys, sort=False)
    shifted = grouped[cols].shift(shift) if shift else df[cols]
    return shifted, grouped.ngroup()


def rolling_sums(df: pd.DataFrame, keys: list, cols: list, windows=WINDOWS,
                 how: str = "sum", prefix: str = "", shift: int = 1) -> pd.DataFrame:
    """{prefix}{col}_l{N} for each window and {prefix}{col}_std season-to-date.
    `df` must already be in (keys, game order) order."""
    shifted, group_ids = _shifted_groups(df, keys, cols, shift)
    work = shifted.copy()
    work["__group"] = group_ids

    out = {}
    for window in windows:
        rolled = work.groupby("__group", sort=False)[cols].rolling(window, min_periods=1)
        agg = (rolled.sum() if how == "sum" else rolled.mean()).droplevel(0)
        for col in cols:
            out[f"{prefix}{col}_l{window}"] = agg[col]

    expanding = work.groupby("__group", sort=False)[cols].expanding(min_periods=1)
    agg = (expanding.sum() if how == "sum" else expanding.mean()).droplevel(0)
    for col in cols:
        out[f"{prefix}{col}_{SEASON_TO_DATE}"] = agg[col]

    return pd.DataFrame(out, index=df.index)


def games_played(df: pd.DataFrame, keys: list, windows=WINDOWS, prefix: str = "",
                 shift: int = 1) -> pd.DataFrame:
    """gp_l{N} / gp_std: how many games each window is actually averaging over."""
    counter = pd.DataFrame({"__one": 1.0}, index=df.index)
    counter[keys] = df[keys]
    return rolling_sums(counter, keys, ["__one"], windows, "sum", prefix, shift).rename(
        columns=lambda c: c.replace("__one", "gp")
    )


def per60(frame: pd.DataFrame, numerators: list, toi_column: str, windows=WINDOWS,
          prefix: str = "") -> pd.DataFrame:
    """{stat}_p60_{window} = stat / (matching-window TOI seconds / 3600). NaN where the
    window has no ice time at all, which keeps "never played" distinct from "played, did
    nothing"."""
    out = {}
    for window in list(windows) + [SEASON_TO_DATE]:
        suffix = f"l{window}" if window != SEASON_TO_DATE else SEASON_TO_DATE
        hours = frame[f"{toi_column}_{suffix}"] / 3600.0
        hours = hours.where(hours > 0)
        for col in numerators:
            out[f"{prefix}{col}_p60_{suffix}"] = frame[f"{col}_{suffix}"] / hours
    return pd.DataFrame(out, index=frame.index)


def last_value(df: pd.DataFrame, keys: list, cols: list, prefix: str = "last_") -> pd.DataFrame:
    """The previous game's raw value -- the shortest window there is."""
    shifted = df.groupby(keys, sort=False)[cols].shift(1)
    return shifted.rename(columns={c: f"{prefix}{c}" for c in cols})


def last_source_date(df: pd.DataFrame, keys: list, date_column: str = "game_date") -> pd.Series:
    """The date of the most recent game feeding this row's windows. base.py asserts this is
    strictly before the row's own game date, which is the leakage check on real data."""
    return df.groupby(keys, sort=False)[date_column].shift(1)


def prior_season_aggregate(player_games: pd.DataFrame, prior_by_season: dict,
                           sum_cols: list, toi_column: str = "toi") -> pd.DataFrame:
    """One row per (player_id, season_id) holding that player's *previous* season totals,
    named prev_*, plus prev_gp and per-60 rates.

    Seasons whose predecessor is not ingested get no rows at all, so the join leaves NULLs --
    which is the correct and explicit state for the earliest ingested season rather than a
    zero that would read as "played but produced nothing".
    """
    totals = (player_games.groupby(["player_id", "season_id"], as_index=False)
              .agg({**{c: "sum" for c in sum_cols}, "game_id": "count"})
              .rename(columns={"game_id": "gp"}))

    rows = []
    for season_id, prior_id in prior_by_season.items():
        if prior_id is None:
            continue
        prior = totals[totals["season_id"] == prior_id].copy()
        prior["season_id"] = season_id
        rows.append(prior)
    if not rows:
        return pd.DataFrame(columns=["player_id", "season_id"])

    prev = pd.concat(rows, ignore_index=True)
    hours = (prev[toi_column] / 3600.0).where(lambda h: h > 0)
    for col in sum_cols:
        if col != toi_column:
            prev[f"{col}_p60"] = prev[col] / hours
    prev[f"{toi_column}_per_game"] = prev[toi_column] / prev["gp"].where(prev["gp"] > 0)

    renames = {c: f"prev_{c}" for c in prev.columns if c not in ("player_id", "season_id")}
    return prev.rename(columns=renames)

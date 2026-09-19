"""Corrects league-level drift between the training seasons and the one being predicted.

The models learn a league. The next one is a different league: between 2024-25 and 2025-26,
elite power-play time rose 10.7% and league shooting percentage rose 4.2%, continuing a trend
(10.18% -> 10.64% -> 11.09% over three seasons). A model fit on the old level projects the new
one low, and the error concentrates in exactly the players whose level matters most.

This was originally misdiagnosed as over-shrinkage in the goals model and Tweedie
over-shrinkage in `pp_toi`. It is neither: loosening the goals model's regularization on the
early-stop slice does not reduce elite bias (+0.6% at the tight setting, +1.7% loosened), and
the drift figures above match the observed bias almost exactly.

The fix reuses the mechanism already in the stack. Every count model carries a mean-matching
intercept fitted on the training rows; this re-estimates that intercept from *recent completed
games* instead, as a multiplicative factor on lambda:

    factor(category, date) = sum(actual) / sum(predicted)   over games in [date - window, date)

Leakage-safe by construction: a game on date D only ever uses games that finished before D,
which is exactly what a manager knows at that evening's lock.

Two deliberate choices keep this from becoming another tuned knob:

- **Almost nothing here is tuned.** The window is 30 days and the ramp needs 3,000
  player-games -- chosen as "about a month of hockey" and "enough rows for a stable ratio",
  not selected against any score. The one exception is honest to record: league-wide versus
  stratified *was* chosen by comparing holdout bias, so the benefit below is mildly
  optimistic and wants re-checking on a season this model has never seen.
- **It ramps in and is clamped.** Early in a season there is no history, so the factor starts
  at 1.0 and blends in as games accumulate; it is clamped to [0.8, 1.25] so one freak week
  cannot swing a projection.

It corrects the *level* of a category league-wide. It cannot fix a player whose own role
changed -- that is the rolling features' job.
"""

import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

WINDOW_DAYS = 30
MIN_ROWS = 3000

# League-wide by default. Stratifying by predicted value is implemented below and is better
# motivated in theory -- drift is stronger at the top -- but measured worse on the holdout
# (mean absolute per-tier fantasy-point bias 2.58% against 2.26% league-wide), because the
# strata are per-category while the decisions that care about level are per-player, so the
# two partitions do not line up. Kept, not used.
STRATA = 1
CLAMP = (0.8, 1.25)
CATEGORIES = ["shots", "hits", "blocks", "assists", "goals", "pim", "toi", "ev_toi", "pp_toi"]


def strata_of(values: pd.Series, count=STRATA) -> np.ndarray:
    """Quantile bins of the model's own prediction.

    Drift is not uniform across the talent range -- between 2024-25 and 2025-26 league
    power-play time rose 6.2% while *elite* power-play time rose 10.7% -- so one league-wide
    factor under-corrects the top and over-corrects the bottom. Binning by predicted value
    lets each stratum carry its own correction. The bins come from predictions, never
    outcomes, so this stays leakage-safe.
    """
    ranks = values.rank(pct=True, method="first")
    return np.clip((ranks * count).astype(int), 0, count - 1)


def rolling_factors(frame: pd.DataFrame, categories=None, window_days=WINDOW_DAYS,
                    min_rows=MIN_ROWS, strata=STRATA) -> pd.DataFrame:
    """One factor per (date, stratum, category), from games strictly before that date.

    `frame` needs `game_date`, `target_played`, and matching `target_*` / `pred_*` columns.
    Returns a frame indexed by (game_date, stratum); `strata=1` gives the league-wide form.
    """
    categories = [c for c in (categories or CATEGORIES)
                  if f"pred_{c}" in frame and f"target_{c}" in frame]
    played = frame[frame["target_played"].astype(bool)].copy()
    played["game_date"] = pd.to_datetime(played["game_date"])

    # One stratum column per category: a player can be elite for hits and fringe for goals.
    for c in categories:
        played[f"s_{c}"] = strata_of(played[f"pred_{c}"], strata)

    per_stratum = min_rows / max(strata, 1)
    out = []
    for c in categories:
        daily = played.groupby(["game_date", f"s_{c}"]).agg(
            rows=("target_played", "size"),
            actual=(f"target_{c}", "sum"),
            predicted=(f"pred_{c}", "sum"),
        ).reset_index().rename(columns={f"s_{c}": "stratum"})
        for stratum, group in daily.groupby("stratum"):
            group = group.sort_values("game_date")
            dates = group["game_date"].to_numpy()
            for i, date in enumerate(dates):
                window = group.iloc[:i]
                window = window[window["game_date"] > date - pd.Timedelta(days=window_days)]
                rows = int(window["rows"].sum())
                ramp = min(rows / per_stratum, 1.0) if per_stratum else 1.0
                predicted = window["predicted"].sum()
                raw = (window["actual"].sum() / predicted) if predicted > 0 else 1.0
                blended = 1.0 + (raw - 1.0) * ramp      # no history -> no correction
                out.append({"game_date": date, "stratum": stratum, "category": c,
                            "history_rows": rows,
                            "factor": float(np.clip(blended, *CLAMP))})
    return (pd.DataFrame(out)
            .pivot_table(index=["game_date", "stratum"], columns="category", values="factor")
            .rename_axis(columns=None))


def apply(frame: pd.DataFrame, factors: pd.DataFrame, categories=None,
          strata=STRATA) -> pd.DataFrame:
    """Scale each row's `pred_*` by its (date, stratum) factor. Returns a copy."""
    categories = [c for c in (categories or CATEGORIES) if c in factors.columns]
    scaled = frame.copy()
    dates = pd.to_datetime(scaled["game_date"])
    for c in categories:
        column = f"pred_{c}"
        if column not in scaled:
            continue
        stratum = strata_of(scaled[column], strata)
        key = pd.MultiIndex.from_arrays([dates, stratum])
        factor = factors[c].reindex(key).to_numpy()
        scaled[column] = scaled[column].to_numpy() * np.where(np.isfinite(factor), factor, 1.0)
    return scaled


def recalibrate(frame: pd.DataFrame, **kwargs) -> pd.DataFrame:
    """rolling_factors + apply, the way callers normally want it."""
    factors = rolling_factors(frame, **kwargs)
    log.info("drift factors over %d (date, stratum) pairs: %s", len(factors),
             ", ".join(f"{c} {factors[c].mean():.3f}" for c in factors.columns
                       if c != "history_rows"))
    return apply(frame, factors)

"""Loading the feature tables and turning them into a model matrix.

Three rules live here, because every model depends on them being applied identically:

1. **No game-N truth reaches the features.** Every `target_*` and `label_*` column is the
   outcome of the game being predicted; `build_feature_matrix` drops them and then asserts
   none survived, so a new column in `ModelFeatures/` cannot leak in silently.
2. **Splits are chronological.** Fit on the earlier seasons, early-stop on the tail of the
   latest fitting season, hold the newest season out entirely. Nothing is ever shuffled
   across time.
3. **Variant B's copies stay together.** B holds `copies` perturbed versions of each
   candidate row; they share a `game_date`, so date-based splitting keeps them in the same
   side by construction (asserted), and each carries `weight = 1/copies` so B does not count
   three times against A in the loss.

Seasons whose prior season is not ingested lack the `prev_*` columns entirely rather than
carrying them as NULL -- 2023-24 has 463 columns against 2024-25's 482 -- so loading takes
the union and fills the gap with NaN, which LightGBM splits on natively.
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd

import paths

log = logging.getLogger(__name__)

# Identity and bookkeeping columns: never features, but kept on the frame so predictions can
# be joined back and grouped.
KEY_COLUMNS = [
    "season_id", "game_id", "game_date", "team_id", "player_id", "position",
    "variant", "copy_index",
]

# Dropped from the model matrix on top of the target_/label_ families.
DROP_FROM_FEATURES = {
    # keys and bookkeeping
    "season_id", "game_id", "game_date", "variant", "copy_index",
    "source_game_id", "source_game_date", "source_team_id",
    # raw identities: a player must reach the model through his rolling history, not as an
    # id the trees can memorize. His line-mates' and opposing goalie's *quality* columns
    # stay; only the bare ids go.
    "player_id", "opp_goalie_player_id",
    "feat_mate1_id", "feat_mate2_id", "feat_partner_id",
    # Team identity goes for the same reason a player's does, and it was measured rather
    # than assumed. As categoricals these two were taking a large share of the gain in the
    # models that lean hardest on team context -- memorizing which clubs conceded shots in
    # the training seasons instead of learning it from form. Rosters and coaches turn over,
    # so that does not transfer to a new season. Dropping them improved every model on the
    # 2025-26 holdout and hurt none: hits R2 31.0% -> 31.3%, blocks 20.0% -> 20.2%, shots
    # 20.7% -> 20.8%, top-100 capture 0.698 -> 0.700. A team's *form* still reaches the
    # model through the 60-odd team_* and opp_* columns; only the bare id is gone.
    "team_id", "opp_team_id",
}

# Passed to LightGBM as categorical features; everything else is numeric or boolean.
# Position only: team identity is dropped outright (see DROP_FROM_FEATURES).
CATEGORICAL = ["position"]

# Carried into the saved predictions so evaluate.py can build its two naive baselines
# without reloading the whole 449-column feature table.
BASELINE_COLUMNS = [
    "gp_l10", "gp_std", "mean_toi_std", "mean_toi_l10",
    *[f"{stat}_p60_std" for stat in
      ("shots", "hits", "blocks", "assists", "goals", "pim", "ppp")],
    *[f"{stat}_l10" for stat in
      ("shots", "hits", "blocks", "assists", "goals", "pim", "ppp", "points")],
]


def load_seasons(seasons, variant="B", features_dir: Path | None = None) -> pd.DataFrame:
    """Concatenate the given seasons' feature tables, taking the union of columns."""
    frames = []
    for season in seasons:
        path = paths.feature_table(season, variant, features_dir)
        if not path.exists():
            raise FileNotFoundError(
                f"{path} is missing -- build it with ModelFeatures/build_feature_table.py "
                f"--season {season}" + ("" if variant == "A" else f" --variant {variant}")
            )
        frame = pd.read_parquet(path)
        frame["season"] = season
        frames.append(frame)
        log.info("loaded %s variant %s: %d rows x %d columns", season, variant,
                 len(frame), frame.shape[1])

    table = pd.concat(frames, ignore_index=True, sort=False)
    table["game_date"] = pd.to_datetime(table["game_date"])
    return table.sort_values(["game_date", "game_id", "player_id"]).reset_index(drop=True)


def feature_columns(table: pd.DataFrame) -> list[str]:
    """Every column that may be fed to a model, in a stable order."""
    columns = [
        c for c in table.columns
        if not c.startswith("target_")
        and not c.startswith("label_")
        and c not in DROP_FROM_FEATURES
        and c != "season"
    ]
    leaked = [c for c in columns if c.startswith(("target_", "label_"))]
    assert not leaked, f"game-N truth reached the feature matrix: {leaked}"
    return columns


def build_feature_matrix(table: pd.DataFrame, columns: list[str] | None = None):
    """The model matrix and the column list used to build it.

    Booleans become floats and the categorical ids become pandas categoricals, which is what
    LightGBM wants; everything else is left as-is so NaN keeps meaning 'no history'.
    """
    columns = columns or feature_columns(table)
    matrix = table.reindex(columns=columns).copy()

    for column in matrix.columns:
        if matrix[column].dtype == bool:
            matrix[column] = matrix[column].astype("float32")
        elif matrix[column].dtype == object and column not in CATEGORICAL:
            # An object column is usually a genuine category, but it can also be numbers the
            # driver handed back as Decimals -- SQL Server DECIMAL columns arrive that way and
            # survive a groupby sum as dtype=object. Try numeric first: mistaking a rate for a
            # categorical silently destroys its ordering, and LightGBM cannot serialize the
            # result either (it fails with "Circular reference detected").
            numeric = pd.to_numeric(matrix[column], errors="coerce")
            if numeric.notna().sum() >= matrix[column].notna().sum():
                matrix[column] = numeric.astype("float64")
            else:
                matrix[column] = matrix[column].astype("category")

    for column in CATEGORICAL:
        if column in matrix.columns:
            matrix[column] = matrix[column].astype("category")

    return matrix, columns


def categorical_in(columns: list[str]) -> list[str]:
    return [c for c in CATEGORICAL if c in columns]


def copy_weights(table: pd.DataFrame) -> np.ndarray:
    """1/copies per row, so a 3x-copied variant B carries the same total weight as A."""
    if "copy_index" not in table.columns:
        return np.ones(len(table), dtype="float64")
    copies = table["copy_index"].nunique()
    return np.full(len(table), 1.0 / max(copies, 1), dtype="float64")


class Split:
    """The three chronological slices every model is trained against."""

    def __init__(self, table, fit_index, early_index, holdout_index, cutoff):
        self.table = table
        self.fit = fit_index
        self.early = early_index
        self.holdout = holdout_index
        self.cutoff = cutoff

    def __repr__(self):
        return (f"Split(fit={len(self.fit):,}, early_stop={len(self.early):,}, "
                f"holdout={len(self.holdout):,}, cutoff={self.cutoff:%Y-%m-%d})")


def chronological_split(table: pd.DataFrame, train_seasons, holdout_season,
                        early_stop_fraction=0.25) -> Split:
    """Fit on `train_seasons`, early-stop on the last `early_stop_fraction` of the latest one
    by date, hold `holdout_season` out whole.

    `holdout_season=None` is the deployment case: every available season goes into the fit
    and there is nothing left to score against. Legitimate once the holdout has done its job
    -- a model shipped for next season should not be handicapped by withholding the most
    recent one, which is also the most relevant one. It does mean the run produces no
    metrics, so the numbers in `docs/` keep describing the last model that *was* scored.
    """
    in_train = table["season"].isin(train_seasons)
    in_holdout = (table["season"] == holdout_season) if holdout_season else pd.Series(
        False, index=table.index)
    if not in_train.any():
        raise ValueError(f"no rows for training seasons {train_seasons}")
    if holdout_season and not in_holdout.any():
        raise ValueError(f"no rows for holdout season {holdout_season}")

    latest = max(train_seasons)
    latest_dates = table.loc[table["season"] == latest, "game_date"]
    cutoff = latest_dates.quantile(1 - early_stop_fraction)

    fit = table.index[in_train & (table["game_date"] < cutoff)]
    early = table.index[in_train & (table["game_date"] >= cutoff)]
    holdout = table.index[in_holdout]

    _assert_copies_together(table, fit, early, holdout)
    return Split(table, fit, early, holdout, cutoff)


def _assert_copies_together(table, *indexes):
    """Variant B's perturbed copies of one candidate must never straddle a split -- the
    copies differ only in their lineup, so a copy on each side is a near-duplicate leak."""
    if "copy_index" not in table.columns or table["copy_index"].nunique() < 2:
        return
    membership = pd.Series(-1, index=table.index)
    for number, index in enumerate(indexes):
        membership.loc[index] = number
    keyed = table.loc[membership >= 0, ["game_id", "player_id"]].copy()
    keyed["side"] = membership.loc[membership >= 0]
    straddling = keyed.groupby(["game_id", "player_id"])["side"].nunique()
    count = int((straddling > 1).sum())
    assert count == 0, f"{count} candidate(s) had perturbed copies on both sides of a split"

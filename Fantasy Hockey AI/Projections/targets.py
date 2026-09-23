"""The model stack: one spec per target, and the offsets that chain them together.

Every count model is fit with a Poisson objective and an `init_score` offset. `init_score` is
LightGBM's per-row intercept, and under a log link an intercept of `log(TOI/3600)` means the
trees can only learn a *per-60 rate* -- the opportunity term is supplied, not fitted. That is
the "rate x opportunity" decomposition the build plan asks for, and it is why a fourth-liner
and a first-liner with the same per-60 profile are not pushed toward the same count.

The offsets come from *out-of-fold predictions* of the upstream model, never from the actual
TOI or actual shots. Live, the shots model sees a predicted TOI with error in it; training it
against the truth would teach it a precision it will never have at the lock.

Order matters: `PIPELINE` is both the training order and the prediction chain.

    plays -> toi -> {ev_toi, pp_toi} -> shots -> {hits, blocks, assists, pim} -> goals
                                                          -> pp_point_share, sh_point_share

Nothing in this module knows what a goal is worth. These models project stats; a scoring
system is applied downstream by whatever consumes them (`weights.py`), so one fitted stack
serves any number of leagues.
"""

from dataclasses import dataclass, field

import numpy as np

# Floors before taking a log, so a near-zero upstream prediction cannot produce -inf.
MIN_TOI_SECONDS = 60.0
MIN_SHOTS = 0.05

BASE_PARAMS = {
    "learning_rate": 0.04,
    "num_leaves": 63,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.7,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 5.0,
    "num_threads": 0,
    "verbosity": -1,
    "seed": 17,
}


@dataclass
class Target:
    name: str
    column: str                      # the target_* column in the feature table
    objective: str
    rows: str = "played"             # "all", "played", or "scored"
    offset: str | None = None        # None, "toi", or "shots"
    params: dict = field(default_factory=dict)
    weight_column: str | None = None  # extra per-row weight (on top of the copy weight)
    note: str = ""

    def lgb_params(self) -> dict:
        merged = dict(BASE_PARAMS)
        merged["objective"] = self.objective
        merged.update(self.params)
        return merged


PIPELINE = [
    Target("plays", "target_played", "binary", rows="all",
           params={"metric": ["binary_logloss", "auc"]},
           note="Multiplies every other projection: a projection is worth zero if he is a "
                "healthy scratch. Fit on every lockout-knowable candidate, not just players "
                "who dressed."),

    Target("toi", "target_toi", "regression",
           params={"metric": ["l2", "l1"]},
           note="Total ice time in seconds. In-game injury truncation is unforecastable but "
                "rare (0.5% of played rows under five minutes), so plain L2 holds."),
    Target("ev_toi", "target_ev_toi", "regression",
           params={"metric": ["l2", "l1"]},
           note="Even-strength seconds."),
    Target("pp_toi", "target_pp_toi", "poisson",
           params={"metric": ["poisson", "l1"]},
           note="Power-play seconds: a large zero mass for anyone off both units. Tweedie is "
                "the obvious shape for that and is again slightly worse -- measured on the "
                "early-stop slice, p=1.4 biases the top PP decile -2.9% against Poisson's "
                "-0.8% at the same MAE. Same lesson as PIM: Tweedie over-shrinks the tail."),

    Target("shots", "target_shots", "poisson", offset="toi",
           params={"metric": ["poisson", "l1"]},
           note="High volume and fairly stable; the sanity check for the whole offset scheme, "
                "since every other count offsets on the same predicted ice time."),
    Target("hits", "target_hits", "poisson", offset="toi",
           params={"metric": ["poisson", "l1"]},
           note="The most scorekeeper-contaminated stat on the sheet -- rink-to-rink variance is "
                "large and persistent -- so arena_hit_factor is load-bearing here, not a "
                "refinement. Role-driven and sticky, hence one of the better-projected counts."),
    Target("blocks", "target_blocks", "poisson", offset="toi",
           params={"metric": ["poisson", "l1"]},
           note="Penalty-kill and defensive-zone deployment drive it, which makes it far more "
                "predictable for defencemen than for forwards."),
    Target("assists", "target_assists", "poisson", offset="toi",
           params={"metric": ["poisson", "l1"]},
           note="Its own model rather than a twin of goals: an assist depends on line-mates' "
                "finishing, which is a different generating process from a player's own."),
    Target("pim", "target_pim", "poisson", offset="toi",
           params={"metric": ["poisson", "l1"]},
           note="83% zeros with a lump at 2 and a thin major/misconduct tail. Tweedie was the "
                "obvious shape and was measurably worse: at variance powers 1.6/1.3/1.1 it "
                "under-predicted the holdout mean by 21.5/16.9/13.0% against Poisson's "
                "10.7%, and lost on RMSE too. Poisson on the minutes it is. The weakest "
                "model in the stack -- barely predictable at game level."),

    Target("goals", "target_goals", "poisson", offset="shots",
           params={"num_leaves": 15, "min_data_in_leaf": 1000, "lambda_l2": 50.0,
                   "feature_fraction": 0.5, "metric": ["poisson", "l1"]},
           note="Chained through shots: with log(E[shots]) as the offset the trees can only "
                "learn a log-shooting-percentage correction, and the tight leaf/L2 budget is "
                "the heavy shrinkage that keeps it from fitting hot-hand noise. Deliberately "
                "NOT loosened: the elite under-projection seen on the 2025-26 holdout is "
                "league drift (shooting% has risen ~4% a season), not over-shrinkage. On the "
                "early-stop slice this setting gives +0.6% elite bias, and leaves31/min500/"
                "L2=20 and leaves63/min200/L2=5 give +1.7% and +1.2% at equal deviance and "
                "AUC -- looser is not better, just noisier. Drift is handled by drift.py."),

    Target("pp_point_share", "target_ppp", "cross_entropy", rows="scored",
           weight_column="target_points",
           params={"num_leaves": 31, "min_data_in_leaf": 500, "metric": ["cross_entropy"]},
           note="P(a given point is a power-play point), fit on scorers with each row "
                "weighted by his points. Sampling each point's strength downstream keeps "
                "PPP <= points, which an independent PPP count model would violate."),
    Target("sh_point_share", "target_shp", "cross_entropy", rows="scored",
           weight_column="target_points",
           params={"num_leaves": 15, "min_data_in_leaf": 2000, "lambda_l2": 20.0,
                   "metric": ["cross_entropy"]},
           note="The short-handed twin of pp_point_share. Short-handed points are rare -- "
                "well under 1% of most scoring systems -- and an earlier version of this "
                "stack skipped them for that reason. That was a scoring-dependent decision "
                "baked into the models, so it is gone: a league that pays for short-handed "
                "work now gets a projection instead of a zero. Heavily regularized, because "
                "the base rate is tiny and the honest answer is usually 'almost never'."),
]

BY_NAME = {target.name: target for target in PIPELINE}

# Both point-strength shares are modelled, so the stack carries no assumption about what any
# league pays for. They are shares of a point rather than counts, so PPP + SHP <= points holds
# by construction; `predict.py` clamps the pair in the rare case they sum above 1.
POINT_SHARES = ["pp_point_share", "sh_point_share"]


def row_mask(table, rows: str):
    """Which rows a target is fit on."""
    if rows == "all":
        return np.ones(len(table), dtype=bool)
    played = table["target_played"].to_numpy(dtype=bool)
    if rows == "played":
        return played
    if rows == "scored":
        return played & (table["target_points"].fillna(0).to_numpy() > 0)
    raise ValueError(f"unknown row filter {rows!r}")


def label_values(target: Target, table):
    """The label column, with share targets converted from counts to fractions of a point.

    Keyed off the `_point_share` suffix rather than a single hard-coded name: cross-entropy
    needs a label in [0, 1], and a count of 2 power-play points is not that.
    """
    values = table[target.column].astype("float64")
    if target.name.endswith("_point_share"):
        points = table["target_points"].astype("float64")
        return (values / points.where(points > 0)).clip(0.0, 1.0)
    return values


def offset_values(target: Target, upstream) -> np.ndarray | None:
    """`init_score` for this target, from the upstream model's out-of-fold predictions.

    `upstream` is a frame carrying a `toi` and/or `shots` column of predictions.
    """
    if target.offset is None:
        return None
    if target.offset == "toi":
        seconds = np.asarray(upstream["toi"], dtype="float64")
        return np.log(np.maximum(seconds, MIN_TOI_SECONDS) / 3600.0)
    if target.offset == "shots":
        shots = np.asarray(upstream["shots"], dtype="float64")
        return np.log(np.maximum(shots, MIN_SHOTS))
    raise ValueError(f"unknown offset {target.offset!r}")


def mean_matching_intercept(labels, weights, offset, rows) -> float:
    """The intercept that makes the zero-tree prediction match the fitting rows' mean.

    A bare `log(TOI/3600)` offset starts the model at a rate of exactly 1.0 per 60 minutes.
    Shots run at 5.6 per 60, so the trees would have to climb log(5.6) = 1.72 in link space
    from the intercept alone -- and with a small learning rate and early stopping they stop
    short, which showed up as a flat 12-28% under-prediction on every offset model except
    assists, whose true rate of 1.05 per 60 happens to sit where the bare offset starts.

    Adding `log(sum(w*y) / sum(w*exp(offset)))` -- the closed-form Poisson MLE for an
    intercept given an offset -- starts the model mean-matched, so boosting only has to
    learn who differs from average. Computed on the fitting rows alone, never the holdout.
    """
    labels = np.asarray(labels, dtype="float64")[rows]
    weights = np.asarray(weights, dtype="float64")[rows]
    expected = np.exp(np.asarray(offset, dtype="float64")[rows])
    numerator = float(np.sum(weights * labels))
    denominator = float(np.sum(weights * expected))
    if numerator <= 0 or denominator <= 0:
        return 0.0
    return float(np.log(numerator / denominator))

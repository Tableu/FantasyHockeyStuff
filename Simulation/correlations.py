#!/usr/bin/env python
"""Fits the spread *between* players that the projection layer does not carry.

`Projections/calibrate.py` fits how much one player's night varies around his own mean. This
fits how players' nights move together, which is the number head-to-head decisions actually
run on, and it is fit the same way: measured off the scored holdout rather than assumed.

Three things come out of a run, all into `reports/correlations.json`:

1. **The measured targets** -- within-player, teammate and opponent residual correlation
   matrices, from `Projections/reports/predictions_B.parquet`.
2. **The normal-scale matrices the copula needs.** Correlation attenuates on its way through
   a discrete inverse CDF, so asking the copula for 0.046 gets less than 0.046 in the
   counts. The fit simulates, measures what came out, and corrects, until the realized
   correlations land on the measured ones.
3. **The penalty-incident weights** -- minor / major / misconduct -- fit by maximum
   likelihood against the holdout distribution of per-game penalty minutes.

What the measurement found, on 46,654 played player-games:

    teammate, same category   assists +0.046, pim +0.030, blocks +0.021, hits +0.013,
                              shots +0.011, goals -0.002
    teammate, across          assists x goals +0.070 -- one goal, two assists, and by far
                              the largest cross-player number on the board
    opponent, same category   everything within noise of zero except pim, at +0.060:
                              a fight hands both benches minutes at once

Usage:
    python correlations.py                      # fit and write reports/correlations.json
    python correlations.py --sims 200 --iterations 4
"""

import argparse
import json
import logging

import numpy as np
import pandas as pd
from scipy import optimize, special

import copula as copula_module
import marginals
import paths
import sampler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("correlations")

CATEGORIES = sampler.CATEGORIES

# Pairs whose correlation a *mechanism* already supplies, so the copula must not also ask
# for it. Goals are thinned out of sampled shots, which reproduces the measured within-player
# shots/goals correlation of 0.299 on its own; starting the fit anywhere else would
# double-count it.
MECHANISM_PAIRS = {("shots", "goals")}

# The side an NHL team dresses, and so the block size the fitted teammate matrix has to stay
# feasible for. Drawing a smaller subset -- a fantasy roster's two or three players from one
# club -- is always easier, never harder.
REFERENCE_TEAM_SIZE = 18


def parse_args():
    parser = argparse.ArgumentParser(description="Fit the between-player correlation structure")
    parser.add_argument("--variant", choices=("A", "B"), default="B",
                        help="Which scored holdout to measure (default B, the trained one)")
    parser.add_argument("--sims", type=int, default=100,
                        help="Draws per fitting iteration (default 100)")
    parser.add_argument("--batch-sims", type=int, default=20,
                        help="Draws held in memory at once (default 20)")
    parser.add_argument("--iterations", type=int, default=3,
                        help="Attenuation-correction passes (default 3)")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--out", default=None)
    return parser.parse_args()


def load_holdout(variant):
    """The scored holdout, one copy per player-game, restricted to players who dressed."""
    path = paths.holdout_predictions(variant)
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing -- run Projections/train.py --all")
    frame = pd.read_parquet(path)
    frame = frame[frame["target_played"].astype(bool)]
    if "copy_index" in frame.columns:
        frame = frame[frame["copy_index"] == 0]
    return frame.reset_index(drop=True)


def load_dispersion():
    payload = json.loads(paths.DISPERSION_PATH.read_text(encoding="utf-8"))
    return {name: entry["theta"] for name, entry in payload["categories"].items()}


def fit_dispersion(frame, categories=None):
    """Maximum-likelihood NB theta per category, under `Var = mu + theta*mu^2`.

    This is `Projections/calibrate.py`'s estimator, and normally its answer is simply read
    from `dispersion.json` rather than recomputed. It exists here for one purpose: a
    split-half check is only out-of-sample if *every* fitted number is re-fit on the
    training half, and the dispersion is fitted on the same holdout as everything else.
    Using the shipped theta there would quietly leave the test half inside the fit.
    """
    out = {}
    for category in (categories or CATEGORIES):
        actual = frame[f"target_{category}"].to_numpy("float64")
        mu = np.maximum(frame[f"pred_{category}"].to_numpy("float64"), 1e-6)

        def negative_log_likelihood(log_theta):
            size = 1.0 / np.exp(log_theta)
            return -float(np.sum(special.gammaln(actual + size) - special.gammaln(size)
                                 - special.gammaln(actual + 1.0)
                                 + size * np.log(size / (size + mu))
                                 + actual * np.log(mu / (size + mu))))

        result = optimize.minimize_scalar(negative_log_likelihood,
                                          bounds=(np.log(1e-4), np.log(20.0)),
                                          method="bounded")
        out[category] = float(np.exp(result.x))
    return out


def standardized(frame, dispersion):
    """(actual - mu) / sd under the fitted NB, the same scale calibrate.py works on."""
    out = np.empty((len(frame), len(CATEGORIES)))
    for index, category in enumerate(CATEGORIES):
        mu = np.maximum(frame[f"pred_{category}"].to_numpy("float64"), 1e-6)
        actual = frame[f"target_{category}"].to_numpy("float64")
        theta = dispersion.get(category) or 0.0
        out[:, index] = (actual - mu) / np.sqrt(mu + theta * mu ** 2)
    return out


class StructureAccumulator:
    """Sufficient statistics for the three correlation matrices, poolable across draws.

    Every estimator is a sum of products over pairs of rows, so a pass adds its sums and the
    pair counts and nothing has to be held in memory between passes.
    """

    def __init__(self, size):
        self.size = size
        self.within = np.zeros((size, size))
        self.teammate = np.zeros((size, size))
        self.opponent = np.zeros((size, size))
        self.n_rows = 0.0
        self.n_teammate = 0.0
        self.n_opponent = 0.0
        self.sum_squares = np.zeros(size)
        self.sum_values = np.zeros(size)

    def add(self, residuals, team_index, game_index):
        rows, size = residuals.shape
        team_sums = np.stack([np.bincount(team_index, r, int(team_index.max()) + 1)
                              for r in residuals.T], axis=1)
        game_sums = np.stack([np.bincount(game_index, r, int(game_index.max()) + 1)
                              for r in residuals.T], axis=1)
        team_counts = np.bincount(team_index, minlength=team_sums.shape[0]).astype("float64")
        game_counts = np.bincount(game_index, minlength=game_sums.shape[0]).astype("float64")

        self.within += residuals.T @ residuals
        # ordered pairs i != j on the same team
        self.teammate += team_sums.T @ team_sums - residuals.T @ residuals
        # ordered pairs across the two teams of a game
        self.opponent += (game_sums.T @ game_sums - team_sums.T @ team_sums)
        self.n_rows += rows
        self.n_teammate += float((team_counts * (team_counts - 1)).sum())
        self.n_opponent += float((game_counts ** 2).sum() - (team_counts ** 2).sum())
        self.sum_squares += (residuals ** 2).sum(axis=0)
        self.sum_values += residuals.sum(axis=0)

    def matrices(self):
        variance = self.sum_squares / self.n_rows - (self.sum_values / self.n_rows) ** 2
        scale = np.outer(np.sqrt(variance), np.sqrt(variance))
        return (self.within / self.n_rows / scale,
                self.teammate / self.n_teammate / scale,
                self.opponent / self.n_opponent / scale)


def measure(residuals, team_index, game_index):
    accumulator = StructureAccumulator(residuals.shape[1])
    accumulator.add(residuals, team_index, game_index)
    return accumulator.matrices()


def fit_incident_weights(frame):
    """Minor / major / misconduct weights, by maximum likelihood on per-game PIM.

    The holdout's penalty minutes are 98.7% even and pile up on 0, 2, 4, 5 and 10. That is
    a count of incidents with a lumpy size, not an overdispersed count, and these two free
    parameters are the whole of it.
    """
    lam = np.maximum(frame["pred_pim"].to_numpy("float64"), 1e-6)
    observed = np.clip(frame["target_pim"].to_numpy("float64"), 0, 60).astype(int)
    # The PIM model under-predicts its own level by about 11% -- a known, separately handled
    # bias (Projections/drift.py). Fitting the incident mix against a biased rate would push
    # that bias into the *sizes*: fewer, bigger penalties, which showed up as a 25% variance
    # over-shoot and half the true rate of four- and five-minute nights. The size mix is a
    # property of hockey, not of the projection's level, so the level is divided out first.
    level = float(observed.sum() / lam.sum())
    log.info("penalty level: observed / projected = %.4f, divided out before the size fit",
             level)
    lam = lam * level
    # Binned by lambda: the pmf is a convolution per lambda, and 40 bins is far finer than
    # the spread of a per-game PIM projection warrants.
    bins = np.quantile(lam, np.linspace(0, 1, 41))
    index = np.clip(np.searchsorted(bins, lam) - 1, 0, 39)
    bin_lambda = np.array([lam[index == b].mean() if (index == b).any() else 0.0
                           for b in range(40)])

    def negative_log_likelihood(free):
        major, misconduct, latent = free
        if (major < 1e-4 or misconduct < 1e-5 or major + misconduct > 0.5
                or latent < 0.0 or latent > 20.0):
            return 1e12
        weights = np.array([1.0 - major - misconduct, major, misconduct])
        table = np.stack([marginals.compound_poisson_pmf(value, weights, 60, latent)
                          for value in bin_lambda])
        probability = np.maximum(table[index, observed], 1e-12)
        return -float(np.log(probability).sum())

    result = optimize.minimize(negative_log_likelihood, x0=[0.05, 0.02, 0.5],
                               method="Nelder-Mead",
                               options={"xatol": 1e-4, "fatol": 1e-2, "maxiter": 400})
    major, misconduct, latent = result.x
    weights = np.array([1.0 - major - misconduct, major, misconduct])
    log.info("penalty incidents: minor %.4f major %.4f misconduct %.4f (mean %.2f min), "
             "latent intensity variance %.3f",
             *weights, float(weights @ marginals.INCIDENT_MINUTES), latent)
    return weights, float(latent), float(result.fun)


def lambda_frame(frame):
    """The holdout's predictions, shaped like a `predict.py` lambda table.

    `p_plays` is forced to 1: the measured correlations were computed over players who
    dressed, so the simulated ones have to be conditioned the same way or the scratch mass
    would dilute them.
    """
    out = pd.DataFrame({
        "game_id": frame["game_id"].to_numpy(),
        "team_id": frame["team_id"].to_numpy(),
        "player_id": frame["player_id"].to_numpy(),
        "p_plays": 1.0,
        "pp_point_share": frame["pred_pp_point_share"].to_numpy(),
        "sh_point_share": frame["pred_sh_point_share"].to_numpy(),
    })
    for category in CATEGORIES:
        out[f"lambda_{category}"] = frame[f"pred_{category}"].to_numpy()
    return out


def simulated_structure(table, dispersion, weights, latent_variance, matrices, sims,
                        batch_sims, seed):
    """Draw from a candidate structure and measure what correlation actually came out."""
    within, teammate, opponent = matrices
    fitted = copula_module.GameCopula(within, teammate, opponent, CATEGORIES)
    simulator = sampler.Simulator(dispersion, fitted, weights, latent_variance,
                                  seed=seed)
    team_index = pd.factorize(table["game_id"].astype(str) + ":"
                              + table["team_id"].astype(str))[0]
    game_index = pd.factorize(table["game_id"])[0]

    accumulator = StructureAccumulator(len(CATEGORIES))
    means = np.zeros(len(CATEGORIES))
    drawn = 0
    while drawn < sims:
        batch = min(batch_sims, sims - drawn)
        draws = simulator.draw(table, batch)
        for index in range(batch):
            residuals = np.empty((len(table), len(CATEGORIES)))
            for position, category in enumerate(CATEGORIES):
                mu = np.maximum(table[f"lambda_{category}"].to_numpy("float64"), 1e-6)
                theta = dispersion.get(category) or 0.0
                residuals[:, position] = ((draws[category][:, index] - mu)
                                          / np.sqrt(mu + theta * mu ** 2))
            accumulator.add(residuals, team_index, game_index)
        means += np.array([draws[c].sum() for c in CATEGORIES], dtype="float64")
        drawn += batch
    return accumulator.matrices(), means / (len(table) * sims)


def correct(normal, target, realized, damping=0.7, floor=0.15):
    """One damped Newton step on a nearly linear attenuation map: realized ~ a * normal."""
    attenuation = np.where(np.abs(normal) > 1e-4, realized / np.where(normal == 0, 1, normal),
                           np.nan)
    attenuation = np.where(np.isfinite(attenuation), attenuation, 0.5)
    attenuation = np.clip(attenuation, floor, 2.0)
    stepped = np.where(np.abs(normal) > 1e-4, target / attenuation, target * 2.0)
    stepped = normal + damping * (stepped - normal)
    return np.clip(stepped, -0.95, 0.95)


def make_feasible(within, teammate, opponent, team_size=REFERENCE_TEAM_SIZE):
    """Project the cross-player pair onto what two exchangeable sides can actually carry.

    The constraint is not on the teammate matrix alone. What has to be PSD is the joint
    covariance of the two teams' level vectors, `[[Psi_A, O], [O, Psi_B]]` with
    `Psi = T + (W - T)/n` -- teammate structure and opponent structure together. Fitting
    against the teammate bound alone left that block at a minimum eigenvalue of -0.15, and
    clipping it at draw time inflated penalty minutes by 11%: a distorted marginal, which is
    a bug rather than an approximation.

    So the step projects the block itself, then reads `T` and `O` back out of it. Asking for
    more is not a modelling choice the copula can honour -- the implied joint distribution
    does not exist -- and it is better to fit inside the wall than to report a matrix that
    gets silently clipped later.
    """
    size = len(within)
    probe = copula_module.GameCopula(within, teammate, opponent)
    joint = probe.joint_block((team_size, team_size))
    values, vectors = np.linalg.eigh((joint + joint.T) / 2.0)
    floor = (1.0 - copula_module.FEASIBILITY_MARGIN) * max(float(np.abs(values).max()), 1e-9)
    if values.min() >= floor:
        return teammate, opponent, False
    projected = vectors @ np.diag(np.clip(values, floor, None)) @ vectors.T
    psi = (projected[:size, :size] + projected[size:, size:]) / 2.0
    new_opponent = (projected[:size, size:] + projected[size:, :size].T) / 2.0
    # Psi = T + (W - T)/n, so T = (Psi - W/n) / (1 - 1/n)
    new_teammate = (psi - within / team_size) / (1.0 - 1.0 / team_size)
    return new_teammate, new_opponent, True


def fit(holdout, dispersion, sims=100, batch_sims=20, iterations=6, seed=17, source=""):
    """Measure the structure on these rows, then correct it for attenuation by simulation.

    Takes the rows rather than loading them, so the same routine fits the shipped structure
    and fits one half of a season for `validate.py --split-half`.
    """
    log.info("measuring on %d played player-games", len(holdout))

    residuals = standardized(holdout, dispersion)
    team_index = pd.factorize(holdout["game_id"].astype(str) + ":"
                              + holdout["team_id"].astype(str))[0]
    game_index = pd.factorize(holdout["game_id"])[0]
    target_within, target_teammate, target_opponent = measure(residuals, team_index, game_index)

    frame = pd.DataFrame(target_teammate, index=CATEGORIES, columns=CATEGORIES)
    log.info("measured teammate correlation:\n%s", frame.round(4).to_string())

    weights, latent_variance, _ = fit_incident_weights(holdout)
    table = lambda_frame(holdout)

    # Start the copula at the measured targets, then correct for attenuation. The two
    # mechanism pairs start at zero: thinning already supplies them.
    start_within = np.eye(len(CATEGORIES))
    for i, a in enumerate(CATEGORIES):
        for j, b in enumerate(CATEGORIES):
            if i != j and (a, b) not in MECHANISM_PAIRS and (b, a) not in MECHANISM_PAIRS:
                start_within[i, j] = target_within[i, j]
    matrices = [start_within, target_teammate.copy(), target_opponent.copy()]
    targets = [target_within, target_teammate, target_opponent]

    history = []
    for iteration in range(iterations):
        realized, means = simulated_structure(table, dispersion, weights, latent_variance,
                                              matrices, sims, batch_sims, seed + iteration)
        errors = [float(np.abs(r - t).max()) for r, t in zip(realized, targets)]
        log.info("iteration %d: max |realized - target|  within %.4f teammate %.4f "
                 "opponent %.4f", iteration, *errors)
        history.append({"iteration": iteration,
                        "max_abs_error": {"within": round(errors[0], 5),
                                          "teammate": round(errors[1], 5),
                                          "opponent": round(errors[2], 5)},
                        "teammate_realized": copula_module._round(realized[1], 5)})
        if iteration == iterations - 1:
            final_realized = realized
            final_means = means
            break
        new = []
        for index, (matrix, target, got) in enumerate(zip(matrices, targets, realized)):
            stepped = correct(matrix, target, got)
            if index == 0:
                np.fill_diagonal(stepped, 1.0)
                for a, b in MECHANISM_PAIRS:
                    i, j = CATEGORIES.index(a), CATEGORIES.index(b)
                    stepped[i, j] = stepped[j, i] = 0.0
            new.append((stepped + stepped.T) / 2.0)
        new[1], new[2], projected = make_feasible(*new)
        if projected:
            log.info("cross-player pair projected onto the feasible set for two sides of %d",
                     REFERENCE_TEAM_SIZE)
        matrices = new

    fitted = copula_module.GameCopula(*matrices, CATEGORIES)
    payload = {
        "source": source,
        "rows": len(holdout),
        "sims_per_iteration": sims,
        "categories": CATEGORIES,
        "measured": {
            "within": copula_module._round(target_within, 5),
            "teammate": copula_module._round(target_teammate, 5),
            "opponent": copula_module._round(target_opponent, 5),
        },
        "normal_scale": {
            "within": copula_module._round(matrices[0], 5),
            "teammate": copula_module._round(matrices[1], 5),
            "opponent": copula_module._round(matrices[2], 5),
        },
        "realized": {
            "within": copula_module._round(final_realized[0], 5),
            "teammate": copula_module._round(final_realized[1], 5),
            "opponent": copula_module._round(final_realized[2], 5),
        },
        "exchangeable_block": {
            "teammate_eigenvalues": [round(float(v), 5)
                                     for v in np.linalg.eigvalsh(matrices[1])],
            "note": "the teammate block is built exchangeably rather than as a sum of "
                    "factors, so a negative eigenvalue is representable; the bound is "
                    "-1/(n-1) for a side of n skaters",
        },
        "simulated_means": {c: round(float(m), 4) for c, m in zip(CATEGORIES, final_means)},
        "penalty_incidents": {
            "minutes": marginals.INCIDENT_MINUTES.tolist(),
            "weights": [round(float(w), 5) for w in weights],
            "mean_minutes_per_incident": round(float(weights @ marginals.INCIDENT_MINUTES), 4),
            "latent_variance": round(latent_variance, 5),
        },
        "iterations": history,
    }
    return payload


def structure(payload):
    """The copula a fitted payload describes."""
    fitted = payload["normal_scale"]
    return copula_module.GameCopula(fitted["within"], fitted["teammate"], fitted["opponent"],
                                    payload.get("categories") or CATEGORIES)


def run(args):
    dispersion = load_dispersion()
    holdout = load_holdout(args.variant)
    payload = fit(holdout, dispersion, args.sims, args.batch_sims, args.iterations,
                  args.seed, paths.holdout_predictions(args.variant).name)
    destination = paths.ensure(paths.REPORTS_DIR) / (args.out or "correlations.json")
    destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("wrote %s", destination)
    return payload


def main():
    run(parse_args())


if __name__ == "__main__":
    main()

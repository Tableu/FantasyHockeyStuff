#!/usr/bin/env python
"""Fits the spread the point projections do not carry.

The projection models predict a mean. Section 5 turns each mean into a distribution by
drawing one latent "game quality" multiplier per simulation from a Gamma centred on 1.0 and
scaling every category's lambda by it before sampling Poisson -- which is exactly a negative
binomial draw, since a Gamma-mixed Poisson *is* an NB. So the parameter Section 5 needs is
the NB dispersion, and this fits it from the holdout residuals instead of letting the
simulator invent one.

Parameterization: Var = mu + theta * mu^2. theta = 0 collapses to Poisson; larger theta is a
fatter tail. It is fit per category by maximum likelihood over the holdout rows.

A second number comes out of the same data: the *shared* game-quality variance, the part of
the overdispersion common to all of one player's categories in one game. The per-category
theta above is the total; the shared component is what makes a big-shots night also a
big-goals night, and it is estimated from the correlation of the categories' standardized
residuals. Section 5 uses the shared part for the Gamma multiplier and the remainder as
per-category noise, so it does not double-count.

Usage:
    python calibrate.py --variant B
"""

import argparse
import json
import logging

import numpy as np
import pandas as pd
from scipy import optimize, special

import paths

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("calibrate")

CATEGORIES = ["shots", "hits", "blocks", "assists", "goals", "pim"]


def parse_args():
    parser = argparse.ArgumentParser(description="Fit the per-category NB dispersion")
    parser.add_argument("--variant", choices=("A", "B"), default="B")
    parser.add_argument("--season", default="2025-26",
                        help="The holdout season whose predictions to fit on")
    return parser.parse_args()


def negative_log_likelihood(log_theta, actual, mu):
    """NB log-likelihood under Var = mu + theta*mu^2, in the size/probability form."""
    theta = np.exp(log_theta)
    size = 1.0 / theta
    log_likelihood = (special.gammaln(actual + size) - special.gammaln(size)
                      - special.gammaln(actual + 1.0)
                      + size * np.log(size / (size + mu))
                      + actual * np.log(mu / (size + mu)))
    return -float(np.sum(log_likelihood))


def fit_dispersion(actual, mu):
    """Maximum-likelihood theta, bounded well away from 0 and from absurd fat tails."""
    actual = np.asarray(actual, dtype="float64")
    mu = np.maximum(np.asarray(mu, dtype="float64"), 1e-6)
    good = np.isfinite(actual) & np.isfinite(mu)
    actual, mu = actual[good], mu[good]
    if len(actual) < 100:
        return None
    result = optimize.minimize_scalar(
        negative_log_likelihood, bounds=(np.log(1e-4), np.log(20.0)),
        args=(actual, mu), method="bounded")
    return float(np.exp(result.x))


def standardized_residuals(actual, mu, theta):
    """(actual - mu) / sd under the fitted NB -- comparable across categories."""
    variance = mu + theta * mu ** 2
    return (actual - mu) / np.sqrt(np.maximum(variance, 1e-9))


def shared_game_quality(frame, thetas):
    """The Gamma multiplier's variance, from how much the categories move together.

    If a shared multiplier g (mean 1, variance v) scales every lambda, then two categories'
    standardized residuals in the same player-game correlate roughly in proportion to v. The
    mean off-diagonal correlation is therefore a direct, if rough, estimate -- and a rough
    one honestly labelled beats a guessed constant in the simulator.
    """
    residuals = pd.DataFrame({
        category: standardized_residuals(
            frame[f"target_{category}"].to_numpy(dtype="float64"),
            np.maximum(frame[f"pred_{category}"].to_numpy(dtype="float64"), 1e-6),
            thetas[category])
        for category in CATEGORIES if thetas.get(category) is not None
    })
    correlation = residuals.corr()
    off_diagonal = correlation.to_numpy()[~np.eye(len(correlation), dtype=bool)]
    mean_correlation = float(np.nanmean(off_diagonal))
    return {
        "mean_residual_correlation": round(mean_correlation, 4),
        "gamma_variance": round(max(mean_correlation, 0.0), 4),
        "pairwise": {f"{a}|{b}": round(float(correlation.loc[a, b]), 4)
                     for i, a in enumerate(correlation.index)
                     for b in correlation.columns[i + 1:]},
    }


def dispersion_path(season):
    """Keyed by the holdout season whose residuals it was fitted on."""
    return paths.REPORTS_DIR / f"dispersion_{season}.json"


def load_dispersion(season="2025-26") -> dict:
    """What evaluate.py reads; empty before calibrate.py has run."""
    path = dispersion_path(season)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {k: v["theta"] for k, v in payload.get("categories", {}).items()
            if v.get("theta") is not None}


def run(variant, season="2025-26"):
    path = paths.predictions(variant, season)
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing -- run train.py --all --variant {variant} "
                                f"--holdout-season {season}")
    frame = pd.read_parquet(path)
    frame = frame[frame["target_played"].astype(bool)]

    categories, thetas = {}, {}
    for category in CATEGORIES:
        if f"pred_{category}" not in frame:
            continue
        actual = frame[f"target_{category}"].to_numpy(dtype="float64")
        mu = np.maximum(frame[f"pred_{category}"].to_numpy(dtype="float64"), 1e-6)
        theta = fit_dispersion(actual, mu)
        thetas[category] = theta
        # Var(Y) = E[Var(Y|mu)] + Var(mu): the marginal variance of the actuals includes
        # the spread of the projections themselves, so comparing it straight against the
        # conditional NB variance would make every model look under-dispersed. The row
        # reconciles when nb_implied + lambda_variance ~= observed.
        observed = float(np.var(actual))
        implied = float(np.mean(mu + (theta or 0.0) * mu ** 2))
        lambda_variance = float(np.var(mu))
        categories[category] = {
            "theta": None if theta is None else round(theta, 4),
            "mean_lambda": round(float(mu.mean()), 4),
            "observed_variance": round(observed, 4),
            "nb_implied_variance": round(implied, 4),
            "lambda_variance": round(lambda_variance, 4),
            "reconciliation_gap": round(observed - implied - lambda_variance, 4),
            "poisson_implied_variance": round(float(mu.mean()), 4),
        }
        log.info("%-8s theta %.4f  observed %.3f  =  NB %.3f + Var(lambda) %.3f  (gap %+.3f)",
                 category, theta or 0.0, observed, implied, lambda_variance,
                 observed - implied - lambda_variance)

    payload = {
        "variant": variant,
        "rows": len(frame),
        "parameterization": "Var = mu + theta * mu^2",
        "categories": categories,
        "game_quality": shared_game_quality(frame, thetas),
    }
    paths.ensure(paths.REPORTS_DIR)
    out = dispersion_path(season)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("shared game-quality variance %.4f -> %s",
             payload["game_quality"]["gamma_variance"], out.name)
    return payload


def main():
    args = parse_args()
    run(args.variant, args.season)


if __name__ == "__main__":
    main()

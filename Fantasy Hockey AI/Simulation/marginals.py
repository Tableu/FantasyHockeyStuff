"""The per-category outcome distributions, and how a uniform draw becomes a stat line.

Every sampler here is an **inverse-CDF** function: it takes a uniform in (0,1) and returns a
count. That is what lets `copula.py` impose correlation between players without touching any
marginal -- the correlation lives in how the uniforms are drawn, the distribution lives here.

Four structural choices, each measured on the 2025-26 holdout rather than assumed:

1. **Counts are negative binomial**, `Var = mu + theta*mu^2`, with theta fit by
   `Projections/calibrate.py`. A Gamma-mixed Poisson *is* an NB, so this is the build plan's
   "shared game-quality multiplier" written in its closed form.

2. **Goals are thinned out of shots, not drawn on their own.** A goal is a shot on goal, and
   the holdout agrees: 9 rows in 46,654 have more goals than shots (7 of them a goal with no
   shot recorded -- the same scorekeeping quirk that gives goalies saves+GA one above shots
   against). Drawing `goals ~ Binomial(shots_drawn, lambda_goals/lambda_shots)` makes "2
   goals on 0 shots" impossible by construction, keeps `E[goals] = lambda_goals` exactly, and
   reproduces the measured within-player shots/goals residual correlation of 0.299 from the
   mechanism instead of from a fitted parameter. It also implies goals inherit shots'
   dispersion, `Var = mu + 0.063*mu^2` against a measured theta of ~0 -- a 1% difference in
   variance at a mean of 0.165, which is not worth a parameter.

3. **PIM is a compound Poisson, not an overdispersed count.** Its fitted theta of 7.5 is the
   largest in the stack by two orders of magnitude, and the reason is visible in the support:
   on the holdout, 98.7% of penalty minutes are even. An NB would happily draw 1 and 3, which
   essentially never occur. Minutes are a *count of incidents* -- minor, major, misconduct --
   times a lumpy size. Poisson thinning makes that three Poissons over one rate, so the mean
   stays `lambda_pim` by construction while the support comes out right.
   The incidents also arrive over a **latent intensity** rather than a fixed one, because
   penalties escalate: a player who takes one is likelier to take another the same night.
   Without it, four-minute nights come out at 0.014 against an observed 0.033, and tying the
   three incident types to one draw instead over-disperses the total by 41%. One fitted
   variance on a mean-one multiplier covers both; at zero it collapses to plain thinning.

4. **Power-play and short-handed points are drawn per point**, not as their own counts. The
   projection carries them as shares, so each of a player's sampled points is classified
   once: PP, SH or even strength. `ppp <= points` then holds in every draw, which an
   independent PPP count model cannot promise.

Nothing in this module knows what a goal is worth. Scoring is applied downstream by
`scoring.py`.
"""

import numpy as np
from scipy.special import ndtri

# Counts are clipped here. The holdout maxima are 15 shots, 15 hits, 11 blocks, 5 assists;
# 40 leaves room for a tail far beyond anything observed without letting a pathological
# lambda run the recurrence forever.
MAX_COUNT = 40

# Penalty incident sizes, in minutes: minor, major, misconduct. The weights on them are fit
# by `correlations.py` against the holdout distribution of per-game penalty minutes.
INCIDENT_MINUTES = np.array([2.0, 5.0, 10.0])


def _inverse_cdf(uniforms, pmf, ratio, kmax=MAX_COUNT):
    """Walk a count distribution's pmf recurrence until the CDF passes each uniform.

    `pmf` is P(Y=0) and `ratio(k)` is P(Y=k+1)/P(Y=k), both arrays shaped like `uniforms`.
    Counting how many partial CDFs a uniform exceeds *is* the quantile, and doing it with a
    recurrence keeps the whole thing in numpy: scipy's `ppf` over 5M draws takes ten
    seconds, this takes a fraction of one.
    """
    cdf = pmf.copy()
    counts = np.zeros(uniforms.shape, dtype=np.int16)
    for k in range(kmax):
        exceeded = uniforms > cdf
        if not exceeded.any():
            break
        counts += exceeded
        pmf = pmf * ratio(k)
        cdf = cdf + pmf
    return counts


def sample_negative_binomial(uniforms, mu, theta):
    """NB under `Var = mu + theta*mu^2`; collapses to Poisson as theta -> 0."""
    mu = np.maximum(np.asarray(mu, dtype="float64"), 1e-9)
    if theta is None or theta <= 1e-4:
        return sample_poisson(uniforms, mu)
    size = 1.0 / theta
    prob = mu / (mu + size)
    pmf0 = np.exp(size * np.log(size / (size + mu)))
    return _inverse_cdf(uniforms, pmf0, lambda k: (k + size) / (k + 1.0) * prob)


def sample_poisson(uniforms, mu):
    mu = np.maximum(np.asarray(mu, dtype="float64"), 1e-9)
    return _inverse_cdf(uniforms, np.exp(-mu), lambda k: mu / (k + 1.0))


def sample_binomial(uniforms, trials, prob):
    """Binomial with a per-draw number of trials -- how goals come out of sampled shots."""
    trials = np.asarray(trials, dtype="float64")
    prob = np.clip(np.asarray(prob, dtype="float64"), 0.0, 1.0 - 1e-9)
    kmax = int(trials.max()) if trials.size else 1
    pmf0 = np.exp(trials * np.log1p(-prob))
    odds = prob / (1.0 - prob)

    def ratio(k):
        return np.maximum(trials - k, 0.0) / (k + 1.0) * odds

    return _inverse_cdf(uniforms, pmf0, ratio, kmax=max(kmax, 1))


def latent_intensity(uniforms, variance):
    """A mean-one multiplier on a player's penalty rate for the night, from his uniform.

    Lognormal rather than Gamma: the inverse CDF is `exp(sigma*ndtri(u))`, which costs one
    cheap transform, where a Gamma quantile costs an incomplete-gamma inversion per draw.
    Mean one and variance `variance` either way, and the latent is a nuisance shape whose
    job is to carry escalation, not to be interpreted.
    """
    if variance is None or variance <= 1e-9:
        return np.ones(np.shape(uniforms))
    sigma = np.sqrt(np.log1p(variance))
    return np.exp(sigma * ndtri(np.clip(uniforms, 1e-12, 1.0 - 1e-12)) - 0.5 * sigma ** 2)


def sample_penalty_minutes(uniforms, lambda_pim, incident_weights, latent_variance=0.0,
                           rng=None):
    """Minutes as `2*N_minor + 5*N_major + 10*N_misconduct` over a shared latent intensity.

    Poisson thinning: if incidents arrive Poisson at rate `lambda_pim / mean_size` and each
    is independently a minor / major / misconduct, the three counts are independent Poissons
    at the thinned rates -- so the mean is `lambda_pim` exactly, whatever the weights and
    whatever the latent, since the multiplier has mean one.

    The copula's uniform draws the **minor count**; the latent gets its own. It has to be
    that way round in both respects. It must drive something count-shaped, because routing
    it through the latent alone attenuates the correlation by 95% and penalty minutes carry
    the largest cross-player number measured -- opponents at +0.056, a fight handing both
    benches minutes at once. And the two must be *independent*: one uniform that both raises
    the intensity and picks a high quantile under it is not a draw from the mixture at all.
    That inflated the mean by 54%, which is the failure mode this module exists to prevent,
    and it is why `simulate.py`'s output is checked against `p_plays * lambda` rather than
    only against a holdout.
    """
    weights = np.asarray(incident_weights, dtype="float64")
    mean_size = float(weights @ INCIDENT_MINUTES)
    rate = np.maximum(np.asarray(lambda_pim, dtype="float64"), 0.0) / mean_size
    rng = rng or np.random.default_rng()
    intensity = latent_intensity(rng.random(np.shape(uniforms)), latent_variance) * rate
    minutes = np.zeros(np.shape(uniforms), dtype=np.int16)
    for index, (weight, size) in enumerate(zip(weights, INCIDENT_MINUTES)):
        draw = uniforms if index == 0 else rng.random(np.shape(uniforms))
        incidents = sample_poisson(draw, intensity * weight)
        minutes = (minutes + incidents * np.int16(size)).astype(np.int16)
    return minutes


def split_point_strengths(points, pp_share, sh_share, rng):
    """Classify each sampled point as power-play, short-handed or even strength.

    Drawn per point, so `ppp + shp <= points` always -- which an independent PPP count model
    cannot promise. The two shares are already clamped to sum to at most one by `predict.py`.
    """
    total = points.astype("int64")
    pp_share = np.broadcast_to(np.asarray(pp_share, dtype="float64"), total.shape)
    sh_share = np.broadcast_to(np.asarray(sh_share, dtype="float64"), total.shape)
    power_play = rng.binomial(total, pp_share)
    remaining = total - power_play
    # Conditional on a point not being a power-play point, the short-handed chance rescales.
    conditional = np.clip(sh_share / np.maximum(1.0 - pp_share, 1e-9), 0.0, 1.0)
    short_handed = rng.binomial(remaining, conditional)
    return power_play.astype(np.int16), short_handed.astype(np.int16)


def compound_poisson_pmf(lambda_pim, weights, max_minutes=60, latent_variance=0.0,
                         nodes=24):
    """P(minutes = m) for m in 0..max_minutes at one lambda -- used to fit the parameters.

    With a latent intensity the pmf is a mixture, integrated over the lognormal by
    Gauss-Hermite quadrature; `latent_variance = 0` returns the plain compound Poisson.
    """
    if latent_variance and latent_variance > 1e-9:
        abscissa, quad_weights = np.polynomial.hermite_e.hermegauss(nodes)
        quad_weights = quad_weights / quad_weights.sum()
        sigma = np.sqrt(np.log1p(latent_variance))
        multipliers = np.exp(sigma * abscissa - 0.5 * sigma ** 2)
        return sum(w * _fixed_pmf(lambda_pim * m, weights, max_minutes)
                   for w, m in zip(quad_weights, multipliers))
    return _fixed_pmf(lambda_pim, weights, max_minutes)


def _fixed_pmf(lambda_pim, weights, max_minutes=60):
    weights = np.asarray(weights, dtype="float64")
    mean_size = float(weights @ INCIDENT_MINUTES)
    rate = lambda_pim / mean_size
    grid = np.zeros(max_minutes + 1)
    grid[0] = 1.0
    for weight, size in zip(weights, INCIDENT_MINUTES):
        component = np.zeros(max_minutes + 1)
        thinned = rate * weight
        size = int(size)
        count, term = 0, float(np.exp(-thinned))
        while count * size <= max_minutes:
            component[count * size] = term
            count += 1
            term = term * thinned / count
        grid = np.convolve(grid, component)[:max_minutes + 1]
    return grid

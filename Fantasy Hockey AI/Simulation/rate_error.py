#!/usr/bin/env python
"""How far a skater's per-game scoring lands from a preseason projection: a distribution per stat.
Plan: ~/.claude/plans/boom-bust-odds.md, step 2. Results: docs/rate-error.md.

    python rate_error.py --evaluate              # rolling-origin check on the yardstick projection
    python rate_error.py --consensus             # the transfer to the consensus, 2024-25 / 2025-26
    python rate_error.py --season 2026-27        # fit on every season before it -> reports/

The other half of a season's spread (games played is `games_played.py`). For each scored stat c
a season's count is

    count ~ Poisson(projected rate x games played x M),   log M ~ Normal(m(x), s(x)^2)

so M is the season's *rate* shock -- a breakout, a collapse, a new role -- and the Poisson is the
ordinary luck of counting it over that many games. Keeping them apart matters: the luck shrinks
with games played, the shock does not, and a zero is a count like any other. m and log s are
linear in what is known before the season (age, position, how many games stand behind the
projection, how far the projection sits from the position's mean, a new team) plus the season's
own games played, which the simulator draws first. Fitted by maximum likelihood with
Gauss-Hermite quadrature over the shock and an analytic gradient; scipy only.

**The yardstick projection.** Only two seasons have the consensus beside the actuals, too few for
tails. So every season from 2003-04 gets a Marcel-style projection -- the last three seasons'
per-game rates weighted 5/4/3 by games, pulled toward the position's mean by `k` games of it --
and the shock is fitted against that. `--consensus` then measures, on 2024-25 and 2025-26, how the
consensus's errors compare on the same players, per stat (a shift and a scale factor).

PIM is counted in penalty units (minutes / 2): a minor is one event, not two.

Import-safe (no module-level `paths`), like `games_played.py`.
"""

import argparse
import json
import logging

import numpy as np
import pandas as pd
from scipy import optimize, special

import games_played

log = logging.getLogger("rate_error")

STATS = ("goals", "assists", "ppp", "shp", "shots", "hits", "blocks", "pim")
TRACKED_FROM = {"hits": "2005-06", "blocks": "2005-06"}     # NaN before (not tracked)
MARCEL_WEIGHTS = (5.0, 4.0, 3.0)
K_GRID = (0, 10, 20, 40, 60, 80, 120, 160, 240, 320, 480)
MIN_GP = 10                    # a season with fewer games says little about a rate
REGULAR_GP = 20                # who sets the position's mean rate
L2 = 1e-3
NODES, WEIGHTS = np.polynomial.hermite_e.hermegauss(32)
LOG_WEIGHTS = np.log(WEIGHTS / WEIGHTS.sum())


# --- the yardstick projection -------------------------------------------------------------------

def _counts(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["pim"] = np.round(out["pim"] / 2.0)                   # penalty units
    return out


def marcel_inputs(totals: pd.DataFrame, team_games: pd.DataFrame) -> pd.DataFrame:
    """Per (target season, skater in the games-played pool): weighted counts and games over the
    three seasons before, the position's mean rate last season, and the target season's actual
    counts where it has been played. Rates are finished in `project` once `k` is known."""
    games = games_played.season_games(team_games)
    seasons = sorted(games.index)
    sk = _counts(totals[~totals["is_goalie"].astype(bool)])
    by_season = {s: g.set_index("player_id") for s, g in sk.groupby("season")}
    rows = []
    for season in seasons:
        i = seasons.index(season)
        prev = seasons[max(0, i - 3):i][::-1]
        if not prev or prev[0] not in by_season:
            continue
        last = by_season[prev[0]]
        share1 = last["gp"] / games[prev[0]]
        pool = share1[share1 >= games_played.MIN_PREV_SHARE].index
        f = pd.DataFrame(index=pool)
        f["season"] = season
        f["is_d"] = (last.loc[pool, "position"] == "D").astype(float)
        f["last_teams"] = last.loc[pool, "teams"]
        regulars = last[last["gp"] >= REGULAR_GP]
        for stat in STATS:
            num = np.zeros(len(pool))
            den = np.zeros(len(pool))
            for w, s in zip(MARCEL_WEIGHTS, prev):
                rows_s = by_season.get(s)
                if rows_s is None or s < TRACKED_FROM.get(stat, ""):
                    continue
                c = rows_s[stat].reindex(pool)
                g = rows_s["gp"].reindex(pool)
                ok = c.notna() & g.notna()
                num += w * np.where(ok, c.fillna(0), 0.0)
                den += w * np.where(ok, g.fillna(0), 0.0)
            f[f"wnum_{stat}"] = num
            f[f"wgp_{stat}"] = den
            if prev[0] < TRACKED_FROM.get(stat, ""):
                f[f"prior_{stat}"] = np.nan
            else:
                pos_rate = regulars.groupby(regulars["position"] == "D").apply(
                    lambda g: g[stat].sum() / g["gp"].sum(), include_groups=False)
                f[f"prior_{stat}"] = np.where(f["is_d"] == 1, pos_rate.get(True, np.nan),
                                              pos_rate.get(False, np.nan))
        now = by_season.get(season)
        if now is not None:
            f["gp"] = now["gp"].reindex(pool)
            f["teams"] = now["teams"].reindex(pool)
            for stat in STATS:
                f[stat] = now[stat].reindex(pool)
        rows.append(f.reset_index(names="player_id"))
    return pd.concat(rows, ignore_index=True)


def project(inputs: pd.DataFrame, k: dict) -> pd.DataFrame:
    """Marcel rates per game: (weighted counts + k x the position's mean) / (weighted games + k)."""
    out = inputs.copy()
    for stat in STATS:
        kk = k[stat]
        out[f"proj_{stat}"] = ((out[f"wnum_{stat}"] + kk * out[f"prior_{stat}"])
                               / (out[f"wgp_{stat}"] + kk))
        out.loc[out[f"wgp_{stat}"] + kk <= 0, f"proj_{stat}"] = out[f"prior_{stat}"]
    return out


def fit_k(inputs: pd.DataFrame) -> dict:
    """Per stat, the `k` that minimizes games-weighted squared error of the per-game rate."""
    done = inputs[inputs["gp"] >= MIN_GP]
    best = {}
    for stat in STATS:
        d = done[done[stat].notna() & done[f"prior_{stat}"].notna()]
        rate = d[stat] / d["gp"]
        errs = []
        for kk in K_GRID:
            proj = (d[f"wnum_{stat}"] + kk * d[f"prior_{stat}"]) / (d[f"wgp_{stat}"] + kk)
            proj = proj.where(d[f"wgp_{stat}"] + kk > 0, d[f"prior_{stat}"])
            errs.append(float((d["gp"] * (rate - proj) ** 2).sum()))
        best[stat] = K_GRID[int(np.argmin(errs))]
    return best


# --- covariates ---------------------------------------------------------------------------------

def _new_team(last: pd.Series, now: pd.Series) -> np.ndarray:
    """True when none of this season's teams was one of last season's -- a move before or early
    in the season. (A trade-deadline move keeps a team in common and does not count.)"""
    out = []
    for a, b in zip(last, now):
        if not isinstance(a, str) or not isinstance(b, str):
            out.append(0.0)
        else:
            out.append(float(not (set(a.split(",")) & set(b.split(",")))))
    return np.array(out)


def covariate_frame(projected: pd.DataFrame, players: pd.DataFrame) -> pd.DataFrame:
    out = projected.copy()
    birth = players.set_index("player_id")["birth_date"]
    ages = []
    for season, g in out.groupby("season", sort=False):
        ages.append(games_played.age_on(birth.reindex(g["player_id"]), season)
                    .set_axis(g.index))
    out["age"] = pd.concat(ages).reindex(out.index)
    out["age"] = out["age"].fillna(out["age"].median())
    out["new_team"] = _new_team(out["last_teams"], out.get("teams", out["last_teams"]))
    return out


def design(frame: pd.DataFrame, stat: str) -> tuple:
    a = frame["age"].to_numpy()
    evidence = np.log1p(frame[f"wgp_{stat}"].to_numpy() / 12.0)     # 12 = 5+4+3: one season
    level = np.log(np.maximum(frame[f"proj_{stat}"].to_numpy(), 1e-4)
                   / np.maximum(frame[f"prior_{stat}"].to_numpy(), 1e-4))
    cols = {
        "intercept": np.ones(len(frame)),
        "age": (a - 27.0) / 4.0,
        "age_over_31": np.maximum(a - 31.0, 0) / 4.0,
        "age_under_24": np.maximum(24.0 - a, 0) / 3.0,
        "is_d": frame["is_d"].to_numpy(),
        "evidence": (evidence - 5.0) / 1.0,
        "level": np.clip(level, -4, 4),
        "new_team": frame["new_team"].to_numpy(),
        "log_gp": np.log(np.maximum(frame["gp"].to_numpy(float), 1.0) / 60.0),
    }
    names = list(cols)
    return np.column_stack([cols[c] for c in names]), names


# --- the model ----------------------------------------------------------------------------------

def _loglik_and_grad(theta, X, y, expo, simple: bool):
    """Mean log-likelihood of Poisson(expo x exp(m + s z)), z ~ N(0,1), and its gradient."""
    p = X.shape[1]
    beta, gamma = theta[:p], theta[p:]
    if simple:                                  # the comparison: one m and one s per stat
        beta = np.r_[beta[0], np.zeros(p - 1)]
        gamma = np.r_[gamma[0], np.zeros(p - 1)]
    m = X @ beta
    s = np.exp(np.clip(X @ gamma, -6, 3))
    eta = m[:, None] + s[:, None] * NODES[None, :]
    lam = expo[:, None] * np.exp(np.clip(eta, -30, 30))
    logp = y[:, None] * np.log(np.maximum(lam, 1e-300)) - lam - special.gammaln(y + 1)[:, None]
    a = LOG_WEIGHTS[None, :] + logp
    ll = special.logsumexp(a, axis=1)
    post = np.exp(a - ll[:, None])
    resid = y[:, None] - lam
    d_m = (post * resid).sum(axis=1)
    d_s = (post * resid * NODES[None, :]).sum(axis=1) * s
    g_beta = X.T @ d_m / len(y)
    g_gamma = X.T @ d_s / len(y)
    if simple:
        g_beta[1:] = 0.0
        g_gamma[1:] = 0.0
    return ll.mean(), np.r_[g_beta, g_gamma]


def _objective(theta, X, y, expo, simple):
    ll, g = _loglik_and_grad(theta, X, y, expo, simple)
    p = X.shape[1]
    pen = L2 * (np.sum(theta[1:p] ** 2) + np.sum(theta[p + 1:] ** 2))
    gpen = np.zeros_like(theta)
    gpen[1:p] = 2 * L2 * theta[1:p]
    gpen[p + 1:] = 2 * L2 * theta[p + 1:]
    return -ll + pen, -g + gpen


def _usable(frame: pd.DataFrame, stat: str) -> pd.DataFrame:
    return frame[(frame["gp"] >= MIN_GP) & frame[stat].notna()
                 & frame[f"proj_{stat}"].notna() & (frame[f"proj_{stat}"] > 0)]


def fit_stat(frame: pd.DataFrame, stat: str, simple: bool = False) -> dict:
    d = _usable(frame, stat)
    X, names = design(d, stat)
    y = d[stat].to_numpy(float)
    expo = d[f"proj_{stat}"].to_numpy() * d["gp"].to_numpy(float)
    p = X.shape[1]
    theta0 = np.zeros(2 * p)
    theta0[p] = np.log(0.25)
    res = optimize.minimize(_objective, theta0, args=(X, y, expo, simple), jac=True,
                            method="L-BFGS-B", options={"maxiter": 3000})
    if not res.success:
        log.warning("%s: fit did not converge: %s", stat, res.message)
    theta = res.x.copy()
    if simple:
        theta[1:p] = 0.0
        theta[p + 1:] = 0.0
    return {"names": names, "beta": theta[:p].tolist(), "gamma": theta[p:].tolist(),
            "rows": int(len(d)), "negloglik": float(res.fun)}


def shock(params: dict, frame: pd.DataFrame, stat: str) -> tuple:
    """(m, s) of log M per row."""
    X, names = design(frame, stat)
    if names != params["names"]:
        raise ValueError("fitted covariates differ from this design")
    return X @ np.array(params["beta"]), np.exp(np.clip(X @ np.array(params["gamma"]), -6, 3))


def fit(frame: pd.DataFrame, simple: bool = False) -> dict:
    return {stat: fit_stat(frame, stat, simple) for stat in STATS}


# --- the check ----------------------------------------------------------------------------------

def predictive_pit(params: dict, frame: pd.DataFrame, stat: str, rng) -> tuple:
    """Randomized PIT of each observed count under the Poisson-lognormal, and its log score."""
    d = _usable(frame, stat)
    m, s = shock(params, d, stat)
    y = d[stat].to_numpy(float)
    expo = d[f"proj_{stat}"].to_numpy() * d["gp"].to_numpy(float)
    lam = expo[:, None] * np.exp(np.clip(m[:, None] + s[:, None] * NODES[None, :], -30, 30))
    w = np.exp(LOG_WEIGHTS)[None, :]
    from scipy import stats as st
    below = (w * st.poisson.cdf(y[:, None] - 1, lam)).sum(axis=1)
    at = (w * st.poisson.pmf(y[:, None], lam)).sum(axis=1)
    pit = below + rng.uniform(size=len(y)) * at
    return d.index, pit, np.log(np.maximum(at, 1e-300))


def rolling_origin(frame: pd.DataFrame, first_scored: str, k_by_season: dict, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for season in sorted(s for s in frame["season"].unique() if s >= first_scored):
        train = frame[frame["season"] < season]
        test = frame[frame["season"] == season]
        for label, simple in (("model", False), ("constant", True)):
            params = fit(train, simple)
            for stat in STATS:
                if test[stat].notna().sum() == 0:
                    continue
                idx, pit, logscore = predictive_pit(params[stat], test, stat, rng)
                rows.append(pd.DataFrame({"season": season, "stat": stat, "fit": label,
                                          "row": idx, "pit": pit, "logscore": logscore}))
        log.info("%s scored", season)
    return pd.concat(rows, ignore_index=True)


def summarize(scored: pd.DataFrame) -> pd.DataFrame:
    g = scored.groupby(["stat", "fit"])
    t = pd.DataFrame({
        "rows": g.size(),
        "log score": g["logscore"].mean(),
        "PIT in 10-90": g["pit"].apply(lambda p: ((p >= 0.1) & (p <= 0.9)).mean()),
        "PIT < 10": g["pit"].apply(lambda p: (p < 0.1).mean()),
        "PIT > 90": g["pit"].apply(lambda p: (p > 0.9).mean()),
    })
    return t.unstack("fit")


# --- stats move together ------------------------------------------------------------------------

def posterior_z(params: dict, frame: pd.DataFrame, stat: str) -> pd.Series:
    """E[z | the season's count] per usable row: each player's standardized shock, shrunk by
    how much his count could tell."""
    d = _usable(frame, stat)
    m, s = shock(params, d, stat)
    y = d[stat].to_numpy(float)
    expo = d[f"proj_{stat}"].to_numpy() * d["gp"].to_numpy(float)
    lam = expo[:, None] * np.exp(np.clip(m[:, None] + s[:, None] * NODES[None, :], -30, 30))
    a = LOG_WEIGHTS[None, :] + y[:, None] * np.log(np.maximum(lam, 1e-300)) - lam
    post = np.exp(a - special.logsumexp(a, axis=1)[:, None])
    return pd.Series((post * NODES[None, :]).sum(axis=1), index=d.index)


def latent_correlation(params: dict, frame: pd.DataFrame, min_gp: int = 60) -> pd.DataFrame:
    """Correlation of the shocks across stats. The posterior means are shrunk, each by its own
    reliability r = Var(E[z|y]) (the shock itself has variance 1), and shrinkage attenuates a
    covariance by r_i x r_j -- so Cov(z_i, z_j) = Cov(zhat_i, zhat_j) / (r_i r_j). Rows with
    `min_gp` or more games, where the counts say the most. Counting noise shared across stats
    (a power-play goal is a goal and a PPP) leaks in; clipped to a valid correlation matrix."""
    rows = frame[frame["gp"] >= min_gp]
    z = pd.DataFrame({stat: posterior_z(params[stat], rows, stat) for stat in STATS}).dropna()
    cov = np.cov(z.to_numpy().T)
    r = np.diag(cov)
    corr = cov / np.outer(r, r)
    np.fill_diagonal(corr, 1.0)
    vals, vecs = np.linalg.eigh((corr + corr.T) / 2)
    fixed = vecs @ np.diag(np.maximum(vals, 1e-3)) @ vecs.T
    d = np.sqrt(np.diag(fixed))
    return pd.DataFrame(fixed / np.outer(d, d), index=list(STATS), columns=list(STATS))


def sample_multipliers(params: dict, frame: pd.DataFrame, corr: pd.DataFrame | None,
                       draws: int, rng) -> dict:
    """{stat: draws x rows} rate multipliers M = exp(m + s z), z correlated across stats by
    `corr` (independent when None). `frame` needs `gp`: the draws' games, or the actual ones."""
    z = rng.standard_normal((draws, len(frame), len(STATS)))
    if corr is not None:
        z = z @ np.linalg.cholesky(corr.loc[list(STATS), list(STATS)].to_numpy()).T
    out = {}
    for j, stat in enumerate(STATS):
        m, s = shock(params[stat], frame, stat)
        out[stat] = np.exp(m[None, :] + s[None, :] * z[:, :, j])
    return out


def fantasy_draws(params, frame, corr, weights: dict, draws: int, rng) -> np.ndarray:
    """Season fantasy points, draws x rows, at each row's own `gp`: Poisson counts at
    projected rate x games x the shock. PIM is drawn in penalty units and scored as minutes."""
    mult = sample_multipliers(params, frame, corr, draws, rng)
    gp = frame["gp"].to_numpy(float)
    total = np.zeros((draws, len(frame)))
    for stat in STATS:
        w = weights.get(stat, 0.0)
        if not w:
            continue
        lam = frame[f"proj_{stat}"].fillna(0).to_numpy()[None, :] * gp[None, :] * mult[stat]
        counts = rng.poisson(lam)
        total += w * counts * (2.0 if stat == "pim" else 1.0)
    return total


def actual_fantasy(frame: pd.DataFrame, weights: dict) -> np.ndarray:
    return sum(weights.get(stat, 0.0) * frame[stat].fillna(0).to_numpy(float)
               * (2.0 if stat == "pim" else 1.0) for stat in STATS)


def crps_from_draws(draws: np.ndarray, actual: np.ndarray) -> np.ndarray:
    """CRPS per row from an ensemble: E|X - y| - E|X - X'| / 2 (sorted-sample formula)."""
    x = np.sort(draws, axis=0)
    n = x.shape[0]
    term1 = np.abs(x - actual[None, :]).mean(axis=0)
    i = np.arange(1, n + 1)[:, None]
    term2 = ((2 * i - n - 1) * x).sum(axis=0) / (n * n)
    return term1 - term2


def fantasy_check(frame: pd.DataFrame, first_scored: str, weights: dict, draws: int = 2000,
                  seed: int = 0) -> pd.DataFrame:
    """Season fantasy points at the actual games played, per held-out season: coverage of the
    10-90 range and CRPS, with the stats' shocks correlated vs independent vs one constant
    spread per stat. Rows where every stat is tracked (2005-06 on)."""
    rng = np.random.default_rng(seed)
    out = []
    usable = frame[(frame["gp"] >= MIN_GP)]
    for stat in STATS:
        usable = usable[usable[stat].notna() & usable[f"proj_{stat}"].notna()]
    for season in sorted(s for s in usable["season"].unique() if s >= first_scored):
        train = frame[frame["season"] < season]
        test = usable[usable["season"] == season]
        params = fit(train)
        simple = fit(train, simple=True)
        corr = latent_correlation(params, train)
        actual = actual_fantasy(test, weights)
        for label, prm, cr in (("correlated", params, corr), ("independent", params, None),
                               ("constant", simple, latent_correlation(simple, train))):
            sims = fantasy_draws(prm, test, cr, weights, draws, rng)
            lo, hi = np.percentile(sims, [10, 90], axis=0)
            out.append({"season": season, "fit": label, "players": len(test),
                        "in 10-90": float(((actual >= lo) & (actual <= hi)).mean()),
                        "below 10": float((actual < lo).mean()),
                        "CRPS": float(crps_from_draws(sims, actual).mean()),
                        "mean sim": float(sims.mean()), "mean actual": float(actual.mean())})
        log.info("%s fantasy check", season)
    return pd.DataFrame(out)


# --- the season everyone shares -----------------------------------------------------------------

def season_effects(frame: pd.DataFrame, first: str = "2008-09") -> dict:
    """The league-wide part of a season's shock: per stat and season, log(all counts / all
    projected counts) over the players with `MIN_GP` games. Its spread is 4-6% for most stats
    (shots fell about 10% league-wide in 2024-25), and every player shares it. The per-player
    shock was fitted across seasons, so it already contains this variance; the simulator should
    draw it once per season draw and take it out of each player's shock (s^2 - sd^2), or the
    league moves as 600 independent players and a 2024-25 cannot happen."""
    d = frame[(frame["gp"] >= MIN_GP) & (frame["season"] >= first)]
    logs = {}
    for stat in STATS:
        x = d[d[stat].notna() & d[f"proj_{stat}"].notna()]
        expected = (x[f"proj_{stat}"] * x["gp"]).groupby(x["season"]).sum()
        logs[stat] = np.log(x.groupby("season")[stat].sum() / expected)
    table = pd.DataFrame(logs).dropna()
    centred = table - table.mean()
    return {"seasons": table.index.tolist(), "sd": centred.std().to_dict(),
            "corr": centred.corr().round(4).to_dict()}


# --- the consensus ------------------------------------------------------------------------------

def consensus_rates(external: pd.DataFrame, min_sources: int = 3) -> pd.DataFrame:
    """Per skater the sources project: per-game rates from the consensus line (per stat, the
    mean over the sources that project it -- a missing stat is missing, not zero), PIM in
    penalty units. Only players `min_sources` or more sources cover."""
    sk = external[~external["is_goalie"].astype(bool)].copy()
    # A negative count is a source's placeholder or artefact, not a projection (Bangers 2025-26
    # gives PPP -1 for four players; Dom 2026-27 small negative goals for three): missing.
    sk[list(STATS)] = sk[list(STATS)].where(sk[list(STATS)] >= 0)
    lines = sk.groupby("player_id")[["games"] + list(STATS)].mean()
    lines["sources"] = sk.groupby("player_id")["source"].nunique()
    lines = lines[(lines["sources"] >= min_sources) & (lines["games"] > 0)]
    rates = lines[list(STATS)].div(lines["games"], axis=0)
    rates["pim"] = rates["pim"] / 2.0
    rates["cons_games"] = lines["games"]
    return rates


def with_consensus(frame: pd.DataFrame, rates: pd.DataFrame, season: str) -> pd.DataFrame:
    """The season's rows with the consensus rate in place of the yardstick, for the players the
    consensus covers; the yardstick kept as `marcel_<stat>`."""
    d = frame[(frame["season"] == season) & frame["player_id"].isin(rates.index)].copy()
    for stat in STATS:
        d[f"marcel_{stat}"] = d[f"proj_{stat}"]
        d[f"proj_{stat}"] = d["player_id"].map(rates[stat]).to_numpy()
    return d


def fit_transfer(params: dict, rows: pd.DataFrame, stat: str) -> tuple:
    """(shift, log scale) that make the history's shock fit the consensus's errors on `rows`:
    m' = m + shift, s' = s x exp(log scale). Maximum likelihood, the history's fit held fixed."""
    d = _usable(rows, stat)
    m, s = shock(params, d, stat)
    y = d[stat].to_numpy(float)
    expo = d[f"proj_{stat}"].to_numpy() * d["gp"].to_numpy(float)

    def nll(t):
        eta = (m + t[0])[:, None] + (s * np.exp(t[1]))[:, None] * NODES[None, :]
        lam = expo[:, None] * np.exp(np.clip(eta, -30, 30))
        a = LOG_WEIGHTS[None, :] + y[:, None] * np.log(np.maximum(lam, 1e-300)) - lam
        return -special.logsumexp(a, axis=1).mean()

    res = optimize.minimize(nll, np.zeros(2), method="Nelder-Mead")
    return float(res.x[0]), float(res.x[1])


def adjusted(params: dict, transfer: dict) -> dict:
    """The history's fit with the consensus transfer folded into the intercepts."""
    out = {}
    for stat, prm in params.items():
        shift, log_scale = transfer.get(stat, (0.0, 0.0))
        beta = list(prm["beta"])
        gamma = list(prm["gamma"])
        beta[0] += shift
        gamma[0] += log_scale
        out[stat] = {**prm, "beta": beta, "gamma": gamma}
    return out


def consensus_report(frame: pd.DataFrame, paths, seed: int = 0) -> pd.DataFrame:
    """Per season with the consensus beside the actuals: per stat, the log score of the
    history's shock around the yardstick and around the consensus on the same players, and
    the (shift, scale) that would make the shock fit the consensus's errors that season."""
    rng = np.random.default_rng(seed)
    out = []
    for season in ("2024-25", "2025-26"):
        external = pd.read_parquet(paths.FEATURES_DIR / f"external_projections_{season}.parquet")
        rows = with_consensus(frame, consensus_rates(external), season)
        params = fit(frame[frame["season"] < season])
        for stat in STATS:
            both = rows[rows[f"marcel_{stat}"].notna()]
            marcel = both.assign(**{f"proj_{stat}": both[f"marcel_{stat}"]})
            _, _, ls_m = predictive_pit(params[stat], marcel, stat, rng)
            _, pit, ls_c = predictive_pit(params[stat], both, stat, rng)
            shift, log_scale = fit_transfer(params[stat], both, stat)
            out.append({"season": season, "stat": stat, "players": len(ls_c),
                        "log score yardstick": ls_m.mean(), "log score consensus": ls_c.mean(),
                        "consensus PIT in 10-90": ((pit >= 0.1) & (pit <= 0.9)).mean(),
                        "shift": shift, "scale": float(np.exp(log_scale))})
    return pd.DataFrame(out)


# --- CLI ----------------------------------------------------------------------------------------

def build(features_dir, holdout_from: str = "2024-25") -> tuple:
    totals, _, team_games, players = games_played.load_inputs(features_dir)
    inputs = marcel_inputs(totals, team_games)
    k = fit_k(inputs[inputs["season"] < holdout_from])
    frame = covariate_frame(project(inputs, k), players)
    return frame, k


def main():
    import paths

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--first-scored", default="2008-09")
    parser.add_argument("--season", action="append", default=[])
    parser.add_argument("--fantasy", action="append", default=[],
                        help="Season fantasy-point check under this scoring file (repeatable)")
    parser.add_argument("--consensus", action="store_true",
                        help="The consensus against the yardstick, 2024-25 and 2025-26")
    args = parser.parse_args()

    frame, k = build(paths.FEATURES_DIR)
    log.info("Marcel k (games of the position mean, fitted before 2024-25): %s", k)

    for season in args.season:
        train = frame[frame["season"] < season]
        fitted = fit(train)
        params = {"for_season": season, "marcel_k": k, "stats": fitted,
                  "correlation": latent_correlation(fitted, train).round(4).to_dict(),
                  "season_effects": season_effects(train)}
        out = paths.ensure(paths.REPORTS_DIR) / f"rate_error_{season}.json"
        out.write_text(json.dumps(params, indent=1))
        log.info("%s -> %s", season, out)

    if args.evaluate:
        scored = rolling_origin(frame, args.first_scored, k)
        scored.to_parquet(paths.ensure(paths.REPORTS_DIR) / "rate_error_scored.parquet",
                          index=False)
        pd.set_option("display.width", 250)
        print(summarize(scored).round(4).to_string())

    for league in args.fantasy:
        weights = json.loads((paths.SCORESETS_DIR / f"{league}.json").read_text())["skaters"]
        table = fantasy_check(frame, args.first_scored, weights)
        table.to_parquet(paths.REPORTS_DIR / f"rate_error_fantasy_{league}.parquet", index=False)
        pooled = table.groupby("fit").apply(lambda x: pd.Series({
            "in 10-90": np.average(x["in 10-90"], weights=x["players"]),
            "below 10": np.average(x["below 10"], weights=x["players"]),
            "CRPS": np.average(x["CRPS"], weights=x["players"])}), include_groups=False)
        print(league)
        print(pooled.round(4).to_string())

    if args.consensus:
        print(consensus_report(frame, paths).round(3).to_string(index=False))


if __name__ == "__main__":
    main()

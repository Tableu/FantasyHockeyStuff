#!/usr/bin/env python
"""How many games a player plays in a season: a distribution, not a number.
Plan: ~/.claude/plans/boom-bust-odds.md, step 1. Results: docs/games-played.md.

    python games_played.py --evaluate [--goalies]    # rolling-origin check
    python games_played.py --season 2026-27 [--goalies]   # fit on every season before it

The season-level half of a player's spread. The game-level sampler draws around a known
projection; this is the risk a draft takes on before a puck drops -- he gets hurt, he loses his
spot, he is scratched. It is fitted on the NHL's own season lines (`season_totals.parquet`,
2000-01 on) and the injury spells (`injury_seasons.parquet`), both exported by
ModelFeatures/build_season_history.py.

**Who is modelled.** A skater who played at least `MIN_PREV_SHARE` of his team's games last
season -- the returning regulars a draft board is made of -- or a goalie with
`MIN_PREV_START_SHARE` of the starts (his outcome is games started). Rookies and call-ups have
no prior line and are not covered here. The outcome is 0 when he has no line; a player with no
line AND no injury spell this season left the league (retired, Europe, the minors all year) and
is dropped, since a board would not list him -- the count is reported.

**The model.** A location-scale ordered logit over bands of share played (`ORDINAL_EDGES`), the
location and the scale each a linear function of what is known before the season: age,
position, the last three seasons' share of games played and share lost to injury, the number of
spells, last season's ice time and scoring rate. Ordinal because the shape is a pile of full
seasons and a long, lumpy left tail; a beta-binomial and a two-part mixture of them are kept
(`--model`) as the comparisons it beat. COVID-protocol absences (2020-21, 2021-22) are not
injury history.

Import-safe: nothing here imports `paths` at module level, so `season_spread.py` (and
Season/, whose `paths` shadows this folder's) can import it. Fitted numbers are read and written
as JSON, keyed by the season they are FOR and fitted only on seasons before it.
"""

import argparse
import json
import logging

import numpy as np
import pandas as pd
from scipy import optimize, special

log = logging.getLogger("games_played")

MIN_PREV_SHARE = 0.25          # last season's games played / team games, to be modelled
MIN_PREV_START_SHARE = 0.15    # a goalie: last season's starts / team games (about 12 starts)
FIRST_TARGET = "2003-04"       # the first season with three prior seasons (2000-01 on)
L2 = 1e-3                      # a light ridge on every coefficient but the intercepts
SHARE_BINS = (0.0, 0.25, 0.5, 0.6, 0.7, 0.75, 0.8, 0.9, 0.95, 1.0001)


# --- the frame ---------------------------------------------------------------------------------

def season_games(team_games: pd.DataFrame) -> pd.Series:
    """Regular-season games per team, per season. The most any team played: 2019-20 stopped at
    68-71, and a player's own team is not always one team."""
    return team_games.groupby("season")["games"].max()


def age_on(birth_dates: pd.Series, season: str) -> pd.Series:
    """Age on 1 October of the season's first year."""
    opener = pd.Timestamp(f"{season[:4]}-10-01")
    born = pd.to_datetime(birth_dates, errors="coerce")
    return (opener - born).dt.days / 365.25


def build_frame(totals: pd.DataFrame, injury: pd.DataFrame, team_games: pd.DataFrame,
                players: pd.DataFrame, target_seasons=None, goalies: bool = False) -> pd.DataFrame:
    """One row per (target season, returning regular): features from the three seasons before,
    the outcome from the target season where it has been played.

    `target_seasons` defaults to every season with a prior season; a season with a schedule but
    no totals yet (the one about to start) gets rows with no outcome, for prediction.

    `goalies`: the count is games STARTED (a relief appearance scores almost nothing), the pool
    is last season's goalies with `MIN_PREV_START_SHARE` of the starts, and there is no injury
    history (the spells source excludes goalies) -- so a goalie hurt all season looks like one
    who left, and is dropped with them. His covariates are his starts shares and last season's
    save percentage, not ice time and scoring.
    """
    games = season_games(team_games)
    count = "starts" if goalies else "gp"
    of_kind = totals[totals["is_goalie"].astype(bool) == goalies].copy()
    of_kind["points"] = of_kind["goals"] + of_kind["assists"]
    by_season = {s: g.set_index("player_id") for s, g in of_kind.groupby("season")}
    inj = {} if goalies else {s: g.set_index("player_id") for s, g in injury.groupby("season")}
    threshold = MIN_PREV_START_SHARE if goalies else MIN_PREV_SHARE
    seasons = sorted(games.index)
    if target_seasons is None:
        target_seasons = [s for s in seasons if s >= FIRST_TARGET]
    birth = players.set_index("player_id")["birth_date"]

    frames = []
    for season in target_seasons:
        i = seasons.index(season)
        prev = seasons[max(0, i - 3):i][::-1]            # last season first
        if not prev or prev[0] not in by_season:
            continue
        last = by_season[prev[0]]
        share1 = last[count] / games[prev[0]]
        pool = share1[share1 >= threshold].index
        f = pd.DataFrame(index=pool)
        f["season"] = season
        f["goalie"] = goalies
        f["n"] = int(games[season])
        f["is_d"] = (last.loc[pool, "position"] == "D").astype(float)
        f["age"] = age_on(birth.reindex(pool), season).to_numpy()
        f["toi1"] = last.loc[pool, "toi_per_game"].to_numpy() / 60.0
        f["ppg1"] = (last.loc[pool, "points"] / last.loc[pool, "gp"]).to_numpy()
        if goalies:
            shots = last.loc[pool, "shots_against"].to_numpy(float)
            f["sv1"] = np.where(shots > 0, last.loc[pool, "saves"].to_numpy(float)
                                / np.maximum(shots, 1), np.nan)
        for k in range(3):
            s = prev[k] if k < len(prev) else None
            rows = by_season.get(s)
            hurt = inj.get(s)
            if rows is None:
                f[f"gp{k + 1}"] = 0.0
                f[f"inj{k + 1}"] = 0.0
                f[f"spells{k + 1}"] = 0.0
                f[f"missing{k + 1}"] = 1.0
                continue
            g = games[s]
            f[f"gp{k + 1}"] = (rows[count].reindex(pool).fillna(0) / g).clip(0, 1).to_numpy()
            lost = hurt["injury_games"].reindex(pool).fillna(0) if hurt is not None else 0.0
            f[f"inj{k + 1}"] = np.clip(np.asarray(lost, dtype=float) / g, 0, 1)
            spells = hurt["spells"].reindex(pool).fillna(0) if hurt is not None else 0.0
            f[f"spells{k + 1}"] = np.asarray(spells, dtype=float)
            f[f"missing{k + 1}"] = (~pool.isin(rows.index)).astype(float)

        played = by_season.get(season)
        if played is not None:
            gp = played[count].reindex(pool)
            hurt = inj.get(season)
            injured = pool.isin(hurt.index) if hurt is not None else np.zeros(len(pool), bool)
            f["left"] = (gp.isna() & ~injured).to_numpy()
            f["k"] = gp.fillna(0).clip(upper=f["n"]).to_numpy()
            f["injury_games"] = (hurt["injury_games"].reindex(pool).fillna(0).to_numpy()
                                 if hurt is not None else 0.0)
        frames.append(f.reset_index(names="player_id"))
    frame = pd.concat(frames, ignore_index=True)
    frame["age"] = frame["age"].fillna(frame["age"].median())
    if goalies:
        frame["sv1"] = frame["sv1"].fillna(frame["sv1"].median())
    return frame


# --- the design ---------------------------------------------------------------------------------

def design(frame: pd.DataFrame) -> tuple:
    """The covariates, same for the mean and the precision. Returns (X, names)."""
    kinds = frame["goalie"].unique()
    if len(kinds) != 1:
        raise ValueError("a frame is skaters or goalies, not both")
    a = frame["age"].to_numpy()
    cols = {
        "intercept": np.ones(len(frame)),
        "age": (a - 27.0) / 4.0,
        "age_over_31": np.maximum(a - 31.0, 0) / 4.0,
        "age_under_24": np.maximum(24.0 - a, 0) / 3.0,
    }
    if kinds[0]:
        cols.update({
            "starts1": frame["gp1"].to_numpy(), "starts2": frame["gp2"].to_numpy(),
            "starts3": frame["gp3"].to_numpy(),
            "missing2": frame["missing2"].to_numpy(), "missing3": frame["missing3"].to_numpy(),
            "sv1": (frame["sv1"].to_numpy() - 0.905) / 0.012,
        })
        names = list(cols)
        return np.column_stack([cols[c] for c in names]), names
    cols.update({
        "is_d": frame["is_d"].to_numpy(),
        "gp1": frame["gp1"].to_numpy(), "gp2": frame["gp2"].to_numpy(),
        "gp3": frame["gp3"].to_numpy(),
        "missing2": frame["missing2"].to_numpy(), "missing3": frame["missing3"].to_numpy(),
        "inj1": frame["inj1"].to_numpy(), "inj2": frame["inj2"].to_numpy(),
        "inj3": frame["inj3"].to_numpy(),
        "spells3": np.log1p(frame[["spells1", "spells2", "spells3"]].sum(axis=1).to_numpy()),
        "toi1": (frame["toi1"].to_numpy() - 16.0) / 4.0,
        "ppg1": (frame["ppg1"].to_numpy() - 0.4) / 0.3,
    })
    names = list(cols)
    return np.column_stack([cols[c] for c in names]), names


MODELS = ("ordinal", "mixture", "betabinomial")
# The ordinal model's bands of share played: fine near a full season, where most players land.
ORDINAL_EDGES = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.93, 0.96, 0.99)


def _band_of(k: np.ndarray, n: np.ndarray) -> np.ndarray:
    """The ordinal band of each games-played count: the last edge at or below its share."""
    return np.searchsorted(np.array(ORDINAL_EDGES), k / n, side="right") - 1


def _ordinal_cdf(theta: np.ndarray, X: np.ndarray) -> np.ndarray:
    """P(band <= j) for j = 0..J-2, rows x (J-1): a location-scale ordered logit. Cut points
    increase by construction; the covariates move the location and stretch the scale, so a
    risky player's distribution can be wider, not only lower."""
    J = len(ORDINAL_EDGES)
    p = X.shape[1]
    cuts = theta[0] + np.concatenate([[0.0], np.cumsum(np.exp(theta[1:J - 1]))])
    loc = X[:, 1:] @ theta[J - 1:J - 1 + p - 1]
    scale = np.exp(np.clip(X[:, 1:] @ theta[J - 1 + p - 1:], -5, 5))
    return special.expit((cuts[None, :] - loc[:, None]) / scale[:, None])


def _ordinal_band_probs(theta, X):
    cdf = _ordinal_cdf(theta, X)
    ones = np.ones((len(X), 1))
    full = np.hstack([cdf, ones])
    return np.diff(np.hstack([np.zeros((len(X), 1)), full]), axis=1)


def _components(theta: np.ndarray, X: np.ndarray, model: str) -> list:
    """[(log weight, alpha, beta), ...] per component, arrays over rows. The mixture is a clean
    season (most of the games, a few scratches or rest days) or a disrupted one (an injury, a
    demotion): the weight and both means move with the covariates, each keeps one precision."""
    p = X.shape[1]
    if model == "betabinomial":
        mu = special.expit(X @ theta[:p])
        phi = np.exp(np.clip(X @ theta[p:2 * p], -8, 12))
        return [(np.zeros(len(X)), mu * phi, (1 - mu) * phi)]
    w, m1, m2, logphi = theta[:p], theta[p:2 * p], theta[2 * p:3 * p], theta[3 * p:]
    z = X @ w
    out = []
    for logw, m, lp in ((-np.logaddexp(0, -z), m1, logphi[0]),
                        (-np.logaddexp(0, z), m2, logphi[1])):
        mu = special.expit(X @ m)
        phi = np.exp(np.clip(lp, -8, 12))
        out.append((logw, mu * phi, (1 - mu) * phi))
    return out


def _loglik_rows(theta, X, k, n, model):
    if model == "ordinal":
        probs = _ordinal_band_probs(theta, X)
        band = _band_of(k, n)
        return np.log(np.maximum(probs[np.arange(len(k)), band], 1e-12))
    terms = [logw + special.betaln(k + a, n - k + b) - special.betaln(a, b)
             for logw, a, b in _components(theta, X, model)]
    return special.logsumexp(np.vstack(terms), axis=0)


def _negloglik(theta, X, k, n, model):
    p = X.shape[1]
    if model == "ordinal":
        penalty = L2 * np.sum(theta[len(ORDINAL_EDGES) - 1:] ** 2)
    else:
        blocks = theta[:len(theta) - (2 if model == "mixture" else 0)].reshape(-1, p)
        penalty = L2 * np.sum(blocks[:, 1:] ** 2)        # every coefficient but the intercepts
    return -_loglik_rows(theta, X, k, n, model).mean() + penalty


def fit(frame: pd.DataFrame, model: str = "ordinal") -> dict:
    """Maximum likelihood on the rows with an outcome that did not leave the league."""
    if model not in MODELS:
        raise ValueError(f"model {model!r}; use one of {MODELS}")
    rows = frame[frame["k"].notna() & ~frame["left"].astype(bool)]
    X, names = design(rows)
    k = rows["k"].to_numpy(float)
    n = rows["n"].to_numpy(float)
    p = X.shape[1]
    if model == "ordinal":
        band = _band_of(k, n)
        J = len(ORDINAL_EDGES)
        cum = np.cumsum(np.bincount(band, minlength=J))[:J - 1] / len(band)
        cuts = special.logit(np.clip(cum, 1e-3, 1 - 1e-3))
        cuts = np.maximum.accumulate(cuts + np.arange(J - 1) * 1e-3)
        theta0 = np.concatenate([[cuts[0]], np.log(np.maximum(np.diff(cuts), 1e-3)),
                                 np.zeros(2 * (p - 1))])
    elif model == "betabinomial":
        theta0 = np.zeros(2 * p)
        theta0[0] = special.logit(np.clip((k / n).mean(), 0.05, 0.95))
        theta0[p] = np.log(3.0)
    else:
        theta0 = np.zeros(3 * p + 2)
        theta0[0] = special.logit(0.6)                   # share of clean seasons
        theta0[p] = special.logit(0.95)                  # clean: most of the games
        theta0[2 * p] = special.logit(0.6)               # disrupted
        theta0[3 * p:] = np.log([40.0, 3.0])
    res = optimize.minimize(_negloglik, theta0, args=(X, k, n, model), method="L-BFGS-B",
                            options={"maxiter": 5000})
    if not res.success:
        log.warning("fit did not converge: %s", res.message)
    return {"model": model, "goalies": bool(rows["goalie"].iloc[0]), "names": names, "theta": res.x.tolist(), "rows": int(len(rows)),
            "seasons": sorted(rows["season"].unique().tolist()), "negloglik": float(res.fun),
            "min_prev_share": MIN_PREV_SHARE}


def components(params: dict, frame: pd.DataFrame) -> list:
    X, names = design(frame)
    if names != params["names"]:
        raise ValueError("fitted covariates differ from this design")
    return _components(np.array(params["theta"]), X, params["model"])


def pmf(params: dict, frame: pd.DataFrame) -> np.ndarray:
    """P(games played = 0..n_max) per row, zero past each row's own n."""
    n = frame["n"].to_numpy(int)
    kk = np.arange(n.max() + 1)[None, :]
    nn = n[:, None]
    valid = kk <= nn
    if params["model"] == "ordinal":
        X, names = design(frame)
        if names != params["names"]:
            raise ValueError("fitted covariates differ from this design")
        bands = _ordinal_band_probs(np.array(params["theta"]), X)
        band = np.where(valid, _band_of(np.minimum(kk, nn), nn), -1)
        counts = np.stack([(band == j).sum(axis=1) for j in range(bands.shape[1])], axis=1)
        per_game = np.where(counts > 0, bands / np.maximum(counts, 1), 0.0)
        total = np.where(valid, np.take_along_axis(per_game, np.maximum(band, 0), axis=1), 0.0)
        return total / total.sum(axis=1, keepdims=True)
    comb = special.gammaln(nn + 1) - special.gammaln(kk + 1) - special.gammaln(np.maximum(nn - kk, 0) + 1)
    total = np.zeros((len(frame), kk.shape[1]))
    for logw, a, b in components(params, frame):
        logp = (logw[:, None] + comb + special.betaln(kk + a[:, None], nn - kk + b[:, None])
                - special.betaln(a, b)[:, None])
        total += np.where(valid, np.exp(np.where(valid, logp, -np.inf)), 0.0)
    return total / total.sum(axis=1, keepdims=True)


def sample(params: dict, frame: pd.DataFrame, draws: int, rng: np.random.Generator) -> np.ndarray:
    """Games played, draws x rows."""
    if params["model"] == "ordinal":
        cdf = np.cumsum(pmf(params, frame), axis=1)
        u = rng.uniform(size=(draws, len(frame)))
        return (u[..., None] > cdf[None, :, :]).sum(axis=-1)
    comps = components(params, frame)
    weights = np.exp(np.vstack([c[0] for c in comps]))              # components x rows
    pick = (rng.uniform(size=(draws, len(frame)))[..., None]
            > np.cumsum(weights, axis=0).T[None, :, :]).sum(axis=-1)
    pick = np.minimum(pick, len(comps) - 1)
    a = np.vstack([c[1] for c in comps])[pick, np.arange(len(frame))[None, :]]
    b = np.vstack([c[2] for c in comps])[pick, np.arange(len(frame))[None, :]]
    return rng.binomial(frame["n"].to_numpy(int)[None, :], rng.beta(a, b))


# --- the check ----------------------------------------------------------------------------------

def _bin_probs_from_pmf(prob: np.ndarray, n: np.ndarray) -> np.ndarray:
    share = np.arange(prob.shape[1])[None, :] / n[:, None]
    edges = np.array(SHARE_BINS)
    idx = np.clip(np.searchsorted(edges, share, side="right") - 1, 0, len(edges) - 2)
    out = np.zeros((prob.shape[0], len(edges) - 1))
    for b in range(len(edges) - 1):
        out[:, b] = np.where(idx == b, prob, 0.0).sum(axis=1)
    return out


def _bin_of(share: np.ndarray) -> np.ndarray:
    edges = np.array(SHARE_BINS)
    return np.clip(np.searchsorted(edges, share, side="right") - 1, 0, len(edges) - 2)


def _tier(frame: pd.DataFrame) -> pd.Series:
    """The baseline's cells: last season's share played x age band."""
    gp = pd.cut(frame["gp1"], [-1, 0.5, 0.75, 0.9, 2], labels=["<50", "50-75", "75-90", "90+"])
    age = pd.cut(frame["age"], [0, 24, 30, 34, 99], labels=["<24", "24-30", "31-34", "35+"])
    return gp.astype(str) + "|" + age.astype(str)


def tier_baseline(train: pd.DataFrame, test: pd.DataFrame) -> tuple:
    """Per cell, the training seasons' histogram of share played and mean share: the "same odds
    for everyone in his tier" model the fit has to beat."""
    t = train.assign(cell=_tier(train), bin=_bin_of((train["k"] / train["n"]).to_numpy()))
    hist = (t.groupby("cell")["bin"].value_counts(normalize=True).unstack(fill_value=0)
            .reindex(columns=range(len(SHARE_BINS) - 1), fill_value=0))
    overall = t["bin"].value_counts(normalize=True).reindex(range(len(SHARE_BINS) - 1),
                                                            fill_value=0)
    cells = _tier(test)
    probs = np.vstack([hist.loc[c].to_numpy() if c in hist.index else overall.to_numpy()
                       for c in cells])
    mean_share = t.groupby("cell").apply(lambda g: (g["k"] / g["n"]).mean(), include_groups=False)
    exp_share = cells.map(mean_share).fillna((t["k"] / t["n"]).mean()).to_numpy()
    return probs, exp_share


def rps(probs: np.ndarray, outcome_bin: np.ndarray) -> np.ndarray:
    """Ranked probability score per row (lower is better)."""
    cdf = np.cumsum(probs, axis=1)
    obs = (np.arange(probs.shape[1])[None, :] >= outcome_bin[:, None]).astype(float)
    return ((cdf - obs) ** 2).sum(axis=1)


# Each event is "share below a cut" or "share at or above a cut", with every cut an edge of
# SHARE_BINS, so the band probabilities add up to exactly the event that is scored. (A first
# version summed the bands below 0.8 and scored share < 0.75: every P(under 75%) read ~5 points
# high.)
EVENTS = {"under half": ("<", 0.5), "under 75%": ("<", 0.75), "90%+": (">=", 0.9)}


def _event_bands(event: str) -> list:
    side, cut = EVENTS[event]
    edge = SHARE_BINS.index(cut)
    bands = range(len(SHARE_BINS) - 1)
    return [b for b in bands if (b < edge if side == "<" else b >= edge)]


def _event_happened(event: str, share: np.ndarray) -> np.ndarray:
    side, cut = EVENTS[event]
    return (share < cut) if side == "<" else (share >= cut)


def score_season(params: dict, train: pd.DataFrame, test: pd.DataFrame,
                 rng: np.random.Generator) -> pd.DataFrame:
    """Per row of one held-out season: model and baseline probabilities and outcomes."""
    n = test["n"].to_numpy(int)
    k = test["k"].to_numpy(int)
    prob = pmf(params, test)
    model_bins = _bin_probs_from_pmf(prob, n)
    base_bins, base_share = tier_baseline(train, test)
    share = k / n
    outcome_bin = _bin_of(share)
    cdf = np.cumsum(prob, axis=1)
    below = np.where(k > 0, cdf[np.arange(len(k)), np.maximum(k - 1, 0)], 0.0)
    at = prob[np.arange(len(k)), k]
    pit = below + rng.uniform(size=len(k)) * at            # randomized PIT for a count
    out = pd.DataFrame({
        "season": test["season"].to_numpy(), "player_id": test["player_id"].to_numpy(),
        "n": n, "k": k, "share": share,
        "exp_share_model": (prob * np.arange(prob.shape[1])[None, :]).sum(axis=1) / n,
        "exp_share_base": base_share, "pit": pit,
        "rps_model": rps(model_bins, outcome_bin), "rps_base": rps(base_bins, outcome_bin),
    })
    for name in EVENTS:
        bands = _event_bands(name)
        out[f"p_model {name}"] = model_bins[:, bands].sum(axis=1)
        out[f"p_base {name}"] = base_bins[:, bands].sum(axis=1)
        out[f"y {name}"] = _event_happened(name, share).astype(float)
    return out


def rolling_origin(frame: pd.DataFrame, first_scored: str, seed: int = 0,
                   model: str = "ordinal") -> pd.DataFrame:
    """Score each season from `first_scored` on with a fit on the seasons before it only."""
    rng = np.random.default_rng(seed)
    usable = frame[frame["k"].notna() & ~frame["left"].astype(bool)]
    scored = []
    for season in sorted(s for s in usable["season"].unique() if s >= first_scored):
        train = usable[usable["season"] < season]
        test = usable[usable["season"] == season]
        params = fit(train, model)
        scored.append(score_season(params, train, test, rng))
        log.info("%s: fit on %d rows, scored %d", season, params["rows"], len(test))
    return pd.concat(scored, ignore_index=True)


def summarize(scored: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for label, g in [("all scored seasons", scored)] + list(scored.groupby("season")):
        r = {"seasons": label, "players": len(g),
             "RPS model": g["rps_model"].mean(), "RPS tier": g["rps_base"].mean(),
             "MAE games model": (abs(g["exp_share_model"] - g["share"]) * g["n"]).mean(),
             "MAE games tier": (abs(g["exp_share_base"] - g["share"]) * g["n"]).mean(),
             "PIT in 10-90": ((g["pit"] >= 0.1) & (g["pit"] <= 0.9)).mean()}
        for name in EVENTS:
            r[f"Brier {name} model"] = ((g[f"p_model {name}"] - g[f"y {name}"]) ** 2).mean()
            r[f"Brier {name} tier"] = ((g[f"p_base {name}"] - g[f"y {name}"]) ** 2).mean()
        rows.append(r)
    return pd.DataFrame(rows)


def reliability(scored: pd.DataFrame, event: str, bins: int = 10) -> pd.DataFrame:
    p = scored[f"p_model {event}"]
    cut = pd.qcut(p, bins, duplicates="drop")
    return (scored.groupby(cut, observed=True)
            .agg(players=(f"y {event}", "size"), predicted=(f"p_model {event}", "mean"),
                 observed=(f"y {event}", "mean")))


# --- CLI ----------------------------------------------------------------------------------------

def load_inputs(features_dir):
    return (pd.read_parquet(features_dir / "season_totals.parquet"),
            pd.read_parquet(features_dir / "injury_seasons.parquet"),
            pd.read_parquet(features_dir / "team_games.parquet"),
            pd.read_parquet(features_dir / "players.parquet"))


def main():
    import paths                                    # CLI only: keeps the module import-safe

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--season", action="append", default=[],
                        help="Fit for this season on the seasons before it (repeatable)")
    parser.add_argument("--evaluate", action="store_true", help="Rolling-origin check")
    parser.add_argument("--first-scored", default="2008-09")
    parser.add_argument("--model", choices=MODELS, default="ordinal")
    parser.add_argument("--goalies", action="store_true", help="Goalies' games started")
    args = parser.parse_args()

    totals, injury, team_games, players = load_inputs(paths.FEATURES_DIR)
    frame = build_frame(totals, injury, team_games, players, goalies=args.goalies)
    kind = "goalies" if args.goalies else "skaters"
    done = frame[frame["k"].notna()]
    log.info("%d returning-regular rows with an outcome; %d left the league (dropped)",
             int((~done["left"].astype(bool)).sum()), int(done["left"].astype(bool).sum()))

    for season in args.season:
        train = frame[(frame["season"] < season) & frame["k"].notna()
                      & ~frame["left"].fillna(False).astype(bool)]
        params = fit(train, args.model)
        params["for_season"] = season
        out = paths.ensure(paths.REPORTS_DIR) / f"games_played_{kind}_{season}.json"
        out.write_text(json.dumps(params, indent=1))
        log.info("%s: fitted on %d rows (%s..%s) -> %s", season, params["rows"],
                 params["seasons"][0], params["seasons"][-1], out)

    if args.evaluate:
        scored = rolling_origin(frame, args.first_scored, model=args.model)
        scored.to_parquet(paths.ensure(paths.REPORTS_DIR) / f"games_played_scored_{kind}_{args.model}.parquet",
                          index=False)
        table = summarize(scored)
        pd.set_option("display.width", 250)
        print(table.round(4).to_string(index=False))
        for event in EVENTS:
            print(f"\nreliability, P({event}):")
            print(reliability(scored, event).round(3).to_string())


if __name__ == "__main__":
    main()

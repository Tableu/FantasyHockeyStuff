"""Whole seasons, drawn: every draftable player's season fantasy points as a distribution.
Plan: ~/.claude/plans/boom-bust-odds.md, step 3. The par curve and the boom/bust odds built on
these draws live in Season/boom_bust.py.

A season draw, for one player:

    games       from `games_played` (a returning regular) or the consensus's games scaled by
                how far the consensus has overshot before (anyone else -- rookies, call-ups)
    per game    the consensus's per-game rates, x the league's season shock (one draw per season,
                shared by everyone: shots fell 10% league-wide in 2024-25), x his own rate shock
                from `rate_error` (correlated across stats, its spread net of the league's)
    counts      Poisson at rate x games, scored with the league's weights

Goalies: games started from `games_played --goalies`, fantasy points per start from the
consensus, and one log-normal shock per goalie fitted on history (`fit_goalie_shock`).

Why the consensus's rates and not its totals: its totals assume about 10 games too many per
skater (docs/games-played.md), while its per-game rates are as good as anything we have
(docs/rate-error.md).

No `paths` and no database: every input is handed in. Import-safe, so Season/ can use it.
"""

import logging

import numpy as np
import pandas as pd
from scipy import optimize, special

import games_played
import rate_error

log = logging.getLogger("season_spread")

STATS = rate_error.STATS
GOALIE_STATS = ("wins", "losses", "ot_losses", "shutouts", "saves", "goals_against")
MIN_SOURCES = 3
MIN_SHOCK_SD = 0.02


# --- the consensus ------------------------------------------------------------------------------

def consensus_skaters(external: pd.DataFrame, teams: pd.DataFrame,
                      min_sources: int = MIN_SOURCES) -> pd.DataFrame:
    """Per skater `min_sources` or more sources project: per-game rates (PIM in penalty units),
    projected games, and the team the sources most often put him on (as an abbreviation)."""
    rates = rate_error.consensus_rates(external, min_sources)
    sk = external[~external["is_goalie"].astype(bool)]
    team = (sk.groupby("player_id")["team_id"]
            .agg(lambda s: s.mode().iloc[0] if s.notna().any() else np.nan))
    abbrev = teams.set_index("team_id")["team"]
    rates["team"] = team.reindex(rates.index).map(abbrev)
    return rates


def consensus_goalies(external: pd.DataFrame, weights: dict,
                      min_sources: int = MIN_SOURCES) -> pd.DataFrame:
    """Per goalie: projected starts and fantasy points per start under `weights`. A source
    without starts contributes its games. Negative counts are missing, as for skaters."""
    g = external[external["is_goalie"].astype(bool)].copy()
    g[list(GOALIE_STATS)] = g[list(GOALIE_STATS)].where(g[list(GOALIE_STATS)] >= 0)
    g["starts"] = g["games_started"].where(g["games_started"] > 0, g["games"])
    lines = g.groupby("player_id")[["starts"] + list(GOALIE_STATS)].mean()
    lines["sources"] = g.groupby("player_id")["source"].nunique()
    lines = lines[(lines["sources"] >= min_sources) & (lines["starts"] > 0)]
    fps = sum(weights.get(q, 0.0) * lines[q].fillna(0) for q in GOALIE_STATS) / lines["starts"]
    return pd.DataFrame({"cons_starts": lines["starts"], "fps": fps})


# --- games for players the games-played model does not cover ------------------------------------

def fit_fallback(history: list) -> dict:
    """How far the consensus's games overshoot for players outside the games-played pool, and
    how widely they scatter: `history` is [(consensus games Series, actual games Series, season
    length)] for earlier seasons. Returns the ratio (actual / consensus, pooled) and a
    beta-binomial precision fitted to the shares. A player who never played counts as 0 games.

    Shares are of each season's own length: 82 games through 2025-26, 84 from 2026-27. (Until
    2026-09-28 this and `sample_fallback` divided by a fixed 82.)"""
    cons = pd.concat([c for c, _, _ in history])
    act = pd.concat([a.reindex(c.index).fillna(0) for c, a, _ in history])
    n = np.concatenate([np.full(len(c), float(length)) for c, _, length in history])
    ratio = float(act.sum() / cons.sum())
    mu = np.clip(cons.to_numpy() / n * ratio, 0.02, 0.97)
    k = np.clip(act.to_numpy(), 0, n)

    def nll(log_phi):
        phi = np.exp(log_phi[0])
        a, b = mu * phi, (1 - mu) * phi
        return -(special.betaln(k + a, n - k + b) - special.betaln(a, b)).mean()

    phi = float(np.exp(optimize.minimize(nll, [np.log(3.0)], method="Nelder-Mead").x[0]))
    return {"ratio": ratio, "precision": phi, "players": int(len(cons))}


def sample_fallback(cons_games: np.ndarray, n: int, fallback: dict, draws: int, rng) -> np.ndarray:
    """Games out of `n`, the season's length -- the same length the consensus projects over."""
    mu = np.clip(cons_games / float(n) * fallback["ratio"], 0.02, 0.97)
    phi = fallback["precision"]
    p = rng.beta(mu * phi, (1 - mu) * phi, size=(draws, len(mu)))
    return rng.binomial(n, p)


# --- skaters ------------------------------------------------------------------------------------

def skater_frame(season: str, totals, injury, team_games, players, consensus: pd.DataFrame,
                 marcel_k: dict) -> pd.DataFrame:
    """One row per consensus skater for `season`: the games-played covariates where he is a
    returning regular (`in_pool`), the rate shock's covariates, and the consensus rates as the
    projection. A stat no source projects for him (most sources skip SHP) takes the Marcel
    yardstick's rate (`marcel_k`, from the rate fit), else his position's mean -- not zero."""
    gp_rows = games_played.build_frame(totals, injury, team_games, players, target_seasons=[season])
    gp_rows = gp_rows.set_index("player_id")
    marcel = rate_error.marcel_inputs(totals, team_games)
    marcel = rate_error.project(marcel[marcel["season"] == season], marcel_k).set_index("player_id")

    frame = pd.DataFrame(index=consensus.index.rename("player_id"))
    frame["season"] = season
    frame["in_pool"] = frame.index.isin(gp_rows.index)
    frame["n"] = int(games_played.season_games(team_games)[season])
    frame["cons_games"] = consensus["cons_games"]
    position = players.set_index("player_id")["position"].reindex(frame.index)
    frame["is_d"] = (position == "D").astype(float)
    # Everyone gets the rate covariates; a player outside last season's pool has no games behind
    # a projection (evidence 0) and his position's mean as the prior.
    for stat in STATS:
        frame[f"wgp_{stat}"] = marcel[f"wgp_{stat}"].reindex(frame.index).fillna(0.0)
        prior = marcel[f"prior_{stat}"]
        pos_prior = prior.groupby(marcel["is_d"]).first()
        frame[f"prior_{stat}"] = prior.reindex(frame.index)
        missing = frame[f"prior_{stat}"].isna()
        frame.loc[missing, f"prior_{stat}"] = frame.loc[missing, "is_d"].map(pos_prior)
        yardstick = marcel[f"proj_{stat}"].reindex(frame.index)
        frame[f"proj_{stat}"] = (consensus[stat].fillna(yardstick)
                                 .fillna(frame[f"prior_{stat}"]).fillna(0.0))
    frame["last_teams"] = marcel["last_teams"].reindex(frame.index)
    frame["teams"] = consensus["team"]
    frame["gp"] = 60.0                                   # placeholder; the draws set it
    frame = rate_error.covariate_frame(frame.reset_index(), players).set_index("player_id")
    return frame.join(gp_rows.drop(columns=[c for c in gp_rows.columns if c in frame.columns]),
                      how="left")


def _gp_draws(frame, gp_params, fallback, draws, rng) -> np.ndarray:
    out = np.zeros((draws, len(frame)), dtype=np.int16)
    pool = frame["in_pool"].to_numpy()
    if pool.any():
        rows = frame[pool].reset_index()
        out[:, pool] = games_played.sample(gp_params, rows, draws, rng)
    if (~pool).any():
        out[:, ~pool] = sample_fallback(frame.loc[~pool, "cons_games"].to_numpy(float),
                                        int(frame["n"].iloc[0]), fallback, draws, rng)
    return out


def _shock_parts(params: dict, frame: pd.DataFrame, stat: str) -> tuple:
    """(m at 60 games, s at 60 games, the games-played slopes of m and log s)."""
    prm = params[stat]
    X, names = rate_error.design(frame.assign(gp=60.0), stat)
    X = rate_error.clip_to_fit(X, names, prm)
    j = names.index("log_gp")
    beta, gamma = np.array(prm["beta"]), np.array(prm["gamma"])
    return X @ beta, X @ gamma, beta[j], gamma[j]


def sample_skaters(frame: pd.DataFrame, gp_params: dict, rate_fit: dict, fallback: dict,
                   weights: dict, draws: int, rng) -> tuple:
    """(fantasy points, games played), each draws x players, for the skaters in `frame`."""
    gp = _gp_draws(frame, gp_params, fallback, draws, rng)
    log_gp = np.log(np.maximum(gp, 1) / 60.0)
    params = rate_fit["stats"]
    season_sd = pd.Series(rate_fit["season_effects"]["sd"])
    season_corr = pd.DataFrame(rate_fit["season_effects"]["corr"]).loc[list(STATS), list(STATS)]
    cov = season_corr.to_numpy() * np.outer(season_sd[list(STATS)], season_sd[list(STATS)])
    league = rng.multivariate_normal(np.zeros(len(STATS)), cov, size=draws)     # draws x stats

    corr = pd.DataFrame(rate_fit["correlation"]).loc[list(STATS), list(STATS)].to_numpy()
    z = rng.standard_normal((draws, len(frame), len(STATS))) @ np.linalg.cholesky(corr).T
    points = np.zeros((draws, len(frame)), dtype=np.float32)
    for j, stat in enumerate(STATS):
        w = weights.get(stat, 0.0)
        if not w:
            continue
        m0, lg0, bm, bg = _shock_parts(params, frame, stat)
        m = m0[None, :] + bm * log_gp
        s = np.exp(np.clip(lg0[None, :] + bg * log_gp, -6, 3))
        s_own = np.sqrt(np.maximum(s ** 2 - season_sd[stat] ** 2, MIN_SHOCK_SD ** 2))
        # The league's shock replaces its share of each player's, so the mean multiplier is kept.
        shift = (s ** 2 - s_own ** 2 - season_sd[stat] ** 2) / 2.0
        mult = np.exp(m + shift + s_own * z[:, :, j] + league[:, j][:, None])
        lam = frame[f"proj_{stat}"].to_numpy()[None, :] * gp * mult
        points += np.float32(w * (2.0 if stat == "pim" else 1.0)) * rng.poisson(lam).astype(np.float32)
    return points, gp


# --- goalies ------------------------------------------------------------------------------------

def goalie_fps(totals: pd.DataFrame, weights: dict) -> pd.DataFrame:
    """Per goalie-season: starts and fantasy points per start under `weights`."""
    g = totals[totals["is_goalie"].astype(bool)].copy()
    g = g[g["starts"] > 0]
    g["fp"] = sum(weights.get(q, 0.0) * g[q].fillna(0) for q in GOALIE_STATS)
    g["fps"] = g["fp"] / g["starts"]
    return g[["season", "player_id", "starts", "fps"]]


def fit_goalie_shock(totals: pd.DataFrame, weights: dict, before: str,
                     first: str = "2005-06", min_starts: int = 10) -> dict:
    """One log-normal shock on a goalie's points per start: log(actual / projected) with the
    projection a 5/4/3 start-weighted Marcel of his last three seasons pulled toward the league
    by 20 starts. Its variance is split by moments into a season shock and counting noise that
    shrinks with starts: E[r^2] = s^2 + c / starts. Fitted on seasons `first` .. before `before`."""
    per = goalie_fps(totals, weights)
    seasons = sorted(per["season"].unique())
    by = {s: g.set_index("player_id") for s, g in per.groupby("season")}
    rows = []
    for s in seasons:
        if s < first or s >= before:
            continue
        i = seasons.index(s)
        prev = seasons[max(0, i - 3):i][::-1]
        league = (per[per["season"] == prev[0]]["fps"] * per[per["season"] == prev[0]]["starts"]
                  ).sum() / per[per["season"] == prev[0]]["starts"].sum()
        now = by[s][by[s]["starts"] >= min_starts]
        num = pd.Series(20.0 * league, index=now.index)
        den = pd.Series(20.0, index=now.index)
        for w, ps in zip((5.0, 4.0, 3.0), prev):
            b = by.get(ps)
            if b is None:
                continue
            st = b["starts"].reindex(now.index).fillna(0)
            num += w * st * b["fps"].reindex(now.index).fillna(0)
            den += w * st
        proj = num / den
        ok = (proj > 0) & (now["fps"] > 0)
        rows.append(pd.DataFrame({"r": np.log(now["fps"][ok] / proj[ok]),
                                  "starts": now["starts"][ok]}))
    d = pd.concat(rows)
    m = float(np.average(d["r"], weights=d["starts"]))
    resid2 = (d["r"] - m) ** 2
    A = np.column_stack([np.ones(len(d)), 1.0 / d["starts"].to_numpy()])
    coef, *_ = np.linalg.lstsq(A, resid2.to_numpy(), rcond=None)
    return {"m": m, "s": float(np.sqrt(max(coef[0], MIN_SHOCK_SD ** 2))),
            "noise": float(max(coef[1], 0.0)), "rows": int(len(d))}


def goalie_frame(season: str, totals, injury, team_games, players, consensus: pd.DataFrame
                 ) -> pd.DataFrame:
    rows = games_played.build_frame(totals, injury, team_games, players, target_seasons=[season],
                                    goalies=True).set_index("player_id")
    frame = pd.DataFrame(index=consensus.index.rename("player_id"))
    frame["season"] = season
    frame["in_pool"] = frame.index.isin(rows.index)
    frame["n"] = int(games_played.season_games(team_games)[season])
    frame["cons_games"] = consensus["cons_starts"]
    frame["fps"] = consensus["fps"]
    return frame.join(rows.drop(columns=[c for c in rows.columns if c in frame.columns]), how="left")


def sample_goalies(frame, gp_params, shock: dict, fallback: dict, draws: int, rng) -> tuple:
    starts = _gp_draws(frame, gp_params, fallback, draws, rng)
    noise_sd = np.sqrt(shock["noise"] / np.maximum(starts, 1))
    r = shock["m"] + shock["s"] * rng.standard_normal((draws, len(frame))) \
        + noise_sd * rng.standard_normal((draws, len(frame)))
    points = (starts * frame["fps"].to_numpy()[None, :] * np.exp(r)).astype(np.float32)
    return points, starts


# --- a summary ----------------------------------------------------------------------------------

def summary(points: np.ndarray, games: np.ndarray, index) -> pd.DataFrame:
    q = np.percentile(points, [10, 50, 90], axis=0)
    return pd.DataFrame({"mean": points.mean(axis=0), "p10": q[0], "p50": q[1], "p90": q[2],
                         "exp_gp": games.mean(axis=0)}, index=index)

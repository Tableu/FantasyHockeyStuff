"""One draw of a night: the lambda table in, sampled stat lines out.

This is the piece the build plan's Section 5 is about. `Projections/predict.py` hands over a
mean per category per player-game; this turns each mean into an outcome, many times, so that
everything downstream can ask distributional questions -- a floor, a ceiling, the chance of
beating a number -- instead of only "what is the expected value".

The order of operations is the order the constraints run in:

    plays ~ Bernoulli(p_plays)          a scratch scores zero, so this gates everything
    shots, hits, blocks, assists ~ NB   correlated across players by copula.py
    goals ~ Binomial(shots, p)          a goal is a shot, so goals come out of shots
    pim = 2*minors + 5*majors + 10*misconducts
    each point -> PP / SH / EV          so ppp + shp <= points, always

Nothing here applies a scoring system. The output is a stat line; `scoring.py` turns it into
points under whatever league is asked for, and the same draws serve several at once.

Shape convention throughout: arrays are [rows, sims], rows in the order of the frame passed
in. int16 is deliberate -- a full season at 1,000 sims is 62M draws per category, and float64
would make that 500MB for no gain on a count that never exceeds 40.
"""

import logging

import numpy as np
import pandas as pd

import marginals

log = logging.getLogger(__name__)

# The order the copula's columns are in; goals is carried here too, even though it is drawn
# as a binomial, because its *correlation* with a linemate's assists is the largest
# cross-player number in the whole measurement (+0.070) and has to come from somewhere.
CATEGORIES = ["shots", "hits", "blocks", "assists", "goals", "pim"]

REQUIRED_COLUMNS = ["game_id", "team_id", "player_id", "p_plays",
                    *[f"lambda_{c}" for c in CATEGORIES],
                    "pp_point_share", "sh_point_share"]


class Draws:
    """Sampled stat lines for a block of player-games."""

    def __init__(self, keys: pd.DataFrame, counts: dict, played: np.ndarray):
        self.keys = keys.reset_index(drop=True)
        self.counts = counts
        self.played = played

    @property
    def n_rows(self):
        return len(self.keys)

    @property
    def n_sims(self):
        return self.played.shape[1]

    def __getitem__(self, category):
        return self.counts[category]

    def categories(self):
        return list(self.counts)


class Simulator:
    """Holds the calibrated structure so a caller can draw slate after slate."""

    def __init__(self, dispersion: dict, copula, incident_weights,
                 penalty_latent_variance=0.0, seed=17):
        self.dispersion = dispersion
        self.copula = copula
        self.incident_weights = np.asarray(incident_weights, dtype="float64")
        self.penalty_latent_variance = float(penalty_latent_variance)
        self.rng = np.random.default_rng(seed)

    def theta(self, category):
        return self.dispersion.get(category)

    def draw(self, frame: pd.DataFrame, n_sims: int) -> Draws:
        missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
        if missing:
            raise KeyError(f"lambda table is missing {missing}; it should come from "
                           f"Projections/predict.py")
        frame = frame.reset_index(drop=True)
        rows = len(frame)

        game_index = pd.factorize(frame["game_id"])[0]
        team_index = pd.factorize(
            frame["game_id"].astype(str) + ":" + frame["team_id"].astype(str))[0]
        uniforms = self.copula.draw(game_index, team_index, n_sims, self.rng)

        # A scratch scores nothing, so this multiplies every category. Drawn independently
        # per player: healthy scratches are mildly anti-correlated within a team (a coach
        # dresses a fixed 18), but that is a roster-construction effect the lineup feed
        # already resolves on any day that matters, and it moves no category's mean.
        plays = self.rng.random((rows, n_sims)) < frame["p_plays"].to_numpy()[:, None]

        column = {c: CATEGORIES.index(c) for c in CATEGORIES}
        counts = {}
        for category in ("shots", "hits", "blocks", "assists"):
            lam = frame[f"lambda_{category}"].to_numpy("float64")[:, None]
            counts[category] = marginals.sample_negative_binomial(
                uniforms[:, :, column[category]], np.broadcast_to(lam, (rows, n_sims)),
                self.theta(category))

        # Goals are a share of the shots that were actually drawn, so a two-goal night
        # cannot happen on zero shots and E[goals] still lands on lambda_goals.
        lambda_shots = np.maximum(frame["lambda_shots"].to_numpy("float64"), 1e-9)
        shooting = np.clip(frame["lambda_goals"].to_numpy("float64") / lambda_shots, 0.0, 1.0)
        counts["goals"] = marginals.sample_binomial(
            uniforms[:, :, column["goals"]], counts["shots"],
            np.broadcast_to(shooting[:, None], (rows, n_sims)))

        lam_pim = frame["lambda_pim"].to_numpy("float64")[:, None]
        counts["pim"] = marginals.sample_penalty_minutes(
            uniforms[:, :, column["pim"]], np.broadcast_to(lam_pim, (rows, n_sims)),
            self.incident_weights, self.penalty_latent_variance, self.rng)

        points = (counts["goals"] + counts["assists"]).astype(np.int16)
        power_play, short_handed = marginals.split_point_strengths(
            points, frame["pp_point_share"].to_numpy("float64")[:, None],
            frame["sh_point_share"].to_numpy("float64")[:, None], self.rng)
        counts["points"] = points
        counts["ppp"] = power_play
        counts["shp"] = short_handed

        for category, values in counts.items():
            counts[category] = np.where(plays, values, 0).astype(np.int16)

        keys = frame[[c for c in ("season_id", "game_id", "game_date", "team_id",
                                  "player_id", "position") if c in frame.columns]]
        return Draws(keys, counts, plays)


def played_mean(draws: Draws, category):
    """Mean of a category over draws where the player dressed -- for calibration checks."""
    values = draws[category].astype("float64")
    played = draws.played
    return float(values[played].mean()) if played.any() else float("nan")

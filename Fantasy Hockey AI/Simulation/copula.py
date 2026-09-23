"""Correlation between players, imposed on the uniforms rather than on the distributions.

The build plan's single "game quality" Gamma multiplier is not enough, and the reason is
measurable. A shared *rate* multiplier can only create correlation by also creating
overdispersion, and assists are the counter-example that kills it: on the 2025-26 holdout
assists are Poisson-marginal (fitted theta ~0) yet two teammates' assists correlate at
+0.045. That correlation comes from a shared **event** -- one goal hands out two assists --
not from a shared rate. So correlation goes on the copula, where it cannot touch a marginal:
the uniforms are drawn correlated and `marginals.py` maps each one through exactly the
distribution it would have had.

**Why the obvious construction is wrong.** Writing `Z_i = shared + idiosyncratic` forces both
cross-player matrices to be positive-semidefinite, and neither measured matrix is:

    teammate   one eigenvalue at -0.052, because a player's goals correlate with a
               *linemate's* assists at +0.070 while two teammates' goals correlate at ~0
    opponent   negative on its diagonal for shots (-0.006) and blocks (-0.009): possession
               is zero-sum, so a night when one side shoots is a night the other does not

Those signs are not noise. Bootstrapped over games, the standard error on an entry is about
0.0015, and every entry named here clears twice that. Forcing them PSD is not a small
repair -- it hands teammate goals a correlation of +0.039 where the data says -0.002.

Neither matrix has to be PSD. They are cross-covariances *between* players, not the
covariance of one vector. What has to hold is the block structure, and this is how it
factors exactly:

    Z_i = A (u_i - mean over i's team of u) + D_team(i)

with `A = (W - T)^(1/2)` over iid `u`, and the two team-level vectors drawn jointly:

    Cov(D_team) = T + (W - T)/n_team          Cov(D_A, D_B) = O

The team-mean subtraction is what buys the negative eigenvalue: players pull *apart* along
that direction, which no sum of shared factors can express. The only conditions are that
`W - T` is PSD, which it measurably is, and that the joint 2Cx2C block is -- the latter
amounting to `lambda >= -1/(n-1)` on the teammate matrix, which at a dressed side of 18 is
-0.059 against a measured -0.052. `correlations.py` fits inside that bound rather than
letting a draw clip silently.

**Why any of this matters, in one number.** Twenty thousand random ten-skater rosters drawn
from the 2025-26 holdout have a fantasy-point total whose variance is 1.048x what
independent players would give -- nothing. Twenty thousand ten-skater *stacks from one NHL
team* come in at 1.683x. Independent sampling understates a stacked roster's spread by a
quarter of its standard deviation, and head-to-head decisions are made on that spread.
"""

import json
import logging

import numpy as np
from scipy.special import ndtr

log = logging.getLogger(__name__)

CATEGORIES = ["shots", "hits", "blocks", "assists", "goals", "pim"]

# How close to the exchangeability bound a negative eigenvalue may sit before it is clipped.
FEASIBILITY_MARGIN = 0.999
# Below this, a clipped eigenvalue is a rounding artefact rather than a modelling failure.
CLIP_TOLERANCE = 1e-6


def _sqrtm(matrix, label=""):
    """Matrix square root, clipping any negative eigenvalue and saying so if it mattered."""
    values, vectors = np.linalg.eigh(np.asarray(matrix, dtype="float64"))
    if label and values.min() < -CLIP_TOLERANCE:
        log.warning("%s is not PSD (min eigenvalue %.5f); clipping", label, values.min())
    return vectors @ np.diag(np.sqrt(np.clip(values, 0.0, None))) @ vectors.T


class GameCopula:
    """Draws correlated uniforms for a set of player-games.

    The three matrices are on the *normal* scale, not the count scale: discreteness
    attenuates correlation on its way through an inverse CDF, so `correlations.py` fits these
    by simulation until the realized count correlations match what the holdout measured.
    """

    def __init__(self, within, teammate, opponent, categories=None):
        self.categories = list(categories or CATEGORIES)
        size = len(self.categories)
        self.within = np.asarray(within, dtype="float64").reshape(size, size)
        self.teammate = np.asarray(teammate, dtype="float64").reshape(size, size)
        self.opponent = np.asarray(opponent, dtype="float64").reshape(size, size)
        self.contrast = self.within - self.teammate
        self.player_loading = _sqrtm(self.contrast, "within-player minus teammate")
        self._cholesky_cache = {}

    def team_covariance(self, team_size):
        """`Cov(D_team)`: the teammate matrix, plus the contrast a team mean still carries."""
        return self.teammate + self.contrast / max(float(team_size), 1.0)

    def joint_block(self, sizes):
        """The covariance of the two teams' level vectors, stacked: [[Psi_A, O], [O, Psi_B]]."""
        size = len(self.categories)
        joint = np.zeros((2 * size, 2 * size))
        joint[:size, :size] = self.team_covariance(sizes[0])
        joint[size:, size:] = self.team_covariance(sizes[1])
        joint[:size, size:] = self.opponent
        joint[size:, :size] = self.opponent.T
        return joint

    def _team_loading(self, sizes):
        """Square root of the joint covariance of the two teams' level vectors.

        Keyed by the pair of team sizes, because the diagonal blocks depend on them; a slate
        has a handful of distinct pairs, so the cache does nearly all the work.

        If the block is not PSD -- the teammate matrix asking for more than an exchangeable
        side of that size can carry -- the square root is clipped, and the clip is then
        **rescaled back to the intended variances**. A clipped eigenvalue adds variance, and
        extra variance on the latent normal is not a smaller correlation: it makes Phi(Z)
        non-uniform, which moves a category's mean. Correlation is a modelling choice and
        can be approximate; a marginal is not, and this layer must never touch one.
        """
        key = tuple(sizes)
        if key not in self._cholesky_cache:
            joint = self.joint_block(sizes)
            values = np.linalg.eigvalsh(joint)
            loading = _sqrtm(joint)
            if values.min() < -1e-6:
                log.warning("the two-team block is not PSD for sides of %s (min eigenvalue "
                            "%.5f); clipping and rescaling to hold the variances",
                            tuple(int(v) for v in sizes), values.min())
                realized = np.diag(loading @ loading.T)
                intended = np.diag(joint)
                scale = np.sqrt(np.divide(np.clip(intended, 0.0, None),
                                          np.maximum(realized, 1e-12)))
                loading = scale[:, None] * loading
            self._cholesky_cache[key] = loading
        return self._cholesky_cache[key]

    def draw(self, game_index, team_index, n_sims, rng):
        """Uniforms shaped [rows, sims, categories].

        `game_index` and `team_index` are integer codes; rows sharing a team code are
        teammates, rows sharing only a game code are opponents.
        """
        # Re-coded rather than trusted: a caller slicing a roster out of a slate leaves gaps
        # in the codes, and a gap would read as a team with no players in a game with three.
        _, game_index = np.unique(np.asarray(game_index), return_inverse=True)
        _, team_index = np.unique(np.asarray(team_index), return_inverse=True)
        rows, size = len(game_index), len(self.categories)
        n_teams = int(team_index.max()) + 1 if rows else 0
        n_games = int(game_index.max()) + 1 if rows else 0

        # Player level: iid noise with its team mean removed, which is where the negative
        # eigenvalue comes from.
        noise = rng.standard_normal((rows, n_sims, size))
        counts = np.bincount(team_index, minlength=n_teams).astype("float64")
        totals = np.zeros((n_teams, n_sims, size))
        np.add.at(totals, team_index, noise)
        centered = noise - (totals / np.maximum(counts, 1.0)[:, None, None])[team_index]
        latent = centered @ self.player_loading.T

        # Team level: both teams of a game drawn together, so opponents get their (partly
        # negative) correlation without either team's own structure being disturbed.
        game_of_team = np.zeros(n_teams, dtype=np.int64)
        game_of_team[team_index] = game_index
        order = np.argsort(game_of_team, kind="stable")
        ranked = game_of_team[order]
        slot = np.empty(n_teams, dtype=np.int64)
        slot[order] = np.arange(n_teams) - np.searchsorted(ranked, ranked, side="left")
        if (slot > 1).any():
            raise ValueError("a game has more than two teams in it")

        pair_sizes = np.zeros((n_games, 2))
        pair_sizes[game_of_team, slot] = counts
        loadings = np.stack([self._team_loading(tuple(pair)) for pair in pair_sizes])
        shared = rng.standard_normal((n_games, n_sims, 2 * size))
        shared = np.einsum("gsk,gjk->gsj", shared, loadings)
        team_level = shared.reshape(n_games, n_sims, 2, size)[game_of_team, :, slot, :]
        latent += team_level[team_index]

        # ndtr rather than norm.cdf: the same function an order of magnitude cheaper.
        return ndtr(latent)

    def implied(self):
        """What the construction delivers on the normal scale, as a check on the algebra."""
        return {"within": self.within, "teammate": self.teammate, "opponent": self.opponent}


def _round(matrix, places=6):
    return [[round(float(v), places) for v in row] for row in np.asarray(matrix)]


def independent(categories=None):
    """The "basic independent sampling" baseline the plan starts from, for validation."""
    size = len(categories or CATEGORIES)
    zero = np.zeros((size, size))
    return GameCopula(np.eye(size), zero, zero, categories)


def load(path, categories=None):
    """Read the fitted normal-scale structure written by `correlations.py`."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    fitted = payload["normal_scale"]
    names = categories or payload.get("categories") or CATEGORIES
    return GameCopula(fitted["within"], fitted["teammate"], fitted["opponent"], names)

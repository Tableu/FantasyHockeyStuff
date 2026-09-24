"""The bridge to the Monte Carlo layer, and the one place that knows it is a sibling folder.

`Simulation/` uses flat modules -- `import marginals`, `import paths` -- which works because that
folder is the working directory when it runs. Importing from it puts two sibling folders on
`sys.path` at once, and then **the module names collide**: `Season/paths.py` shadows
`Simulation/paths.py`, so `Simulation/correlations.py` asking for `paths.dispersion_path` gets this
folder's paths module and fails. The same collision already forced `calendar.py` to become
`schedule.py`.

So this module takes the narrow route. Of everything in `Simulation/`, only four things are needed
and three of them import nothing local:

    sampler    imports marginals only          -> safe
    copula     imports json, numpy, scipy      -> safe
    marginals  imports numpy, scipy            -> safe
    scoring    imports paths                   -> COLLIDES, see below

`correlations.py` and `simulate.py` are the two that read `paths` for their defaults, so this
module does not import them at all. Both of the things they would have supplied -- the fitted
dispersion and the fitted copula structure -- are JSON files, and JSON read by path is exactly how
every other layer in this stack consumes the one below it.

**On `scoring`:** it does `import paths` and asks for `paths.SCORESETS_DIR`, which resolves to
*this* folder's paths module. It works because `Season/paths.py` and `Simulation/paths.py` both
point `SCORESETS_DIR` at the shared `Settings/scoring/` folder. That agreement is
load-bearing, so it is written down here and asserted below rather than left to be discovered
when one of the two changes.
"""

import json
import logging
import sys

import paths

log = logging.getLogger("simlayer")

sys.path.insert(0, str(paths.SIMULATION_DIR))

import copula as copula_module      # noqa: E402
import goalies as goalies_module    # noqa: E402
import sampler as sampler_module    # noqa: E402
import scoring as scoring_module    # noqa: E402

_sim_paths = paths.SIMULATION_DIR / "paths.py"


def assert_scoreset_dirs_agree() -> None:
    """The load-bearing agreement, checked rather than assumed.

    `Simulation/scoring.py` resolves `paths.SCORESETS_DIR` against whichever `paths` module got
    imported first, which here is this folder's. If the two ever disagree, scoring files would
    silently be looked for in the wrong place -- so fail loudly instead.
    """
    text = _sim_paths.read_text(encoding="utf-8")
    if 'SCORESETS_DIR = PROJECT_ROOT.parent / "Settings" / "scoring"' not in text:
        raise RuntimeError(f"{_sim_paths} no longer points SCORESETS_DIR at Settings/scoring; "
                           f"Simulation/scoring.py resolves it against Season/paths.py under the "
                           f"flat-module convention, so the two have to agree. See simlayer.py.")
    if not paths.SCORESETS_DIR.exists():
        raise RuntimeError(f"{paths.SCORESETS_DIR} does not exist, so no scoring file can load")


def load_scoreset(name):
    assert_scoreset_dirs_agree()
    return scoring_module.load(paths.scoreset(name))


def load_goalie_fit(season):
    """The goalie sampler's fitted numbers FOR `season` (fitted on the seasons before it)."""
    path = paths.goalie_fit_path(season)
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing -- run Simulation/goalie_fit.py --season "
                                f"{season}")
    fit = goalies_module.GoalieFit.load(path)
    if season in fit.payload["trained_on"]:
        raise AssertionError(f"{path.name} was trained on {season}, the season it is for")
    log.info("goalie sampler: fit on %s", ", ".join(fit.payload["trained_on"]))
    return fit


def build_simulator(season, seed=90210, independent=False):
    """The calibrated sampler, assembled from the two fitted JSONs rather than from `simulate.py`.

    This mirrors `Simulation/simulate.py:build_simulator` deliberately -- if that function's
    construction changes, this one has to change with it. The alternative was importing it, which
    the module docstring explains is not available.
    """
    dispersion_path, correlations_path = paths.dispersion_path(season), paths.correlations_path(season)
    if not dispersion_path.exists():
        raise FileNotFoundError(f"{dispersion_path} is missing -- it is fitted by "
                                f"Projections/calibrate.py --season {season}")
    if not correlations_path.exists():
        raise FileNotFoundError(f"{correlations_path} is missing -- run "
                                f"Simulation/correlations.py first. Refit it whenever the "
                                f"projection models are retrained: these are residual "
                                f"correlations and they belong to a particular fit.")

    dispersion = json.loads(dispersion_path.read_text(encoding="utf-8"))
    thetas = {name: spec["theta"] for name, spec in dispersion["categories"].items()}
    payload = json.loads(correlations_path.read_text(encoding="utf-8"))
    structure = (copula_module.independent() if independent
                 else copula_module.load(correlations_path))
    penalties = payload["penalty_incidents"]

    log.info("simulator: dispersion from %s, structure from %s (fit on %s)",
             dispersion_path.name, correlations_path.name,
             payload.get("source", "unknown"))
    return sampler_module.Simulator(thetas, structure, penalties["weights"],
                                    penalties.get("latent_variance", 0.0), seed=seed)

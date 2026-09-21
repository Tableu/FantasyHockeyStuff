"""Turning sampled stat lines into points, for whatever league is asked about.

The projection layer is scoring-agnostic and so is the sampler: what a goal is worth is
data, supplied as a JSON file, and one set of draws can be scored under several leagues at
once. `Projections/scoresets/` holds the examples; none is a default and nothing loads one
automatically.

The file format is the one `Projections/weights.py` reads, and deliberately so -- the same
file has to mean the same thing to the model report and to the simulator. One difference in
*interpretation*: there, `ppp` and `shp` are shares of a projected point, because the model
predicts a mean. Here they are counts, because a draw has already decided how many of this
player's points were power-play points.
"""

import json
from pathlib import Path

import numpy as np

import paths

SKATER_QUANTITIES = ["goals", "assists", "shots", "hits", "blocks", "pim", "ppp", "shp"]


class ScoreSet:
    """One league's scoring, loaded from JSON."""

    def __init__(self, payload: dict, source: Path | None = None):
        self.name = payload.get("name") or (source.stem if source else "unnamed")
        self.description = payload.get("description", "")
        self.skaters = {k: float(v) for k, v in (payload.get("skaters") or {}).items()}
        self.goalies = {k: float(v) for k, v in (payload.get("goalies") or {}).items()}
        self.source = source
        unknown = [k for k in self.skaters if k not in SKATER_QUANTITIES]
        if unknown:
            raise ValueError(f"{self.name}: unknown skater quantities {unknown}; "
                             f"known: {SKATER_QUANTITIES}")

    def __repr__(self):
        return f"ScoreSet({self.name!r}, {len(self.skaters)} skater weights)"

    def describe(self):
        return ", ".join(f"{k} {v:g}" for k, v in self.skaters.items())

    def score_draws(self, draws):
        """Points per [row, sim] for sampled stat lines."""
        total = None
        for quantity, weight in self.skaters.items():
            if quantity not in draws.counts:
                continue
            contribution = draws[quantity].astype("float32") * np.float32(weight)
            total = contribution if total is None else total + contribution
        if total is None:
            raise ValueError(f"{self.name} scores none of the sampled categories")
        return total

    def score_columns(self, frame, prefix=""):
        """Points per row for a frame of counts -- the actuals, or a lambda table."""
        total = np.zeros(len(frame))
        for quantity, weight in self.skaters.items():
            column = f"{prefix}{quantity}"
            if column in frame.columns:
                total += frame[column].fillna(0.0).to_numpy("float64") * weight
        return total


def load(path) -> ScoreSet:
    path = Path(path)
    if not path.exists():
        candidate = paths.SCORESETS_DIR / f"{path.stem}.json"
        if not candidate.exists():
            raise FileNotFoundError(f"no scoring file at {path} or {candidate}")
        path = candidate
    return ScoreSet(json.loads(path.read_text(encoding="utf-8")), path)


def available():
    return sorted(paths.SCORESETS_DIR.glob("*.json")) if paths.SCORESETS_DIR.exists() else []

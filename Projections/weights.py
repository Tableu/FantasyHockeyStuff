"""Applying a scoring system to a stat line -- mechanism only, no league baked in.

The models in this folder project **stats**, never points. Nothing here, and nothing in
`train.py` or `predict.py`, knows what a goal is worth. A scoring system is data the caller
supplies, so the same fitted models serve any number of leagues:

    python evaluate.py --weights scoresets/points-league.json
    python evaluate.py --weights scoresets/banger-league.json

`scoresets/` holds example files. None of them is a default and nothing loads one
automatically -- if no weights are given, only per-category metrics are reported.

A scoring file is JSON:

    {
      "name": "...",
      "description": "...",
      "skaters": {"goals": 4.0, "assists": 2.5, "ppp": 1.0, ...},
      "goalies": {"wins": 3.0, ...}
    }

Keys under `skaters` name a projected quantity. `ppp` and `shp` are bonuses layered on the
underlying point, which is why the projection carries them as *shares* of a point rather than
as independent counts -- see `targets.py`.
"""

import json
from pathlib import Path

import pandas as pd

import paths

# Quantities a scoring file may reference, and how to read them off a projection frame.
# `share` entries are a fraction of the player's points, not a count.
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

    def describe(self) -> str:
        return ", ".join(f"{k} {v:g}" for k, v in self.skaters.items())

    def score(self, stats: pd.DataFrame, prefix: str = "") -> pd.Series:
        """Points per row for a stat line under this scoring.

        `prefix` picks the column family, so the same call scores actuals (`target_`) and
        projections (`lambda_`).
        """
        total = pd.Series(0.0, index=stats.index)
        for quantity, weight in self.skaters.items():
            column = f"{prefix}{quantity}"
            if column in stats.columns:
                total += stats[column].fillna(0.0) * weight
        return total


def load(path) -> ScoreSet:
    path = Path(path)
    if not path.exists():
        candidate = paths.SCORESETS_DIR / f"{path.stem}.json"
        if candidate.exists():
            path = candidate
        else:
            raise FileNotFoundError(f"no scoring file at {path} or {candidate}")
    return ScoreSet(json.loads(path.read_text(encoding="utf-8")), path)


def available() -> list[Path]:
    """Example scoring files shipped alongside, for `--list-scoresets`."""
    return sorted(paths.SCORESETS_DIR.glob("*.json")) if paths.SCORESETS_DIR.exists() else []

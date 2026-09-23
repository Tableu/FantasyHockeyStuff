"""Turning sampled stat lines into points, for whatever league is asked about.

The projection layer is scoring-agnostic and so is the sampler: what a goal is worth is
data, supplied as a JSON file, and one set of draws can be scored under several leagues at
once. `LeagueSettings/scoring/` holds the scoring files; none is a default and nothing loads one
automatically.

The file format is the one `Projections/weights.py` reads, and deliberately so -- the same
file has to mean the same thing to the model report and to the simulator. One difference in
*interpretation*: there, `ppp` and `shp` are shares of a projected point, because the model
predicts a mean. Here they are counts, because a draw has already decided how many of this
player's points were power-play points.

Skaters and goalies score through the same two methods, picked with `side`. They are kept
apart because they are different stat lines, not different weights on one: `wins` and
`shutouts` are per-start indicators and a goalie's `goals_against` is a *cost*, the one
negative quantity anywhere in this format. That asymmetry is the whole reason goalies matter
to a head-to-head matchup out of proportion to their mean -- a skater's floor is 0.00 and a
pulled goalie's is deeply negative.

A scoreset need not price every category, so both `scored()` and the CLI report which
quantities a given file actually reaches. A weight the frame has no column for and a category
the league genuinely does not score look identical once they are summed, and one of those is
a bug.
"""

import json
from pathlib import Path

import numpy as np

import paths

SKATER_QUANTITIES = ["goals", "assists", "shots", "hits", "blocks", "pim", "ppp", "shp"]

# Per-start goalie quantities, as `ModelFeatures/build_goalie_starts.py` emits them: the three
# decisions as indicators (a start has exactly one, or none if he was pulled without one),
# `shutouts` as a bonus layered on a win, and the two volume terms whose ratio is the whole of
# goalie value -- per-shot worth is `saves_weight * SV% + goals_against_weight`.
GOALIE_QUANTITIES = ["wins", "losses", "ot_losses", "shutouts", "saves", "goals_against"]

SIDES = {"skaters": SKATER_QUANTITIES, "goalies": GOALIE_QUANTITIES}


class ScoreSet:
    """One league's scoring, loaded from JSON."""

    def __init__(self, payload: dict, source: Path | None = None):
        self.name = payload.get("name") or (source.stem if source else "unnamed")
        self.description = payload.get("description", "")
        self.skaters = {k: float(v) for k, v in (payload.get("skaters") or {}).items()}
        self.goalies = {k: float(v) for k, v in (payload.get("goalies") or {}).items()}
        self.source = source
        for side in SIDES:
            unknown = [k for k in self.weights(side) if k not in SIDES[side]]
            if unknown:
                raise ValueError(f"{self.name}: unknown {side[:-1]} quantities {unknown}; "
                                 f"known: {SIDES[side]}")

    def __repr__(self):
        return (f"ScoreSet({self.name!r}, {len(self.skaters)} skater / "
                f"{len(self.goalies)} goalie weights)")

    def weights(self, side="skaters") -> dict:
        if side not in SIDES:
            raise ValueError(f"side must be one of {sorted(SIDES)}, not {side!r}")
        return self.skaters if side == "skaters" else self.goalies

    def describe(self, side="skaters") -> str:
        return ", ".join(f"{k} {v:g}" for k, v in self.weights(side).items())

    def scored(self, side="skaters") -> list:
        """The quantities this file prices, in the canonical order.

        Reported rather than assumed: `banger-league.json` prices no `losses` and no
        `ot_losses`, so a goalie's decision there is upside only. That is a legitimate league
        and an easy silent bug, and the difference is visible only if something says so.
        """
        priced = self.weights(side)
        return [q for q in SIDES[side] if q in priced]

    def missing(self, side="skaters") -> list:
        """The quantities this file leaves unpriced -- the other half of `scored`."""
        priced = self.weights(side)
        return [q for q in SIDES[side] if q not in priced]

    def score_draws(self, draws, side="skaters"):
        """Points per [row, sim] for sampled stat lines."""
        total = None
        for quantity, weight in self.weights(side).items():
            if quantity not in draws.counts:
                continue
            contribution = draws[quantity].astype("float32") * np.float32(weight)
            total = contribution if total is None else total + contribution
        if total is None:
            raise ValueError(f"{self.name} scores none of the sampled {side} categories; "
                             f"it prices {self.scored(side)} and the draws carry "
                             f"{sorted(draws.counts)}")
        return total

    def score_columns(self, frame, prefix="", side="skaters"):
        """Points per row for a frame of counts -- the actuals, or a lambda table."""
        total = np.zeros(len(frame))
        for quantity, weight in self.weights(side).items():
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

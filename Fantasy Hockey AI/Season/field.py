"""The simulated opponents: how the managers who are not ours draft.

Assumptions about the other managers live in `Settings/field.json`, apart from our own strategy
(`strategy.json`) and the league's rules (`rosters/`). Like every settings file it has no code
defaults: a key the file forgets is an error at load.

    opponent_board = "last_season"      every non-VOR seat drafts from last season's fantasy totals
                                        under this league's scoring -- a naive autodraft
    opponent_board = "source_subsets"   each non-VOR seat draws 1-3 (`sources_per_opponent`) of
                                        the season's external projection sources, seeded by
                                        (replication, seat), and drafts by value over replacement
                                        on their consensus (`Decisions/draft.source_board`)

A real leaguemate reads one to three rankers rather than every source, so the room agrees on the
stars and disagrees deeper in the draft, as a real room does. ADP is deliberately not used: it is
built for standard formats, and this league's hits and blocks make its values different.

Seeded by (replication, seat) only, so the shipped run and a candidate run in `tune.py`'s
seat-paired design face identical opponents.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

import paths

BOARDS = ("last_season", "source_subsets")
SEED = 20260924


@dataclass(frozen=True)
class FieldConfig:
    name: str
    opponent_board: str
    sources_per_opponent: tuple          # (fewest, most), inclusive
    max_goalies: int                     # an opponent never drafts more goalies than this

    def describe(self) -> str:
        if self.opponent_board == "last_season":
            return "opponents draft by last season's totals"
        lo, hi = self.sources_per_opponent
        return (f"opponents draft by {lo}-{hi} external sources each, at most "
                f"{self.max_goalies} goalies")


def parse(payload: dict, name: str = "") -> FieldConfig:
    keys = {"description", "opponent_board", "sources_per_opponent", "opponent_max_goalies"}
    if set(payload) != keys:
        raise ValueError(f"field {name}: needs exactly {sorted(keys)}; got {sorted(payload)}")
    if payload["opponent_board"] not in BOARDS:
        raise ValueError(f"field {name}: opponent_board {payload['opponent_board']!r}; use one of "
                         f"{BOARDS}")
    span = payload["sources_per_opponent"]
    if (not isinstance(span, list) or len(span) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) for v in span)
            or not 1 <= span[0] <= span[1]):
        raise ValueError(f"field {name}: sources_per_opponent must be [fewest, most] with "
                         f"1 <= fewest <= most; got {span!r}")
    cap = payload["opponent_max_goalies"]
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 2:
        raise ValueError(f"field {name}: opponent_max_goalies must be a whole number >= 2 (the "
                         f"league starts two); got {cap!r}")
    return FieldConfig(name=name, opponent_board=payload["opponent_board"],
                       sources_per_opponent=(span[0], span[1]), max_goalies=cap)


def load(name: str | None = None) -> FieldConfig:
    path = paths.field_config(name) if name else paths.FIELD_CONFIG
    return parse(json.loads(Path(path).read_text(encoding="utf-8")), name=Path(path).stem)


def draw_sources(sources, span, replication: int, seat: int) -> tuple:
    """Which sources this seat reads in this replication: a seeded draw of between span[0] and
    span[1] of them, without replacement, all equally likely."""
    names = sorted(sources)
    rng = np.random.default_rng([SEED, int(replication), int(seat)])
    lo, hi = span
    k = int(rng.integers(lo, min(hi, len(names)) + 1))
    return tuple(sorted(rng.choice(names, size=k, replace=False).tolist()))


def with_overrides(config: FieldConfig, board=None, sources=None) -> FieldConfig:
    """Command-line overrides: a board kind, and a source count as "2" or "1-3"."""
    from dataclasses import replace

    if board:
        config = replace(config, opponent_board=board)
    if sources:
        lo, _, hi = str(sources).partition("-")
        span = (int(lo), int(hi or lo))
        if not 1 <= span[0] <= span[1]:
            raise ValueError(f"--opponent-sources {sources!r}")
        config = replace(config, sources_per_opponent=span)
    return config

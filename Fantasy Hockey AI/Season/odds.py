"""Reading a saved boom/bust run (boom_bust.py writes it): the draft board, the assistant and the
draft window ask its odds at any pick without re-simulating.

Moved from boom_bust.py (2026-10-24) so the live side reads saved runs without importing the
simulation that writes them (the draft room, the field, the season draws). boom_bust.py imports
these back, so `boom_bust.load_odds` still works for its own checks.

    load_odds(season, scoring)   the saved run, or None when there is none
    Odds.at(player_ids, pick)    boom and bust for each player were he taken at `pick`
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

import paths


class Odds:
    """A saved run for one season and scoring: the per-player table, the par curve and the
    realized-VOR draws, so the odds can be asked at any pick without re-simulating."""

    COLUMNS = ("room_pick", "boom_pct", "bust_pct", "p10", "p90", "exp_gp")

    def __init__(self, season: str, scoring: str, reports=None):
        stem = Path(reports or paths.REPORTS_DIR) / f"boom_bust_{season}_{scoring}"
        self.table = pd.read_parquet(f"{stem}.parquet")
        self.par = pd.read_csv(f"{stem}_par.csv")["par"].to_numpy()
        self.vor = np.load(f"{stem}_vor_draws.npy", mmap_mode="r")
        self.col = {int(p): j for j, p in
                    enumerate(pd.read_csv(f"{stem}_players.csv")["player_id"])}
        self.meta = json.loads(Path(f"{stem}_meta.json").read_text())
        self.margin = int(self.meta["margin"])

    def fits(self, teams: int, picks_total: int) -> bool:
        """Whether this run's room matches a league of `teams` and `picks_total` picks."""
        return self.margin == teams and len(self.par) == picks_total

    def at(self, player_ids, pick: int) -> pd.DataFrame:
        """Boom and bust for each player were he taken at `pick`; NaN for a player with no draws."""
        p = int(pick)
        later = self.par[min(p + self.margin, len(self.par)) - 1]
        earlier = self.par[max(p - self.margin, 1) - 1]
        ids = [int(i) for i in player_ids]
        known = [i for i in ids if i in self.col]
        out = pd.DataFrame(index=pd.Index(ids, name="player_id"), columns=["boom", "bust"],
                           dtype=float)
        if known:
            draws = np.asarray(self.vor[:, [self.col[i] for i in known]])
            out.loc[known, "boom"] = (draws >= earlier).mean(axis=0)
            out.loc[known, "bust"] = (draws < later).mean(axis=0)
        return out


def load_odds(season: str, scoring: str):
    """The saved run for `season` and `scoring`, or None when there is none."""
    stem = paths.REPORTS_DIR / f"boom_bust_{season}_{scoring}"
    if not Path(f"{stem}.parquet").exists():
        return None
    return Odds(season, scoring)

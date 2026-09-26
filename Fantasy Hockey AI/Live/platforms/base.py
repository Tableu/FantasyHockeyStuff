"""What every adapter returns, in platform ids, the one map from those to our PlayerIDs, and the
draft rehearsal that works on any of them."""

import dataclasses
import time

import pandas as pd

import livepaths


@dataclasses.dataclass
class TeamRoster:
    """One fantasy team's holdings: platform player ids, IR apart from the rest, and -- when the
    platform shows it -- which lineup slot each rostered player is in ({slot label: [ids]})."""
    team_id: int
    name: str
    roster: list
    ir: list
    lineup: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Matchup:
    """This scoring period's head-to-head: both teams' points so far."""
    period: int
    team_id: int
    opponent_id: int | None
    points: float
    opponent_points: float


class Replay:
    """A rehearsal of draft night on a finished draft (--replay-season): every poll really reads the
    platform -- the adapter built for that past season -- but the picks not yet "made" are hidden,
    one revealed every `seconds` from `start` on, so polling, parsing, matching and redraws all run
    as they will on draft night. Any adapter with a past-season draft_board() works."""

    def __init__(self, adapter, seconds: float = 5.0, start: int = 0):
        self.adapter, self.seconds, self.start, self.t0 = adapter, float(seconds), int(start), None

    def draft_board(self) -> dict:
        board = self.adapter.draft_board()
        if self.t0 is None:
            self.t0 = time.time()
        made = self.start + int((time.time() - self.t0) / self.seconds)
        for row in board.get("rows", []):
            for cell in row.get("cells", []):
                if cell["slot"]["overall"] > made:
                    cell.pop("player", None)
        return board

    def __getattr__(self, name):
        return getattr(self.adapter, name)


class PlayerIds:
    """A platform's player id -> our PlayerID, from ModelFeatures' platform_ids.parquet (written by
    the pipeline's importers into Fantasy.PlatformPlayerIDs). A platform's ids are stable across
    seasons, so the newest season's map is used for any season read. An id with no PlayerID is
    reported, never guessed."""

    def __init__(self, platform: str):
        ids = pd.read_parquet(livepaths.platform_ids())
        ids = ids[ids["platform"].str.lower() == platform.lower()].sort_values("season")
        self.platform = platform
        self.map = dict(zip(ids["external_id"].astype(str), ids["player_id"].astype(int)))

    def get(self, external_id):
        return self.map.get(str(external_id))

    def resolve(self, external_ids) -> tuple:
        """(PlayerIDs found, external ids with none)."""
        found, missing = [], []
        for external_id in external_ids:
            player_id = self.get(external_id)
            (found if player_id is not None else missing).append(player_id if player_id is not None
                                                                  else external_id)
        return found, missing

"""What every adapter returns, in platform ids, and the one map from those to our PlayerIDs."""

import dataclasses

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

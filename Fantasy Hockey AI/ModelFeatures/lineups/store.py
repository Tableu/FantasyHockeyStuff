"""In-memory view of Lineups.GameLineups and Injuries.Spells for the training-data code
(calibration / perturb / features): one TeamGame per (team, game) in schedule order, so
"the team's previous game" and "was this player injured on that date" are plain lookups.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date


@dataclass
class PlayerLineup:
    position: str
    dressed: bool
    line: int | None
    pair: int | None
    pp: int | None
    pk: int | None
    starting_goalie: bool

    @property
    def is_forward(self) -> bool:
        return self.position in ("C", "L", "R")

    @property
    def is_defence(self) -> bool:
        return self.position == "D"

    @property
    def is_goalie(self) -> bool:
        return self.position == "G"

    def copy(self) -> "PlayerLineup":
        return PlayerLineup(self.position, self.dressed, self.line, self.pair, self.pp, self.pk, self.starting_goalie)


@dataclass
class TeamGame:
    game_id: int
    nhl_game_id: int
    game_date: date
    season_id: int
    team_id: int
    players: dict = field(default_factory=dict)   # {PlayerID: PlayerLineup}
    had_pp: bool = False
    had_pk: bool = False

    def unit_members(self, attr: str, rank: int) -> set:
        return {p for p, pl in self.players.items() if getattr(pl, attr) == rank}

    def mates(self, player_id: int, attr: str) -> set:
        rank = getattr(self.players[player_id], attr)
        if rank is None:
            return set()
        return self.unit_members(attr, rank) - {player_id}


def load_team_games(cursor, season_ids: list) -> dict:
    """{TeamID: [TeamGame, ...]} in game-date order, for the given SeasonIDs."""
    placeholders = ",".join("?" * len(season_ids))
    cursor.execute(
        f"""
        SELECT l.GameID, g.NHLGameID, g.GameDate, g.SeasonID, l.TeamID, l.PlayerID, l.PositionCode, l.Dressed,
               l.ForwardLine, l.DefensePair, l.PowerPlayUnit, l.PenaltyKillUnit, l.IsStartingGoalie,
               l.TeamHadPowerPlay, l.TeamHadPenaltyKill
        FROM Lineups.GameLineups l
        JOIN Game.Games g ON g.GameID = l.GameID
        WHERE g.SeasonID IN ({placeholders})
        ORDER BY l.TeamID, g.GameDate, g.NHLGameID
        """,
        *season_ids,
    )
    by_team: dict = defaultdict(list)
    current: TeamGame | None = None
    for r in cursor.fetchall():
        if current is None or current.team_id != r.TeamID or current.game_id != r.GameID:
            current = TeamGame(r.GameID, r.NHLGameID, r.GameDate, r.SeasonID, r.TeamID, had_pp=bool(r.TeamHadPowerPlay), had_pk=bool(r.TeamHadPenaltyKill))
            by_team[r.TeamID].append(current)
        current.players[r.PlayerID] = PlayerLineup(
            r.PositionCode, bool(r.Dressed), r.ForwardLine, r.DefensePair, r.PowerPlayUnit, r.PenaltyKillUnit, bool(r.IsStartingGoalie),
        )
    return dict(by_team)


def load_injury_spells(cursor, season_ids: list) -> dict:
    """{(TeamID, PlayerID): [(StartDate, EndDate), ...]} for dated spells in the seasons."""
    placeholders = ",".join("?" * len(season_ids))
    cursor.execute(
        f"SELECT TeamID, PlayerID, StartDate, EndDate FROM Injuries.Spells "
        f"WHERE SeasonID IN ({placeholders}) AND PlayerID IS NOT NULL AND StartDate IS NOT NULL",
        *season_ids,
    )
    spells: dict = defaultdict(list)
    for r in cursor.fetchall():
        spells[(r.TeamID, r.PlayerID)].append((r.StartDate, r.EndDate))
    return dict(spells)


def load_player_positions(cursor) -> dict:
    """{PlayerID: PositionCode} from Reference.Players -- the fallback for a candidate whose
    position no lineup in the window reveals (an injured player who never dressed in it).
    Position is a static player attribute, so reading it leaks nothing about the game."""
    cursor.execute("SELECT PlayerID, PositionCode FROM Reference.Players WHERE PositionCode IS NOT NULL")
    return {r.PlayerID: r.PositionCode for r in cursor.fetchall()}


def injured_on(spells: dict, team_id: int, player_id: int, on_date: date) -> bool:
    """Inside a spell on that date: REALIZED absence. A spell's StartDate is the first game he
    missed, so this is true on that first game too -- which a lockout cannot always know. Use it
    for labels, never for a lockout-time feature (see `injured_known_on`)."""
    return any(start <= on_date <= end for start, end in spells.get((team_id, player_id), ()))


def injured_known_on(spells: dict, team_id: int, player_id: int, on_date: date) -> bool:
    """Known to be out at the lockout on that date: inside a spell that had ALREADY cost him a
    game. The spell's first game is unknown -- 90% of those players dressed the game before
    (2025-26: 885 rows), and whether the absence was announced before the lock is not in the
    history. Conservative on purpose, like the lineup noise: a backtest should understate what
    the live injury feed will know, not overstate it."""
    return any(start < on_date <= end for start, end in spells.get((team_id, player_id), ()))

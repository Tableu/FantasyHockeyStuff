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


# Spells come from ONE source per season, or every injury would count twice: the NHL Injury Viz
# history up to 2025-26, and from 2026-27 the spells snapshot_live's reports build night by night
# (pipeline build_live_spells). A later Injury Viz import of a live season is a check, not a source.
LIVE_SPELL_SOURCE = "Live snapshots"
FIRST_LIVE_SEASON_START_YEAR = 2026


def load_injury_spells(cursor, season_ids: list) -> dict:
    """{(TeamID, PlayerID): [(StartDate, EndDate, first_game_known), ...]} for dated spells in
    the seasons. `first_game_known` is True for a live spell: it only counts games he was
    reported out for BEFORE the lock, so its first game was knowable, unlike a history spell's."""
    placeholders = ",".join("?" * len(season_ids))
    cursor.execute(
        f"SELECT s.TeamID, s.PlayerID, s.StartDate, s.EndDate, src.SourceName "
        f"FROM Injuries.Spells s JOIN Injuries.Sources src ON src.SourceID = s.SourceID "
        f"JOIN Reference.Seasons x ON x.SeasonID = s.SeasonID "
        f"WHERE s.SeasonID IN ({placeholders}) AND s.PlayerID IS NOT NULL AND s.StartDate IS NOT NULL "
        f"AND (CASE WHEN x.StartYear >= ? THEN 1 ELSE 0 END) = "
        f"    (CASE WHEN src.SourceName = ? THEN 1 ELSE 0 END)",
        *season_ids, FIRST_LIVE_SEASON_START_YEAR, LIVE_SPELL_SOURCE,
    )
    spells: dict = defaultdict(list)
    for r in cursor.fetchall():
        spells[(r.TeamID, r.PlayerID)].append((r.StartDate, r.EndDate, r.SourceName == LIVE_SPELL_SOURCE))
    return dict(spells)


def merge_spells(spells: dict, extra: dict) -> dict:
    """`spells` with `extra`'s spells added (same {(TeamID, PlayerID): [(start, end, known)]} shape)."""
    merged = {key: list(value) for key, value in spells.items()}
    for key, value in extra.items():
        merged.setdefault(key, []).extend(value)
    return merged


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
    return any(start <= on_date <= end for start, end, _ in spells.get((team_id, player_id), ()))


def injured_known_on(spells: dict, team_id: int, player_id: int, on_date: date) -> bool:
    """Known to be out at the lockout on that date: inside a spell that had ALREADY cost him a
    game. The spell's first game is unknown -- 90% of those players dressed the game before
    (2025-26: 885 rows), and whether the absence was announced before the lock is not in the
    history. Conservative on purpose, like the lineup noise: a backtest should understate what
    the live injury feed will know, not overstate it. A live spell (2026-27 on) was reported
    before the lock by construction, so its first game counts."""
    return any((start <= on_date if known else start < on_date) and on_date <= end
               for start, end, known in spells.get((team_id, player_id), ()))

"""The league's rules, as data rather than constants.

Every layer below this one is parameterized over the scoring system; this one is parameterized
over the format, for the same reason. Section 0 fixes the target league, but the whole point of
the section 16 ladder is comparing managers under one set of rules, and a rule that lives in a
literal somewhere in `engine.py` cannot be varied to find out which rule the result depended
on.

Slot keys are the platform's own slot names -- `C`, `LW`, `RW`, `D`, `G` -- because eligibility
comes from the platform rather than from the NHL. `Fantasy.PlayerPositions` carries it (exported
by `ModelFeatures/build_fantasy_positions.py`), and it is genuinely multi-position: on Yahoo
2026-27, 15% of players are eligible at more than one slot, most often LW/RW.

**That is what makes nightly slotting an assignment problem rather than a sort.** With one
position per player the problem decomposes -- the C slots cannot take a player the D slots
wanted, so filling each position with its own best `n` is optimal by construction. Multi-position
eligibility couples the slots, and a greedy fill can strand points on the bench: a C/LW taken by
a C slot may be exactly the player the second LW slot needed. `Decisions/slots.py` solves it properly.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import paths

SKATER_POSITIONS = ("C", "LW", "RW", "D")
GOALIE_POSITION = "G"
FORWARD_POSITIONS = frozenset({"C", "LW", "RW"})

# The platform's slot names against the NHL PositionCode the feature tables carry. Used only as
# the fallback for a player the platform has never listed -- 2.2% of the 2025-26 universe.
NHL_TO_FANTASY = {"C": "C", "L": "LW", "R": "RW", "D": "D", "G": "G"}

# **A slot is not a position.** Most slots accept exactly one, but a composite slot accepts a set,
# and a format with composite slots behaves qualitatively differently: a spare forward is no longer
# stranded because the two centre slots are full, so the effective bench shrinks and the nightly
# assignment couples every slot to every other. That is why this is a mapping rather than an
# identity, and why `Decisions/slots.py` tests set intersection rather than membership.
SLOT_POSITIONS = {
    "C": frozenset({"C"}),
    "LW": frozenset({"LW"}),
    "RW": frozenset({"RW"}),
    "D": frozenset({"D"}),
    "G": frozenset({"G"}),
    "F": FORWARD_POSITIONS,                                  # any forward
    "F/D": FORWARD_POSITIONS | frozenset({"D"}),             # any skater
    "UTIL": FORWARD_POSITIONS | frozenset({"D"}),            # the usual alias for F/D
}


@dataclass(frozen=True)
class LeagueConfig:
    name: str
    teams: int
    active_slots: dict
    bench: int
    ir: int
    moves_per_week: int
    regular_season_weeks: int
    playoff_rounds: int
    playoff_teams: int
    week_starts_on: str = "MON"
    moves_carry_over: bool = False
    eligibility_platform: str = "yahoo"
    eligibility_season: str = "2026-27"
    description: str = ""
    source: Path | None = field(default=None, compare=False)

    def __post_init__(self):
        if self.teams % 2:
            raise ValueError(f"{self.name}: {self.teams} teams cannot be paired into matchups")
        if self.playoff_teams > self.teams:
            raise ValueError(f"{self.name}: {self.playoff_teams} playoff teams of {self.teams}")
        # A bracket has to halve cleanly, or "8 teams over 3 rounds" hides a bye nobody chose.
        if self.playoff_teams != 2 ** self.playoff_rounds:
            raise ValueError(f"{self.name}: {self.playoff_teams} playoff teams do not fill "
                             f"{self.playoff_rounds} rounds (expected {2 ** self.playoff_rounds})")
        unknown = set(self.active_slots) - set(SLOT_POSITIONS)
        if unknown:
            raise ValueError(f"{self.name}: unknown slot codes {sorted(unknown)}; known slots are "
                             f"{sorted(SLOT_POSITIONS)}")

    @property
    def active(self) -> int:
        return sum(self.active_slots.values())

    @property
    def accepts(self) -> dict:
        """{slot code: the positions it will take} for this league's slots only."""
        return {slot: SLOT_POSITIONS[slot] for slot in self.active_slots}

    @property
    def active_skater_slots(self) -> int:
        return sum(count for slot, count in self.active_slots.items()
                   if GOALIE_POSITION not in SLOT_POSITIONS[slot])

    @property
    def active_goalie_slots(self) -> int:
        return sum(count for slot, count in self.active_slots.items()
                   if GOALIE_POSITION in SLOT_POSITIONS[slot])

    @property
    def composite_slots(self) -> int:
        """Slots accepting more than one position -- the ones that couple the assignment."""
        return sum(count for slot, count in self.active_slots.items()
                   if len(SLOT_POSITIONS[slot]) > 1)

    @property
    def roster_size(self) -> int:
        """Active plus bench. IR is deliberately excluded: a stashed player is off the roster,
        which is the whole reason stashing is a lever rather than a formality."""
        return self.active + self.bench

    @property
    def rostered_league_wide(self) -> int:
        return self.roster_size * self.teams

    def slot_order(self) -> list:
        """Slots as a flat list, one entry per fillable slot, in a stable order.

        This is the left-hand side of the nightly assignment: `Decisions/slots.py` matches these against
        eligible players. The order is stable so that two runs produce the same lineup when the
        assignment has ties, and carries no priority of its own -- the solver does not need one.
        """
        return [position for position in sorted(self.active_slots)
                for _ in range(self.active_slots[position])]

    def describe(self) -> str:
        slots = " ".join(f"{n}{p}" for p, n in self.active_slots.items())
        composite = (f", {self.composite_slots} of them composite" if self.composite_slots
                     else "")
        return (f"{self.name}: {self.teams} teams, {slots} active{composite}, {self.bench} bench, "
                f"{self.ir} IR, {self.moves_per_week} moves/week")


def load(path=None) -> LeagueConfig:
    path = paths.league_config(path) if path else paths.LEAGUE_CONFIG
    if not path.exists():
        raise FileNotFoundError(f"no league config at {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    known = {f for f in LeagueConfig.__dataclass_fields__ if f != "source"}
    unknown = set(payload) - known
    if unknown:
        raise ValueError(f"{path}: unknown league settings {sorted(unknown)}; known: "
                         f"{sorted(known)}")
    return LeagueConfig(source=path, **payload)

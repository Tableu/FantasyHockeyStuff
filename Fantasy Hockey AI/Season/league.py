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
# assignment couples every slot to every other. What each slot accepts is a league setting
# (`slot_positions` in the roster file), tested by set intersection in `Decisions/slots.py`.
POSITIONS = frozenset(SKATER_POSITIONS) | {GOALIE_POSITION}

# The rules the harness implements, and the values it can run. A setting outside these is refused
# at load rather than silently ignored -- a FAAB league run as rolling waivers would produce
# numbers that look like a result.
SUPPORTED_RULES = {
    "lineup_lock": {"daily"},           # lineups set every night before the lock
    "waivers": {"rolling"},             # claim priority rolls: a winner goes to the back
    "ir_eligible": {"injured"},         # IR takes a player in an injury spell at the lockout
}
MOVE_ACTIONS = ("add", "claim", "drop", "ir_stash", "ir_activate")

# A tied week: "split" gives each team half a win (a W-L-T record ranked by win percentage);
# "loss" gives neither team anything.
SUPPORTED_TIES = {"split", "loss"}
# The season's shape. Each block is required and checked key by key, like `rules`.
SUPPORTED_SCHEDULE = {"type": {"round_robin"}}
SUPPORTED_DRAFT = {"type": {"snake", "linear"}, "order": {"lottery"}}
SUPPORTED_PLAYOFFS = {"seeding": {"record"}, "tiebreak": {"points_for"}}
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def _check_block(league, label, block, supported, numeric):
    """A settings block has exactly its keys, a supported value for each choice, and a
    non-negative whole number for each count."""
    expected = set(supported) | set(numeric)
    if set(block) != expected:
        raise ValueError(f"{league}: {label} needs exactly {sorted(expected)}; got {sorted(block)}")
    for key, allowed in supported.items():
        if block[key] not in allowed:
            raise ValueError(f"{league}: {label}.{key} = {block[key]!r} is not supported by the "
                             f"harness yet (supported: {sorted(allowed)})")
    for key, minimum in numeric.items():
        value = block[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{league}: {label}.{key} must be a whole number >= {minimum}; "
                             f"got {value!r}")



@dataclass(frozen=True)
class LeagueConfig:
    name: str
    teams: int
    active_slots: dict
    bench: int
    ir: int
    moves_per_week: int
    # Days a dropped player spends on waivers before anyone may add him. Required, so a config
    # cannot silently inherit a default; the target league's real value is still to be checked.
    waiver_days: int
    # {slot code: [positions it accepts]} -- e.g. "F": ["C", "LW", "RW"]. Required.
    slot_positions: dict
    # League rules: lineup_lock, waivers, ir_eligible (see SUPPORTED_RULES) and move_cost, the
    # moves each action spends from the weekly budget ({"add": 1, "claim": 1, "drop": 0, ...}).
    rules: dict
    # How a tied week is scored: "split" or "loss" (SUPPORTED_TIES).
    ties: str
    # {"type": "round_robin", "regular_season_weeks": n, "week_starts_on": "MON"}.
    schedule: dict
    # {"type": "snake" | "linear", "order": "lottery", "keepers": 0}. Keepers are not modelled, so
    # only 0 is accepted.
    draft: dict
    # {"teams", "rounds", "weeks_per_round", "seeding", "tiebreak"}. Validated, not yet simulated:
    # the replay scores the regular season only.
    playoffs: dict
    moves_carry_over: bool = False
    eligibility_platform: str = "yahoo"
    eligibility_season: str = "2026-27"
    description: str = ""
    source: Path | None = field(default=None, compare=False)

    def __post_init__(self):
        if self.teams % 2:
            raise ValueError(f"{self.name}: {self.teams} teams cannot be paired into matchups")
        if self.ties not in SUPPORTED_TIES:
            raise ValueError(f"{self.name}: ties = {self.ties!r}; supported: "
                             f"{sorted(SUPPORTED_TIES)}")
        _check_block(self.name, "schedule", {k: v for k, v in self.schedule.items()
                                             if k != "week_starts_on"},
                     SUPPORTED_SCHEDULE, {"regular_season_weeks": 1})
        if self.schedule.get("week_starts_on") not in WEEKDAYS:
            raise ValueError(f"{self.name}: schedule.week_starts_on must be one of "
                             f"{list(WEEKDAYS)}; got {self.schedule.get('week_starts_on')!r}")
        _check_block(self.name, "draft", self.draft, SUPPORTED_DRAFT, {"keepers": 0})
        if self.draft["keepers"]:
            raise ValueError(f"{self.name}: keepers are not modelled; draft.keepers must be 0")
        _check_block(self.name, "playoffs", self.playoffs, SUPPORTED_PLAYOFFS,
                     {"teams": 2, "rounds": 1, "weeks_per_round": 1})
        if self.playoff_teams > self.teams:
            raise ValueError(f"{self.name}: {self.playoff_teams} playoff teams of {self.teams}")
        # A bracket has to halve cleanly, or "8 teams over 3 rounds" hides a bye nobody chose.
        if self.playoff_teams != 2 ** self.playoff_rounds:
            raise ValueError(f"{self.name}: {self.playoff_teams} playoff teams do not fill "
                             f"{self.playoff_rounds} rounds (expected {2 ** self.playoff_rounds})")
        undefined = set(self.active_slots) - set(self.slot_positions)
        if undefined:
            raise ValueError(f"{self.name}: active slot(s) {sorted(undefined)} have no entry in "
                             f"slot_positions; defined: {sorted(self.slot_positions)}")
        for slot, accepted in self.slot_positions.items():
            accepted = set(accepted)
            if not accepted or accepted - POSITIONS:
                raise ValueError(f"{self.name}: slot {slot} accepts {sorted(accepted)}; positions "
                                 f"are {sorted(POSITIONS)}")
            if GOALIE_POSITION in accepted and len(accepted) > 1:
                raise ValueError(f"{self.name}: slot {slot} mixes goalies and skaters, which the "
                                 f"harness does not support (it counts goalie slots separately)")
        missing = set(SUPPORTED_RULES) - set(self.rules) | ({"move_cost"} - set(self.rules))
        if missing:
            raise ValueError(f"{self.name}: rules is missing {sorted(missing)}")
        extra = set(self.rules) - set(SUPPORTED_RULES) - {"move_cost"}
        if extra:
            raise ValueError(f"{self.name}: unknown rule(s) {sorted(extra)}")
        for rule, allowed in SUPPORTED_RULES.items():
            if self.rules[rule] not in allowed:
                raise ValueError(f"{self.name}: {rule} = {self.rules[rule]!r} is not supported by "
                                 f"the harness yet (supported: {sorted(allowed)})")
        costs = self.rules["move_cost"]
        if set(costs) != set(MOVE_ACTIONS) or any(int(v) != v or v < 0 for v in costs.values()):
            raise ValueError(f"{self.name}: move_cost needs a non-negative whole number for each "
                             f"of {list(MOVE_ACTIONS)}; got {costs}")

    @property
    def regular_season_weeks(self) -> int:
        return self.schedule["regular_season_weeks"]

    @property
    def week_starts_on(self) -> str:
        return self.schedule["week_starts_on"]

    @property
    def playoff_teams(self) -> int:
        return self.playoffs["teams"]

    @property
    def playoff_rounds(self) -> int:
        return self.playoffs["rounds"]

    @property
    def playoff_weeks(self) -> int:
        return self.playoffs["rounds"] * self.playoffs["weeks_per_round"]

    def tie_share(self) -> float:
        """What each team gets for a tied week, in wins."""
        return 0.5 if self.ties == "split" else 0.0

    @property
    def active(self) -> int:
        return sum(self.active_slots.values())

    @property
    def accepts(self) -> dict:
        """{slot code: the positions it will take} for this league's slots only."""
        return {slot: frozenset(self.slot_positions[slot]) for slot in self.active_slots}

    @property
    def active_skater_slots(self) -> int:
        return sum(count for slot, count in self.active_slots.items()
                   if GOALIE_POSITION not in self.accepts[slot])

    @property
    def active_goalie_slots(self) -> int:
        return sum(count for slot, count in self.active_slots.items()
                   if GOALIE_POSITION in self.accepts[slot])

    @property
    def composite_slots(self) -> int:
        """Slots accepting more than one position -- the ones that couple the assignment."""
        return sum(count for slot, count in self.active_slots.items()
                   if len(self.accepts[slot]) > 1)

    def move_cost(self, action: str) -> int:
        """Moves an action spends from the week's budget (rules.move_cost)."""
        return int(self.rules["move_cost"][action])

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

"""What a player, and a swap, is worth over a forward window -- one estimator for both sides.

The ladder's rung 3 lost to a manager who never transacts partly because its two sides were not
valued the same way: a naive rate shrinks an unproven free agent toward the league mean while a
rostered player carries his own measured low rate, so the comparison favours the unknown. Every
comparison here therefore runs both players through the same function.

Two levels, and the second is the point of this module:

**A player** is worth his rate per team game times his team's games in the window. The rate
already carries P(plays), so a healthy scratch is not valued like a regular.

**A swap** is worth what it changes on the roster, night by night. A player only earns on nights
he is started, and whether he is started depends on who else plays that night and which slots
they can fill -- composite slots included. So the swap is priced by solving the lineup with and
without it on every night either player's team plays. Nights neither plays are unchanged and are
skipped, which keeps it to a handful of small assignment problems per candidate.
"""

import slots as slots_module

# Per-game sd/mean for a skater's fantasy points, measured over 465 candidates on three slates:
# median 0.912, mean 0.990. The same constant rung 4 uses for a player with no draw tonight.
PER_GAME_CV = 0.9


def rate(view, player_id, source="per_game") -> float:
    """Expected fantasy points per team game, P(plays) included.

    `source="ros"` reads the rest-of-season projection and falls back to the per-game carried
    rate for anyone it does not cover (goalies, and skaters with no rest-of-season row yet).
    """
    if source == "ros":
        value = view.ros_rate(player_id)
        if value is not None:
            return value
    return view.projected_rate(player_id)


def known(view, player_id, source="per_game") -> bool:
    """Whether anything has projected this player yet. Unknown is not zero: `rate` falls back to 0
    for a player with no row, which is fine for ranking a free agent nobody has seen but wrong for
    choosing whom to drop."""
    if source == "ros" and view.ros_rate(player_id) is not None:
        return True
    return view.projected_rate(player_id, default=None) is not None


def player_value(view, player_id, weeks_ahead, source="per_game") -> float:
    """His rate times his team's games from today through the window's end."""
    games = view.games_through(player_id, weeks_ahead=weeks_ahead)
    return rate(view, player_id, source) * games if games > 0 else 0.0


class RosterNights:
    """A roster's lineup value on each night of a window, computed once and reused per candidate.

    `values` is the rate each player is worth when he plays. Tonight a player flagged injured is
    left out; on later nights he is not, because a manager knows he is out today, not for how long.
    """

    def __init__(self, view, roster, values, weeks_ahead, slot_order, eligibility, accepts):
        self.view = view
        self.values = values
        self.weeks_ahead = weeks_ahead
        self.slot_order = slot_order
        self.eligibility = eligibility
        self.accepts = accepts
        self.roster = list(roster)
        self._nights = {}
        self.playing = {}                  # night -> players on the roster who play that night
        for player_id in self.roster:
            for night in self.nights(player_id):
                self.playing.setdefault(night, []).append(player_id)
        self.base = {night: self._value(players, night)
                     for night, players in self.playing.items()}

    def _value(self, players, night) -> float:
        today = night == self.view.day
        startable = {p: self.values[p] for p in players
                     if not (today and p in self.view.unavailable)}
        if not startable:
            return 0.0
        lineup = slots_module.assign(self.slot_order, startable, self.eligibility, self.accepts)
        return slots_module.total_value(lineup, startable)

    def removal_cost(self, player_id) -> float:
        """Lineup points over the window lost by taking `player_id` off this roster."""
        cost = 0.0
        for night in self.nights(player_id):
            players = [p for p in self.playing.get(night, []) if p != player_id]
            cost += self.base.get(night, 0.0) - self._value(players, night)
        return cost

    def nights(self, player_id) -> list:
        if player_id is None:              # no drop: an open roster spot
            return []
        if player_id not in self._nights:
            self._nights[player_id] = self.view.nights_through(player_id, self.weeks_ahead)
        return self._nights[player_id]

    def swap_gain(self, incoming, outgoing, from_day=None) -> float:
        """Lineup points over the window with `incoming` in place of `outgoing`, minus without.
        `outgoing=None` is an add into an open spot. `from_day` prices a swap that only happens
        later -- a waiver claim, awarded when the player clears -- so only nights from then on
        count, for both sides: the outgoing player keeps playing until the swap."""
        gain = 0.0
        for night in set(self.nights(incoming)) | set(self.nights(outgoing)):
            if from_day is not None and night < from_day:
                continue
            players = [p for p in self.playing.get(night, []) if p != outgoing]
            if night in self.nights(incoming):
                players.append(incoming)
            gain += self._value(players, night) - self.base.get(night, 0.0)
        return gain

    def swap_sd(self, incoming, outgoing) -> float:
        """A rough sd for that gain: both players' per-game spread over their games."""
        variance = (len(self.nights(incoming)) * self.values[incoming] ** 2
                    + len(self.nights(outgoing)) * self.values.get(outgoing, 0.0) ** 2)
        return PER_GAME_CV * variance ** 0.5

"""Streaming: spend the moves the upgrade rule leaves behind on this week's games.

Section 9's add/drop rule is an upgrader. It prices both sides of a swap over H weeks, so it
moves 0-2 times in most weeks and about five of the seven moves expire unused. That pricing is
right for a regular and wrong for a replaceable body: the incoming player's value after Sunday is
his own, but the drop loses the dropped player's entire forward value -- even when he could be
bought back from the pool next week for one move. So a one-week rental can never clear it.

This module prices a rental honestly, in three pieces:

1. **Streaming spots.** Rostered skaters whose forward value is below replacement
   (`valuation.player_value` minus the best free agent eligible at their positions, < 0), at
   most k, cheapest first -- not simply the roster's k cheapest. Recomputed
   every night and never stored, so a streamer who turns out good stops being the cheapest and is
   simply kept. Unknown players are never spots (unknown is not zero). Goalies are not streamed.

2. **The drop cost is what cannot be bought back.** Dropping a spot holder costs

       drop_cost = max(0, value_after_week(outgoing) - replacement_after_week(his positions))

   over weeks 1..H after this one -- about zero for a real streaming spot, large for a regular,
   which is what stops a stream from ever dropping one. The gain is priced on the ROSTER, this
   week only, with the same `RosterNights.swap_gain` the upgrade rule uses.

3. **Expiring moves lower the bar, never the floor.** A stream must clear

       bar(t) = max(drop_cost, lam * f(t)) + margin * sd(gain)

   with f(t) the share of the week still ahead after today (0 on the last night): lam is the
   shadow price of a move -- what the best stream later in the week would be worth -- and it falls
   to nothing as the moves expire. The bar can fall to `drop_cost` but not below (section 8).
   `reserve` moves, falling linearly to 0 by the last day, are held back for upgrades.

Optionally (`gate`), a stream's gain is scaled by phi(z)/phi(0) of the matchup z: a stream pays
only this week, so in head-to-head its worth is the change in P(win), which vanishes in a week
already decided. Upgrades are never gated -- they pay across weeks.

Nothing here reads a file or a table; it acts through the view's league state.
"""

import logging
import math
from dataclasses import dataclass

import valuation

log = logging.getLogger("streaming")


@dataclass(frozen=True)
class StreamParams:
    # No defaults: the values are settings, in Settings/strategy.json. See `strategy.py`.
    spots: int                  # k streaming spots; 0 turns streaming off (rung 7 == rung 5)
    reserve: int                # moves held back for upgrades on the week's first day, -> 0
    lam: float                  # points: the shadow price of a move at the start of the week
    margin: float               # sds of the week's gain a stream must also clear
    gate: bool                  # scale the gain by phi(z)/phi(0) of the matchup z
    flat: bool                  # hold the bar at lam/2 all week (the falling bar's average)
    claim: bool                 # may claim a rental off waivers (priced from his clear date)
    shortlist: int              # free agents priced on the roster per pass

    def describe(self) -> str:
        return (f"k={self.spots} r={self.reserve} lam={self.lam:g} ms={self.margin:g}"
                f"{' gate' if self.gate else ''}{' flat' if self.flat else ''}"
                f"{' claim' if self.claim else ''}")


def _week_share_left(view) -> float:
    """f(t): the share of the matchup week still ahead after today. 0 on its last night."""
    week = view.calendar.weeks[view.week - 1]
    span = (week.end - week.start).days
    return (week.end - view.day).days / span if span > 0 else 0.0


def reserve_today(view, params) -> int:
    return int(round(params.reserve * _week_share_left(view)))


def value_after_week(view, player_id, weeks, source) -> float:
    """His value over weeks 1..H after this one: the part of a drop a rental does not replace."""
    if weeks == 0:
        return 0.0
    return (valuation.player_value(view, player_id, weeks, source)
            - valuation.player_value(view, player_id, 0, source))


class Replacement:
    """The best free agent at a set of positions, over a window -- memoized per night."""

    def __init__(self, view, pool, weeks, source, eligibility, after_week):
        self.eligibility = eligibility
        value = value_after_week if after_week else valuation.player_value
        self.values = sorted(((value(view, p, weeks, source), p) for p in pool), reverse=True)
        self._memo = {}

    def at(self, positions, exclude=()) -> float:
        key = (frozenset(positions), frozenset(exclude))
        if key not in self._memo:
            self._memo[key] = next((v for v, p in self.values
                                    if p not in exclude
                                    and self.eligibility.get(p, frozenset()) & positions), 0.0)
        return self._memo[key]


def is_goalie(eligibility, player_id) -> bool:
    return "G" in eligibility.get(player_id, frozenset())


def spots(view, params, horizon, source, pool, eligibility) -> list:
    """Rostered skaters whose forward value is BELOW replacement -- the best free agent eligible
    at their positions, over the rest of the season -- cheapest first, at most k of them.

    Below replacement, not merely lowest on the roster. The first version took the k lowest, and
    a roster with no replaceable body still had k "spots": it rented away Fantilli (331 season
    points) on opening night and Brock Nelson (321) in week 4, because they happened to be the
    roster's cheapest. A player the pool cannot match is not a streaming spot, however cheap he is
    relative to his teammates, so a strong roster may have none at all.
    """
    if params.spots <= 0:
        return []
    # Over the REST OF THE SEASON, not the upgrade rule's H weeks. Over three weeks two fewer team
    # games made Brock Nelson "below" Pavel Zacha at an equal rate (3.05 vs 3.02; 219.7 vs 217.5
    # rest of season), and Hanifin below Letang on seven straight days with a HIGHER rate. A short
    # schedule gap is what a rental exploits, not what makes a player replaceable.
    horizon = None
    replacement = Replacement(view, pool, horizon, source, eligibility, after_week=False)
    scored = []
    for p in view.roster:
        if is_goalie(eligibility, p) or not valuation.known(view, p, source):
            continue
        over = (valuation.player_value(view, p, horizon, source)
                - replacement.at(eligibility.get(p, frozenset())))
        if over < 0.0:
            scored.append((over, p))
    return [p for _, p in sorted(scored)[:params.spots]]


def run(view, params: StreamParams, horizon, source, slot_order, accepts, fieldable,
        z=0.0) -> list:
    """Make this team's streams for today, after the upgrades. Returns what was done.

    `horizon` and `source` are the upgrade rule's (H weeks, rate source), so both of a manager's
    move decisions value a player the same way.
    """
    if params.spots <= 0:
        return []
    state = view._state
    eligibility = state.eligibility
    # The flat arm tests whether letting the bar fall as moves expire is doing anything.
    share_left = 0.5 if params.flat else _week_share_left(view)
    scale = math.exp(-0.5 * z * z) if params.gate else 1.0
    done, reserved = [], set()

    while view.moves_left - reserve_today(view, params) > 0:
        roster = list(view.roster)
        pool = [p for p in view.free_agents()
                if (params.claim or not view.on_waivers(p)) and p not in reserved
                and not is_goalie(eligibility, p)]
        holders = [p for p in spots(view, params, horizon, source, pool, eligibility)
                   if p not in reserved]
        outgoing_options = ([None] if view.roster_room() > 0 else []) + holders
        if not outgoing_options:
            break
        week_value = {p: valuation.player_value(view, p, 0, source) for p in pool}
        candidates = sorted((p for p in pool if week_value[p] > 0.0),
                            key=lambda p: week_value[p], reverse=True)[:params.shortlist]
        if not candidates:
            break

        rates = {p: valuation.rate(view, p, source) for p in roster + candidates}
        nights = valuation.RosterNights(view, roster, rates, 0, slot_order, eligibility, accepts)
        after = Replacement(view, pool, horizon, source, eligibility, after_week=True)
        cost = {}
        for o in outgoing_options:
            if o is None:
                cost[o] = 0.0
            else:
                cost[o] = max(0.0, value_after_week(view, o, horizon, source)
                              - after.at(eligibility.get(o, frozenset())))

        best = None
        for incoming in candidates:
            for outgoing in outgoing_options:
                if outgoing is not None and not fieldable(
                        [p for p in roster if p != outgoing] + [incoming], eligibility, roster):
                    continue
                # A claim is awarded when the player clears waivers, so it pays from then.
                clears = view.waiver_clears(incoming) if view.on_waivers(incoming) else None
                gain = scale * nights.swap_gain(incoming, outgoing, from_day=clears)
                floor = cost[outgoing]
                bar = (max(floor, params.lam * share_left)
                       + params.margin * nights.swap_sd(incoming, outgoing))
                if gain > bar and (best is None or gain - bar > best[0]):
                    best = (gain - bar, gain, bar, floor, incoming, outgoing)
        if best is None:
            break

        _, gain, bar, floor, incoming, outgoing = best
        reserved.update({incoming, outgoing} - {None})
        claiming = view.on_waivers(incoming)
        try:
            if claiming:
                state.submit_claim(view.team_index, incoming, drop=outgoing, today=view.day)
            else:
                state.add(view.team_index, incoming, view.day, drop=outgoing, reason="rental")
        except Exception as error:                     # noqa: BLE001 - state raises IllegalMove
            log.debug("team %d could not stream %s: %s", view.team_index, incoming, error)
            continue
        done.append({"day": view.day, "kind": "rental claim" if claiming else "rental",
                     "incoming": incoming,
                     "outgoing": outgoing, "predicted_gain": gain, "bar": bar,
                     "drop_cost": floor, "spot": outgoing in holders,
                     "reserve": reserve_today(view, params), "moves_left": view.moves_left,
                     "incoming_games": len(nights.nights(incoming))})
        if claiming:
            # The claim resolves later; stop pricing against a roster that may change first.
            break
    return done

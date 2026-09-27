"""Add / drop: when a free agent is worth a move, and whom he replaces.

Section 9 calls this the one decision that matters, and section 7 says why: rung 4 beats a naive
streamer by 9 points a week and still loses 7 a week to a manager who never transacts. The free
agent pool is what fourteen drafts left behind, so every acquisition trades talent for games; a
better ranking only picks the least-bad version of that trade. The rule has to be allowed to say
no, and to say it by default.

The rule, in the order it is applied:

1. **Shortlist** the best `shortlist` free agents by their own forward value, so the expensive
   step below runs on a handful of candidates rather than the whole pool.
2. **Price each against the cheapest few drops** that keep the roster fieldable, on the roster:
   `valuation.RosterNights.swap_gain` solves the lineup on every night either player's team plays.
3. **Act only past a margin**: the best pair must clear `margin` x its own sd. `margin = inf` is a
   manager that never moves -- the "hold" arm, and the point of the parameter: the same rule with
   the margin raised far enough *is* not transacting, so a tuned margin cannot do worse than that
   except by noise.
4. **Waiver claims** clear the same bar plus `claim_premium`, the points a claim must be worth to
   spend the top of the priority queue. On by default since 2026-09-23 at a premium of 0 -- a
   claim clears the same margin as an add, and priority is treated as free. Provisional: the
   premium is unmeasured (a sweep over {0, 5} is planned). Before that, the default was infinite
   and no rung ever claimed; `claim_premium=inf` restores that.
   A claim's gain counts only nights from the day the player clears waivers (as streaming's
   rentals always have); until 2026-09-26 it was priced from today.

Then repeat while moves remain. Nothing here reads a file or a table: it is handed a view, and
it acts through the view's league state, whose methods raise on an illegal move.
"""

import logging
import math
from dataclasses import dataclass

import valuation

log = logging.getLogger("adddrop")


@dataclass(frozen=True)
class AddDropParams:
    # No defaults: the values are settings, in Settings/strategy.json (its README says why
    # each is what it is). See `strategy.py`.
    horizon_weeks: int | None         # weeks past the current one both sides are priced over
    margin: float                     # sds of the gain a move must clear; inf never moves
    rate_source: str                  # "ros" (rest-of-season, holdout) or "per_game"
    claim_premium: float              # extra points a waiver claim must clear; inf never claims
    shortlist: int                    # free agents priced on the roster per pass
    drop_shortlist: int               # cheapest fieldable drops tried against each
    tail: str                         # value after the window: "none", "cost" or "net" (below)

    def __post_init__(self):
        if self.tail not in TAILS:
            raise ValueError(f"adddrop tail {self.tail!r}; use one of {TAILS}")

    def describe(self) -> str:
        return (f"H={self.horizon_weeks} m={self.margin:g} rate={self.rate_source} "
                f"claim={self.claim_premium:g}" + ("" if self.tail == "none" else f" tail={self.tail}"))


# What the rest of the season after the H-week window counts for. A swap is permanent, but the
# window prices it as if the season ended with the window: Byfield (3.73 pts/g, 9 games in the
# window) lost to Lundell (3.15, 12 games) on opening night 2026-27, and ~35 points of Byfield
# after the window never entered the sum.
#   none   ignored -- section 9's rule as shipped
#   cost   a drop must also cover what it loses after the window, tail(out) - tail(in) when
#          positive, added to the bar -- streaming's drop cost, for a permanent swap
#   net    both ways: gain += tail(in) - tail(out)
TAILS = ("none", "cost", "net")


def tail_value(view, player_id, params) -> float:
    """His value from the end of the window to the end of the season, at the rule's rate."""
    if player_id is None or params.horizon_weeks is None:
        return 0.0
    return (valuation.player_value(view, player_id, None, params.rate_source)
            - valuation.player_value(view, player_id, params.horizon_weeks, params.rate_source))


def price(view, params: AddDropParams, slot_order, accepts, fieldable, reserved=(),
          shortlist=None) -> list:
    """Every (incoming, outgoing) pair the rule prices today, best first by gain over its bar:
    [{"incoming", "outgoing", "gain", "bar", "claim", "rates", "nights"}]. `shortlist` widens the
    free agents priced past params.shortlist (the plan window's options); the drops tried per
    incoming stay params.drop_shortlist. A pair clears when gain > bar."""
    eligibility = view._state.eligibility
    roster = [p for p in view.roster if p not in view.ir]
    if not roster:
        return []
    pool = [p for p in view.free_agents()
            if not view.on_waivers(p) or not math.isinf(params.claim_premium)]
    pool = [p for p in pool if p not in reserved]
    forward = {p: valuation.player_value(view, p, params.horizon_weeks, params.rate_source)
               for p in pool + roster}
    candidates = sorted((p for p in pool if forward[p] > 0.0),
                        key=lambda p: forward[p], reverse=True)[:shortlist or params.shortlist]
    if not candidates:
        return []

    rates = {p: valuation.rate(view, p, params.rate_source) for p in roster + candidates}
    nights = valuation.RosterNights(view, roster, rates, params.horizon_weeks, slot_order,
                                    eligibility, accepts)
    # A rostered player with no rate yet is unknown, not worthless: never a drop candidate.
    drops = [d for d in sorted(roster, key=lambda p: forward[p])
             if d not in reserved and valuation.known(view, d, params.rate_source)]
    if view.roster_room() > 0:
        # An open spot (a stash made it) is the cheapest "drop" there is: nobody leaves. The
        # forced drop when the injured player returns is priced then, by manage_ir.
        drops = [None] + drops

    tails = {}

    def tail(p):
        if p not in tails:
            tails[p] = tail_value(view, p, params)
        return tails[p]

    pairs = []
    for incoming in candidates:
        tried = 0
        for outgoing in drops:
            if tried >= params.drop_shortlist:
                break
            if not fieldable([p for p in roster if p != outgoing] + [incoming], eligibility,
                             roster):
                continue
            tried += 1
            # A claim is awarded when the player clears waivers, so it pays only from then --
            # the streaming rule's pricing. Before 2026-09-26 it was priced from today.
            clears = view.waiver_clears(incoming) if view.on_waivers(incoming) else None
            gain = nights.swap_gain(incoming, outgoing, from_day=clears)
            bar = params.margin * nights.swap_sd(incoming, outgoing)
            if clears is not None:
                bar += params.claim_premium
            lost = 0.0
            if params.tail != "none":
                lost = tail(outgoing) - tail(incoming)
                if params.tail == "cost":
                    bar += max(0.0, lost)
                else:
                    gain -= lost
            pairs.append({"incoming": incoming, "outgoing": outgoing, "gain": gain, "bar": bar,
                          "claim": clears is not None, "rates": rates, "nights": nights,
                          "tail_lost": lost})
    # Stable, so equal margins keep the pricing order: the first priced wins, as it always has.
    pairs.sort(key=lambda q: q["gain"] - q["bar"], reverse=True)
    return pairs


def run(view, params: AddDropParams, slot_order, accepts, fieldable) -> list:
    """Make this team's moves for today. Returns what was done, for the manager's log.

    `fieldable(after, eligibility, before)` is the manager's own legality check -- a swap that
    leaves the roster able to fill fewer slots than it could before is not an improvement at any
    value. Relative, so a goalie on IR does not freeze every move (see `Manager._fieldable`).
    """
    if math.isinf(params.margin) or view.moves_left <= 0:
        return []
    state = view._state
    done, reserved = [], set()

    while view.moves_left > 0:
        pairs = price(view, params, slot_order, accepts, fieldable, reserved)
        best = next((q for q in pairs if q["gain"] > q["bar"]), None)
        if best is None:
            break

        incoming, outgoing, gain = best["incoming"], best["outgoing"], best["gain"]
        rates, nights = best["rates"], best["nights"]
        reserved.update({incoming, outgoing} - {None})
        try:
            if best["claim"]:
                state.submit_claim(view.team_index, incoming, drop=outgoing, today=view.day)
                kind = "claim"
            else:
                state.add(view.team_index, incoming, view.day, drop=outgoing, reason="upgrade")
                kind = "add"
        except Exception as error:                     # noqa: BLE001 - state raises IllegalMove
            log.debug("team %d could not take %s: %s", view.team_index, incoming, error)
            continue
        done.append({"day": view.day, "kind": kind, "incoming": incoming,
                     "outgoing": outgoing, "predicted_gain": gain,
                     "incoming_rate": rates[incoming], "outgoing_rate": rates.get(outgoing, 0.0),
                     "incoming_games": len(nights.nights(incoming)),
                     "outgoing_games": len(nights.nights(outgoing))})
        if kind == "claim":
            # A claim resolves tomorrow and does not spend today's move; stop pricing against a
            # roster that may be about to change.
            break
    return done

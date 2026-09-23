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
   spend the top of the priority queue. Infinite by default, which is how every rung behaved before.

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
    # Weeks past the current one both sides are priced over. 3, as section 9's plan set it: on the
    # 2025-26 sensitivity check (Season/docs/ladder-league_sens-*.md) H=1, 3 and the rest of the
    # season all cleared hold, with 3 and season within noise of each other and ahead of 1. That is
    # a sensitivity reading on the only clean holdout, not a tuned value -- section 11 tunes it.
    horizon_weeks: int | None = 3
    margin: float = 1.0               # sds of the gain a move must clear; inf never moves
    rate_source: str = "ros"          # "ros" (rest-of-season, holdout) or "per_game"
    claim_premium: float = math.inf   # extra points a waiver claim must clear
    shortlist: int = 10               # free agents priced on the roster per pass
    drop_shortlist: int = 4           # cheapest fieldable drops tried against each

    def describe(self) -> str:
        return (f"H={self.horizon_weeks} m={self.margin:g} rate={self.rate_source} "
                f"claim={self.claim_premium:g}")


def run(view, params: AddDropParams, slot_order, accepts, fieldable) -> list:
    """Make this team's moves for today. Returns what was done, for the manager's log.

    `fieldable(roster, eligibility)` is the manager's own legality check -- a swap that leaves an
    active slot unfillable is not an improvement at any value.
    """
    if math.isinf(params.margin) or view.moves_left <= 0:
        return []
    state = view._state
    eligibility = state.eligibility
    done, reserved = [], set()

    while view.moves_left > 0:
        roster = [p for p in view.roster if p not in view.ir]
        if not roster:
            break
        pool = [p for p in view.free_agents()
                if not view.on_waivers(p) or not math.isinf(params.claim_premium)]
        pool = [p for p in pool if p not in reserved]
        forward = {p: valuation.player_value(view, p, params.horizon_weeks, params.rate_source)
                   for p in pool + roster}
        candidates = sorted((p for p in pool if forward[p] > 0.0),
                            key=lambda p: forward[p], reverse=True)[:params.shortlist]
        if not candidates:
            break

        rates = {p: valuation.rate(view, p, params.rate_source) for p in roster + candidates}
        nights = valuation.RosterNights(view, roster, rates, params.horizon_weeks, slot_order,
                                        eligibility, accepts)
        # A rostered player with no rate yet is unknown, not worthless: never a drop candidate.
        drops = [d for d in sorted(roster, key=lambda p: forward[p])
                 if d not in reserved and valuation.known(view, d, params.rate_source)]

        best = None
        for incoming in candidates:
            tried = 0
            for outgoing in drops:
                if tried >= params.drop_shortlist:
                    break
                if not fieldable([p for p in roster if p != outgoing] + [incoming], eligibility):
                    continue
                tried += 1
                gain = nights.swap_gain(incoming, outgoing)
                bar = params.margin * nights.swap_sd(incoming, outgoing)
                if view.on_waivers(incoming):
                    bar += params.claim_premium
                if gain > bar and (best is None or gain - bar > best[0]):
                    best = (gain - bar, gain, incoming, outgoing)
        if best is None:
            break

        _, gain, incoming, outgoing = best
        reserved.update({incoming, outgoing})
        try:
            if view.on_waivers(incoming):
                state.submit_claim(view.team_index, incoming, drop=outgoing)
                kind = "claim"
            else:
                state.add(view.team_index, incoming, view.day, drop=outgoing, reason="upgrade")
                kind = "add"
        except Exception as error:                     # noqa: BLE001 - state raises IllegalMove
            log.debug("team %d could not take %s: %s", view.team_index, incoming, error)
            continue
        done.append({"day": view.day, "kind": kind, "incoming": incoming,
                     "outgoing": outgoing, "predicted_gain": gain,
                     "incoming_rate": rates[incoming], "outgoing_rate": rates[outgoing],
                     "incoming_games": len(nights.nights(incoming)),
                     "outgoing_games": len(nights.nights(outgoing))})
        if kind == "claim":
            # A claim resolves tomorrow and does not spend today's move; stop pricing against a
            # roster that may be about to change.
            break
    return done

"""The Matchup's live P(win): the plan's P(win) carried through tonight's games, between refreshes.

The plan's z (Decisions/managers.py _z) is the week's margin over its spread: points banked plus
what each side's starters are still expected to score, over the root of both sides' variance. Here
the same z is made again each time the Matchup reads it (once a minute while it is on screen),
with no new platform call and no plan run:

    banked     each day's points as the platform scored them, today's live points included
    tonight    each player the platform has in a counting slot today, by his game: not started,
               his whole projection; under way, his projection if he plays scaled by the share of
               regulation left (none if he has no box line -- not dressed); over, nothing. The
               variance per player: (CV x his mean)^2 for a game to come, scaled by the share left
               for one under way -- managers.FALLBACK_CV's measured sd/mean, the plan's own
               stand-in for a player with no draw
    later      each night after today as the plan projected it: its mean and its variance (the
               plan's matchup_projection, nights and night_var)

    p_win(h2h, projection, today)   {p_win, z, margin, sd, banked: {me, opp}} or None

None when it cannot be made the same way as the plan's: a day up to today the platform's points
were not read for, or a plan made before its projection kept each night's variance.
"""

import math
import statistics

CV = 0.9                # Decisions/managers.py FALLBACK_CV: a game's sd over its mean
Z_CLIP = 3.0            # Settings/strategy-*.json z_clip: the plan's z is clipped the same
STARTED = {"LIVE", "CRIT"}
FINAL = {"OFF", "FINAL"}
OUT_OF_LINEUP = ("BN", "IR")


def share_left(game: dict) -> float:
    """The share of regulation still to play in `game` (games.Games.day's game): 1 before it starts,
    0 once over or in overtime."""
    state = game.get("state")
    if state in FINAL:
        return 0.0
    if state not in STARTED:
        return 1.0
    period = game.get("period") or 1
    if game.get("period_type") not in (None, "REG") or period > 3:
        return 0.0
    if game.get("intermission"):
        elapsed = 20.0 * period
    else:
        try:
            minutes, seconds = (int(x) for x in (game.get("clock") or "20:00").split(":"))
        except ValueError:
            minutes, seconds = 20, 0
        elapsed = 20.0 * (period - 1) + 20.0 - (minutes + seconds / 60.0)
    return min(1.0, max(0.0, 1.0 - elapsed / 60.0))


def _tonight(players: list, today: str, side: str) -> tuple:
    """(mean, variance) of what `side`'s counting players still score today."""
    mean = var = 0.0
    for p in players:
        if p["side"] != side:
            continue
        e = p["days"].get(today)
        if not e or not e["counts"] or not e["game"] or not e["xpts"]:
            continue
        state = e["game"].get("state")
        if state in FINAL:
            continue
        if state in STARTED:
            if e["line"] is None:
                continue                 # his game is on and he has no line: not dressed
            # He plays: his mean if he plays, over the time left.
            full = e["xpts"] / e["p_plays"] if e["p_plays"] else e["xpts"]
            left = share_left(e["game"])
            mean += full * left
            var += (CV * full) ** 2 * left
        else:
            mean += e["xpts"]
            var += (CV * e["xpts"]) ** 2
    return mean, var


def p_win(h2h: dict, projection: dict | None, today: str) -> dict | None:
    """`h2h`: headtohead.view's answer; `projection`: the plan's matchup_projection; `today`: ISO."""
    # A plan from before night_var was kept has none; an empty one is a week with no nights left.
    if not projection or not all("night_var" in (projection.get(s) or {}) for s in ("me", "opp")):
        return None
    banked = {"me": 0.0, "opp": 0.0}
    for d in h2h["days"]:
        if d["date"] > today:
            continue
        got = d.get("platform")
        if not got or got.get("me") is None or got.get("opp") is None:
            return None
        banked["me"] += got["me"]
        banked["opp"] += got["opp"]
    total, var = {}, 0.0
    for side in ("me", "opp"):
        mean, v = _tonight(h2h["players"], today, side)
        nights, night_var = projection[side]["nights"], projection[side]["night_var"]
        mean += sum(m for d, m in nights.items() if d > today)
        v += sum(x for d, x in night_var.items() if d > today)
        total[side] = banked[side] + mean
        var += v
    margin = total["me"] - total["opp"]
    sd = math.sqrt(var)
    if sd <= 1e-9:
        p = 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
        z = Z_CLIP if margin > 0 else -Z_CLIP if margin < 0 else 0.0
    else:
        z = max(-Z_CLIP, min(Z_CLIP, margin / sd))
        p = statistics.NormalDist().cdf(z)
    return {"p_win": round(p, 3), "z": round(z, 2), "margin": round(margin, 2), "sd": round(sd, 2),
            "banked": {k: round(v, 2) for k, v in banked.items()}}

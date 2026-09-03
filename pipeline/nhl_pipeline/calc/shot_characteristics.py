"""Pure math: normalize shot coordinates to an 'attacking right' convention, then compute
distance/angle to the net. No DB access here -- called from ingest/shots.py, which has the
per-play homeTeamDefendingSide value in hand (that field isn't persisted anywhere in the
schema, so this has to happen at ingestion time, not as a later pass over stored rows).
"""

import math

NET_X = 89.0


def normalize_coordinates(x, y, home_defending_side: str | None, team_is_home: bool):
    """home_defending_side is the side of the rink the HOME team's own net is on for this
    play (it can flip between periods). The away team therefore attacks that same side,
    and the home team attacks the opposite side. Flip (x, y) so the shooting team's shot
    is expressed as if it were always attacking the right side of the rink.
    """
    if x is None or y is None or home_defending_side not in ("left", "right"):
        return x, y
    flip = (team_is_home and home_defending_side == "right") or (
        not team_is_home and home_defending_side == "left"
    )
    return (-x, -y) if flip else (x, y)


def compute_distance_angle(x, y):
    """Distance/angle to the net, assuming (x, y) already normalized to attacking-right."""
    if x is None or y is None:
        return None, None
    dx = NET_X - float(x)
    dy = float(y)
    distance = math.hypot(dx, dy)
    angle = math.degrees(math.atan2(abs(dy), dx)) if (dx != 0 or dy != 0) else 0.0
    return round(distance, 2), round(angle, 2)

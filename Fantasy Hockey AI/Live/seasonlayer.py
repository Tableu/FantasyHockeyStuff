"""The one place Live/ reaches into Season/ -- the shared league core, imported by bare name.

Live/ holds the tools that act on a real league (the draft board, the draft assistant and window,
the live runner). They share Season/'s league model rather than copying it:

    league      the league's rules, as data (Settings/rosters/)
    state       rosters, the move budget, waivers, IR legality
    view        what a manager may see
    schedule    the calendar and matchup weeks
    engine      goalie_line only (the league-average goalie start)
    inputs      season inputs (external projections, actuals, goalie history)
    paths       Season/'s paths -- shared data locations (FEATURES_DIR, schedule(), players() ...)
    simlayer    the bridge to Simulation/ (scoring files, the sampler)
    decisionlayer  the bridge to Decisions/ (managers, draft, slots, strategy ...)

Season/ and its bridges use the flat-module convention (each folder on sys.path, imported by bare
name), so importing this module first puts Season/ on sys.path and every Live module then imports
those names as Season/ itself does. **The hazard is a name collision**: a module in Live/ named
like one in Season/ (or Decisions/, Simulation/) would shadow it for everyone. So Live/ has no
`paths.py` -- its own locations are in `livepaths.py` -- and `assert_no_collisions` checks the
folder at import time rather than trusting it.

Season/ never imports Live/: the simulator must not touch a platform or the network.
"""

import sys
from pathlib import Path

LIVE_DIR = Path(__file__).resolve().parent
SEASON_DIR = LIVE_DIR.parent / "Season"

if str(SEASON_DIR) not in sys.path:
    sys.path.append(str(SEASON_DIR))


def assert_no_collisions() -> None:
    """No module in Live/ may share a name with one in Season/, Decisions/ or Simulation/."""
    mine = {p.stem for p in LIVE_DIR.glob("*.py")}
    for sibling in ("Season", "Decisions", "Simulation"):
        theirs = {p.stem for p in (LIVE_DIR.parent / sibling).glob("*.py")}
        clash = mine & theirs
        if clash:
            raise RuntimeError(f"Live/ modules {sorted(clash)} shadow {sibling}/'s under the flat-module "
                               f"convention; rename them (see seasonlayer.py)")


assert_no_collisions()

"""The one place Server/ reaches into Season/ -- the shared league core, imported by bare name.

Server/ holds the server and the engine that acts on a real league (the live runner, the plan
pass, the platform adapters, the draft board; until 2026-10-24 these were Live/). They share
Season/'s league model rather than copying it:

    league      the league's rules, as data (Settings/rosters/)
    state       rosters, the move budget, waivers, IR legality
    view        what a manager may see
    schedule    the calendar and matchup weeks
    inputs      season inputs (external projections, actuals, goalie history, the goalie line)
    odds        a saved boom/bust run, read for the draft board
    paths       Season/'s paths -- shared data locations (FEATURES_DIR, schedule(), players() ...)
    simlayer    the bridge to Simulation/ (scoring files, the sampler)
    decisionlayer  the bridge to Decisions/ (managers, draft, slots, strategy ...)

Season/ and its bridges use the flat-module convention (each folder on sys.path, imported by bare
name), so importing this module first puts Season/ on sys.path and every Server module then imports
those names as Season/ itself does. **The hazard is a name collision**: a module in Server/ named
like one in Season/ (or Decisions/, Simulation/) would shadow it for everyone. So Server/ has no
`paths.py` -- its own locations are in `livepaths.py` -- and `assert_no_collisions` checks the
folder at import time rather than trusting it.

Season/ never imports Server/: the simulator must not touch a platform or the network.
"""

import sys
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
SEASON_DIR = ENGINE_DIR.parent / "Season"

if str(SEASON_DIR) not in sys.path:
    sys.path.append(str(SEASON_DIR))


def assert_no_collisions() -> None:
    """No module in Server/ may share a name with one in Season/, Decisions/ or Simulation/."""
    def names(folder):   # modules and packages alike: either one shadows a bare import
        return ({p.stem for p in folder.glob("*.py")}
                | {p.name for p in folder.iterdir() if (p / "__init__.py").exists()})

    mine = names(ENGINE_DIR)
    for sibling in ("Season", "Decisions", "Simulation"):
        theirs = names(ENGINE_DIR.parent / sibling)
        clash = mine & theirs
        if clash:
            raise RuntimeError(f"Server/ modules {sorted(clash)} shadow {sibling}/'s under the flat-module "
                               f"convention; rename them (see seasonlayer.py)")


assert_no_collisions()

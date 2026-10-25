"""The one place Server/ reaches into Core/ -- the shared league model, imported by bare name.

Server/ holds the server and the engine that acts on a real league (the live runner, the plan
pass, the platform adapters, the draft board; until 2026-10-24 these were Live/). They share
Core/'s league model with the backtests rather than copying it:

    league      the league's rules, as data (Settings/rosters/)
    state       rosters, the move budget, waivers, IR legality
    view        what a manager may see
    schedule    the calendar and matchup weeks
    inputs      season inputs (external projections, actuals, goalie history, the goalie line)
    odds        a saved boom/bust run, read for the draft board
    paths       the shared data locations (FEATURES_DIR, schedule(), players() ...)
    simlayer    the bridge to Simulation/ (scoring files, the sampler)
    decisionlayer  the bridge to Decisions/ (managers, draft, slots, strategy ...)

Until 2026-10-24 those were in Season/ beside the backtest harness, and this was seasonlayer.py.
**Season/ is never on the server's path now**: importing this refuses to start if it is, so no
server or planning process can pick up harness code (the engine, the ladder, tune ...) by a
stray import.

Core/ uses the flat-module convention (each folder on sys.path, imported by bare name), so
importing this module first puts Core/ on sys.path and every Server module then imports those
names as Core/ itself does. **The hazard is a name collision**: a module in Server/ named like one
in Core/ (or Decisions/, Simulation/) would shadow it for everyone. So Server/ has no `paths.py`
-- its own locations are in `livepaths.py` -- and `assert_no_collisions` checks the folder at
import time rather than trusting it.

Core/ never imports Server/: the league model must not touch a platform or the network.
"""

import sys
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
CORE_DIR = ENGINE_DIR.parent / "Core"
SEASON_DIR = ENGINE_DIR.parent / "Season"

if str(CORE_DIR) not in sys.path:
    sys.path.append(str(CORE_DIR))


def assert_no_harness() -> None:
    """Season/ (the backtest harness) is not on this process's sys.path."""
    if any(Path(p).resolve() == SEASON_DIR for p in sys.path if p):
        raise RuntimeError(f"{SEASON_DIR} is on sys.path: the server runs on Core/ alone, never the "
                           "backtest harness (see corelayer.py)")


def assert_no_collisions() -> None:
    """No module in Server/ may share a name with one in Core/, Decisions/ or Simulation/."""
    def names(folder):   # modules and packages alike: either one shadows a bare import
        return ({p.stem for p in folder.glob("*.py")}
                | {p.name for p in folder.iterdir() if (p / "__init__.py").exists()})

    mine = names(ENGINE_DIR)
    for sibling in ("Core", "Decisions", "Simulation"):
        clash = mine & names(ENGINE_DIR.parent / sibling)
        if clash:
            raise RuntimeError(f"Server/ modules {sorted(clash)} shadow {sibling}/'s under the flat-module "
                               f"convention; rename them (see corelayer.py)")


assert_no_harness()
assert_no_collisions()

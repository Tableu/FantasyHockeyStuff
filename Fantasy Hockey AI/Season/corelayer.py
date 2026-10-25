"""The one place Season/ reaches into Core/ -- the league model it shares with the live plan.

Core/ holds what a backtest and the live plan both run on (paths, league, state, view, schedule,
inputs, odds, and the bridges simlayer and decisionlayer); Season/ keeps the harness that measures
with it (engine, ladder, oneseat, tune, verify, ...). Until 2026-10-24 they were one folder.

Every Season module that imports a Core module imports this first: it puts Core/ at the front of
sys.path, where Season/'s own copies used to sit, so `import paths` finds Core/paths.py ahead of
Simulation/paths.py (simlayer.py says why that matters). **The hazard is a name collision** under
the flat-module convention, so Season/'s names are checked against Core/'s, Decisions/' and
Simulation/'s here rather than trusted.

`front()` is for the two process pools (ladder.py, branch.py): a spawned worker starts from the
parent's sys.path, where simlayer has since put Simulation/ first, so Core/ and Season/ go back in
front before the pool starts -- as Season/ alone did before the split.
"""

import sys
from pathlib import Path

SEASON_DIR = Path(__file__).resolve().parent
CORE_DIR = SEASON_DIR.parent / "Core"

if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))


def front() -> None:
    """Core/ first and Season/ second on sys.path, for a process pool's workers."""
    for folder in (SEASON_DIR, CORE_DIR):
        sys.path.insert(0, str(folder))


def assert_no_collisions() -> None:
    """No module in Season/ may share a name with one in Core/, Decisions/ or Simulation/."""
    def names(folder):   # modules and packages alike: either one shadows a bare import
        return ({p.stem for p in folder.glob("*.py")}
                | {p.name for p in folder.iterdir() if (p / "__init__.py").exists()})

    mine = names(SEASON_DIR)
    for sibling in ("Core", "Decisions", "Simulation"):
        clash = mine & names(SEASON_DIR.parent / sibling)
        if clash:
            raise RuntimeError(f"Season/ modules {sorted(clash)} shadow {sibling}/'s under the "
                               "flat-module convention; rename them (see corelayer.py)")


assert_no_collisions()

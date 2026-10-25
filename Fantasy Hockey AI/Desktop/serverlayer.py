"""The one place Desktop/ reaches into the engine -- the live league code the server runs, imported
by bare name (leagues, livepaths, platforms, draft_board, ...), and through it Season/.

Desktop/ holds the draft tools alone (draft_assistant.py, draft_gui.py and its tables, sheets.py),
kept for the next drafts when the plan window retired (2026-10-24). They share the engine rather
than copying it: importing this module first puts the engine folder on sys.path and imports its
`seasonlayer`, which puts Season/ there too. **The hazard is a name collision** under the
flat-module convention, so `assert_no_collisions` checks this folder against the engine's and
Season/'s, Decisions/' and Simulation/'s at import time.
"""

import sys
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parent
ENGINE_DIR = DESKTOP_DIR.parent / "Live"

if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import seasonlayer  # noqa: E402,F401 -- Season/ on sys.path


def assert_no_collisions() -> None:
    """No module here may share a name with one in the engine, Season/, Decisions/ or Simulation/."""
    def names(folder):   # modules and packages alike: either one shadows a bare import
        return ({p.stem for p in folder.glob("*.py")}
                | {p.name for p in folder.iterdir() if (p / "__init__.py").exists()})

    mine = names(DESKTOP_DIR)
    for sibling in (ENGINE_DIR, *(DESKTOP_DIR.parent / s for s in ("Season", "Decisions", "Simulation"))):
        clash = mine & names(sibling)
        if clash:
            raise RuntimeError(f"Desktop/ modules {sorted(clash)} shadow {sibling.name}/'s under the "
                               "flat-module convention; rename them (see serverlayer.py)")


assert_no_collisions()

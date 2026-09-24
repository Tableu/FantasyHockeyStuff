"""The decision policies, imported from `Decisions/` in one place.

Season measures; `Decisions/` chooses. Every manager the ladder seats, the lineup solver, the draft
rule and rung 3's box-score estimator live in that sibling folder so a live runner can use the same
code a backtest scored. This module is how Season reaches it, the way `simlayer.py` is how it
reaches `Simulation/`.

**The hazard is the flat-module convention.** Each folder is put on `sys.path` and imported by bare
module name, so two folders defining the same name means whichever comes first on the path wins,
silently. It has already happened once: `Season/paths.py` shadows `Simulation/paths.py`, which is
why `simlayer.py` can import only the parts of Simulation that never touch `paths`. So before
`Decisions/` goes on the path, every one of its module names is checked against Season's and
Simulation's, and a collision stops the run instead of quietly handing a manager the wrong module.
"""

import sys

import paths

_decision_modules = {p.stem for p in paths.DECISIONS_DIR.glob("*.py")}
_elsewhere = {p.stem for directory in (paths.PROJECT_ROOT, paths.SIMULATION_DIR)
              for p in directory.glob("*.py")}
_collisions = sorted(_decision_modules & _elsewhere)
if _collisions:
    raise ImportError(f"Decisions/ and Season/ or Simulation/ both define {_collisions}; under the "
                      f"flat-module convention one would silently shadow the other. Rename one. "
                      f"See decisionlayer.py.")

if str(paths.DECISIONS_DIR) not in sys.path:
    sys.path.append(str(paths.DECISIONS_DIR))

import adddrop        # noqa: E402
import draft          # noqa: E402
import estimators     # noqa: E402
import managers       # noqa: E402
import orchestrator   # noqa: E402
import slots          # noqa: E402
import strategy       # noqa: E402
import streaming      # noqa: E402
import valuation      # noqa: E402

__all__ = ["adddrop", "draft", "estimators", "managers", "orchestrator", "slots",
           "strategy", "streaming", "valuation"]


def load_strategy(name=None):
    """The strategy parameters from Settings/ (a name there, or a path). Decisions/ does
    no file I/O, so the file is read here and handed over parsed."""
    import json

    path = paths.strategy_config(name) if name else paths.STRATEGY_CONFIG
    if not path.exists():
        raise FileNotFoundError(f"no strategy settings at {path}")
    return strategy.from_dict(json.loads(path.read_text(encoding="utf-8")), name=path.stem)

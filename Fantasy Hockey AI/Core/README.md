# Core

The league model the backtests and the live plan share. Split out of `Season/` on 2026-10-24
(plan `season-core-split.md`) so the server runs on these modules alone, without the backtest
harness, and the harness can change without touching the live plan.

```
paths.py          the shared data locations (ModelFeatures' features, Projections' reports,
                  Settings/, Simulation's fits); REPORTS_DIR and DOCS_DIR are Season/'s
league.py         the format as data: slots, bench, IR, the move cap, waivers, locks, playoffs
                  (Settings/rosters/)
schedule.py       game days and matchup weeks, derived from the schedule, never assumed
inputs.py         the loaders and the provenance assertions; goalie_line, the league-average start
state.py          rosters, IR, the free-agent pool, waivers, the weekly move counter
view.py           SlateView -- the only thing a manager sees
odds.py           reading a saved boom/bust run (Season/boom_bust.py writes it; the draft board reads it)
simlayer.py       the bridge to Simulation/ (scoring, the sampler, copula, goalies)
decisionlayer.py  the bridge to Decisions/ (managers, draft, slots, strategy, valuation, ...)
```

## Who uses it

- **Season/** (the harness: engine, ladder, oneseat, tune, verify, ...): every module that imports
  a Core module imports `Season/corelayer.py` first, which puts Core/ at the front of sys.path.
- **Server/** (the server and the live engine): through `Server/corelayer.py`, which appends Core/
  to sys.path and **refuses to start if Season/ is on it** -- no server or planning process can
  load harness code.
- **Desktop/** (the draft tools): through `Server/`'s bridge.

Core/ imports nothing from Season/, Server/ or Desktop/, and opens no database or network
connection: every input arrives by path.

## The flat-module convention

Every folder goes on `sys.path` and is imported by bare name (`import league`), so two folders
defining the same name means whichever comes first wins, silently. Each bridge checks its folder's
names against Core/'s, Decisions/' and Simulation/'s at import time. One collision is deliberate
and load-bearing: `Core/paths.py` shadows `Simulation/paths.py`, which is why simlayer.py imports
only the Simulation modules that never read `paths`, and checks that both paths modules point
`SCORESETS_DIR` at the same folder.

Process pools (Season/ladder.py, branch.py) call `corelayer.front()` before starting, because a
spawned worker inherits the parent's sys.path, where simlayer has put Simulation/ first.

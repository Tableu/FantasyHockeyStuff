# Decisions

The decision policies: section 9 of the Fantasy Hockey AI build plan. What a manager does with
what it knows — draft, set a lineup, add and drop, and (later) weigh a trade.

```
ModelFeatures/   features                              "is this feature knowable at the lock?"
Projections/     a mean per player-game, and per ROS   "how close is this projection?"
Simulation/      a distribution, calibrated            "is this spread right?"
Decisions/       a choice                              "what should I do with it?"
Season/          a won week                            "does using any of it beat not using it?"
```

**Season measures, Decisions chooses.** `Season/` runs the league: the calendar, roster state and
its rules, the leakage guard, the day loop, outcomes, and the ladder. Every policy it seats lives
here. The split exists so a live runner (section 10) can call the same code a backtest scored.

## Rules for this folder

- **No `paths.py`, no file reads, no database.** Every input arrives as an argument: a view of the
  slate, a league config, a scoreset, tables. What a policy may see is decided by whoever builds
  its view, not here.
- **Imports nothing from `Season/`.** Season reaches this folder through `Season/decisionlayer.py`,
  which puts it on `sys.path` and refuses to start if a module name here also exists in `Season/`
  or `Simulation/`. Under the flat-module convention a duplicate name silently shadows the other
  one — the same way `Season/paths.py` shadows `Simulation/paths.py`.

## Files

```
managers.py     the four ladder rungs behind one interface, LADDER, build_field
slots.py        the nightly lineup as an exact maximum-weight assignment; matching_size
draft.py        draft boards, the prior rate, and choose_pick (positional need enforced)
estimators.py   NaiveHistory: rung 3's box-score projection
```

## The interface a manager is handed

A manager gets one view per team per night (`Season/view.py`'s `SlateView` in the backtest), and
reads only these:

| Group | Members |
|---|---|
| Holdings | `roster`, `ir`, `moves_left`, `waiver_priority`, `free_agents()`, `on_waivers(p)`, `opponent_roster()` |
| Tonight | `available(p)`, `startable(players)`, `ir_eligible()`, `unavailable`, `playing_tonight` |
| Schedule | `games_remaining(p)`, `games_through(p, weeks_ahead)`, `day`, `week` |
| Estimates | `history` (a `NaiveHistory`), `projected_rate(p)`, `moments(scoreset, players)`, `projected_points(scoreset)`, `p_start_column` |
| The matchup | `my_week_points`, `opponent_week_points` |
| Acting | `_state`: `add`, `drop`, `stash`, `activate`, `submit_claim`, and `eligibility` |

Every change goes through the state's own methods, which raise on an illegal move rather than
quietly refusing it.

## Checks

The solver is checked against brute force by `Season/verify.py` (`assignment`), which passes the
league's `SLOT_POSITIONS` into `slots.verify_optimal`.

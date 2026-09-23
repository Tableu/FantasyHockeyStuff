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
managers.py     the ladder rungs behind one interface, LADDER, build_field (rungs 1-4, plus
                section 9's rung 5 add/drop, rung 6 hold, and section 10's rung 7 orchestrator)
slots.py        the nightly lineup as an exact maximum-weight assignment; matching_size
draft.py        draft boards, the prior rates (per game played, and per team game as the
                fallback for a player the projections have not reached), and choose_pick
estimators.py   NaiveHistory: rung 3's box-score projection
valuation.py    a player's and a swap's forward value; a swap is priced on the roster by
                re-solving the lineup on every night it touches; known() -- unknown is not zero
adddrop.py      section 9's add/drop rule and AddDropParams (default H=3, m=1, rest-of-season)
streaming.py    section 10's rentals: k streaming spots, a drop cost of what cannot be bought
                back, a bar that falls as the week's moves expire, a reserve kept for upgrades
orchestrator.py DailyPlan: the section 10 day as a fixed, logged sequence -- matchup, IR,
                upgrade, stream, lineup -- delegating every decision to the module that owns it
```

## IR is not free on a full roster

Activations and drops cost no move, but an activation needs a roster spot, so on a full roster
it **forces a drop**. `Manager.manage_ir` resolves every recovered player the same day -- an open
spot first, then a swap with a newly injured player, then a priced drop (`activation_drop`: box
score for rungs 2-3, the roster-level removal cost for rungs 4 and up, the returning player
included as a candidate). "Recovered" means his team's latest lockout report says healthy
(`view.injured`), not that his team is idle tonight -- the per-night flag alone made every rung
activate injured players on dark nights. And an add may fill the spot a stash opens without
dropping anyone, which is the only way a stash is worth anything.

The add/drop rule's results, and the sensitivity reading behind its defaults, are in
`Season/README.md` ("Section 9: the add/drop rule").

## The interface a manager is handed

A manager gets one view per team per night (`Season/view.py`'s `SlateView` in the backtest), and
reads only these:

| Group | Members |
|---|---|
| Holdings | `roster`, `ir`, `moves_left`, `waiver_priority`, `free_agents()`, `on_waivers(p)`, `opponent_roster()` |
| Tonight | `available(p)`, `startable(players)`, `ir_eligible()`, `healthy_on_ir()`, `roster_room()`, `unavailable`, `injured`, `playing_tonight` |
| Schedule | `games_remaining(p)`, `games_through(p, weeks_ahead)`, `day`, `week` |
| Estimates | `history` (a `NaiveHistory`), `projected_rate(p, default)`, `ros_rate(p, default)`, `moments(scoreset, players)`, `projected_points(scoreset)`, `p_start_column` |
| The matchup | `my_week_points`, `opponent_week_points` |
| Acting | `_state`: `add`, `drop`, `stash`, `activate`, `submit_claim`, and `eligibility` |

Every change goes through the state's own methods, which raise on an illegal move rather than
quietly refusing it.

`projected_rate` and `ros_rate` return `default` for a player nobody has projected yet; pass
`default=None` to tell "unknown" from "worth zero".

## Checks

The solver is checked against brute force by `Season/verify.py` (`assignment`), which passes the
league's `SLOT_POSITIONS` into `slots.verify_optimal`. `hold` checks that the add/drop rule at an
infinite margin never moves, and `opening rates` that no opening-week skater is priced as unknown
and that an unprojected player reads as unknown rather than zero.

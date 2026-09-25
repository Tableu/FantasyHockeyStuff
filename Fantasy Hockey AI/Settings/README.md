# Settings

The league's rules and the managers' strategy, as files you edit. Nothing in `Projections/`,
`Simulation/`, `Season/` or `Decisions/` hard-codes what a goal is worth, how many slots a team
has, or how far ahead a manager looks -- they all read from here.

```
scoring/        what each stat is worth                 read by Projections, Simulation, Season  (--weights)
rosters/        slots, rules, schedule, draft, playoffs read by Season                           (--league)
strategy.json   how the managers decide                 read by Season, handed to Decisions      (--strategy)
```

The first two are what the league imposes; `strategy.json` is what a manager chooses.

To set up your league, copy the closest file in each folder, edit it, and pass its name:

```
python evaluate.py --weights my-league          # Projections/
python simulate.py --weights my-league          # Simulation/
python ladder.py   --weights my-league --league my-league   # Season/
```

A bare name is looked up here (`my-league` -> `scoring/my-league.json`); a path to a file
anywhere else works too. Unknown keys fail on load rather than being silently ignored.

## scoring/

```json
{
  "name": "points-league",
  "description": "...",
  "skaters": {"goals": 4.0, "assists": 2.5, "ppp": 1.0, "shp": 1.25,
              "hits": 0.4, "blocks": 0.4, "shots": 0.25, "pim": 0.2},
  "goalies": {"wins": 3.0, "losses": -1.5, "ot_losses": 1.0, "shutouts": 2.5,
              "saves": 0.25, "goals_against": -1.0}
}
```

- **Skater keys:** `goals assists shots hits blocks pim ppp shp`. `ppp` and `shp` are counted
  per point (a power-play goal earns `goals` + `ppp`).
- **Goalie keys:** `wins losses ot_losses shutouts saves goals_against`. Give a cost as a
  negative weight.
- A stat you leave out scores 0. `banger-league.json` prices no `losses`, for example.

`Projections/` and `Simulation/` have no default scoring file: without `--weights` they report
per-stat numbers only. `Season/` defaults to `points-league`.

## rosters/

```json
{
  "name": "target-league",
  "teams": 14,
  "active_slots": {"C": 2, "LW": 2, "RW": 2, "F": 1, "D": 4, "F/D": 1, "G": 2},
  "bench": 4,
  "ir": 2,
  "moves_per_week": 7,
  "moves_carry_over": false,
  "waiver_days": 2,
  "ties": "split",
  "schedule": {"type": "round_robin", "regular_season_weeks": 24,
               "regular_season_weeks_by_season": {"2025-26": 21}, "week_starts_on": "MON",
               "min_first_week_games": 20},
  "draft": {"type": "snake", "order": "lottery", "keepers": 0},
  "playoffs": {"teams": 6, "byes": 2, "rounds": 3, "weeks_per_round": 1,
               "seeding": "record", "tiebreak": "points_for"},
  "eligibility_platform": "yahoo",
  "eligibility_season": "2026-27",
  "slot_positions": {"C": ["C"], "LW": ["LW"], "RW": ["RW"], "D": ["D"], "G": ["G"],
                     "F": ["C", "LW", "RW"], "F/D": ["C", "LW", "RW", "D"]},
  "rules": {"lineup_lock": "daily", "waivers": "rolling", "ir_eligible": "injured",
            "move_cost": {"add": 1, "claim": 1, "drop": 0, "ir_stash": 0, "ir_activate": 0}}
}
```

- **Slots:** every slot in `active_slots` needs an entry in `slot_positions` naming the positions
  (`C LW RW D G`) it accepts. A composite slot lists several; a slot may not mix `G` with skaters.
- **Rules:** `lineup_lock` `daily`, `waivers` `rolling`, `ir_eligible` `injured` are the values the
  harness implements. `move_cost` is what each action spends from `moves_per_week`.
- **Ties:** `split` (half a win each) or `loss` (neither team gets a win).
- **Schedule:** `round_robin` matchup weeks starting `week_starts_on`. Give the regular season's
  length as `regular_season_weeks` or as `regular_season_end` (`"MM-DD"`: through the week
  containing that date), not both. `regular_season_weeks_by_season` overrides the length for a
  season whose calendar is short -- 2025-26 has only 26 matchup weeks with games (the Olympic
  break), so the target league's 24 + 3 plays as 21 + 3 there. The regular season plus the
  playoff weeks must fit the season's calendar, or the run is refused. A first week with fewer
  than `min_first_week_games` NHL games is merged into the second, as a platform stretches week 1
  over an early opener: 2024-25's first week is the one Prague game. 2025-26's (26 games) is not.
- **Draft:** `snake` or `linear`, seat order drawn by `lottery`. Keepers are not modelled (`0`).
- **Playoffs:** simulated after the regular season. A fixed bracket of `2 ** rounds` slots, the top
  `byes` seeds skipping round one (`byes` must equal `2 ** rounds - teams`): with 6 teams, round
  one is 3v6 and 4v5, then 1 plays the 4/5 winner and 2 the 3/6 winner; never reseeded. Seeded by
  record (`seeding: record`), ties broken by regular-season points (`tiebreak: points_for`); a
  tied playoff matchup goes to the team with more regular-season points. Each round lasts
  `weeks_per_round` weeks. The ladder reports `playoff_rate` and `title_rate` per rung; every
  other metric (points per week, moves, slot fill) stays regular-season only.
- `teams` must be even. A value the harness does not implement is refused at load, never ignored.
- Position eligibility comes from the platform named in `eligibility_platform`.
- `league.json` is the default when `--league` is omitted.

`Season/` names its ladder reports and docs after the roster file (`ladder_2025-26_<file>.json`),
so give each format its own file rather than editing one in place between runs.

## strategy.json

What a manager *chooses*: horizons, margins, the streaming layer, the priors. Section 11 tunes
these, so none of them lives in code any more. `strategy.json` is the shipped strategy, read by
`Season/` unless `--strategy` names another file here.

```
python ladder.py --strategy my-strategy          # a name here, or a path
python ladder.py --margin 0.5 --streams 3        # single values, over the file's
```

`Season/decisionlayer.py` (`load_strategy`) reads the file; `Decisions/strategy.py` parses it into
a `Strategy` and `managers.build_field` hands it to every seat. **Every key is required** and
unknown keys are refused: a value the file forgets is an error, never a default quietly inherited
from code. Write `"inf"` for an unbounded number and `"season"` for a horizon of the rest of the
season.

Changing a strategy needs no model rebuild -- the projections do not know how they are used.

### Sections

| section | key | value | meaning |
|---|---|---|---|
| `adddrop` (rung 5+) | `horizon_weeks` | 3 | weeks past the current one both sides of a swap are priced over |
| | `margin` | 1.0 | sds of its own gain a move must clear; `"inf"` never moves (rung 6) |
| | `rate_source` | `ros` | `ros` (rest-of-season projection) or `per_game` |
| | `claim_premium` | 0.0 | extra points a waiver claim must clear; `"inf"` never claims |
| | `shortlist` | 10 | free agents priced on the roster per pass |
| | `drop_shortlist` | 4 | cheapest fieldable drops tried against each |
| `streaming` (rung 7) | `spots` | 2 | streaming spots; 0 makes rung 7 identical to rung 5 |
| | `reserve` | 2 | moves held for upgrades on a week's first day, falling to 0 |
| | `lam` | 2.0 | points a rental must clear early in the week, falling to 0 |
| | `margin` | 0.0 | sds of the week's gain a rental must also clear |
| | `gate` | false | scale a rental's gain by phi(z)/phi(0) of the matchup z |
| | `flat` | false | hold the bar at lam/2 all week instead of letting it fall |
| | `claim` | true | a rental may be claimed off waivers, priced from his clear date |
| | `shortlist` | 12 | free agents priced on the roster per pass |
| `rung3_streamer` | `horizon_weeks` | 1 | how far ahead rung 3 prices a swap |
| `rung4_full_system` | `horizon_weeks` | 1 | how far ahead rung 4 prices an acquisition (matched to rung 3) |
| | `drop_horizon_weeks` | 3 | window a forced IR-activation drop is priced over (rungs 2-4) |
| | `drop_rate_source` | `per_game` | the rate that drop is priced on |
| | `z_clip` | 3.0 | the matchup z is clipped to +-this; the normal tails are not trusted |
| | `z_source` | `closed_form` | how rung 4+ reads the week: `closed_form` (per-player moments) or `sampled` (both rosters' remaining week drawn on shared sims, P(win) read off the draws) |
| `playoffs` | `eliminated` | `hold` | a team out of the playoffs stops transacting (`continue` keeps it trading); its IR is still resolved |
| | `future_week_weight` | `p_advance` | in the playoffs a later round's nights count by the chance of reaching it (a bye week counts 0); `flat` counts every night in full |
| `priors` | `prior_rate_shrink_games` | 20 | games of league mean mixed into last season's per-game rate |
| | `goalie_start_share_prior` | 0.5 | the naive P(start) shrinks toward a tandem split... |
| | `goalie_start_share_prior_games` | 2 | ...by this many games |
| | `opening_days` | 7 | days of rest-of-season rows the VOR draft board treats as draft day |
| `draft` | `vor_values` | `consensus` | what the VOR draft board values players on: `consensus`, the external sources' preseason projections alone (the only values a real draft has), or `own_model`, our opening-week rest-of-season rows (a backtest reference; they need the season's own games) |
| | `min_sources` | 3 | sources a player needs for the consensus; below it he keeps last season's total, or his thin consensus if he has none |
| | `undated_sources` | `include` | a source with no publish date (Dom's 2025-26 sheet; every 2026-27 file today) is used, or dropped with `exclude`; logged either way |

### Where the values come from

**Tuned on 2024-25 (2026-09-24): nothing changed.** `Season/tune.py` searched the add/drop and
streaming values against the shipped ones on 2024-25 (a build trained on 2023-24 alone), and no
change cleared two paired standard errors at 8 seat-paired drafts (errors of ±0.5-1.6 points a
week) -- see `Season/README.md`, section 11. Most single steps away cost points; none gains more
than about one. So the values below are the shipped ones, now measured, not only reasoned. 2025-26, the clean holdout,
was not touched.

- **`adddrop.horizon_weeks = 3`** is section 9's plan. On the 2025-26 sensitivity check
  (`Season/docs/ladder-league_sens-*.md`) H = 1, 3 and the rest of the season all cleared hold,
  with 3 and season within noise of each other and ahead of 1.
- **`claim_premium = 0`** since 2026-09-23, when claims were made to resolve: a claim clears the
  same bar as an add and priority is treated as free. Unmeasured; a sweep over {0, 5} is planned.
- **The streaming values** are the section 10 v1 settings; the ablations are in `Season/README.md`.
- **`z_source = closed_form`**: the sampled week is slightly better calibrated (Brier 0.1356 against
  0.1383 over 12,150 manager-days) but moved no ladder number and doubles a run's time.
- **`playoffs`**: `hold` and `p_advance` are the principled settings, adopted 2026-09-24; against
  `continue` / `flat` they move a few playoff rounds between rungs, within noise, and leave every
  regular season identical.
- **`draft.vor_values = consensus`** since 2026-09-24 (`Season/docs/board-accuracy-2025-26.md`,
  `Season/README.md`): it ranks value over replacement better than our model or last season in
  every format and scoring, and drafting by it beats the last-season board with the orchestrator
  in all four. Our model's rows cannot be built before a real season, so it is not a live option.
- **`goalie_start_share_prior`**: deliberately not "who started last game", which has an AUC of
  0.520 over all candidates.

The goalie prior is shared by every seat, because the naive P(start) is part of the view the
engine builds for all of them, so a field must carry one strategy. The engine checks this.

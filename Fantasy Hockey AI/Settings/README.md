# Settings

The league's rules and the managers' strategy, as files you edit. Nothing in `Projections/`,
`Simulation/`, `Season/` or `Decisions/` hard-codes what a goal is worth, how many slots a team
has, or how far ahead a manager looks -- they all read from here.

```
scoring/        what each stat is worth                 read by Projections, Simulation, Season  (--weights)
rosters/        slots, rules, schedule, draft, playoffs read by Season                           (--league)
strategy.json   how the managers decide                 read by Season, handed to Decisions      (--strategy)
field.json      how the simulated opponents draft       read by Season                           (--field)
live.json       how tonight's reports become odds       read by Projections/project_tonight.py
leagues/        the leagues we play: platform, id, team  read by Live (--league), pipeline importers
```

The first two are what the league imposes; `strategy.json` is what a manager chooses; `field.json`
is what the backtest assumes about everyone else.

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
  "eligibility_platform": "fleaflicker",
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
- Position eligibility comes from the platform named in `eligibility_platform`. `league.json` reads
  Fleaflicker's (the target league's platform) since 2026-09-28; it was Yahoo's, which grants
  multiple positions more often (15% of listed players against Fleaflicker's 9%).
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
| | `rate_source` | `ros` | `ros` (the model's rest-of-season projection, updated through the season), `per_game`, or `board` (the preseason consensus per team game, frozen all season -- what the live plan prices skaters on today, Live/live.py ros_seed; goalies on their start share). `board` is a backtest arm for that comparison (2026-10-02) |
| | `claim_premium` | 0.0 | extra points a waiver claim must clear; `"inf"` never claims |
| | `shortlist` | 10 | free agents priced on the roster per pass |
| | `drop_shortlist` | 4 | cheapest fieldable drops tried against each |
| | `tail` | `none` | value after the pricing window: `none` ignores it; `cost` makes a swap also cover the rest-of-season value it loses (outgoing's after the window minus incoming's, added to the bar); `net` counts it both ways |
| | `goalie_absence` | false | an injured goalie's value over a window (the free-agent shortlist, drop costs, streaming spots) counts only his games from his expected return. A skater's rest-of-season rate already reads his injury flag; a goalie's start share does not. On his expected absence alone, predicted starts over the next 28 days: 2.50 -> 1.61 error (2024-25), 2.15 -> 1.17 (2025-26). Realistic league (2024-25, 32 drafts): +0.23 +/- 0.26 pts/wk, win +0.001; it changed only 2 of 32 drafts (IR and the pool's long-absence filter already cover most injured goalies). **On for `beagles` and `espn-la`** (the user's call 2026-09-30, a correctness fix) |
| `streaming` (rung 7) | `spots` | 2 | streaming spots: at most this many rostered players below replacement may be streamed (the furthest below first); 0 makes rung 7 identical to rung 5. **99 (no limit) for `beagles` and `espn-la`** (the user, 2026-09-27): every player below replacement is streamable -- 2024-25, strategy-espn-la, 32 drafts, +2.08 +/- 0.49 pts/wk, win +0.016 +/- 0.007, drafts 17-32 +3.17 +/- 0.53; rentals ~97 -> ~108 a season. With a limit of 2, a goalie spot (goalie rentals) displaced a skater's |
| | `reserve` | 2 | moves held for upgrades on a week's first day, falling to 0 |
| | `lam` | 2.0 | points a rental must clear early in the week, falling to 0 |
| | `margin` | 0.0 | sds of the week's gain a rental must also clear |
| | `gate` | false | scale a rental's gain by phi(z)/phi(0) of the matchup z |
| | `flat` | false | hold the bar at lam/2 all week instead of letting it fall |
| | `claim` | true | a rental may be claimed off waivers, priced from his clear date |
| | `shortlist` | 12 | free agents priced on the roster per pass |
| | `mode` | `daily` | `daily`: each day's rentals on their own, against the falling `lam` bar; `week`: plan the rest of the week's moves together and make today's (Decisions/weekplan.py; no `lam`). The live leagues' strategies run `week`: seat-paired on 2024-25, 16 drafts, +2.01 +/- 0.62 pts/wk on the live settings (+1.79 +/- 0.80 on this file's) |
| | `survival` | 1.0 | week mode: a move planned d days ahead ranks at survival^d x its edge (P(the free agent is still free)); 0.6 / 0.8 / 1.0 were within noise, 1.0 kept |
| | `next_week` | 0.0 | week mode: a move made today on the week's last 2 days also counts next week's nights at this weight (with `gate`, this week's at phi(z)/phi(0)): buy next week's roster with moves about to expire. Off: on the live strategy, 2024-25, 8 drafts, every variant lost -- 0.5 -1.34 +/- 0.48, 1.0 -2.68 +/- 0.69, gate+0.5 -1.46 +/- 0.94, gate+1.0 -3.26 +/- 0.75 pts/wk -- and none raised the win rate (weekend rentals were traded for pickups Monday's fresh moves make anyway). Now only on the week's last day with P(win) >= 0.9 (needs `gate`): gate+0.5 -0.94 +/- 0.35, gate+1.0 -1.29 +/- 0.72; and with that Sunday's games not counted at all (what the code now does): -1.51 +/- 0.40 / -1.45 +/- 0.62 -- still off |
| | `goalies` | false | week mode: goalies may be rented and dropped for a rental, tonight priced on the simulated line (P(start) and the matchup); a goalie projected to start half or more of his team's remaining games (Projections/goalie_workload.py) is never dropped for one. **On for `beagles` and `espn-la`** (the user, 2026-09-27): 2024-25, strategy-espn-la, 32 drafts, +0.70 +/- 0.41 pts/wk, win +0.013 +/- 0.006, ~9 more moves a season. Unprotected it read +1.62 +/- 0.52, half of it from renting starters away; before goalie workload rows, a goalie's rate was P(start) x the league-average line and elite starters read as replaceable (the plan rented Daccord for Shesterkin) |
| | `spot_tolerance` | 0.0 | rest-of-season points: a skater within this much of replacement (the best free agent at his positions) is a streaming spot too, not only one below it; his drop cost is still charged. Goalies stay strictly below. Asked for 2026-09-28: the Beagles' week plans were all D and G because every forward was above replacement, Kane by 0.1 points. Off: seat-paired, 32 drafts, 2024-25 chose 10 for `beagles` (+1.38 +/- 0.32 pts/wk, win +0.004 +/- 0.007; 25: +1.27 +/- 0.40) and none for `espn-la` (10: +0.34 +/- 0.30; 25: +0.04 +/- 0.39), but 2025-26 did not confirm it (beagles 10: -0.21 +/- 0.44, win -0.006 +/- 0.007). Set to 10 for `beagles` 2026-09-28 (the user's call); **5 for `beagles`** since 2026-09-29: in the realistic league (2024-25, 64 drafts) 0, 2.5 and 10 each lose 1.6-2.4 pts/wk against it (`Season/README.md`) |
| | `starter_share` | 0.5 | week mode, `goalies`: a goalie projected to start this share of his team's remaining games is never a rental's drop. Judgment call, unmeasured |
| | `late_days` | 1 | week mode, `next_week`: the pickup for next week is priced only on the week's last this-many days... |
| | `clear_win_z` | 1.2816 | ...and only with the matchup z at least this (1.2816 = P(win) 0.9; needs `gate`). Judgment call |
| | `chain_tie` | 0.1 | week mode: team slots within this many expected points count as tied, and the one whose pick finishes playing sooner goes first. Judgment call |
| | `lazy` | true | week mode: lazy greedy (`weekplan.WeekPlanner._plan_lazy`) -- after the first round, candidates are priced from the highest ceiling (best edge when last priced) down, and a round stops once the best edge found beats the next ceiling. Not exact: a different move in 4.8% of rounds on about half the trial prices (drafts 0-2, 2026-10-05). For speed, the user's call on neutral results: 2024-25, 56 drafts, +0.22 +/- 0.60 pts/wk, win -0.002 +/- 0.013 (28: +1.26 +/- 0.88); confirmed 2025-26, 56 drafts, -0.16 +/- 0.69, win +0.017 +/- 0.014; moves unchanged. The opponents run it too (strategy.json) |
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
| | `min_sources` | 2 | sources a player needs for the consensus; below it he keeps last season's total, or his thin consensus if he has none |
| | `undated_sources` | `include` | a source with no publish date (Dom's 2025-26 sheet; every 2026-27 file today) is used, or dropped with `exclude`; logged either way |
| `roster` | `repair_wait_days` | 7 | a roster that cannot fill every slot spends a move to repair it (every rung that transacts), unless an IR player expected back within this many days would fill the slot himself. Return dates are the injury model's, or `Settings/returns-<league>.json` for a live league. Judgment call, 2026-09-30. Screened in the realistic league (2024-25, 32 drafts): 0 (repair at once) minus 7 = -0.08 +/- 0.22 pts/wk, win -0.016 +/- 0.007 -- neutral on points, a hint in the wait's favour on wins; kept. Tunable as a candidate: `oneseat.py --set roster.repair_wait_days=N` |
| `roster` | `fill_check` | none | whether a move must keep the roster able to fill its lineup slots: `relative` (a move may not leave fewer slots fillable than before, and a short roster is repaired first -- since 2026-09-23, when a goalie on IR froze teams under the old absolute check) or `none` (no check, no repair: an empty slot is priced as the points it loses and the week plan streams into it). `none` since 2026-10-04, the user's argument (you need not roster enough goalies or defencemen to fill every slot) on neutral results: 2024-25, 64 drafts, +0.36 +/- 0.76 pts/wk, win +0.017 +/- 0.014; confirmed 2025-26, 64 drafts, +0.47 +/- 0.82, win -0.006 +/- 0.014; moves unchanged. `oneseat.py --set 'roster.fill_check="relative"'` for the old behaviour |

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
- **`tail = none`** in strategy.json; the live leagues use `tail = cost`. Seat-paired on 2024-25,
  `cost` was neutral (+0.21 +/- 0.40 pts/wk pooled; +0.69 +/- 0.25 14-team points, -0.87 +/- 0.43
  12-team points) and `net` lost (-3.37 +/- 1.04).
- **Each league has its own strategy, `strategy-<league>.json`** (named in its league file's
  `strategy`, shared with no other league): strategy.json but for the league-level choices --
  `adddrop.tail`, `streaming.mode`, `streaming.gate`, `streaming.next_week`, `goalies`, `spots`,
  `spot_tolerance`, the judgment calls `starter_share`, `late_days`, `clear_win_z`, `chain_tie` and
  `roster.repair_wait_days` (verify.py 'live strategy' refuses anything else). Today: `espn-la` and `espn` choose `tail = cost` and `mode =
  week`; `beagles` also turns on the Sunday next-week pickup (`gate = true`, `next_week = 1.0`).
- **`claim_premium = 0`** since 2026-09-23, when claims were made to resolve: a claim clears the
  same bar as an add and priority is treated as free. Unmeasured; a sweep over {0, 5} is planned.
- **The streaming values** are the section 10 v1 settings; the ablations are in `Season/README.md`.
- **`z_source = closed_form`**: the sampled week is slightly better calibrated (Brier 0.1356 against
  0.1383 over 12,150 manager-days) but moved no ladder number and doubles a run's time.
- **`playoffs`**: `hold` and `p_advance` are the principled settings, adopted 2026-09-24; against
  `continue` / `flat` they move a few playoff rounds between rungs, within noise, and leave every
  regular season identical.
- **`draft.min_sources = 2`** since 2026-09-24 (was 3): on 2025-26 the board's VOR rank is the
  same at a minimum of 3 and of 1 in every format (`Season/docs/board-accuracy-2025-26.md`), and
  2026-27 has only three goalie sources, so at 3 a goalie two of them project fell back to last
  season. At 2 he keeps his projection. The section 9-11 ladder numbers were run at 3.
- **`draft.vor_values = consensus`** since 2026-09-24 (`Season/docs/board-accuracy-2025-26.md`,
  `Season/README.md`): it ranks value over replacement better than our model or last season in
  every format and scoring, and drafting by it beats the last-season board with the orchestrator
  in all four. Our model's rows cannot be built before a real season, so it is not a live option.
- **The draft is plain value over replacement** (2026-09-24): fill-starters-first, a goalie cap for
  our seats, a draft-time replacement level, a replacement weight and a bench weight were each
  tried seat-paired on 2024-25 against the realistic field and removed -- none cleared the noise in
  any format (`Season/README.md`, section 9 step 2).
- **`goalie_start_share_prior`**: deliberately not "who started last game", which has an AUC of
  0.520 over all candidates.

The goalie prior is shared by every seat, because the naive P(start) is part of the view the
engine builds for all of them, so a field must carry one strategy. The engine checks this.

## field.json

What the backtest assumes about the other managers in the league -- not a rule of the league and
not our strategy, so kept apart from both. Read by `Season/field.py`; like every file here, every
key is required.

| key | value | meaning |
|---|---|---|
| `opponent_board` | `source_subsets` | how every seat that is not ours drafts: `source_subsets` (below), or `last_season`, the old naive autodraft from last season's fantasy totals, which reproduces every earlier result exactly |
| `sources_per_opponent` | [1, 3] | each opponent seat reads between this many of the season's external projection sources, drawn equally, seeded by (replication, seat) so paired runs face the same room |
| `opponent_max_goalies` | 4 | an opponent never drafts more goalies than this |

**An opponent's board** is the consensus of just the sources it reads (the same per-stat mean as
ours, a stat a source omits skipped, one source enough), scored with the league's file, then value
over replacement against a league drafting by those values -- fixed before the draft, as ours is. A
player none of its sources covers keeps last season's total; so does every goalie for a reader of
skater-only sources. ADP is deliberately not used: it is built for standard formats, and this
league's hits and blocks make its values different.

**Why:** a real leaguemate reads one to three rankers, not every source, and the room agrees on the
stars and disagrees deeper -- an opponent's rank differs from ours by a median 1 spot in our top 20,
7 in 21-100 and 17 in 101-200. The goalie cap is there because without it a reader of one
goalie-rich source was handed goalies by the rest of the room (4-5 in 6 picks). Against this room
our consensus board's edge falls from +6.2 to +3.4 +/- 2.7 points a week, and a perfect board is
worth +5.7 +/- 2.8 instead of +2.6 (`Season/README.md`, section 9 step 2).


## live.json

What the live runner does with tonight's reports that the models never saw
(`Projections/project_tonight.py`):

- `questionable_p_plays_cap`: `DTD` / `GTD` -> the highest `p_plays` (or `p_start` for a goalie
  without a report) such a player gets. Judgment calls until the live snapshots measure them.
- `goalie_report_p_start`: Daily Faceoff report strength -> the named goalie's `p_start`; his
  partners share the rest in proportion to the model. `Confirmed` is measured (99.7%, 2025-26).

Keys starting with `_` are notes (sources, dates), not settings.

## leagues/

One file per league we play (`Live/leagues.py` loads and validates them; unknown or missing keys
fail). Every Live tool takes `--league <name>` (default `beagles`), and the pipeline's Fleaflicker
importer and snapshot job read the active Fleaflicker league's id from here.

```json
{
  "name": "beagles",                      "description": "...",
  "platform": "fleaflicker",              "league_id": 12090,
  "season": "2026-27",                    "team": {"id": 63341, "name": "Burnaby Beagles"},
  "rules": "league",                      "scoring": "points-league",
  "strategy": null,                       "eligibility_platform": "fleaflicker",
  "adp_platform": "espn",                 "active": true,
  "draft_window": { ... }
}
```

- `platform`: `fleaflicker`, `espn` or `standalone`. A league whose draft the tools cannot read
  (ESPN until its reader exists, or one without an id) runs the draft tools `--standalone`
  (`--slot` required).
- `rules` / `scoring` / `strategy`: names in `rosters/`, `scoring/` and here (`null` = strategy.json).
- `eligibility_platform` / `adp_platform`: whose positions the draft tools value players on (the
  aggregate workbook's "Site Used (POS)"; any platform with a
  `fantasy_positions_<platform>_<season>.parquet`), and whose ADP they show beside the board
  (display only). These used to be `strategy.json`'s `draft` block; they are per league now. The
  simulator keeps `rosters/*.json`'s own `eligibility_platform`, so no ladder number moves.
- `active`: whether scheduled jobs run the league.
- `auth`: for a private league, a key into `secrets.json` (gitignored, never committed) holding its
  login -- for ESPN the `espn_s2` and `SWID` cookies from a logged-in browser (F12 -> Application ->
  Cookies -> espn.com). They expire when you log out of ESPN; a 401 means copy fresh ones. `null`
  for a public league.
- `rules` / `scoring` for a readable league can be generated from the platform:
  `Live/import_league_settings.py --league <name> --write` (ESPN today).
- `draft_window`: the draft window's saved settings (⚙ Save as default writes them here): playoff
  dates, teams, slots, bench, points per stat and the two platforms, overriding the fields above
  **for the draft tools only**. `{}` = use the files.

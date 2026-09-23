# LeagueSettings

The league's rules, as files you edit. Nothing in `Projections/`, `Simulation/` or `Season/`
hard-codes what a goal is worth or how many slots a team has -- they all read from here.

```
scoring/   what each stat is worth          read by Projections, Simulation, Season  (--weights)
rosters/   slots, bench, IR, moves, playoffs read by Season                          (--league)
```

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
  "week_starts_on": "MON",
  "regular_season_weeks": 26,
  "playoff_rounds": 3,
  "playoff_teams": 8,
  "eligibility_platform": "yahoo",
  "eligibility_season": "2026-27"
}
```

- **Slots:** `C LW RW D G`, plus composite `F` (any forward) and `F/D` or `UTIL` (any skater).
- `teams` must be even, and `playoff_teams` must equal `2 ** playoff_rounds`.
- Position eligibility comes from the platform named in `eligibility_platform`.
- `league.json` is the default when `--league` is omitted.

`Season/` names its ladder reports and docs after the roster file (`ladder_2025-26_<file>.json`),
so give each format its own file rather than editing one in place between runs.

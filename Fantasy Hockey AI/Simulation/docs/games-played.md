# Games played: a season-level distribution

Step 1 of the boom/bust plan (`~/.claude/plans/boom-bust-odds.md`). `games_played.py` gives
every returning regular a distribution of games played (skaters) or games started (goalies) for
the coming season, fitted only on seasons before it. Built 2026-09-26.

## Data

- `Stats.SeasonTotals`: the NHL.com stats reports' season line per player, 2000-01 to 2025-26,
  24,746 rows (`pipeline/import_season_totals.py`; 3,193 retired players added to
  `Reference.Players` with their birth dates). Skater games played match our own game-level
  `Stats.PlayerSeasonStats` for 2,782 of 2,784 player-seasons in 2023-24 to 2025-26.
  (`PlayerSeasonStats` counts a goalie's *dressed* games, not played ones, so goalies differ
  there by design.)
- `Injuries.Spells`: injury games lost per skater-season. The 642 COVID-protocol spells of
  2020-21 and 2021-22 are kept apart and are not injury history.
- `Reference.Schedule`: team games per season (48 in 2012-13, 68-71 in 2019-20, 56 in 2020-21,
  **84 in 2026-27**).

Exported by `ModelFeatures/build_season_history.py`.

## Who and what

- **Skaters:** played at least 25% of last season's team games. The outcome is games played this
  season, 0 if he has no line. A player with no line *and* no injury spell left the league
  (retired, Europe, the minors all year). That's 1,462 of 14,872 rows, mostly fringe players,
  and they are dropped because a draft board would not list them.
- **Goalies:** made at least 15% of last season's starts. The outcome is games started. There
  is no goalie injury history, so a goalie hurt all season is dropped along with those who left.
- **Covariates:** age (with bends at 24 and 31), position, the last three seasons' share of
  games played and share lost to injury, injury spell count, last season's ice time and points
  per game. For goalies: starts shares and last season's save percentage.
- **Not covered:** rookies and call-ups (no prior NHL line), and any offseason news (a player
  already hurt at the draft). Both are for the season simulator to add.

## Model

A location-scale ordered logit over 15 bands of share played, finer near a full season (0.9 /
0.93 / 0.96 / 0.99). Within a band, games are spread evenly. The covariates shift the location
and stretch the scale, so a risky player's distribution is wider, not just lower. Plain scipy
maximum likelihood, no model library.

Three shapes were compared on the same rolling-origin check (skaters, 2008-09 to 2025-26):

| model | RPS | MAE games | Brier under 75% |
|---|---|---|---|
| **ordered logit (shipped)** | **1.244** | **13.75** | **0.177** |
| two-part beta-binomial mixture | 1.248 | 13.77 | 0.177 |
| beta-binomial | 1.250 | 13.96 | 0.178 |
| tier baseline (last season's share x age band, empirical) | 1.354 | 14.91 | 0.195 |

**A counting bug found on the way:** the first check summed the probability bands below 80% and
scored the outcome at below 75%, so every P(under 75%) looked about 5 points high, in every
season. That looked like a model problem, and a flexible model that matches training
frequencies exactly showed the same gap, which gave it away. Events are now built from the band
edges directly (`_event_bands`).

## Rolling-origin results

Each season is scored with a fit on the seasons before it only.

**Skaters:** better than the tier baseline in **18 of 18** seasons.

| seasons | players | RPS model / tier | MAE games model / tier | PIT in 10-90 |
|---|---|---|---|---|
| all (2008-09 to 2025-26) | 11,100 | 1.244 / 1.354 | 13.7 / 14.9 | 0.805 |
| 2023-24 | 640 | 1.244 / 1.378 | 14.3 / 15.7 | 0.797 |
| 2024-25 | 657 | 1.194 / 1.290 | 14.6 / 15.6 | 0.799 |
| 2025-26 | 645 | 1.235 / 1.342 | 14.1 / 15.2 | 0.806 |

- The 10-90 interval holds 80.5% of outcomes (target 80%).
- **Regulars** (at least 60% of games played and 14+ minutes last season) are calibrated in every decile, for all three events.
- Across all skaters, the riskiest decile (fringe players on about 12 minutes) busts more than predicted: P(under half) says 0.50 when the real rate is 0.57. Those players are rarely drafted.

**Against the consensus** (players at least 3 sources project):

| | 2024-25 | 2025-26 |
|---|---|---|
| mean projected games, consensus | 73.6 | 71.4 |
| mean expected games, model | 61.9 | 62.8 |
| mean actual games | 63.4 | 62.6 |
| median absolute error, model median | 12.96 | 13.19 |
| median absolute error, consensus | 13.73 | 13.34 |
| correlation with actual, model / consensus | 0.574 / 0.598 | 0.555 / 0.573 |

**The consensus projects about 10 games too many per skater.** Its season totals carry that
optimism, so the season simulator should draw **per-game rates x these games-played draws**,
not use the consensus totals. The consensus ranks players slightly better (it knows offseason
news and role), so blending its games-played number in is a follow-up. It can only be fitted
on 2024-25 and checked on 2025-26.

**Goalies:** better than the tier baseline in **18 of 18** seasons (RPS 0.671 vs 0.751;
MAE 10.8 vs 12.2 starts). Intervals are a little wide (87% inside 10-90). Starters' workloads
fell after the 2000s, and the full-history fit still lands about 3 starts under in 2025-26. A
linear era term overshot the other way, and a 5-season window gained only about 1% on this
same check, so neither ships.

## Files

- `reports/games_played_{skaters,goalies}_{2024-25,2025-26,2026-27}.json`: fits for each
  season, on the seasons before it.
- `reports/games_played_scored_{skaters,goalies}_ordinal.parquet`: the rolling-origin rows.

```
python games_played.py --evaluate [--goalies] [--model ordinal|mixture|betabinomial]
python games_played.py --season 2026-27 [--goalies]
```

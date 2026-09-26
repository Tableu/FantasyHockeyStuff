# Scoring-rate error: a season-level shock per stat

Step 2 of the boom/bust plan (`~/.claude/plans/boom-bust-odds.md`). `rate_error.py` gives every
returning skater a distribution for how far his per-game scoring lands from a preseason
projection, stat by stat, with the stats moving together. Built 2026-09-26. Goalies' per-start
rates are not in it yet.

## The model

For each scored stat (goals, assists, PPP, SHP, shots, hits, blocks, PIM in penalty units):

    count ~ Poisson(projected rate x games played x M),   log M ~ Normal(m(x), s(x)^2)

- **M** is the season's rate shock: a breakout, a collapse, a new role.
- **The Poisson** is the ordinary luck of counting over that many games. It shrinks with games
  played; the shock doesn't. A zero is just a count, with no log-of-zero problem.
- **m and log s** are linear in: age (bends at 24 and 31), position, games behind the
  projection, how far the projection sits from the position mean, a new team, and the season's
  own games played (the simulator draws games first; players who play more score at higher
  rates).
- **Fitting:** maximum likelihood, 32-node Gauss-Hermite quadrature, analytic gradient (checked
  against finite differences to 4e-10). Scipy only; about 0.6 s per stat.

**The yardstick projection.** Only 2024-25 and 2025-26 have the consensus beside the actuals, so
every season from 2003-04 gets a Marcel-style projection instead:
- Last three seasons' per-game rates, weighted 5/4/3 by games.
- Pulled toward the position (F/D) mean by k games of that mean. k is fitted before 2024-25:
  goals 120, assists 80, PPP 80, SHP 480, shots 60, hits 40, blocks 160, PIM 120.

The pool is the same as games played: at least 25% of last season's games. At least 10 games
this season are needed to count.

## Rolling origin, 2008-09 to 2025-26

Each season is fitted on the seasons before it. Compared with **one constant shock per stat**
(same average, same spread for everyone):

| stat | log score model / constant | PIT in 10-90 | below 10 / above 90 |
|---|---|---|---|
| goals | -2.622 / -2.682 | 0.810 | 0.094 / 0.097 |
| assists | -3.059 / -3.132 | 0.815 | 0.097 / 0.089 |
| PPP | -2.195 / -2.321 | 0.795 | 0.120 / 0.086 |
| SHP | -0.965 / -0.984 | 0.800 | 0.098 / 0.102 |
| shots | -4.376 / -4.489 | 0.827 | 0.101 / 0.073 |
| hits | -4.381 / -4.406 | 0.791 | **0.136** / 0.073 |
| blocks | -3.790 / -3.818 | 0.829 | 0.081 / 0.090 |
| PIM | -3.113 / -3.127 | 0.795 | 0.103 / 0.102 |

- **Better on all eight stats.** Hits land low too often, since recorded hits depend on the
  arena's scorers and have drifted.
- **Typical shock (the spread at the median player):** shots 0.19; blocks 0.23; assists, hits
  and PIM about 0.28; goals 0.31; **PPP and SHP about 0.79**. Power-play role is the least
  predictable thing a skater has.

**How the stats move together** (shock correlation, adjusted for shrinkage):
- Goals, assists, PPP and shots share one role shock: goals-shots 0.83, assists-PPP 0.87,
  goals-PPP 0.77.
- Hits, blocks and PIM mostly move on their own (0.1-0.2).

**Season fantasy points at the actual games played** (11,000 player-seasons):

| | points league: in 10-90 / CRPS | banger league: in 10-90 / CRPS |
|---|---|---|
| **correlated shocks (shipped)** | **0.808 / 19.6** | **0.805 / 31.6** |
| independent shocks | 0.684 / 19.9 | 0.724 / 31.8 |
| one constant shock per stat | 0.806 / 21.7 | 0.830 / 33.7 |

Ignoring the correlation makes the fantasy-point range far too narrow. A constant shock gets
the width right but aims worse (CRPS about 10% higher).

## The season everyone shares

League-wide scoring moves from season to season, and every player moves with it:

| | goals | assists | PPP | SHP | shots | hits | blocks | PIM |
|---|---|---|---|---|---|---|---|---|
| season-to-season spread | 3.6% | 3.7% | 6.2% | 15.5% | 3.8% | 5.3% | 4.8% | 5.8% |

- **In 2024-25:** shots came in about 10% under projection league-wide, and fantasy points 5-7%
  under. Every player's range missed low together (17% below the 10th percentile).
- **Why it matters:** the per-player shock was fitted across seasons, so it already contains
  this spread, but only as if players moved independently.
- **For step 3:** draw the season shock once per simulated season and take its variance out of
  each player's shock. It mostly cancels in boom/bust measured against par, since par moves
  with the league, but it matters for absolute point ranges. Saved as `season_effects` in the
  fit JSON.

## The consensus

On 2024-25 and 2025-26, the history's shock was scored around the consensus per-game rates
(10-11 sources, players 3+ of them project) and around the yardstick, on the same players:

- **Stat by stat, the consensus is about as accurate as the yardstick.**
  - Hits: the consensus is better both seasons.
  - PPP and SHP: worse in 2024-25, when fewer sources projected them.
  - Everything else: within a few thousandths of a log score.
- **Fantasy points at the actual games played** (points league):

  | | 2024-25 in 10-90 / CRPS | 2025-26 in 10-90 / CRPS |
  |---|---|---|
  | consensus | 0.779 / 21.9 | 0.791 / 20.4 |
  | yardstick | 0.779 / 21.0 | 0.793 / 20.0 |

  The consensus's per-game rates run about 4% optimistic.
- **No transfer factor ships.** A per-stat shift and scale fitted on one season does not hold
  in the next: PIM -0.12 then +0.05, SHP scale 1.22 then 0.96. The shifts are mostly that
  season's league-wide drift. **The history's shock is used around the consensus unchanged.**
- Combined with step 1, where the consensus projects about 10 games too many: the consensus
  **rates** are usable as they are, but its **totals** are not.

**Found on the way:** two projection sources carry negative counts.
- Bangers 2025-26: PPP -1 for four players, likely a "not projected" placeholder.
- Dom 2026-27: goals between -0.2 and -0.5 for three players.

`consensus_rates` treats them as missing. The live board's `Decisions/draft.consensus_lines`
still averages them in (a small effect).

## Files

- `reports/rate_error_{2024-25,2025-26,2026-27}.json`: per stat, the shock's coefficients
  (`beta` for m, `gamma` for log s), `correlation`, `season_effects`, `marcel_k`. Each is fitted
  on the seasons before it.
- `reports/rate_error_scored.parquet` and `rate_error_fantasy_<league>.parquet`: the check rows.

```
python rate_error.py --evaluate --fantasy points-league --fantasy banger-league --consensus
python rate_error.py --season 2026-27
```

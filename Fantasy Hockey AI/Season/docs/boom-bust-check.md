# Boom/bust odds: the calibration check (step 4)

Step 4 of the boom/bust plan (`~/.claude/plans/boom-bust-odds.md`), run 2026-09-26 with
`boom_bust_check.py`. **Passed.** The odds beat both baselines on both held-out seasons.

## How it is graded

- **"Where taken":** each checked season's real draft slot is the mean Yahoo/Fantrax ADP rank,
  top 252 = 14 teams x 18 rounds. ADP enters only here; the model itself never sees it.
- **Predicted:** boom and bust at that pick, from `boom_bust.py`'s draws and par curve. Every
  fit behind them was trained on earlier seasons only.
- **Happened:** the player's actual VOR against the same thresholds. Actual points minus the
  lowest actual replacement among his positions, where a position's replacement is the mean
  actual points of the three best-projected players ADP left undrafted.
- **Baselines:**
  - *Round average:* everyone in a round gets the model's mean odds for that round. Does the
    per-player part add anything?
  - *Other season:* the other checked season's observed rate per round, the historical base
    rate a manager could have used.

## Summary

| | 2024-25 boom | 2024-25 bust | 2025-26 boom | 2025-26 bust |
|---|---|---|---|---|
| Brier, model | **0.213** | **0.226** | **0.207** | **0.221** |
| Brier, round average | 0.233 | 0.238 | 0.234 | 0.248 |
| Brier, other season's rate by round | 0.253 | 0.252 | 0.262 | 0.273 |
| AUC, model | **0.74** | **0.63** | **0.73** | **0.68** |
| AUC, round average | 0.68 | 0.51 | 0.60 | 0.55 |
| mean predicted / observed | 0.43 / 0.52 | 0.43 / 0.34 | 0.45 / 0.39 | 0.44 / 0.44 |

- **The per-player odds carry real information.** Within a round, the round average ranks
  busts no better than chance (AUC 0.51-0.55), while the model gets 0.63-0.68. Read
  comparatively, as intended: a player at 25% bust really is safer than one at 60% at the same
  pick.
- **2025-26 is well calibrated.** Bust is within about 5 points in every quintile; boom runs
  about 6 points high.
- **2024-25 missed in one direction.** It was a season where drafted players beat par: realized
  par was above simulated from round 3 on, and RW's actual replacement (146) sat far below C and
  LW (209). So bust was over-predicted in the upper quintiles (0.68 against 0.51) and boom
  under-predicted.
- **Replacement is thin.** One season's replacement rests on three players per position, so a
  single season can shift a whole position's odds. The ranking (AUC) held up anyway.
- **Season point ranges are too wide for drafted players:** 87-90% inside 10-90 (step 3 saw the
  same). They are not narrowed here. The odds are what the board shows and they calibrate; a
  narrower spread would push every probability outward.

## By group

| group | season | players | bust predicted / observed | boom predicted / observed | AUC bust |
|---|---|---|---|---|---|
| rookies (no 21-game season before) | 2024-25 | 16 | 0.76 / 0.56 | 0.22 / 0.31 | 0.71 |
| | 2025-26 | 13 | 0.78 / 0.69 | 0.19 / 0.23 | 0.78 |
| age 31+ | 2024-25 | 69 | 0.53 / 0.42 | 0.34 / 0.48 | 0.67 |
| | 2025-26 | 73 | 0.54 / 0.49 | 0.38 / 0.38 | 0.63 |
| rounds 1-3 | 2024-25 | 42 | 0.33 / 0.36 | 0.25 / 0.17 | 0.44 |
| | 2025-26 | 42 | 0.40 / 0.50 | 0.34 / 0.24 | 0.73 |
| rounds 10-18 | 2024-25 | 126 | 0.47 / 0.32 | 0.47 / 0.61 | 0.66 |
| | 2025-26 | 126 | 0.46 / 0.46 | 0.49 / 0.44 | 0.68 |

- **Rookies' bust odds run 10-20 points high.** The games fallback (consensus games x 0.54-0.6,
  very wide) is too pessimistic, but they rank well. Rookie priors (draft slot, AHL) remain the
  fix if it matters.
- **Rounds 1-3:** too few players to judge (42 a season). Boom is over-predicted both seasons.

## Detail

### 2024-25

252 players ADP drafts (top 252 by mean Yahoo/Fantrax ADP) with draws.

| | mean predicted | observed | Brier model | Brier round avg | Brier other season | AUC model | AUC round avg |
|---|---|---|---|---|---|---|---|
| boom | 0.433 | 0.520 | 0.2131 | 0.2326 | 0.2526 | 0.744 | 0.675 |
| bust | 0.432 | 0.341 | 0.2256 | 0.2384 | 0.2517 | 0.633 | 0.514 |

Reliability, boom (quintiles of the predicted odds):

| predicted range | players | predicted | observed |
|---|---|---|---|
| (0.0208, 0.265] | 51 | 0.172 | 0.216 |
| (0.265, 0.401] | 50 | 0.337 | 0.360 |
| (0.401, 0.5] | 50 | 0.444 | 0.620 |
| (0.5, 0.596] | 50 | 0.540 | 0.600 |
| (0.596, 0.817] | 51 | 0.674 | 0.804 |

Reliability, bust (quintiles of the predicted odds):

| predicted range | players | predicted | observed |
|---|---|---|---|
| (0.0776, 0.286] | 51 | 0.221 | 0.216 |
| (0.286, 0.379] | 50 | 0.330 | 0.320 |
| (0.379, 0.463] | 50 | 0.422 | 0.240 |
| (0.463, 0.557] | 50 | 0.507 | 0.420 |
| (0.557, 0.926] | 51 | 0.682 | 0.510 |

Season points inside the 10-90 range: 0.869 (below 0.095, above 0.036).

Par by round, simulated vs realized (actual VOR by ADP pick, smoothed):

| round | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simulated | 273 | 180 | 144 | 132 | 128 | 113 | 84 | 70 | 60 | 45 | 38 | 32 | 27 | 20 | 10 | 9 | 5 | -2 |
| realized | 218 | 168 | 162 | 142 | 121 | 107 | 107 | 107 | 88 | 68 | 59 | 59 | 51 | 51 | 51 | 38 | 18 | 9 |

### 2025-26

252 players ADP drafts (top 252 by mean Yahoo/Fantrax ADP) with draws.

| | mean predicted | observed | Brier model | Brier round avg | Brier other season | AUC model | AUC round avg |
|---|---|---|---|---|---|---|---|
| boom | 0.453 | 0.393 | 0.2065 | 0.2343 | 0.2622 | 0.726 | 0.601 |
| bust | 0.443 | 0.444 | 0.2214 | 0.2481 | 0.2727 | 0.682 | 0.547 |

Reliability, boom (quintiles of the predicted odds):

| predicted range | players | predicted | observed |
|---|---|---|---|
| (0.08839999999999999, 0.296] | 51 | 0.190 | 0.098 |
| (0.296, 0.41] | 50 | 0.353 | 0.300 |
| (0.41, 0.525] | 50 | 0.469 | 0.440 |
| (0.525, 0.616] | 50 | 0.564 | 0.500 |
| (0.616, 0.85] | 51 | 0.689 | 0.627 |

Reliability, bust (quintiles of the predicted odds):

| predicted range | players | predicted | observed |
|---|---|---|---|
| (0.126, 0.295] | 51 | 0.226 | 0.235 |
| (0.295, 0.378] | 50 | 0.337 | 0.380 |
| (0.378, 0.475] | 50 | 0.419 | 0.440 |
| (0.475, 0.594] | 50 | 0.527 | 0.500 |
| (0.594, 0.891] | 51 | 0.703 | 0.667 |

Season points inside the 10-90 range: 0.905 (below 0.067, above 0.028).

Par by round, simulated vs realized (actual VOR by ADP pick, smoothed):

| round | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simulated | 221 | 157 | 148 | 131 | 127 | 100 | 84 | 78 | 63 | 52 | 46 | 33 | 22 | 14 | 12 | 12 | 12 | 5 |
| realized | 186 | 136 | 116 | 116 | 100 | 86 | 83 | 79 | 70 | 35 | 34 | 34 | 27 | 24 | 19 | 10 | 4 | -10 |


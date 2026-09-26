# Boom and bust odds: the season simulator and the par curve

Step 3 of the boom/bust plan (`~/.claude/plans/boom-bust-odds.md`). Built 2026-09-26. Step 4
passed (`docs/boom-bust-check.md`). Step 5 put the odds on the draft board, the assistant and
the draft window (Live/README.md): at the median room pick on the board, at your next pick
in the assistant and the window. The margin stays one round and the odds are read
comparatively (the user's call, 2026-09-26).

## Pieces

- **`Simulation/season_spread.py`: whole seasons, drawn.**
  - **Skaters:**
    - Games come from `games_played` for returning regulars.
    - Anyone else (rookies, call-ups) gets a fallback: consensus games x how far the consensus
      overshot before, with a wide beta-binomial. Fitted on earlier consensus seasons: actual /
      consensus = 0.56 (2024-25) and 0.53 (2024-25 + 2025-26), precision about 1.
    - Per-game rates are the consensus's. A stat no source projects (most skip SHP) takes the
      Marcel yardstick, not zero.
    - Rates are multiplied by the league's season shock (one per draw, shared) and his own
      correlated shock (`rate_error`, net of the league's share, mean kept), then Poisson counts.
  - **Goalies:**
    - Starts come from `games_played --goalies`.
    - Points per start come from the consensus.
    - One log-normal shock is fitted on history. Its spread splits into a season shock of about
      0.06 and counting noise of about 1.15 / starts.
- **`Season/boom_bust.py`: the room, the par curve, the odds.**
  - **The room:** 200 simulated drafts in which every seat is a realistic opponent
    (`field.json`, `source_subsets`, goalie cap), run through `draftroom.run`. No ADP.
  - **Realized VOR:** draw d pairs with room d mod 200. A position's realized replacement is the
    mean points of the three best-projected undrafted players. VOR is points minus the lowest
    replacement among his positions (the board's rule).
  - **Par:** par(p) is the mean realized VOR at pick p, pool-adjacent-violators smoothed so it
    never rises.
  - **The odds:** bust = P(VOR < par(p+14)); boom = P(VOR >= par(p-14)). Both are reported at
    the player's median room pick. Every draw is saved so any other pick is a lookup.
  - **Runtime:** 30-40 s for 200 drafts x 5,000 seasons.

## Two bugs caught on the way (both extrapolation)

- **Rookies at evidence -5.** A player with no games behind his projection sat at -5 on the
  evidence covariate. Training never goes below -3.2, and the fitted spread there reached
  exp(5). A 2024-25 board had Matt Benning projected at 24,700 points. Fix: evidence is floored
  at the least any modelled player has. No training row sits below it, so the fits are
  unchanged.
- **Consensus PPP near zero.** The consensus projects PPP at about 0.001 for many depth players,
  a level (log proj / position mean) of -7 that the Marcel-based fit never saw. Fix: every
  covariate is clamped to its training range, and the ranges are stored with each fit.

## First look at the draws (not the step-4 gate)

Full seasons, games drawn, points league:

| season | players | in 10-90 | below / above | mean sim / actual |
|---|---|---|---|---|
| 2024-25, all | 772 | 0.817 | 0.115 / 0.067 | 171 / 162 |
| 2024-25, top 250 by mean | 250 | 0.876 | 0.096 / 0.028 | 289 / 270 |
| 2025-26, all | 765 | 0.810 | 0.112 / 0.077 | 167 / 160 |
| 2025-26, top 250 by mean | 250 | 0.896 | 0.076 / 0.032 | 280 / 267 |

- **The top 250 have ranges that are too wide**, and the draws run 5-7% optimistic.
- **The optimism** comes from the consensus rates (about 4%) and those two seasons' league-wide
  dips. It is shared by everyone, so it mostly cancels against par.
- **The width:** the games-played ranges look wide because their 90th percentile sits at the
  full season. At the actual games played, the rate part covers 83% (2025-26) and 79% (2024-25).
  Not tuned here: step 4 decides with the real question (do the odds come true?).
- **Goalies:** 85-87% inside 10-90.

## What the odds look like: the margin question

With the one-round margin, the average boom and bust for players drafted in at least half the
rooms (2025-26) are:

| margin | mean boom | mean bust | bust across players, 10th-90th pct | rounds 1-3 boom / bust |
|---|---|---|---|---|
| 1 round (14) | 0.47 | 0.43 | 0.26-0.64 | 0.38 / 0.38 |
| 2 rounds (28) | 0.41 | 0.40 | 0.22-0.61 | 0.25 / 0.35 |
| 3 rounds (42) | 0.35 | 0.37 | 0.19-0.57 | 0.19 / 0.29 |

- **Par falls quickly in the first three rounds** (275 to 157 VOR over the first two), then
  slowly (about 10-20 VOR a round). A season's spread is 100-200.
- **So after round 3, beating or missing the next round's par is close to a coin flip**
  whatever the margin. That is the honest shape of a draft, not a flaw of the definition.
- **What carries information is the difference between players:** a 26% bust against a 64% one
  at the same pick.
- The margin is `--margin`, default one round. A wider margin makes the words "boom" and "bust"
  rarer, but does not make the odds more different between players.

## Outputs (`Season/reports/`, gitignored)

- `boom_bust_<season>_<scoring>.parquet`: per player, `room_pick`, `drafted_share`, `boom_pct`,
  `bust_pct`, `mean`, `p10`, `p50`, `p90`, `exp_gp`, `vor_board`.
- `..._par.csv`: the par curve.
- `..._vor_draws.npy` + `..._players.csv`: realized VOR, draws x players, for the draft
  assistant's "at my next pick".
- `..._meta.json`: run settings and the fitted fallback and goalie shock.

```
python boom_bust.py --season 2026-27 --draft-date 2026-09-26
python boom_bust.py --season 2025-26 --draft-date 2025-10-07      # the step-4 backtest's draws
```

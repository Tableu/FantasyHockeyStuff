# Simulation

The Monte Carlo layer — section 5 of the Fantasy Hockey AI build plan. It takes the lambda
table `../Projections/predict.py` writes and turns each mean into a distribution, so the
layers above can ask the questions a manager actually asks:

```
Projections/predict.py   a mean per player-game       "he averages 2.6 shots"
Simulation/simulate.py   a distribution per game      "27% to be held off the sheet,
                                                       8% to go for three"
Simulation/rosters.py    a total, and a matchup       "you are 5.2% to win this week"
```

**This layer applies no scoring system and opens no database.** What a goal is worth is data
you supply (`../LeagueSettings/scoring/*.json`, or your own), so one set of draws answers
several leagues at once; and like `Projections/`, everything here starts from parquet and
JSON read by path. There is no `pyodbc` in `requirements.txt`, and no model library either.

```
paths.py          where the lambda table, the dispersion and the fitted structure live
marginals.py      the per-category distributions, as inverse-CDF functions
copula.py         correlation between players, imposed on the uniforms
correlations.py   fits that structure from the scored holdout        -> reports/correlations.json
sampler.py        one draw of a night: lambdas in, stat lines out
scoring.py        a scoring file applied to sampled stat lines
simulate.py       CLI: a lambda table -> per-player-game distributions
rosters.py        roster totals, head-to-head matchups, start/sit
validate.py       the calibration report                             -> docs/calibration.md
```

## Running it

```bash
pip install -r requirements.txt

python correlations.py                      # once per model build; writes the fitted structure
python simulate.py --season 2025-26 --variant A --sims 2000 \
    --weights points-league --weights banger-league
python validate.py --sims 300 --weights points-league --independent --docs
```

`correlations.py` depends on `../Projections/reports/predictions_B.parquet` and
`dispersion.json`; `simulate.py` on `../Projections/reports/lambdas_<season>_<variant>.parquet`.
Refit the structure whenever the projection models are retrained — the correlations are
residual correlations, so they belong to a particular fit.

## How a night is drawn

The order is the order the constraints run in:

| step | what | why this way |
|------|------|--------------|
| `plays ~ Bernoulli(p_plays)` | the scratch gate | a projection is worth zero if he does not dress, so it multiplies everything |
| `shots, hits, blocks, assists ~ NB(lambda, theta)` | the counts | `Var = mu + theta*mu^2`, theta fit by `Projections/calibrate.py`. A Gamma-mixed Poisson *is* an NB, which is the plan's "game quality multiplier" in closed form |
| `goals ~ Binomial(shots, lambda_goals/lambda_shots)` | goals out of shots | a goal *is* a shot on goal, so two goals on zero shots is impossible by construction, and the measured within-player shots/goals correlation of 0.299 comes out of the mechanism |
| `pim = 2*minors + 5*majors + 10*misconducts` | a compound Poisson over a latent intensity | 98.7% of penalty minutes are even; an NB would draw 1s and 3s that never happen. The latent covers escalation — without it, four-minute nights come out at half their true rate |
| each point → PP / SH / EV | the strength split | the projection carries shares, so `ppp + shp <= points` holds in every draw |

## The part that is not obvious

A single shared "game quality" multiplier, as the plan first sketched it, cannot work, and
the holdout says why. A shared *rate* multiplier only creates correlation by also creating
overdispersion — but assists are Poisson-marginal (fitted theta ~0) and two teammates'
assists still correlate at +0.045. That correlation is a shared **event** — one goal hands
out two assists — not a shared rate. So correlation goes on the copula, where it cannot move
a marginal, and the structure is fit rather than assumed:

| | measured |
|---|---|
| teammates, same category | assists +0.045, pim +0.028, blocks +0.022, hits +0.012, shots +0.011, goals −0.002 |
| teammates, across categories | assists × goals **+0.067**, the largest cross-player number on the board |
| opponents | within noise of zero except pim at **+0.056** — a fight hands both benches minutes at once — and negative for shots and blocks, because possession is zero-sum |

Neither cross-player matrix is positive-semidefinite, and that is not noise: bootstrapped
over games the standard error on an entry is ~0.0015. A sum-of-shared-factors construction
would force both PSD and hand teammate goals a correlation of +0.039 where the data says
−0.002. `copula.py` instead builds the exchangeable block exactly — team-mean-centred noise
plus the two teams' level vectors drawn jointly — which admits the negative eigenvalue. See
its module docstring for the algebra and the one constraint that does bind (a side of 18
cannot carry an eigenvalue below −1/17; the fit stays inside it).

## Why it matters

Twenty thousand ten-skater rosters drawn from the 2025-26 holdout, variance of the total
against what independent players would give:

| roster | independent sampling | this layer | measured on the holdout |
|---|---|---|---|
| ten random skaters | 1.00 | 1.06 | 1.10 |
| ten skaters from one NHL team | 1.00 | 1.61 | 1.74 |

Independent sampling understates a stacked roster's spread by a quarter of its standard
deviation. On a real week that moved an underdog's win probability from 4.1% to 5.2% — and
spread is the whole of what a head-to-head decision turns on.

### It is not fitted to this season's noise

Both the dispersion and the correlation structure are fitted on the 2025-26 holdout, so
agreement there is consistent with the structure being real *and* with it having been fitted
to one season's noise. `validate.py --split-half` settles which: it splits the season at its
median date, re-fits every fitted number on the training half -- dispersion included, or the
test half stays inside the fit -- and scores the held-out half three ways.

| tested on | structure fitted on | stack variance ratio | actual |
|---|---|---|---|
| second half | first half (out-of-sample) | 1.570 | 1.651 |
| second half | second half (in-sample ceiling) | 1.586 | 1.667 |
| second half | nothing (independent) | 1.001 | 1.667 |
| first half | second half (out-of-sample) | 1.585 | 1.781 |
| first half | first half (in-sample ceiling) | 1.583 | 1.791 |

**Out-of-sample is the in-sample ceiling, to within noise** -- in one direction it edges past
it, which is the tell that the remaining gap is Monte Carlo rather than fit. The measured
inputs say the same: teammate assists 0.0444 / 0.0448 across the two halves, assists x goals
0.0681 / 0.0668, teammate goals -0.0025 / -0.0008. This is a stationary structure, not a
season's noise.

Per-category variance ratios move by at most 0.012 between a structure fitted on the other
half and one fitted on the test half itself. The one visible deviation -- blocks at 1.09 on
the second half -- is *equally* present in-sample, so it is the blocks model's +5.3% level
bias showing up as variance, not overfitting.

`docs/calibration.md` carries the full report: means, variance, tails and PIT per category,
generated by `validate.py --docs` rather than typed in beside it. Two things to know when
reading it:

- **A gap between the simulated mean and the *actual* mean is the projection layer's, not
  this one's.** The table separates them: against the lambda it was handed the sampler is
  within 0.1% everywhere, while blocks run +5.3% and penalty minutes −12.2% against reality
  because that is where those models sit (`Projections/drift.py` is the fix for that, and it
  is off by default).
- PIM's variance ratio of 0.79 is the same confound — the rate is 12% low, so the total is
  too. Its *tails* at matched thresholds land within 2%.

## Not covered

**Goalies.** The goalie model stack was measured and removed (see the build log), and the
agreed treatment is `p(start) × league average` rather than projected goalie quality. Nothing
here samples a goalie, so a roster total from `rosters.py` is its skaters only. Doing it
properly needs an empirical start distribution with an explicit pull component — a goalie's
outcome has a real left tail, which is the one place the Gamma-Poisson machinery here would
fit badly — and the per-start goalie lines that would come from are not currently exported to
parquet by `ModelFeatures/`.

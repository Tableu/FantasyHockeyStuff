# Projections

Per-category projection models for skaters — section 3 of the Fantasy Hockey AI build plan.
They read the shared feature table built by the sibling `ModelFeatures/` folder and emit, for
each player-game, the lambda table the Monte Carlo layer (section 5) turns into distributions.

**These models project stats, not fantasy points.** Nothing here knows what a goal is worth.
A scoring system is data you supply — `weights.py` loads a JSON file and applies it — so one
fitted stack serves any number of leagues, and two formats can be compared against one fixed
set of models. `../LeagueSettings/scoring/` holds the scoring files; none of them is a default and nothing loads one
automatically.

## Rest-of-season (section 4)

A different question from the per-game stack, with a different bias: not "what will he do
tonight" but "what will he produce over the games that remain". Tonight's model leans on
recent form because recent form predicts tonight; over a horizon a hot streak should regress
instead of extrapolate.

```
ros.py            forward-looking targets over a date window   -> reports/ros_<season>_<n>d.parquet
ros_baselines.py  the shrinkage ladder, fitted and scored      -> reports/ros_baselines.json
```

The quantity is decomposed the same way the per-game chain is, because the factors stabilize
at very different speeds and lumping them into "points per game" throws that away:

    production  =  team games in the window  x  availability  x  ice time  x  rate per 60

Windows are measured in **days, not games** — a game-count window has to decide whose games
to count, which breaks when a player is traded, while a date window leaves the opportunity
term as something a manager can look up in advance. Windows running past the end of a season
are dropped rather than truncated, since a partial window looks like a player who stopped
producing.

### The ladder comes before any model

Scored on 2025-26 after fitting on 2023-24 and 2024-25, one row per player per week, window
fantasy points under points-league scoring:

| rung | MAE | RMSE | Spearman |
|---|---|---|---|
| last-10 games (recency) | 17.91 | 24.06 | 0.532 |
| season-to-date, unshrunk | 15.05 | 21.14 | 0.680 |
| **empirical-Bayes shrunk** | **13.89** | **18.79** | **0.704** |
| shrunk + a recency term | 13.84 | 18.73 | 0.707 |

Two results worth keeping. **Recency is actively harmful over a horizon** — the last ten
games are 19% worse than season-to-date on MAE and lose 0.15 of Spearman, which is the
clearest confirmation available that a rest-of-season projection is not a per-game projection
with a longer window. And **shrinkage earns its place**: 7.7% better than naive season-to-date
at six weeks, 11.6% better at twelve, with the advantage growing as the horizon lengthens,
which is the signature of true talent mattering more the further out you look.

### The model rung clears it

`ros_train.py` adds a gradient-boosted rung, one model per factor, built so the answer could
have been *no* without the work being wasted: the shrunk estimate is handed to each model as
a feature, so the trees start from the baseline and only learn a correction. Rows are
weighted by how much window stood behind the label — a rate measured over eighteen games is
a better label than one over two — and the objectives are L1, matching the reported metric,
which is the lesson the shrinkage constants taught the hard way.

| rung | MAE | RMSE | bias | Spearman |
|---|---|---|---|---|
| season-to-date | 15.05 | 21.14 | +7.0% | 0.680 |
| shrunk | 13.89 | 18.79 | +4.8% | 0.704 |
| **model** | **12.47** | **16.94** | **−0.2%** | **0.754** |

10.2% better than shrinkage on MAE, 0.05 of Spearman, and the level bias effectively
disappears. It holds at twelve weeks too (22.83 against 25.46, Spearman 0.768 against 0.719)
and under banger-league scoring (21.96 against 24.54). **It beats shrinkage on every one of
the ten factors** — including availability, which shrinkage could not improve on at all
(0.183 against 0.200): a player's own attendance rate is his best single predictor, but team
context and role carry more.

### Expected goals did not earn their billing

Section 2 says individual xG is "materially more predictive of future goals than past goals
are." Over a rest-of-season horizon, measured three ways, it is not:

- Total gain in the goals model, across all 452 features: past goals 0.131, ixG 0.042.
- Dropping every ixG column **improves** goals MAE slightly, 0.3420 → 0.3400.
- Dropping every past-goals column makes it slightly worse, 0.3420 → 0.3428.

Those differences are small enough to be a single fit's noise, which is the point: given a
shrunk goal rate and power-play deployment already in the features, ixG adds nothing
measurable at this horizon. It may still earn its place in the per-game models, where the
question is a different one. What *does* carry the goals model is position (0.25 of gain, the
prior in disguise), the shrunk goal rate (0.11) and power-play ice time (0.07) — and for
assists, power-play deployment is 0.30 of the top-eight gain on its own.

### How long until a player's own numbers count

The fitted shrinkage constants are the interesting output in their own right. Evidence is
counted in hours of ice time for rates, so `k` is how much ice time it takes before a
player's own rate outweighs his position's prior:

| category | k (hours of ice time) | roughly |
|---|---|---|
| hits | 0.30 | about 1 game |
| power-play points | 0.46 | about 2 games |
| shots | 2.60 | about 10 games |
| penalty minutes | 3.11 | about 12 games |
| blocks | 4.09 | about 15 games |
| assists | 6.50 | about 24 games |
| goals | 9.39 | about 35 games |

Ice time itself barely shrinks at all (k = 0.47 games) and availability not at all — a
player's own attendance record is its own best estimate. The recency weights land the same
way round: 0.29 for ice time and 0.09 for power-play points, but 0.00–0.03 for every scoring
rate. **Discount recent form for rates; keep some of it for role**, because a player whose
ice time just moved really is in a new role, while one whose shooting percentage just moved
is not a new shooter.

### Two horizons, and a CLI that emits projections

`--horizon season` runs each window to the end of the season, which is what a draft or a
keep-or-cut decision actually asks about; a fixed `--horizon 42` keeps every row comparable
and suits a trade or a streaming call. The variable mode costs almost nothing because of the
decomposition: availability, ice time and rate per 60 do not depend on how long the window
is, so only the games-remaining multiplier changes with the date.

Season-mode ladder, 2025-26, window fantasy points under points-league:

| rung | MAE | RMSE | bias | Spearman |
|---|---|---|---|---|
| season-to-date | 35.22 | 57.08 | +9.1% | 0.791 |
| shrunk | 30.53 | 45.27 | +4.4% | 0.815 |
| **model** | **27.09** | **38.87** | **+0.6%** | **0.845** |

```bash
python ros_train.py --horizon season --save          # boosters + the shrinkage fit
python ros_predict.py --season 2025-26 --as-of 2026-01-15 --weights points-league
```

`ros_predict.py` emits one row per player as of a date — not as of a game, since not every
team plays every night, so each player's most recent state is taken and its `state_age_days`
reported rather than hidden. Games remaining come off the schedule, so the same models serve
a projection made in October and one made in March. Scoring is applied only if asked for.

Checked end to end against what actually happened after 2026-01-15: Spearman 0.813 and
Pearson 0.819 on rest-of-season points across 846 players, with goals within 0.3% and assists
within 2.2% in aggregate.

### The deployment build, and why the round budget is the regularizer

```bash
python ros_train.py --train 2023-24 2024-25 2025-26 --horizon season --no-holdout --save
```

`--no-holdout` trains on every season given and scores nothing, which is what to ship before
a season starts. It carries no metrics of its own: the figures above come from the build
trained on 2023-24 and 2024-25 and scored against a 2025-26 it never saw. The sidecar records
`deployment_build`, `trained_on` and `scored_on`, and `ros_predict.py` prints that provenance
on every run — plus a warning if the season being projected is one the models trained on.

Saved models are namespaced by horizon (`ros_season_*`, `ros_42d_*`), because a season-length
window and a six-week window are different labels with different noise and loading one where
the other was meant produces plausible-looking numbers.

**Early stopping does not work on this target, and the reason generalizes.** A rest-of-season
label looks forward, so a fit row from November carries a window covering the same games the
validation rows' windows cover. They share outcomes, so validation loss keeps improving well
past the point of generalizing — it never fired once, at any cap. The per-game stack has no
such problem, since its labels are single games.

Swept against a genuinely unseen season instead, the cap turns out to matter:

| rounds | MAE | RMSE | bias | Spearman |
|---|---|---|---|---|
| 100 | **26.41** | **38.02** | +1.8% | **0.854** |
| 250 *(default)* | 26.48 | 38.21 | +1.1% | 0.851 |
| 500 | 26.71 | 38.49 | +0.8% | 0.849 |
| 2000 | 27.09 | 38.87 | **+0.6%** | 0.845 |

Monotone, and in the direction that a trusted early-stopping run would have hidden: more
boosting is steadily *worse* on accuracy and ranking while slowly improving level bias. The
first shipped build ran at 2000 and was the worst row in that table. 250 is the default now —
within 0.3% of the best MAE with meaningfully lower bias.

### Availability is over-projected, and it is not the loss function

Projected totals run high, and increasingly so as a season runs out:

| as of | Nov 1 | Dec 15 | Jan 15 | Feb 15 | Mar 15 |
|---|---|---|---|---|---|
| games | +3.9% | +6.2% | +8.1% | +10.1% | +11.0% |
| blocks | +10.2% | +12.6% | +16.7% | +18.0% | +19.6% |
| mean availability, projected | 0.768 | 0.722 | 0.712 | 0.709 | 0.704 |
| mean availability, realized | 0.739 | 0.681 | 0.659 | 0.644 | 0.634 |

The obvious suspect was the L1 objective, which fits a conditional *median* — and a median
estimate of a left-skewed factor is biased as a mean, which would compound through every
total. **That was tested and it is wrong.** An L2 build moved the composite bias the wrong
way (+1.4% against L1's +0.6%) and cost accuracy (MAE 27.82 against 27.09, Spearman 0.839
against 0.845), so L1 is what ships.

What the per-factor table shows instead is that *every* rung over-projects availability —
naive season-to-date by +7.7%, shrinkage by +6.3%, the model by +5.5%. The likely mechanism
is attrition: the players visible at a given date are the ones currently healthy and in the
league, and from there some get hurt, demoted or waived while nothing pulls the other way.
That is a property of the target rather than of the fit, and it is stated here rather than
corrected, because the correction belongs where `drift.py` sits for the per-game stack —
applied by a consumer that knows whether it wants a ranking or an unbiased total. **Rankings
are unaffected**, which is what draft and trade logic mostly consume.

### Known gaps

- **No aging curve.** Section 4 asks for one and the feature table carries no birthdate, so
  adding it is a `ModelFeatures/` change rather than something to fake here.
- **Rookies get the positional prior and nothing else.** Draft pedigree and AHL production
  are the priors Section 4 wants for small samples; neither is in the database today.
- Windows are fitted and scored on overlapping rows thinned to one per player per week, so
  the effective sample is smaller than the row count suggests.


This folder **never opens a database connection**. `ModelFeatures/` reads NHLStats through the
read-only `FantasyAssistant` login and writes parquet; everything here starts from that
parquet. There is no `pyodbc` in `requirements.txt`, and that is the point.

```
pip install -r requirements.txt
python train.py --all                 # fit on 2023-24+2024-25, hold 2025-26 out (scored)
python train.py --all --no-holdout     --train-seasons 2023-24,2024-25,2025-26   # the deployment build: nothing held back
python calibrate.py                   # fit the dispersion section 5 needs
python evaluate.py --cross-features A # score the holdout, and bound the live-feed risk
python evaluate.py --recalibrate      # ... with drift.py's rolling level correction
python evaluate.py --list-scoresets   # the example scoring files
python evaluate.py --weights points-league.json                    --weights banger-league.json   # composite metrics per format
python modelcards.py                  # render docs/model-cards-B.md from the artefacts
python predict.py --season 2025-26 --variant A
```

## The stack

Everything is one LightGBM model per target, run in this order because each offsets on the
one before:

```
plays ─▶ toi ─▶ ev_toi, pp_toi
                 └─▶ shots ─▶ goals
                      └─▶ hits, blocks, assists, pim
                                  └─▶ pp_point_share
```

| model | objective | offset | what it is |
| --- | --- | --- | --- |
| `plays` | binary + isotonic | — | P(he is in tonight's lineup), over every lockout-knowable candidate |
| `toi`, `ev_toi` | L2 | — | seconds, conditional on playing |
| `pp_toi` | poisson | — | power-play seconds; a large zero mass |
| `shots`, `hits`, `blocks`, `assists`, `pim` | poisson | `log(TOI/3600)` | counts, conditional on playing |

Tweedie looked like the right shape for the two zero-heavy targets (`pp_toi`, `pim`) and was
measurably worse on both — it over-shrinks the tail. Poisson on each.
| `goals` | poisson | `log(E[shots])` | shots times a heavily shrunk shooting percentage |
| `pp_point_share` | cross-entropy | — | P(a given point is a power-play point) |
| `sh_point_share` | cross-entropy | — | the short-handed twin |

Three design decisions carry most of the weight:

**The offset is the opportunity.** `init_score = log(TOI/3600)` under a log link means the
trees can only learn a per-60 rate; the ice time is supplied, not fitted. On top of it goes a
mean-matching intercept, `log(Σwy / Σw·e^offset)`, because a bare offset starts the model at a
rate of exactly 1.0 per 60 — shots run at 5.6, so without the intercept the trees have to
climb log(5.6) in link space and stop short. That showed up as a flat 12–28% under-prediction
on every offset model *except* assists, whose true rate of 1.05 per 60 happens to sit exactly
where the bare offset starts. Adding it cut shots' PIT deviation from 0.052 to 0.008.

**Offsets come from out-of-fold predictions.** Live, the shots model sees a *predicted* TOI
with error in it. Training it against the actual TOI would teach it a precision it will never
have at the lock, so `train.py` fits K-fold models (grouped by game) for `toi` and `shots` and
offsets the downstream models on those.

**Counts are conditional on playing, and `p_plays` stays separate.** The count models are fit
on rows where the player played but predict on every candidate. `predict.py` keeps `p_plays`
as its own column rather than folding it in, because a 40% chance of a 12-point night is not
the same distribution as a certain 4.8-point night — and in a head-to-head format that
difference is the whole variance question.

## Scoring systems

`weights.py` is mechanism only — it loads a JSON file and applies it to a stat line:

```json
{"name": "points-league",
 "skaters": {"goals": 4.0, "assists": 2.5, "ppp": 1.0, "shp": 1.25,
             "hits": 0.4, "blocks": 0.4, "shots": 0.25, "pim": 0.2}}
```

Without `--weights`, `evaluate.py` reports per-category metrics only. Pass one or more and it
adds composite metrics per format. Running the three examples against one fixed set of models
shows how much the format matters:

```
format           MAE   mean pts/game   top-100 capture   best baseline MAE
banger-league   2.541      5.28             0.755              2.632
points-league   1.857      2.77             0.697              1.897
scoring-only    2.698      2.40             0.644              2.694
```

**The models' value is a function of the scoring system, not a property of the models.** In a
peripheral-heavy format they capture 75.5% of the available points and clearly beat every
baseline, because hits, blocks and shots are the categories they project well. In a
goals-and-assists-only format they capture 64.4% and are level with a naive season-rate
baseline (2.698 against 2.694) — those categories sit at the irreducible noise floor, so
there is nothing to add. Anyone choosing a league, or weighting a decision agent's objective,
should read that table first.


## Splits

Fit on 2023-24 + 2024-25, early-stop on the last 25% of 2024-25 by date, hold 2025-26 out
whole. Nothing is shuffled across time. That is the *scored* build, and every number in this
file comes from it.

`--no-holdout` is the **deployment** build: every available season goes into the fit, nothing
is held back, and no metrics come out. That is what currently sits in `models/` — trained on
all three seasons, for projecting 2026-27. The distinction matters, and `docs/model-cards-B.md`
states it at the top: the shipped boosters are not the ones the accuracy tables describe.

Withholding the most recent season from a model you intend to *use* costs real accuracy.
Measured by training on one season at a time and scoring 2025-26:

```
training data                     shots MAE   hits MAE   plays AUC
2023-24 only (2 seasons stale)      1.0349     0.8876     0.9893
2024-25 only (1 season stale)       1.0123     0.8656     0.9898
2023-24 + 2024-25                   1.0137     0.8606     0.9906
```

**Recency beats volume.** One recent season clearly beats one stale season, while adding the
older season on top was close to a wash. Retrain before each season; backfilling further into
the past is a much weaker lever than staying current. Variant B holds three perturbed copies of every
candidate; they share a game date, so date-based splitting keeps them on the same side, which
`data.chronological_split` asserts, and each row carries `weight = 1/copies`.

`train.py --walk-forward` refits monthly on an expanding window across the holdout season
instead, which is how the season simulator will actually consume these models.

Measured, it buys less than expected: fantasy-points MAE 1.847 against the single fit's
1.845, top-100 capture 0.699 against 0.698. What it does help is *level* — the monthly refit
tracks the season's own rates, cutting PIM's bias from −13.0% to −9.9% and blocks' from +5.9%
to +4.7%, with Spearman up a point or two everywhere. So the single fit is the right default
and walk-forward is the honesty check, not a free accuracy gain. Seven monthly refits take
about an hour.

## Variants, and the live-feed bound

Variant B trains on the actual lineup with calibrated noise; variant A uses the previous
game's lineup and is the shape a live Daily Faceoff chart is closer to. `evaluate.py
--cross-features A` scores the B-trained boosters on A's features, which bounds how much of
the accuracy is borrowed from knowing the lineup better than the feed will.

Measured on 2025-26: fantasy-points MAE moves from 1.845 to 1.841 and top-100 capture from
0.698 to 0.697, while `plays` AUC falls from 0.991 to 0.970. That is the expected shape —
knowing tonight's lineup settles *whether* a player dresses, not how many shots he takes once
he does. The category projections do not lean on the lineup feed; P(plays) does.

## League drift, and `drift.py`

The models learn a league; the next season is a different one. Between 2024-25 and 2025-26,
league shooting percentage rose 4.2% (continuing 10.18% → 10.64% → 11.09%) and **elite**
power-play time rose 10.7%. A model fit on the old level projects the new one low, and the
shortfall lands on exactly the players whose level matters most:

```
tier        FP bias   goals bias   PP TOI bias        ... after drift.py
1-50         -5.3%       -7.7%        -9.2%     →   -2.7%  -3.5%  -4.1%
51-100       -5.3%       -8.9%        -7.8%     →   -3.1%  -4.8%  -2.7%
101-200      -4.3%       -8.7%        -5.6%     →   -2.2%  -4.5%  -0.3%
201-350      -1.0%       +0.1%        -3.3%     →   +0.8%  +4.7%  +2.1%
351+         +1.1%       -1.1%        +1.4%     →   +2.5%  +3.4%  +7.0%

mean |per-tier FP bias|:  3.39%  →  2.26%
```

`drift.py` re-estimates each category's level from the trailing 30 days of *completed* games
and scales lambda by it — the same mean-matching intercept the models already carry, refreshed
against the current season instead of the training ones. Leakage-safe by construction: a game
on date D only ever uses games that finished before D.

Two honest caveats. The correction is league-wide and uniform, so it **over-corrects the
bottom** — fringe players were already unbiased and now run 2–3% high. And league-wide versus
stratified-by-predicted-value was chosen by comparing holdout bias, so the improvement above is
mildly optimistic; it wants re-checking on a season the models have never seen.

It is off by default (`--recalibrate` to enable) because it trades MAE for calibration:
fantasy-point MAE goes 1.845 → 1.854 while per-tier bias nearly halves. Take it when *level*
matters — draft boards, trade valuation, head-to-head win probabilities — and leave it off when
ranking is all you need.

### What this replaced

The elite shortfall was first diagnosed as over-shrinkage in the goals model and Tweedie
over-shrinkage in `pp_toi`. Both were wrong. Loosening the goals model on the early-stop slice
does not reduce elite bias (+0.6% tight, +1.7% and +1.2% loosened, at equal deviance and AUC),
and the drift figures above match the observed bias almost exactly. `pp_toi` did move to
Poisson — worth it on its own (MAE 46.9s → 46.1s, ρ 0.764 → 0.776) — but it was never the cause.
The goals model's parameters were left alone deliberately.


## What section 5 consumes

`predict.py` writes one row per player-game: `p_plays`, `toi`/`ev_toi`/`pp_toi`,
`lambda_{shots,hits,blocks,assists,goals,pim}`, `pp_point_share` and `sh_point_share`. No
points columns — a consumer applies its own scoring. `calibrate.py` writes `reports/dispersion.json` beside it: the maximum-likelihood NB
dispersion per category under `Var = mu + theta*mu²`, plus the shared game-quality variance
estimated from how much the categories' residuals move together. Section 5's Gamma multiplier
takes the shared part; the remainder is per-category noise, so the two do not double-count.

Sampling PPP and SHP as *shares of each sampled point* — rather than as independent counts —
is what keeps `PPP + SHP <= points` in the simulator. Both strengths are modelled: an earlier
version skipped short-handed points because they are under 1% of *one particular* scoring
system, which was exactly the kind of league assumption that does not belong in the models.
`predict.py` clamps the pair in the rare case the two independent fits sum above 1.

## Known limits

- **Three seasons.** 2023-24 has no `prev_*` columns, since 2022-23 is not ingested; LightGBM
  splits on the NaN natively. Backfilling further seasons is a re-run of
  `ModelFeatures/build_feature_table.py` and a retrain, nothing more.
- **Season-level drift is the dominant residual bias; `drift.py` halves it.** See below.
- **PIM is the weakest model** (Spearman 0.14). It is 3% of scoring, and the NB shape fits it
  poorly — a lumpy 0/2/5 target is not really a count. Good enough for its weight.
- **Goalies are not modelled here.** They need their own feature table (starts, shots-against,
  save percentage), which is a section 2 job; `ModelFeatures/data/features/goalies_rolling_*`
  only carries opposing-goalie form for the skater table.

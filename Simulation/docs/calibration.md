# Monte Carlo calibration

Scored on the B holdout: 46,654 played player-games x 300 draws. Regenerate with `python validate.py --sims 200 --weights points-league --docs`.

## The sampler does not move a projection

Simulated means against `p_plays * lambda` on the live-shaped variant-A table -- closed form, no holdout, so any gap here is this layer's fault and nobody else's.

| category | simulated | p_plays x lambda | error   |
|----------|-----------|------------------|---------|
| shots    | 1905.75   | 1910.21          | -0.233% |
| hits     | 1370.09   | 1368.87          | +0.089% |
| blocks   | 1000.53   | 999.34           | +0.119% |
| assists  | 357.64    | 358.82           | -0.327% |
| goals    | 205.07    | 205.95           | -0.428% |
| pim      | 531.7     | 533.09           | -0.262% |

## Spread, per category

`sampler` is the simulated mean against the lambda it was handed; `projection` is that lambda against what happened, which is the projection layer's own level bias and is neither created nor repaired here.

| category | sim mean | actual | sampler | projection | sim var | actual var | var ratio | PIT max dev |
|----------|----------|--------|---------|------------|---------|------------|-----------|-------------|
| shots    | 1.5631   | 1.5451 | +0.01%  | +1.15%     | 2.1957  | 2.1772     | 1.0085    | 0.0035      |
| hits     | 1.1204   | 1.1308 | +0.01%  | -0.92%     | 1.9026  | 1.93       | 0.9858    | 0.0028      |
| blocks   | 0.8289   | 0.7873 | -0.00%  | +5.28%     | 1.134   | 1.0784     | 1.0516    | 0.0091      |
| assists  | 0.2823   | 0.2896 | +0.09%  | -2.59%     | 0.3091  | 0.3164     | 0.977     | 0.0031      |
| goals    | 0.1656   | 0.1714 | +0.15%  | -3.55%     | 0.1812  | 0.185      | 0.9794    | 0.0045      |
| pim      | 0.4251   | 0.4838 | +0.08%  | -12.21%    | 1.5712  | 1.9995     | 0.7858    | 0.0173      |

## Tails

P(Y >= k), simulated against observed. Boom games decide head-to-head weeks, so matching the second moment is not enough on its own.

| category | threshold | simulated | actual  | ratio  |
|----------|-----------|-----------|---------|--------|
| shots    | >= 1      | 0.73468   | 0.72738 | 1.01   |
| shots    | >= 3      | 0.21887   | 0.21709 | 1.0082 |
| shots    | >= 5      | 0.04529   | 0.04441 | 1.0198 |
| shots    | >= 7      | 0.00788   | 0.00752 | 1.0479 |
| hits     | >= 1      | 0.57716   | 0.57346 | 1.0065 |
| hits     | >= 3      | 0.13703   | 0.1425  | 0.9616 |
| hits     | >= 5      | 0.0298    | 0.03042 | 0.9799 |
| hits     | >= 7      | 0.00622   | 0.00577 | 1.0785 |
| blocks   | >= 1      | 0.50854   | 0.48864 | 1.0407 |
| blocks   | >= 2      | 0.20445   | 0.19203 | 1.0647 |
| blocks   | >= 4      | 0.02681   | 0.02431 | 1.1029 |
| blocks   | >= 6      | 0.00296   | 0.00219 | 1.3543 |
| assists  | >= 1      | 0.23644   | 0.24159 | 0.9787 |
| assists  | >= 2      | 0.03944   | 0.04139 | 0.9529 |
| assists  | >= 3      | 0.00562   | 0.00587 | 0.9574 |
| goals    | >= 1      | 0.14633   | 0.15208 | 0.9622 |
| goals    | >= 2      | 0.01722   | 0.0173  | 0.9953 |
| goals    | >= 3      | 0.00184   | 0.00199 | 0.9207 |
| pim      | >= 2      | 0.15152   | 0.17205 | 0.8807 |
| pim      | >= 4      | 0.0327    | 0.03333 | 0.9809 |
| pim      | >= 5      | 0.01853   | 0.01886 | 0.9822 |
| pim      | >= 10     | 0.00509   | 0.00581 | 0.8754 |

## Roster totals

The number this layer exists for. A ten-skater roster's variance against what independent players would give: measured off the holdout, simulated here, and -- where the run included it -- under independent sampling for contrast.

| structure   | scoring       | roster | sim ratio | actual ratio | sim sd | actual sd |
|-------------|---------------|--------|-----------|--------------|--------|-----------|
| fitted      | points-league | random | 1.0564    | 1.0976       | 8.075  | 8.236     |
| fitted      | points-league | stack  | 1.6079    | 1.7433       | 9.948  | 10.38     |
| fitted      | banger-league | random | 1.0361    | 1.0451       | 10.421 | 10.621    |
| fitted      | banger-league | stack  | 1.3061    | 1.3691       | 11.707 | 12.156    |
| independent | points-league | random | 1.0016    | 1.0986       | 7.823  | 8.239     |
| independent | points-league | stack  | 0.9997    | 1.7376       | 7.803  | 10.362    |
| independent | banger-league | random | 0.9998    | 1.0427       | 10.219 | 10.608    |
| independent | banger-league | stack  | 0.9999    | 1.3669       | 10.22  | 12.145    |

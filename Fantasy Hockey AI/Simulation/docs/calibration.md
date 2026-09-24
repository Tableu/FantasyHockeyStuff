# Monte Carlo calibration

Scored on the B holdout: 46,654 played player-games x 300 draws. Regenerate with `python validate.py --sims 200 --weights points-league --docs`.

## The sampler does not move a projection

Simulated means against `p_plays * lambda` on the live-shaped variant-A table -- closed form, no holdout, so any gap here is this layer's fault and nobody else's.

| category | simulated | p_plays x lambda | error   |
|----------|-----------|------------------|---------|
| shots    | 1957.5    | 1958.4           | -0.046% |
| hits     | 1341.85   | 1343.25          | -0.104% |
| blocks   | 1035.41   | 1035.29          | +0.011% |
| assists  | 356.8     | 356.65           | +0.042% |
| goals    | 205.02    | 204.83           | +0.092% |
| pim      | 509.87    | 509.84           | +0.005% |

## Spread, per category

`sampler` is the simulated mean against the lambda it was handed; `projection` is that lambda against what happened, which is the projection layer's own level bias and is neither created nor repaired here.

| category | sim mean | actual | sampler | projection | sim var | actual var | var ratio | PIT max dev |
|----------|----------|--------|---------|------------|---------|------------|-----------|-------------|
| shots    | 1.5654   | 1.5451 | -0.01%  | +1.32%     | 2.1977  | 2.1772     | 1.0094    | 0.0031      |
| hits     | 1.1113   | 1.1308 | +0.02%  | -1.75%     | 1.8787  | 1.93       | 0.9734    | 0.0036      |
| blocks   | 0.8269   | 0.7873 | +0.00%  | +5.02%     | 1.1307  | 1.0784     | 1.0485    | 0.0083      |
| assists  | 0.2837   | 0.2896 | +0.02%  | -2.02%     | 0.3098  | 0.3164     | 0.9791    | 0.0024      |
| goals    | 0.1644   | 0.1714 | +0.04%  | -4.14%     | 0.1795  | 0.185      | 0.9702    | 0.0041      |
| pim      | 0.4272   | 0.4838 | +0.33%  | -12.01%    | 1.5768  | 1.9995     | 0.7886    | 0.0168      |

## Tails

P(Y >= k), simulated against observed. Boom games decide head-to-head weeks, so matching the second moment is not enough on its own.

| category | threshold | simulated | actual  | ratio  |
|----------|-----------|-----------|---------|--------|
| shots    | >= 1      | 0.73507   | 0.72738 | 1.0106 |
| shots    | >= 3      | 0.21952   | 0.21709 | 1.0112 |
| shots    | >= 5      | 0.04535   | 0.04441 | 1.0211 |
| shots    | >= 7      | 0.0079    | 0.00752 | 1.0506 |
| hits     | >= 1      | 0.57502   | 0.57346 | 1.0027 |
| hits     | >= 3      | 0.13525   | 0.1425  | 0.9491 |
| hits     | >= 5      | 0.02911   | 0.03042 | 0.9571 |
| hits     | >= 7      | 0.00599   | 0.00577 | 1.0397 |
| blocks   | >= 1      | 0.50778   | 0.48864 | 1.0392 |
| blocks   | >= 2      | 0.20381   | 0.19203 | 1.0613 |
| blocks   | >= 4      | 0.02664   | 0.02431 | 1.0958 |
| blocks   | >= 6      | 0.00296   | 0.00219 | 1.3529 |
| assists  | >= 1      | 0.23778   | 0.24159 | 0.9842 |
| assists  | >= 2      | 0.03957   | 0.04139 | 0.9559 |
| assists  | >= 3      | 0.0056    | 0.00587 | 0.9537 |
| goals    | >= 1      | 0.1455    | 0.15208 | 0.9568 |
| goals    | >= 2      | 0.01694   | 0.0173  | 0.9791 |
| goals    | >= 3      | 0.00178   | 0.00199 | 0.8922 |
| pim      | >= 2      | 0.15227   | 0.17205 | 0.885  |
| pim      | >= 4      | 0.03289   | 0.03333 | 0.9867 |
| pim      | >= 5      | 0.01857   | 0.01886 | 0.9845 |
| pim      | >= 10     | 0.00509   | 0.00581 | 0.877  |

## Roster totals

The number this layer exists for. A ten-skater roster's variance against what independent players would give: measured off the holdout, simulated here, and -- where the run included it -- under independent sampling for contrast.

| structure   | scoring       | roster | sim ratio | actual ratio | sim sd | actual sd |
|-------------|---------------|--------|-----------|--------------|--------|-----------|
| fitted      | points-league | random | 1.0504    | 1.0986       | 8.042  | 8.241     |
| fitted      | points-league | stack  | 1.5573    | 1.7329       | 9.799  | 10.35     |
| independent | points-league | random | 0.9998    | 1.0954       | 7.8    | 8.226     |
| independent | points-league | stack  | 0.9979    | 1.7281       | 7.802  | 10.332    |

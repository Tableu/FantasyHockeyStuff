"""The rest-of-season baseline ladder, and the shrinkage the plan asks for.

Section 4 recommends "Bayesian shrinkage toward an established talent level ... effectively
empirical Bayes, implementable as a simple weighted average". This is that, fitted rather
than assumed &mdash; and it is built *before* any model, because the goalie branch already
paid the tuition on this exact question: save percentage looked like a modelling problem and
turned out to be a shrinkage problem, with LightGBM scoring negative R-squared at every
capacity while empirical Bayes worked. Nothing gets a gradient-boosted model here until it
has beaten the ladder below.

Four rungs, in rising order of what they assume:

    season       season-to-date rate, unshrunk -- the naive answer
    last10       the most recent ten games -- what the per-game model leans on, included
                 precisely to test Section 4's claim that a horizon should discount it
    shrunk       empirical Bayes toward a positional prior, weighted by evidence
    blended      shrunk, with a small recency term, for categories where form is real

The structure being predicted is the one `ros.py` builds:

    production  =  team games in the window  x  availability  x  ice time  x  rate per 60

and each factor is shrunk on its own evidence scale. Rates are weighted by **minutes**, not
games, because a per-60 rate's evidence is ice time; availability and ice time are weighted
by games. The fitted constants are the interesting output in their own right: they say how
long it takes before a player's own numbers outweigh his position's.

Usage:
    python ros_baselines.py --train 2023-24 2024-25 --test 2025-26 \
        --weights points-league
"""

import argparse
import json
import logging

import numpy as np
import pandas as pd
from scipy import optimize

import paths
import ros
import weights as weights_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ros_baselines")

CATEGORIES = ["shots", "hits", "blocks", "assists", "goals", "pim", "ppp", "shp"]

# Each factor: the as-of column, the forward truth, and what its evidence is counted in.
FACTORS = {
    # Availability is a made-over-attempts proportion, so its evidence is the number of
    # *team* games he could have dressed for, not the number he did. Counting his own games
    # instead says a player passed over forty times has no evidence about his availability,
    # when he has a great deal of it -- and the fitted constant then runs away (k = 511
    # games, shrinking everyone to the league prior) to compensate. With attempts as the
    # evidence this is exactly a beta-binomial posterior mean.
    "availability": {"target": "ros_availability", "evidence": "attempts"},
    "toi_per_game": {"target": "ros_toi_per_game", "evidence": "games"},
}
for _category in CATEGORIES:
    FACTORS[f"{_category}_p60"] = {"target": f"ros_{_category}_p60", "evidence": "minutes"}


def parse_args():
    parser = argparse.ArgumentParser(description="Fit and score the ROS baseline ladder")
    parser.add_argument("--train", nargs="+", default=["2023-24", "2024-25"])
    parser.add_argument("--test", default="2025-26")
    parser.add_argument("--horizon", type=int, default=ros.DEFAULT_HORIZON_DAYS)
    parser.add_argument("--weights", action="append", default=None, metavar="FILE",
                        help="Scoring file for the composite metric; repeat for several")
    parser.add_argument("--thin-days", type=int, default=7,
                        help="Keep one as-of row per player per N days, so heavily "
                             "overlapping windows do not dominate the averages (default 7)")
    parser.add_argument("--loss", choices=("mae", "mse"), default="mae",
                        help="Loss the shrinkage constants are fitted to (default mae)")
    parser.add_argument("--out", default="ros_baselines.json")
    return parser.parse_args()


def load(season, horizon):
    path = paths.REPORTS_DIR / f"ros_{season}_{horizon}d.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing -- run ros.py --season {season} "
                                f"--horizon {horizon}")
    frame = pd.read_parquet(path)
    frame["game_date"] = pd.to_datetime(frame["game_date"])
    frame["season"] = season
    return frame


def add_asof(frame):
    """The as-of side of each factor, including the two the feature table does not carry.

    `games_to_date` counts his team's games before tonight, which is what turns a count of
    games played into an availability *rate*; without it, a player who joined in December
    looks unavailable rather than new.
    """
    # Grouped by season as well as team: a counter that runs across a season boundary while
    # games-played resets at it makes every later-season availability rate garbage, and the
    # fitted shrinkage then correctly concludes the as-of value is worthless.
    frame = frame.sort_values(["season", "team_id", "game_date"]).copy()
    frame["team_games_to_date"] = frame.groupby(["season", "team_id"])["game_id"].transform(
        lambda ids: pd.factorize(ids)[0])
    frame["asof_availability"] = np.where(
        frame["team_games_to_date"] > 0,
        frame["gp_std"] / frame["team_games_to_date"].replace(0, np.nan), np.nan)
    frame["asof_availability"] = frame["asof_availability"].clip(0, 1)
    frame["asof_toi_per_game"] = frame["mean_toi_std"]
    for category in CATEGORIES:
        column = f"{category}_p60_std"
        frame[f"asof_{category}_p60"] = frame[column] if column in frame else np.nan
        recent = f"{category}_l10"
        if recent in frame and "mean_toi_l10" in frame and "gp_l10" in frame:
            minutes = (frame["mean_toi_l10"] * frame["gp_l10"] / 3600.0).replace(0, np.nan)
            frame[f"recent_{category}_p60"] = frame[recent] / minutes
    frame["recent_availability"] = (frame["gp_l10"] / 10.0).clip(0, 1)
    frame["recent_toi_per_game"] = frame["mean_toi_l10"]

    # Evidence behind each as-of estimate.
    frame["evidence_games"] = frame["gp_std"].fillna(0.0)
    frame["evidence_attempts"] = frame["team_games_to_date"].astype("float64")
    frame["evidence_minutes"] = (frame["gp_std"] * frame["mean_toi_std"] / 3600.0).fillna(0.0)
    return frame


def thin(frame, days):
    """One as-of row per player per `days`, so a window is not counted seven times over."""
    if days <= 1:
        return frame
    frame = frame.sort_values(["player_id", "game_date"])
    bucket = (frame["game_date"] - frame["game_date"].min()).dt.days // days
    return frame.loc[~frame.assign(bucket=bucket).duplicated(["player_id", "bucket"])]


def fit_prior(frame, factor):
    """The positional prior: what a forward or defenceman does, from the training rows."""
    target = FACTORS[factor]["target"]
    rows = frame[np.isfinite(frame[target])]
    overall = float(rows[target].mean())
    by_position = rows.groupby(rows["position"].astype(str))[target].mean().to_dict()
    return {"overall": overall, "by_position": {k: float(v) for k, v in by_position.items()}}


def prior_values(frame, prior):
    return frame["position"].astype(str).map(prior["by_position"]).fillna(prior["overall"])


def shrink(asof, evidence, prior, k):
    """The weighted average empirical Bayes reduces to: n observed + k prior, over n + k."""
    asof = np.where(np.isfinite(asof), asof, prior)
    return (evidence * asof + k * prior) / (evidence + k)


def fit_k(frame, factor, prior, loss_name="mae"):
    """The shrinkage constant, by minimizing training error under the reported loss.

    Which loss matters here, and it is not a detail. Availability is strongly bimodal --
    a player is a regular or he is not -- so the squared-error-optimal estimate sits in the
    middle of a gap where almost no one actually lives, and fitting to MSE while reporting
    MAE produced a constant of 700-odd attempts that shrank every player to the league mean.
    Fit to the loss the answer is judged on.
    """
    spec = FACTORS[factor]
    target = frame[spec["target"]].to_numpy("float64")
    asof = frame[f"asof_{factor}"].to_numpy("float64")
    evidence = frame[f"evidence_{spec['evidence']}"].to_numpy("float64")
    prior_column = prior_values(frame, prior).to_numpy("float64")
    good = np.isfinite(target) & np.isfinite(evidence)
    target, asof = target[good], asof[good]
    evidence, prior_column = evidence[good], prior_column[good]

    def loss(log_k):
        predicted = shrink(asof, evidence, prior_column, np.exp(log_k))
        error = predicted - target
        return float(np.mean(np.abs(error)) if loss_name == "mae"
                     else np.mean(error ** 2))

    result = optimize.minimize_scalar(loss, bounds=(np.log(1e-3), np.log(1e4)),
                                      method="bounded")
    return float(np.exp(result.x))


def predict(frame, fitted, rung):
    """Assemble a factor prediction for one rung of the ladder."""
    out = pd.DataFrame(index=frame.index)
    for factor, spec in FACTORS.items():
        prior = fitted[factor]["prior"]
        prior_column = prior_values(frame, prior)
        asof = frame.get(f"asof_{factor}")
        if rung == "season":
            values = asof.where(np.isfinite(asof), prior_column)
        elif rung == "last10":
            recent = frame.get(f"recent_{factor}")
            values = (recent if recent is not None else asof)
            values = values.where(np.isfinite(values), prior_column)
        elif rung in ("shrunk", "blended"):
            values = pd.Series(
                shrink(asof.to_numpy("float64"),
                       frame[f"evidence_{spec['evidence']}"].to_numpy("float64"),
                       prior_column.to_numpy("float64"), fitted[factor]["k"]),
                index=frame.index)
            if rung == "blended":
                recent = frame.get(f"recent_{factor}")
                if recent is not None:
                    weight = fitted[factor].get("recency", 0.0)
                    recent = recent.where(np.isfinite(recent), values)
                    values = (1.0 - weight) * values + weight * recent
        else:
            raise ValueError(rung)
        out[factor] = values.clip(lower=0)
    out["availability"] = out["availability"].clip(0, 1)
    return out


def to_totals(frame, factors):
    """Factors to a projected stat line over the window, in counts."""
    games = frame["window_team_games"].to_numpy("float64") * factors["availability"]
    minutes = games * factors["toi_per_game"] / 3600.0
    totals = pd.DataFrame({"games": games}, index=frame.index)
    for category in CATEGORIES:
        totals[category] = minutes * factors[f"{category}_p60"]
    return totals


def actual_totals(frame):
    out = pd.DataFrame({"games": frame["window_played"].astype("float64")},
                       index=frame.index)
    for category in CATEGORIES:
        out[category] = frame[f"window_{category}"].astype("float64")
    return out


def fit_recency(train, fitted, factor, loss_name="mae"):
    """How much weight the last ten games deserve on top of the shrunk estimate."""
    spec = FACTORS[factor]
    target = train[spec["target"]].to_numpy("float64")
    prior_column = prior_values(train, fitted[factor]["prior"]).to_numpy("float64")
    shrunk = shrink(train[f"asof_{factor}"].to_numpy("float64"),
                    train[f"evidence_{spec['evidence']}"].to_numpy("float64"),
                    prior_column, fitted[factor]["k"])
    recent = train.get(f"recent_{factor}")
    if recent is None:
        return 0.0
    recent = recent.to_numpy("float64")
    recent = np.where(np.isfinite(recent), recent, shrunk)
    good = np.isfinite(target)

    def loss(weight):
        blended = (1.0 - weight) * shrunk[good] + weight * recent[good]
        error = blended - target[good]
        return float(np.mean(np.abs(error)) if loss_name == "mae"
                     else np.mean(error ** 2))

    result = optimize.minimize_scalar(loss, bounds=(0.0, 1.0), method="bounded")
    return float(result.x)


def run(args):
    train = pd.concat([load(s, args.horizon) for s in args.train], ignore_index=True)
    test = load(args.test, args.horizon)
    train, test = add_asof(train), add_asof(test)
    train, test = thin(train, args.thin_days), thin(test, args.thin_days)
    log.info("train %d rows (%s), test %d rows (%s), thinned to one row per player per %dd",
             len(train), ", ".join(args.train), len(test), args.test, args.thin_days)

    fitted = {}
    for factor in FACTORS:
        prior = fit_prior(train, factor)
        k = fit_k(train, factor, prior, args.loss)
        fitted[factor] = {"prior": prior, "k": k}
        fitted[factor]["recency"] = fit_recency(train, fitted, factor, args.loss)
        log.info("%-16s k %8.2f %-8s prior %7.3f  recency weight %.2f", factor, k,
                 FACTORS[factor]["evidence"], prior["overall"], fitted[factor]["recency"])

    scoresets = [weights_module.load(w) for w in (args.weights or [])]
    truth = actual_totals(test)
    report = {"train": args.train, "test": args.test, "horizon_days": args.horizon,
              "loss": args.loss,
              "rows": {"train": len(train), "test": len(test)},
              "fitted": {f: {"k": round(v["k"], 3), "recency": round(v["recency"], 3),
                             "prior": round(v["prior"]["overall"], 4)}
                         for f, v in fitted.items()},
              "rungs": {}}

    for rung in ("season", "last10", "shrunk", "blended"):
        factors = predict(test, fitted, rung)
        totals = to_totals(test, factors)
        entry = {"factors": {}, "categories": {}}
        for factor, spec in FACTORS.items():
            target = test[spec["target"]].to_numpy("float64")
            good = np.isfinite(target)
            error = factors[factor].to_numpy("float64")[good] - target[good]
            entry["factors"][factor] = {
                "mae": round(float(np.mean(np.abs(error))), 4),
                "bias_pct": round(float(100 * error.mean()
                                        / max(abs(target[good].mean()), 1e-9)), 2)}
        for category in ["games"] + CATEGORIES:
            error = totals[category] - truth[category]
            entry["categories"][category] = {
                "mae": round(float(error.abs().mean()), 4),
                "bias_pct": round(float(100 * error.mean()
                                        / max(abs(truth[category].mean()), 1e-9)), 2),
                "spearman": round(float(totals[category].corr(truth[category],
                                                              method="spearman")), 4)}
        for scoreset in scoresets:
            predicted_points = scoreset.score(totals)
            actual_points = scoreset.score(truth)
            error = predicted_points - actual_points
            entry.setdefault("composite", {})[scoreset.name] = {
                "mae": round(float(error.abs().mean()), 3),
                "rmse": round(float(np.sqrt((error ** 2).mean())), 3),
                "bias_pct": round(float(100 * error.mean() / actual_points.mean()), 2),
                "spearman": round(float(predicted_points.corr(actual_points,
                                                              method="spearman")), 4),
                "actual_mean": round(float(actual_points.mean()), 2)}
        report["rungs"][rung] = entry
        log.info("%-8s done", rung)

    destination = paths.ensure(paths.REPORTS_DIR) / args.out
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print_report(report, scoresets)
    log.info("wrote %s", destination)
    return report


def print_report(report, scoresets):
    print("\n=== shrinkage fitted on the training seasons ===")
    rows = [[factor, entry["k"], FACTORS[factor]["evidence"], entry["prior"],
             entry["recency"]] for factor, entry in report["fitted"].items()]
    print(pd.DataFrame(rows, columns=["factor", "k", "evidence in", "prior",
                                      "recency weight"]).to_string(index=False))

    for scoreset in scoresets:
        print(f"\n=== window fantasy points under {scoreset.name} "
              f"(mean actual {report['rungs']['season']['composite'][scoreset.name]['actual_mean']}) ===")
        rows = []
        for rung, entry in report["rungs"].items():
            composite = entry["composite"][scoreset.name]
            rows.append([rung, composite["mae"], composite["rmse"],
                         f'{composite["bias_pct"]:+.1f}%', composite["spearman"]])
        print(pd.DataFrame(rows, columns=["rung", "MAE", "RMSE", "bias",
                                          "Spearman"]).to_string(index=False))

    print("\n=== per-factor MAE by rung ===")
    factors = list(report["fitted"])
    rows = []
    for rung, entry in report["rungs"].items():
        rows.append([rung] + [entry["factors"][f]["mae"] for f in factors])
    print(pd.DataFrame(rows, columns=["rung"] + factors).to_string(index=False))


def main():
    run(parse_args())


if __name__ == "__main__":
    main()

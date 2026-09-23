#!/usr/bin/env python
"""Scores the holdout season, against baselines that make the numbers readable.

An MAE of 0.31 on a target averaging 0.17 goals a game means nothing on its own, so every
category is reported next to two naive projections a person could build without any model:

    rate x TOI   his season-to-date per-60 for the stat, times the model's predicted TOI
    last-10      his mean over the previous ten games played

Three families of metric, in rising order of how much they matter downstream:

    point           MAE, RMSE, Spearman -- the usual, and the least informative
    distributional  CRPS against NB(mu, dispersion) and a PIT histogram, because everything
                    downstream consumes distributions, not point estimates
    composite       only when a scoring file is supplied: the stat line converted to points,
                    with MAE, Spearman and top-N start/sit accuracy

**No scoring system is assumed.** These models project stats. Without `--weights` the report
is per-category only; pass one (or several) to see what a given league would have made of the
same projections -- which is also how to compare formats against one fixed set of models.

Usage:
    python evaluate.py --variant B
    python evaluate.py --variant B --cross-features A    # the B-models scored on A features
    python evaluate.py --variant B --weights points-league.json
    python evaluate.py --variant B --weights points-league.json                                    --weights banger-league.json
"""

import argparse
import json
import logging

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
from sklearn.metrics import roc_auc_score

import calibrate
import paths
import weights as weights_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("evaluate")

# The scoring categories, and the feature columns each baseline is built from.
CATEGORIES = ["shots", "hits", "blocks", "assists", "goals", "pim"]

PIT_BINS = 10


def parse_args():
    parser = argparse.ArgumentParser(description="Score the holdout season")
    parser.add_argument("--variant", choices=("A", "B"), default="B")
    parser.add_argument("--cross-features", choices=("A", "B"), default=None,
                        help="Also score the trained models on this variant's holdout "
                             "features, to bound how much the stack depends on the live "
                             "lineup feed being as good as the training lineup")
    parser.add_argument("--holdout-season", default="2025-26")
    parser.add_argument("--weights", action="append", default=None, metavar="FILE",
                        help="Scoring file to also report composite metrics under; repeat "
                             "for several. Omit for per-category metrics only.")
    parser.add_argument("--list-scoresets", action="store_true",
                        help="Show the example scoring files and exit")
    parser.add_argument("--recalibrate", action="store_true",
                        help="Apply drift.py's rolling level correction before scoring")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Score the monthly refit backtest instead of the single fit")
    return parser.parse_args()


def load(variant, season, walk_forward=False):
    path = paths.predictions(variant, season, walk_forward)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing -- run train.py --all --variant {variant}"
            + (" --walk-forward" if walk_forward else ""))
    return pd.read_parquet(path)


def played_rows(frame):
    return frame[frame["target_played"].astype(bool)].copy()


def baselines(frame, category):
    """The two naive projections, as a dict of name -> predicted counts."""
    per60 = frame.get(f"{category}_p60_std")
    last10 = frame.get(f"{category}_l10")
    games10 = frame.get("gp_l10")
    out = {}
    if per60 is not None and "pred_toi" in frame:
        out["rate x TOI"] = (per60.fillna(0) * frame["pred_toi"] / 3600.0).to_numpy()
    if last10 is not None and games10 is not None:
        out["last-10"] = (last10 / games10.replace(0, np.nan)).fillna(0).to_numpy()
    return out


def point_metrics(actual, predicted):
    actual = np.asarray(actual, dtype="float64")
    predicted = np.asarray(predicted, dtype="float64")
    good = np.isfinite(actual) & np.isfinite(predicted)
    actual, predicted = actual[good], predicted[good]
    if len(actual) < 2:
        return {}
    spearman = scipy_stats.spearmanr(actual, predicted).statistic
    return {
        "mae": float(np.mean(np.abs(actual - predicted))),
        "rmse": float(np.sqrt(np.mean((actual - predicted) ** 2))),
        "spearman": float(spearman),
        "mean_actual": float(actual.mean()),
        "mean_predicted": float(predicted.mean()),
        "n": int(len(actual)),
    }


def crps_negative_binomial(actual, mu, dispersion, max_count=25):
    """CRPS for a count forecast, summed over the step function:

        CRPS = sum_k (F(k) - 1{actual <= k})^2

    which is the discrete analogue of the usual integral and is exact for counts.
    """
    actual = np.asarray(actual, dtype="float64")
    mu = np.maximum(np.asarray(mu, dtype="float64"), 1e-6)
    size = 1.0 / max(dispersion, 1e-6)
    probability = size / (size + mu)
    counts = np.arange(max_count + 1)
    cdf = scipy_stats.nbinom.cdf(counts[:, None], size, probability[None, :])
    indicator = (actual[None, :] <= counts[:, None]).astype("float64")
    return float(np.mean(np.sum((cdf - indicator) ** 2, axis=0)))


def pit_histogram(actual, mu, dispersion, bins=PIT_BINS):
    """Randomized PIT for counts: uniform if the forecast distribution is calibrated.

    A U-shape means the spread is too narrow, a hump in the middle means too wide.
    """
    actual = np.asarray(actual, dtype="float64")
    mu = np.maximum(np.asarray(mu, dtype="float64"), 1e-6)
    size = 1.0 / max(dispersion, 1e-6)
    probability = size / (size + mu)
    upper = scipy_stats.nbinom.cdf(actual, size, probability)
    lower = scipy_stats.nbinom.cdf(actual - 1, size, probability)
    rng = np.random.default_rng(17)
    values = lower + rng.random(len(actual)) * (upper - lower)
    counts, _ = np.histogram(values, bins=bins, range=(0.0, 1.0))
    share = counts / max(counts.sum(), 1)
    return {
        "bins": [round(float(s), 4) for s in share],
        # 0 is perfectly uniform; the scale is "share of mass in the wrong bin".
        "deviation": round(float(np.abs(share - 1.0 / bins).sum() / 2), 4),
    }


def stat_line(frame, prefix):
    """The projected or actual stat line, with the strength bonuses resolved to counts.

    The models carry PPP and SHP as *shares of a point*; a scoring system wants counts, so
    the shares are multiplied back out here. Actuals already have the counts.
    """
    stats = pd.DataFrame(index=frame.index)
    for category in CATEGORIES:
        column = f"{prefix}{category}"
        if column in frame:
            stats[category] = frame[column]
    if prefix == "pred_":
        points = frame.get("pred_goals", 0) + frame.get("pred_assists", 0)
        for bonus, share in (("ppp", "pred_pp_point_share"), ("shp", "pred_sh_point_share")):
            if share in frame:
                stats[bonus] = points * frame[share]
    else:
        for bonus in ("ppp", "shp"):
            if f"{prefix}{bonus}" in frame:
                stats[bonus] = frame[f"{prefix}{bonus}"]
    return stats


def start_sit_accuracy(frame, actual_points, predicted_points, top_n=100):
    """Of the N player-games the model would have started on a given date, what share of the
    actual top-N points did they capture. The lineup optimizer's real question.

    Variant B holds several perturbed copies of each candidate, and a manager picks a player
    once, not once per copy -- so the copies are averaged into one row per (game, player)
    before ranking. Without that the top-100 is three copies of thirty-three players and the
    number is not comparable to variant A's.
    """
    work = pd.DataFrame({
        "date": frame["game_date"].to_numpy(),
        "game_id": frame["game_id"].to_numpy(),
        "player_id": frame["player_id"].to_numpy(),
        "actual": np.asarray(actual_points),
        "predicted": np.asarray(predicted_points),
    })
    work = (work.groupby(["date", "game_id", "player_id"], as_index=False)
                .agg({"actual": "mean", "predicted": "mean"}))
    captured, available = 0.0, 0.0
    for _, day in work.groupby("date"):
        if len(day) < top_n:
            continue
        chosen = day.nlargest(top_n, "predicted")["actual"].sum()
        best = day.nlargest(top_n, "actual")["actual"].sum()
        captured += chosen
        available += best
    return float(captured / available) if available else float("nan")


def evaluate(variant, season, walk_forward=False, recalibrate=False, scoresets=()):
    frame = load(variant, season, walk_forward)
    if recalibrate:
        import drift
        frame = drift.recalibrate(frame)
    played = played_rows(frame)
    report = {"variant": variant, "walk_forward": walk_forward, "recalibrated": recalibrate,
              "holdout_rows": len(frame), "played_rows": len(played)}

    # P(plays) -- scored on every candidate, not just the ones who played.
    if "pred_plays" in frame:
        actual = frame["target_played"].astype(float).to_numpy()
        predicted = frame["pred_plays"].to_numpy()
        report["plays"] = {
            "auc": float(roc_auc_score(actual, predicted)),
            "brier": float(np.mean((predicted - actual) ** 2)),
            "log_loss": float(-np.mean(actual * np.log(np.clip(predicted, 1e-9, 1))
                                       + (1 - actual) * np.log(np.clip(1 - predicted, 1e-9, 1)))),
            "base_rate": float(actual.mean()),
            "mean_predicted": float(predicted.mean()),
            "reliability": reliability(actual, predicted),
        }

    for name in ("toi", "ev_toi", "pp_toi"):
        if f"pred_{name}" in played:
            entry = {"model": point_metrics(played[f"target_{name}"], played[f"pred_{name}"])}
            if name == "toi" and "mean_toi_std" in played:
                entry["baseline_season_mean"] = point_metrics(
                    played["target_toi"], played["mean_toi_std"].fillna(0))
            report[name] = entry

    dispersion = calibrate.load_dispersion()
    for category in CATEGORIES:
        prediction = played.get(f"pred_{category}")
        if prediction is None:
            continue
        actual = played[f"target_{category}"]
        entry = {"model": point_metrics(actual, prediction)}
        for label, values in baselines(played, category).items():
            entry[f"baseline_{label}"] = point_metrics(actual, values)
        theta = dispersion.get(category)
        if theta is not None:
            entry["dispersion"] = theta
            entry["crps"] = crps_negative_binomial(actual, prediction, theta)
            entry["crps_poisson"] = crps_negative_binomial(actual, prediction, 1e-6)
            entry["pit"] = pit_histogram(actual, prediction, theta)
        report[category] = entry

    if "pred_pp_point_share" in played:
        scorers = played[played["target_points"].fillna(0) > 0]
        share = (scorers["target_ppp"] / scorers["target_points"]).clip(0, 1)
        report["pp_point_share"] = point_metrics(share, scorers["pred_pp_point_share"])

    if scoresets:
        report["composite"] = {sc.name: composite_metrics(played, sc) for sc in scoresets}
    return report


def composite_metrics(played, scoreset):
    """What one scoring system would have made of these projections.

    Reported per scoring file rather than baked in, so the same fitted models can be judged
    under any format -- and so two formats can be compared against one fixed set of models.
    """
    actual = scoreset.score(stat_line(played, "target_"))
    predicted = scoreset.score(stat_line(played, "pred_"))
    entry = {
        "weights": scoreset.skaters,
        "description": scoreset.description,
        "model": point_metrics(actual, predicted),
        "start_sit_capture_top100": start_sit_accuracy(played, actual, predicted),
    }
    for label in ("rate x TOI", "last-10"):
        naive = pd.DataFrame(index=played.index)
        for category in CATEGORIES:
            values = baselines(played, category).get(label)
            if values is not None:
                naive[category] = values
        if not naive.empty:
            naive_points = scoreset.score(naive)
            entry[f"baseline_{label}"] = point_metrics(actual, naive_points)
            entry[f"start_sit_{label}"] = start_sit_accuracy(played, actual, naive_points)
    return entry


def cross_features(variant, holdout_season, own_report, scoresets=()):
    """Run the *trained* models over another variant's holdout features.

    The models are fit on variant B -- the actual lineup with calibrated noise -- while a
    live Daily Faceoff chart is closer to variant A. Scoring the same boosters on A's
    features is therefore the bound on how much of the stack's accuracy is borrowed from
    knowing the lineup better than the feed will. A small gap means the projections stand on
    player history; a large one means they lean on the lineup, and the number to watch once
    the live job has measured chart-vs-opening error for real.
    """
    import predict

    table = pd.read_parquet(paths.feature_table(holdout_season, variant))
    table["game_date"] = pd.to_datetime(table["game_date"])
    projected = predict.project(table)
    played = table["target_played"].astype(bool).to_numpy()

    scored = projected.loc[played].copy()
    for column in table.columns:
        if column.startswith("target_"):
            scored[column] = table.loc[played, column].to_numpy()
    for category in CATEGORIES:
        scored[f"pred_{category}"] = scored[f"lambda_{category}"]
    scored["pred_pp_point_share"] = scored["pp_point_share"]

    out = {
        "features_variant": variant,
        "rows": int(len(scored)),
        "plays_auc": float(roc_auc_score(table["target_played"].astype(float),
                                         projected["p_plays"])),
        "per_category": {c: point_metrics(scored[f"target_{c}"], scored[f"pred_{c}"])
                         for c in CATEGORIES if f"pred_{c}" in scored},
    }
    for scoreset in scoresets:
        actual = scoreset.score(stat_line(scored, "target_"))
        predicted = scoreset.score(stat_line(scored, "pred_"))
        metrics = point_metrics(actual, predicted)
        own = own_report.get("composite", {}).get(scoreset.name, {}).get("model", {}).get("mae")
        out.setdefault("composite", {})[scoreset.name] = {
            "model": metrics,
            "start_sit_capture_top100": start_sit_accuracy(scored, actual, predicted),
            "mae_delta_vs_own_variant": None if own is None else round(metrics["mae"] - own, 4),
        }
    return out


def reliability(actual, predicted, bins=10):
    """Predicted vs observed rate per decile of predicted probability."""
    edges = np.linspace(0, 1, bins + 1)
    index = np.clip(np.digitize(predicted, edges) - 1, 0, bins - 1)
    rows = []
    for b in range(bins):
        in_bin = index == b
        if not in_bin.any():
            continue
        rows.append({"bin": f"{edges[b]:.1f}-{edges[b + 1]:.1f}",
                     "n": int(in_bin.sum()),
                     "predicted": round(float(predicted[in_bin].mean()), 4),
                     "observed": round(float(actual[in_bin].mean()), 4)})
    return rows


def summarise(report):
    """A scannable table of each category against its best baseline."""
    label = "walk-forward (monthly refit)" if report.get("walk_forward") else "single fit"
    if report.get("recalibrated"):
        label += " + drift recalibration"
    lines = [f"{label} -- {report['played_rows']:,} played rows of "
             f"{report['holdout_rows']:,} candidates"]
    if "plays" in report:
        p = report["plays"]
        lines.append(f"plays      AUC {p['auc']:.4f}  Brier {p['brier']:.4f}  "
                     f"base rate {p['base_rate']:.3f}")
    for name in ("toi", "ev_toi", "pp_toi"):
        if name in report and report[name].get("model"):
            m = report[name]["model"]
            lines.append(f"{name:<10} MAE {m['mae']:7.1f}s  rho {m['spearman']:.3f}")
    header = f"{'category':<10} {'MAE':>7} {'rho':>7} {'CRPS':>7}   vs baselines"
    lines += ["", header, "-" * len(header)]
    for category in CATEGORIES:
        entry = report.get(category)
        if not entry:
            continue
        m = entry["model"]
        against = "  ".join(
            f"{key.replace('baseline_', '')} {entry[key]['mae']:.3f}"
            f"{'+' if entry[key]['mae'] > m['mae'] else '-'}"
            for key in entry if key.startswith("baseline_"))
        crps = entry.get("crps")
        lines.append(f"{category:<10} {m['mae']:7.3f} {m['spearman']:7.3f} "
                     f"{crps if crps is None else f'{crps:7.3f}'}   {against}")
    composite = report.get("composite") or {}
    for name, entry in composite.items():
        m = entry["model"]
        lines += ["", f"under '{name}' scoring: MAE {m['mae']:.3f}  rho {m['spearman']:.3f}"
                      f"  top-100 capture {entry['start_sit_capture_top100']:.3f}"
                      f"  (mean {m['mean_actual']:.2f} pts/game)"]
        for key in entry:
            if key.startswith("baseline_"):
                lines.append(f"            {key.replace('baseline_', ''):<12} "
                             f"MAE {entry[key]['mae']:.3f}")
    if not composite:
        lines += ["", "no scoring file given -- per-category metrics only "
                      "(pass --weights to add composite scoring)"]

    cross = report.get("cross_features")
    if cross:
        lines += ["", f"same models on variant {cross['features_variant']} features "
                      f"(the live-feed bound): plays AUC {cross['plays_auc']:.4f}"]
        for name, entry in (cross.get("composite") or {}).items():
            delta = entry["mae_delta_vs_own_variant"]
            lines.append(f"            {name:<16} MAE {entry['model']['mae']:.3f}"
                         + ("" if delta is None else f" ({delta:+.3f})")
                         + f"  top-100 capture {entry['start_sit_capture_top100']:.3f}")
    return "\n".join(lines)


def main():
    args = parse_args()
    if args.list_scoresets:
        for path in weights_module.available():
            scoreset = weights_module.load(path)
            print(f"{scoreset.name:<16} {scoreset.description}")
            print(f"{'':<16} {scoreset.describe()}")
        return

    paths.ensure(paths.REPORTS_DIR)
    scoresets = [weights_module.load(w) for w in (args.weights or [])]
    if scoresets:
        log.info("scoring under: %s", ", ".join(x.name for x in scoresets))
    report = evaluate(args.variant, args.holdout_season, args.walk_forward, args.recalibrate,
                      scoresets)

    if args.cross_features and not args.walk_forward:
        report["cross_features"] = cross_features(
            args.cross_features, args.holdout_season, report, scoresets)

    kind = "metrics_walkforward_" if args.walk_forward else "metrics_"
    path = paths.REPORTS_DIR / f"{kind}{args.variant}_{args.holdout_season}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(summarise(report))
    log.info("wrote %s", path.name)


if __name__ == "__main__":
    main()

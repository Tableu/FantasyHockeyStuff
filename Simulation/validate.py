#!/usr/bin/env python
"""Does the simulator produce the season that actually happened?

The build plan asks for exactly this and is specific about what "calibrated" has to mean:
match the variance, match the **tails**, and match the variance of a *roster total* rather
than only a player's. So the report has four parts, in rising order of how much they matter
downstream:

    fidelity     the sampler must not move a projection: every category's simulated mean
                 against `p_plays * lambda` on the live-shaped table, closed form, no holdout
                 involved. This is the check that catches a sampler bug as a sampler bug --
                 it is how a 54% inflation in penalty minutes was found, at a point when
                 every holdout comparison still looked plausible.
    means        the same thing against the holdout, where a gap is the projection's bias
    spread       simulated variance against the holdout's, per category
    tails        P(Y >= k) simulated against observed, because boom games decide weeks
    rosters      the variance of a ten-skater total, random and stacked, against the same
                 thing measured directly off the holdout -- and against what independent
                 sampling would have given, which is the baseline this layer exists to beat

Everything is scored on the 2025-26 holdout, conditioned on the player having dressed (the
lambda table's `p_plays` is forced to 1 here) so that the scratch mass cannot flatter or
dilute a spread. Roster checks need a scoring system to turn a stat line into a number, so
they are reported per `--weights` file and only then; without one, the first three parts
still print.

Usage:
    python validate.py --sims 200
    python validate.py --sims 200 --weights points-league --weights banger-league
    python validate.py --sims 200 --weights points-league --independent
"""

import argparse
import json
import logging

import numpy as np
import pandas as pd

import copula as copula_module
import correlations as correlations_module
import paths
import sampler
import scoring as scoring_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("validate")

CATEGORIES = sampler.CATEGORIES
TAIL_THRESHOLDS = {"shots": [1, 3, 5, 7], "hits": [1, 3, 5, 7], "blocks": [1, 2, 4, 6],
                   "assists": [1, 2, 3], "goals": [1, 2, 3], "pim": [2, 4, 5, 10]}
PIT_BINS = 10
ROSTER_SIZE = 10
ROSTERS_PER_DAY = 150


def parse_args():
    parser = argparse.ArgumentParser(description="Check the simulator against the holdout")
    parser.add_argument("--variant", choices=("A", "B"), default="B")
    parser.add_argument("--sims", type=int, default=200)
    parser.add_argument("--weights", action="append", default=None, metavar="FILE",
                        help="Scoring file for the roster checks; repeat for several")
    parser.add_argument("--independent", action="store_true",
                        help="Also run the plan's independent-sampling baseline, for contrast")
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument("--fidelity-season", default="2025-26",
                        help="Season whose live-shaped lambda table the fidelity check uses")
    parser.add_argument("--fidelity-sims", type=int, default=400)
    parser.add_argument("--skip-fidelity", action="store_true")
    parser.add_argument("--out", default="validation.json")
    parser.add_argument("--docs", action="store_true",
                        help="Also write docs/calibration.md from this run")
    return parser.parse_args()


def fidelity(simulator, season, sims, days=6):
    """Simulated means against `p_plays * lambda`, on the table a consumer actually passes.

    Everything else here is scored against the holdout, which conflates a sampler error with
    a projection error. This does not: the answer is known in closed form, scratch gate
    included, so any deviation beyond Monte Carlo noise is this layer's fault.
    """
    path = paths.lambda_table(season, "A")
    if not path.exists():
        log.warning("%s is missing; skipping the fidelity check", path)
        return None
    table = pd.read_parquet(path)
    table["game_date"] = pd.to_datetime(table["game_date"])
    dates = sorted(table["game_date"].unique())[::max(1, len(set(table["game_date"])) // days)]
    table = table[table["game_date"].isin(dates[:days])]
    out = {}
    for day, rows in table.groupby("game_date"):
        draws = simulator.draw(rows, sims)
        for category in CATEGORIES:
            expected = float((rows["p_plays"] * rows[f"lambda_{category}"]).sum())
            got = float(draws[category].astype("float64").mean(axis=1).sum())
            entry = out.setdefault(category, {"simulated": 0.0, "expected": 0.0})
            entry["simulated"] += got
            entry["expected"] += expected
    for category, entry in out.items():
        entry["error_pct"] = round(100 * (entry["simulated"] / entry["expected"] - 1), 3)
        entry["simulated"] = round(entry["simulated"], 2)
        entry["expected"] = round(entry["expected"], 2)
    worst = max(abs(e["error_pct"]) for e in out.values())
    log.info("fidelity on %d days of the live-shaped table: worst category off by %.2f%%",
             len(set(table["game_date"])), worst)
    return out


class MarginalStats:
    """Per-category running totals over every day's draws."""

    def __init__(self):
        self.rows = 0
        self.sum = {c: 0.0 for c in CATEGORIES}
        self.sum_squares = {c: 0.0 for c in CATEGORIES}
        self.tails = {c: {k: 0.0 for k in TAIL_THRESHOLDS[c]} for c in CATEGORIES}
        self.pit = {c: np.zeros(PIT_BINS) for c in CATEGORIES}
        self.lambda_sum = {c: 0.0 for c in CATEGORIES}
        self.actual_sum = {c: 0.0 for c in CATEGORIES}
        self.actual_squares = {c: 0.0 for c in CATEGORIES}
        self.actual_tails = {c: {k: 0.0 for k in TAIL_THRESHOLDS[c]} for c in CATEGORIES}

    def add(self, draws, actual, rng, table=None):
        rows, sims = draws.played.shape
        self.rows += rows
        for category in CATEGORIES:
            if table is not None:
                self.lambda_sum[category] += table[f"lambda_{category}"].sum()
            values = draws[category].astype("float64")
            truth = actual[f"target_{category}"].to_numpy("float64")
            self.sum[category] += values.mean(axis=1).sum()
            self.sum_squares[category] += (values ** 2).mean(axis=1).sum()
            self.actual_sum[category] += truth.sum()
            self.actual_squares[category] += (truth ** 2).sum()
            for threshold in TAIL_THRESHOLDS[category]:
                self.tails[category][threshold] += (values >= threshold).mean(axis=1).sum()
                self.actual_tails[category][threshold] += float((truth >= threshold).sum())
            # Randomized PIT, the discrete-data version: F(y-1) + v * P(Y=y) is uniform when
            # the simulated distribution is the one the outcome came from.
            below = (values < truth[:, None]).mean(axis=1)
            equal = (values == truth[:, None]).mean(axis=1)
            uniform = below + rng.random(rows) * equal
            self.pit[category] += np.histogram(uniform, bins=PIT_BINS, range=(0, 1))[0]

    def report(self):
        out = {}
        for category in CATEGORIES:
            mean = self.sum[category] / self.rows
            projected = self.lambda_sum[category] / self.rows
            variance = self.sum_squares[category] / self.rows - mean ** 2
            actual_mean = self.actual_sum[category] / self.rows
            actual_variance = self.actual_squares[category] / self.rows - actual_mean ** 2
            pit = self.pit[category] / self.pit[category].sum()
            out[category] = {
                "simulated_mean": round(mean, 4),
                "projected_mean": round(projected, 4),
                "actual_mean": round(actual_mean, 4),
                # Against the lambda it was handed: whether the sampler is faithful.
                "sampler_error_pct": round(100 * (mean / max(projected, 1e-9) - 1), 2),
                # Against what happened: the projection layer's own level bias, which this
                # layer neither creates nor fixes (Projections/drift.py does that).
                "projection_error_pct": round(100 * (projected / actual_mean - 1), 2),
                "simulated_variance": round(variance, 4),
                "actual_variance": round(actual_variance, 4),
                "variance_ratio": round(variance / actual_variance, 4),
                "tails": {str(k): {"simulated": round(v / self.rows, 5),
                                   "actual": round(self.actual_tails[category][k] / self.rows, 5),
                                   "ratio": round((v / max(self.actual_tails[category][k], 1e-9)), 4)}
                          for k, v in self.tails[category].items()},
                "pit_max_deviation": round(float(np.abs(pit - 1.0 / PIT_BINS).max()), 4),
                "pit": [round(float(v), 4) for v in pit],
            }
        return out


class RosterStats:
    """Roster-total variance, simulated and actual, random rosters and team stacks."""

    def __init__(self, scoreset):
        self.scoreset = scoreset
        self.simulated = {"random": [], "stack": []}
        self.actual = {"random": [], "stack": []}
        self.player_variance_sim = {"random": [], "stack": []}
        self.player_variance_actual = []

    def add(self, draws, actual, rng):
        points = self.scoreset.score_draws(draws)                      # [rows, sims]
        actual_points = self.scoreset.score_columns(actual, "target_")
        expected = points.mean(axis=1)
        residual = actual_points - expected
        self.player_variance_actual.append(residual)

        team_key = (actual["game_id"].astype(str) + ":" + actual["team_id"].astype(str)).to_numpy()
        rows = len(actual)
        if rows < ROSTER_SIZE:
            return
        by_team = pd.Series(np.arange(rows)).groupby(team_key).apply(list)
        stacks = [np.array(v) for v in by_team if len(v) >= ROSTER_SIZE]

        for mode in ("random", "stack"):
            for _ in range(ROSTERS_PER_DAY):
                if mode == "random":
                    pick = rng.choice(rows, ROSTER_SIZE, replace=False)
                else:
                    if not stacks:
                        continue
                    block = stacks[rng.integers(len(stacks))]
                    pick = rng.choice(block, ROSTER_SIZE, replace=False)
                totals = points[pick].sum(axis=0)
                self.simulated[mode].append(totals - totals.mean())
                self.actual[mode].append(float(residual[pick].sum()))
                self.player_variance_sim[mode].append(float(points[pick].var(axis=1).sum()))

    def report(self):
        out = {}
        actual_player_variance = float(np.var(np.concatenate(self.player_variance_actual)))
        for mode in ("random", "stack"):
            if not self.simulated[mode]:
                continue
            simulated = np.concatenate(self.simulated[mode])
            independent_sim = float(np.mean(self.player_variance_sim[mode]))
            actual = np.array(self.actual[mode])
            out[mode] = {
                "rosters": len(self.actual[mode]),
                "simulated_variance_ratio": round(float(np.var(simulated)) / independent_sim, 4),
                "actual_variance_ratio": round(
                    float(np.var(actual)) / (ROSTER_SIZE * actual_player_variance), 4),
                "simulated_total_sd": round(float(np.std(simulated)), 3),
                "actual_total_sd": round(float(np.std(actual)), 3),
            }
        out["per_player_residual_variance_actual"] = round(actual_player_variance, 4)
        return out


def run(args):
    dispersion = correlations_module.load_dispersion()
    holdout = correlations_module.load_holdout(args.variant)
    table = correlations_module.lambda_frame(holdout)
    table["game_date"] = pd.to_datetime(holdout["game_date"].to_numpy())
    holdout = holdout.copy()
    holdout["game_date"] = table["game_date"]

    payload = json.loads(paths.CORRELATIONS_PATH.read_text(encoding="utf-8"))
    weights = payload["penalty_incidents"]["weights"]
    latent_variance = payload["penalty_incidents"].get("latent_variance", 0.0)
    structures = {"fitted": copula_module.load(paths.CORRELATIONS_PATH)}
    if args.independent:
        structures["independent"] = copula_module.independent()

    scoresets = [scoring_module.load(w) for w in (args.weights or [])]
    results = {}
    for label, structure in structures.items():
        simulator = sampler.Simulator(dispersion, structure, weights, latent_variance,
                                      seed=args.seed)
        rng = np.random.default_rng(args.seed + 1)
        marginals_stats = MarginalStats()
        roster_stats = {s.name: RosterStats(s) for s in scoresets}

        for day, rows in table.groupby("game_date"):
            actual = holdout.loc[rows.index]
            draws = simulator.draw(rows, args.sims)
            marginals_stats.add(draws, actual, rng, rows)
            for stats in roster_stats.values():
                stats.add(draws, actual, rng)

        results[label] = {"marginals": marginals_stats.report(),
                          "rosters": {name: s.report() for name, s in roster_stats.items()}}
        log.info("%s: %d player-games x %d sims", label, marginals_stats.rows, args.sims)

    report = {"variant": args.variant, "sims": args.sims,
              "rows": int(len(table)), "results": results}
    if not args.skip_fidelity:
        simulator = sampler.Simulator(dispersion, structures["fitted"], weights,
                                      latent_variance, seed=args.seed + 99)
        report["fidelity"] = fidelity(simulator, args.fidelity_season, args.fidelity_sims)
    destination = paths.ensure(paths.REPORTS_DIR) / args.out
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print_report(report)
    log.info("wrote %s", destination)
    if args.docs:
        write_docs(report, paths.ensure(paths.DOCS_DIR) / "calibration.md")
    return report


def table(rows, headers):
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    line = lambda cells: "| " + " | ".join(
        str(c).ljust(w) for c, w in zip(cells, widths)) + " |"
    return "\n".join([line(headers), "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
                     + [line(r) for r in rows])


def write_docs(report, destination):
    """The calibration page, written from the run rather than typed out beside it."""
    fitted = report["results"]["fitted"]
    out = ["# Monte Carlo calibration", "",
           f'Scored on the {report["variant"]} holdout: {report["rows"]:,} played '
           f'player-games x {report["sims"]:,} draws. Regenerate with '
           "`python validate.py --sims 200 --weights points-league --docs`.", ""]

    if report.get("fidelity"):
        out += ["## The sampler does not move a projection", "",
                "Simulated means against `p_plays * lambda` on the live-shaped variant-A "
                "table -- closed form, no holdout, so any gap here is this layer's fault "
                "and nobody else's.", "",
                table([[c, v["simulated"], v["expected"], f'{v["error_pct"]:+.3f}%']
                       for c, v in report["fidelity"].items()],
                      ["category", "simulated", "p_plays x lambda", "error"]), ""]

    out += ["## Spread, per category", "",
            "`sampler` is the simulated mean against the lambda it was handed; "
            "`projection` is that lambda against what happened, which is the projection "
            "layer's own level bias and is neither created nor repaired here.", "",
            table([[c, v["simulated_mean"], v["actual_mean"],
                    f'{v["sampler_error_pct"]:+.2f}%', f'{v["projection_error_pct"]:+.2f}%',
                    v["simulated_variance"], v["actual_variance"], v["variance_ratio"],
                    v["pit_max_deviation"]]
                   for c, v in fitted["marginals"].items()],
                  ["category", "sim mean", "actual", "sampler", "projection", "sim var",
                   "actual var", "var ratio", "PIT max dev"]), ""]

    out += ["## Tails", "",
            "P(Y >= k), simulated against observed. Boom games decide head-to-head weeks, "
            "so matching the second moment is not enough on its own.", ""]
    rows = []
    for category, entry in fitted["marginals"].items():
        for threshold, values in entry["tails"].items():
            rows.append([category, f">= {threshold}", values["simulated"],
                         values["actual"], values["ratio"]])
    out += [table(rows, ["category", "threshold", "simulated", "actual", "ratio"]), ""]

    out += ["## Roster totals", "",
            "The number this layer exists for. A ten-skater roster's variance against what "
            "independent players would give: measured off the holdout, simulated here, and "
            "-- where the run included it -- under independent sampling for contrast.", ""]
    rows = []
    for label, result in report["results"].items():
        for name, roster in result["rosters"].items():
            for mode in ("random", "stack"):
                if mode in roster:
                    rows.append([label, name, mode,
                                 roster[mode]["simulated_variance_ratio"],
                                 roster[mode]["actual_variance_ratio"],
                                 roster[mode]["simulated_total_sd"],
                                 roster[mode]["actual_total_sd"]])
    out += [table(rows, ["structure", "scoring", "roster", "sim ratio", "actual ratio",
                         "sim sd", "actual sd"]), ""]

    destination.write_text("\n".join(out), encoding="utf-8")
    log.info("wrote %s", destination)


def print_report(report):
    if report.get("fidelity"):
        print("\n=== sampler fidelity: simulated mean vs p_plays x lambda ===")
        print(pd.DataFrame([{"category": c, **v} for c, v in report["fidelity"].items()]
                           ).to_string(index=False))
    for label, result in report["results"].items():
        print(f"\n=== {label} ===")
        rows = []
        for category, entry in result["marginals"].items():
            rows.append([category, entry["simulated_mean"], entry["projected_mean"],
                         entry["actual_mean"], f'{entry["sampler_error_pct"]:+.2f}%',
                         f'{entry["projection_error_pct"]:+.2f}%',
                         entry["simulated_variance"], entry["actual_variance"],
                         entry["variance_ratio"], entry["pit_max_deviation"]])
        print(pd.DataFrame(rows, columns=["category", "sim mean", "lambda", "actual",
                                          "sampler err", "projection err", "sim var",
                                          "actual var", "var ratio",
                                          "PIT max dev"]).to_string(index=False))
        for category, entry in result["marginals"].items():
            tails = "  ".join(f'P(>={k}) {v["simulated"]:.4f}/{v["actual"]:.4f}'
                              for k, v in entry["tails"].items())
            print(f"  {category:8s} {tails}")
        for name, roster in result["rosters"].items():
            print(f"\n  roster totals under {name}:")
            for mode in ("random", "stack"):
                if mode in roster:
                    entry = roster[mode]
                    print(f"    {mode:7s} variance ratio simulated {entry['simulated_variance_ratio']:.3f}"
                          f"  actual {entry['actual_variance_ratio']:.3f}"
                          f"  (total sd {entry['simulated_total_sd']:.2f} vs "
                          f"{entry['actual_total_sd']:.2f})")


def main():
    run(parse_args())


if __name__ == "__main__":
    main()

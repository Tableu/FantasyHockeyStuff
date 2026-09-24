#!/usr/bin/env python
"""Is the goalie sampler calibrated? Scored against the real starts of a season it never saw.

    python validate_goalies.py --season 2025-26 --sims 300 --weights points-league

The fit (`goalie_fit.py`) comes from the seasons before; the skaters are drawn from the scored
holdout exactly as `validate.py` draws them; the P(start) table is Projections' holdout
prediction. Four questions:

    per start      the starter's line and points against the real starter's, per team-game:
                   mean, variance, tails, W/L/OTL/shutout/pull rates, a PIT of points
    per candidate  mean points per goalie row, which is where P(start) enters
    links          correlation of a starter's points with his own skaters' points and with the
                   opponent's -- the closed form's answer is 0 for both
    rosters        total variance over the sum of its players' variances, for rosters of ten
                   skaters plus two goalies: random, a stack with its own goalies, and a stack with
                   the opponent's goalies
"""

import argparse
import json
import logging

import numpy as np
import pandas as pd

import goalies as goalies_module
import paths
import sampler
import scoring as scoring_module
import validate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("validate_goalies")

PIT_BINS = 10
ROSTERS_PER_DAY = 30


def parse_args():
    parser = argparse.ArgumentParser(description="Validate the goalie sampler")
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--variant", default="B")
    parser.add_argument("--sims", type=int, default=300)
    parser.add_argument("--weights", action="append", default=None, metavar="FILE")
    parser.add_argument("--seed", type=int, default=31)
    parser.add_argument("--out", default=None)
    return parser.parse_args()


def load_truth(season):
    starts = pd.read_parquet(paths.FEATURES_DIR / f"goalie_starts_{season}.parquet")
    starts["game_date"] = pd.to_datetime(starts["game_date"])
    return starts


def corr(x, y):
    x, y = np.asarray(x, dtype="float64"), np.asarray(y, dtype="float64")
    return float(np.corrcoef(x, y)[0, 1]) if len(x) > 2 and x.std() > 0 and y.std() > 0 else float("nan")


def run(args):
    scoreset = scoring_module.load((args.weights or ["points-league"])[0])
    table, holdout = validate.holdout_frames(args.variant)
    correlations = paths.correlations_path(args.season)
    payload = json.loads(correlations.read_text(encoding="utf-8"))
    simulator = sampler.Simulator(
        validate.correlations_module.load_dispersion(args.season),
        validate.copula_module.load(correlations), payload["penalty_incidents"]["weights"],
        payload["penalty_incidents"].get("latent_variance", 0.0), seed=args.seed)
    fit = goalies_module.GoalieFit.load(paths.goalie_fit_path(args.season))
    rng = np.random.default_rng(args.seed + 7)

    pstart = pd.read_parquet(paths.PROJECTIONS_REPORTS / f"goalie_pstart_{args.season}.parquet")
    pstart["game_date"] = pd.to_datetime(pstart["game_date"])
    truth = load_truth(args.season)
    truth["points"] = scoreset.score_columns(truth, side="goalies")
    truth_by_row = truth.set_index(["game_id", "team_id", "player_id"])

    sim_start, act_start = {k: [] for k in ("points", "saves", "ga")}, {k: [] for k in ("points", "saves", "ga")}
    rates_sim = {k: 0.0 for k in ("wins", "losses", "ot_losses", "shutouts", "pulled")}
    rates_act = {k: 0.0 for k in ("wins", "losses", "ot_losses", "shutouts", "pulled")}
    pit = np.zeros(PIT_BINS)
    team_games = 0
    cand_sim, cand_act = [], []
    links = {"own": {"sim": [], "act": []}, "opp": {"sim": [], "act": []}}
    rosters = {m: {"sim_ratio": [], "act_total": [], "act_parts": []} for m in ("random", "own", "opp")}
    residual_sk, residual_g = [], []

    for day, rows in table.groupby("game_date"):
        g_rows = pstart[(pstart["game_date"] == day) & pstart["game_id"].isin(rows["game_id"])]
        if not len(g_rows):
            continue
        both = rows.groupby("game_id")["team_id"].nunique()
        keep = both[both == 2].index
        rows, g_rows = rows[rows["game_id"].isin(keep)], g_rows[g_rows["game_id"].isin(keep)]
        if not len(rows) or not len(g_rows):
            continue
        actual_sk = holdout.loc[rows.index]
        draws = simulator.draw(rows, args.sims)
        gd = goalies_module.draw_goalies(draws, g_rows, fit, rng)
        sk_points = scoreset.score_draws(draws)                                  # [rows, sims]
        g_points = scoreset.score_draws(gd, side="goalies")
        sk_actual = scoreset.score_columns(actual_sk, "target_")

        keys = gd.keys.reset_index(drop=True)
        real = truth_by_row.reindex(pd.MultiIndex.from_frame(keys[["game_id", "team_id", "player_id"]]))
        g_actual = real["points"].fillna(0.0).to_numpy()
        cand_sim.append(g_points.mean(axis=1)); cand_act.append(g_actual)

        # Per team-game: the sampled starter against the real one.
        tg = (keys["game_id"].astype(str) + ":" + keys["team_id"].astype(str)).to_numpy()
        codes, uniq = pd.factorize(tg)
        n = len(uniq)
        start_pts = np.zeros((n, args.sims)); np.add.at(start_pts, codes, g_points * gd.played)
        start_sv = np.zeros((n, args.sims)); np.add.at(start_sv, codes, gd["saves"])
        start_ga = np.zeros((n, args.sims)); np.add.at(start_ga, codes, gd["goals_against"])
        for k in rates_sim:
            per = np.zeros((n, args.sims)); np.add.at(per, codes, gd[k])
            rates_sim[k] += per.mean(axis=1).sum()
        starters = truth[(truth["game_date"] == day) & truth["is_starter"].astype(bool)]
        starters = starters.assign(tg=starters["game_id"].astype(str) + ":" + starters["team_id"].astype(str))
        starters = starters.set_index("tg").reindex(uniq)
        ok = starters["points"].notna().to_numpy()
        team_games += int(ok.sum())
        for name, sim, col in (("points", start_pts, "points"), ("saves", start_sv, "saves"),
                               ("ga", start_ga, "goals_against")):
            sim_start[name].append(sim[ok]); act_start[name].append(starters[col].to_numpy()[ok])
        for k, col in (("wins", "wins"), ("losses", "losses"), ("ot_losses", "ot_losses"),
                       ("shutouts", "shutouts"), ("pulled", "pulled")):
            rates_act[k] += float(starters[col].astype(float)[ok].sum())
        a = starters["points"].to_numpy()[ok]
        s = start_pts[ok]
        v = rng.random(len(a))
        u = (s < a[:, None]).mean(axis=1) + v * (s == a[:, None]).mean(axis=1)
        pit += np.histogram(u, bins=PIT_BINS, range=(0, 1))[0]

        # Links: the starter against his own and the opposing skaters, per team-game.
        sk_tg = (rows["game_id"].astype(str) + ":" + rows["team_id"].astype(str)).map(
            {k: i for i, k in enumerate(uniq)}).to_numpy()
        valid = ~np.isnan(sk_tg.astype("float64"))
        sk_tg_i = sk_tg[valid].astype(int)
        team_sim = np.zeros((n, args.sims)); np.add.at(team_sim, sk_tg_i, sk_points[valid])
        team_act = np.zeros(n); np.add.at(team_act, sk_tg_i, sk_actual[valid])
        game_of = pd.Series(uniq).str.split(":").str[0].to_numpy()
        opp = np.array([np.flatnonzero((game_of == game_of[i]) & (np.arange(n) != i))[0] for i in range(n)])
        col = rng.integers(args.sims)
        links["own"]["sim"] += list(zip(start_pts[:, col], team_sim[:, col]))
        links["opp"]["sim"] += list(zip(start_pts[:, col], team_sim[opp, col]))
        act_pts = starters["points"].to_numpy()
        links["own"]["act"] += [(x, y) for x, y, o in zip(act_pts, team_act, ok) if o]
        links["opp"]["act"] += [(x, y) for x, y, o in zip(act_pts, team_act[opp], ok) if o]

        # Rosters: ten skaters and two goalies, variance of the total over the sum of its parts.
        sk_res = sk_actual - sk_points.mean(axis=1)
        g_res = g_actual - g_points.mean(axis=1)
        residual_sk.append(sk_res); residual_g.append(g_res)
        g_tg = codes
        for _ in range(ROSTERS_PER_DAY):
            for mode in ("random", "own", "opp"):
                if mode == "random":
                    sk = rng.choice(len(rows), 10, replace=False) if len(rows) >= 10 else None
                    gs = rng.choice(len(keys), 2, replace=False) if len(keys) >= 2 else None
                else:
                    team = rng.integers(n)
                    pool = np.flatnonzero(sk_tg == team)
                    gpool = np.flatnonzero(g_tg == (team if mode == "own" else opp[team]))
                    sk = rng.choice(pool, 10, replace=False) if len(pool) >= 10 else None
                    gs = rng.choice(gpool, 2, replace=False) if len(gpool) >= 2 else None
                if sk is None or gs is None:
                    continue
                total = sk_points[sk].sum(axis=0) + g_points[gs].sum(axis=0)
                parts = sk_points[sk].var(axis=1).sum() + g_points[gs].var(axis=1).sum()
                rosters[mode]["sim_ratio"].append(total.var() / parts if parts > 0 else np.nan)
                rosters[mode]["act_total"].append(sk_res[sk].sum() + g_res[gs].sum())
                rosters[mode]["act_parts"].append((sk, gs))

    sim_p, act_p = np.concatenate(sim_start["points"]), np.concatenate(act_start["points"])
    var_sk = float(np.var(np.concatenate(residual_sk)))
    var_g = float(np.var(np.concatenate(residual_g)))
    report = {
        "season": args.season, "scoring": scoreset.name, "sims": args.sims,
        "team_games": team_games, "fit_trained_on": fit.payload["trained_on"],
        "per_start": {
            "points": {"sim_mean": round(float(sim_p.mean()), 3), "actual_mean": round(float(act_p.mean()), 3),
                       "sim_var": round(float(sim_p.var()), 3), "actual_var": round(float(act_p.var()), 3),
                       "variance_ratio": round(float(sim_p.var() / act_p.var()), 4),
                       "p_le_0": [round(float((sim_p <= 0).mean()), 4), round(float((act_p <= 0).mean()), 4)],
                       "p_ge_10": [round(float((sim_p >= 10).mean()), 4), round(float((act_p >= 10).mean()), 4)]},
            "saves": [round(float(np.concatenate(sim_start["saves"]).mean()), 3),
                      round(float(np.concatenate(act_start["saves"]).mean()), 3)],
            "goals_against": [round(float(np.concatenate(sim_start["ga"]).mean()), 3),
                              round(float(np.concatenate(act_start["ga"]).mean()), 3)],
            "goals_against_var": [round(float(np.concatenate(sim_start["ga"]).var()), 3),
                                  round(float(np.concatenate(act_start["ga"]).var()), 3)],
            "goals_against_p0": [round(float((np.concatenate(sim_start["ga"]) == 0).mean()), 4),
                                 round(float((np.concatenate(act_start["ga"]) == 0).mean()), 4)],
            "rates_sim_vs_actual": {k: [round(rates_sim[k] / team_games, 4), round(rates_act[k] / team_games, 4)]
                                    for k in rates_sim},
            "pit": [round(float(x), 4) for x in pit / pit.sum()],
            "pit_max_deviation": round(float(np.abs(pit / pit.sum() - 1 / PIT_BINS).max()), 4),
        },
        "per_candidate": {"sim_mean": round(float(np.concatenate(cand_sim).mean()), 4),
                          "actual_mean": round(float(np.concatenate(cand_act).mean()), 4)},
        "links": {k: {"simulated": round(corr(*zip(*v["sim"])), 4), "actual": round(corr(*zip(*v["act"])), 4),
                      "pairs": len(v["act"])} for k, v in links.items()},
        "rosters": {},
    }
    for mode, r in rosters.items():
        if not r["act_total"]:
            continue
        parts = len(r["act_parts"]) and (10 * var_sk + 2 * var_g)
        report["rosters"][mode] = {
            "rosters": len(r["act_total"]),
            "simulated_variance_ratio": round(float(np.nanmean(r["sim_ratio"])), 4),
            "actual_variance_ratio": round(float(np.var(r["act_total"]) / parts), 4)}
    out = paths.ensure(paths.REPORTS_DIR) / (args.out or f"goalie_validation_{args.season}.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log.info("-> %s", out)
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    run(parse_args())

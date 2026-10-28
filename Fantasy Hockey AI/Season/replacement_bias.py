#!/usr/bin/env python
"""Is the drop cost's free agent overrated? Predicted against real points, roster vs replacement.

    python replacement_bias.py --league beagles --replication 0     # one season, the test seat
    python replacement_bias.py --league beagles --report            # every season run so far

A rental's spot test and drop cost both compare a rostered skater with the BEST free agent
eligible at his positions (`streaming.spots`, `streaming.Replacement`). The best of hundreds of
estimates is picked on its errors as much as its worth, so his estimate should run high; the
rostered player was not picked as a maximum. This measures both sides: each night, for every
rostered skater the test seat's spot test looks at, his predicted points (the strategy's rate
source, today through `WEEKS` weeks after this one) and his eligible best free agent's, against
the real points each scored on those nights. ~/.claude/plans/replacement-bias.md, step 1.
"""

import argparse
import logging
import sys

import numpy as np
import pandas as pd

import corelayer  # noqa: F401 -- puts Core/ on sys.path; see corelayer.py
import paths

log = logging.getLogger("replacement_bias")

WEEKS = 3
OUT_DIR = paths.REPORTS_DIR / "replacement_bias"


def season(league, weights, strategy, replication, season_name="2024-25"):
    from types import SimpleNamespace

    import ladder
    import oneseat
    from decisionlayer import streaming, valuation

    args = SimpleNamespace(strategy=strategy, candidate=None, set=[], opponents="8",
                           opponent_sd=None, season=season_name, prior_season=None, league=league,
                           weights=weights, workers=1, ros_tag=None)
    ctx, shipped, _, base_layout, _, _ = oneseat.setup(args)
    test = base_layout.test_seat(ctx.config, replication)
    source = shipped.adddrop.rate_source
    rows, original = [], streaming.spots

    def recording(view, params, horizon, source_, pool, eligibility, goalies=False):
        if view.team_index == test:
            nights = {}

            def window(p):
                if p not in nights:
                    nights[p] = [pd.Timestamp(n) for n in view.nights_through(p, WEEKS)]
                return nights[p]

            def facts(p):
                return {"rate": valuation.rate(view, p, source),
                        "board": view.board_rate(p), "ros": view.ros_rate(p),
                        "games_played": view.ros_games(p),
                        "predicted": valuation.player_value(view, p, WEEKS, source),
                        "nights": window(p)}

            ranked = sorted(((valuation.player_value(view, p, None, source), p) for p in pool
                             if not streaming.is_goalie(eligibility, p)), reverse=True)
            day = pd.Timestamp(view.day)
            for p in view.roster:
                if streaming.is_goalie(eligibility, p) or not valuation.known(view, p, source):
                    continue
                positions = eligibility.get(p, frozenset())
                best = next(((v, q) for v, q in ranked
                             if eligibility.get(q, frozenset()) & positions), None)
                if best is None:
                    continue
                mine = valuation.player_value(view, p, None, source)
                pair = {"day": day, "replication": replication, "player_id": int(p),
                        "fa_id": int(best[1]), "season_value": mine, "fa_season_value": best[0],
                        "over": mine - best[0],
                        "fa_waivers": bool(view.on_waivers(best[1])),
                        "fa_injured": best[1] in view.injured}
                rows.append({**pair, **{f"my_{k}": v for k, v in facts(p).items()},
                             **{f"fa_{k}": v for k, v in facts(best[1]).items()}})
        return original(view, params, horizon, source_, pool, eligibility, goalies)

    streaming.spots = recording         # weekplan calls it through the same module
    ladder.run_one(ctx.config, ctx.calendar, ctx.data, ctx.eligibility, ctx.scoreset, base_layout,
                   replication, False, 200, shipped)

    actuals = ctx.data["actuals"].copy()
    actuals["fp"] = ctx.scoreset.score_columns(actuals, prefix="target_")
    actuals["game_date"] = pd.to_datetime(actuals["game_date"])
    points = actuals.groupby(["player_id", "game_date"])["fp"].sum().to_dict()
    out = pd.DataFrame(rows)
    for side in ("my", "fa"):
        ids = out["player_id" if side == "my" else "fa_id"]
        out[f"{side}_real"] = [sum(points.get((p, n), 0.0) for n in ns)
                               for p, ns in zip(ids, out[f"{side}_nights"])]
        out[f"{side}_nights"] = out[f"{side}_nights"].map(len)
    path = paths.ensure(OUT_DIR) / f"{league}_r{replication:03d}_{season_name}.parquet"
    out.to_parquet(path)
    print(f"{len(out)} pairs over {out['day'].nunique()} days -> {path}")


def report(league, season_name="2024-25"):
    files = sorted(OUT_DIR.glob(f"{league}_r*_{season_name}.parquet"))
    f = pd.concat(pd.read_parquet(p) for p in files)
    print(f"{league} {season_name}: {len(files)} season(s), {len(f)} pairs; predicted vs real "
          f"points, today through week+{WEEKS}")

    def table(g, label):
        out = []
        for side, name in (("my", "rostered"), ("fa", "best FA")):
            err = g[f"{side}_real"] - g[f"{side}_predicted"]
            out.append({"slice": label, "side": name, "n": len(g),
                        "predicted": g[f"{side}_predicted"].mean(),
                        "real": g[f"{side}_real"].mean(),
                        "real/pred": g[f"{side}_real"].sum() / g[f"{side}_predicted"].sum(),
                        "bias": err.mean(), "se": err.std() / np.sqrt(len(g))})
        return out

    rows = table(f, "all")
    # The decisions that matter: rostered players near or below replacement.
    near = f[f["over"].abs() <= 40]
    rows += table(near, "|over| <= 40")
    rows += table(f[f["over"] < 0], "spots (over < 0)")
    for lo, hi in ((0, 5), (5, 15), (15, 40), (40, 99)):
        g = near[(near["fa_games_played"] >= lo) & (near["fa_games_played"] < hi)]
        if len(g):
            rows += table(g, f"near, FA gp {lo}-{hi}")
    for flag in ("fa_waivers", "fa_injured"):
        g = near[near[flag]]
        if len(g):
            rows += table(g, f"near, {flag}")
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    # Deduplicated per (day, FA): the bias of "the best FA" as a pick, not weighted by roster size.
    picks = f.drop_duplicates(["replication", "day", "fa_id"])
    e = picks["fa_real"] - picks["fa_predicted"]
    print(f"\nbest-FA picks, one row per (day, FA): n={len(picks)}, bias {e.mean():.2f} "
          f"+/- {e.std() / np.sqrt(len(picks)):.2f}, real/pred "
          f"{picks['fa_real'].sum() / picks['fa_predicted'].sum():.3f}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--league", default="beagles")
    p.add_argument("--weights", default=None, help="Scoring (default: the league's name)")
    p.add_argument("--strategy", default=None, help="Default: strategy-<league>")
    p.add_argument("--replication", type=int, action="append", default=[])
    p.add_argument("--season", default="2024-25")
    p.add_argument("--report", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        stream=sys.stderr)
    for noisy in ("inputs", "draft", "draftroom", "engine", "simlayer", "ladder", "schedule",
                  "simulate", "playoffs"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for r in args.replication:
        season(args.league, args.weights or args.league, args.strategy or f"strategy-{args.league}",
               r, args.season)
    if args.report:
        report(args.league, args.season)


if __name__ == "__main__":
    main()

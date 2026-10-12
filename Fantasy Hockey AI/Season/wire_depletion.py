#!/usr/bin/env python
"""How fast the top of the free-agent pool is taken: 12088's real league against the simulator.

    python wire_depletion.py --real                       # 12088, from its transaction log
    python wire_depletion.py --sim --replication 0        # one rung-8 league, its pool each day
    python wire_depletion.py --report                     # the two side by side

Step 1 of the replacement-scarcity plan (~/.claude/plans/replacement-scarcity.md). The drop cost
prices a player against the free agent we could get back; whether that free agent is still there
depends on how fast opponents take the best ones. An A/B of a scarcity-aware drop cost means
nothing if the simulated opponents leave the top of the pool alone, so this compares the two.

One yardstick for both: a free agent's REAL fantasy points (points-league, 12088's scoring) over
the next `WINDOW` days -- what was actually worth having, read by neither league's managers. Each
day the skaters free at the start of the day are ranked on it; for each rank 1..`TOP` the result
records whether he is still free 1, 3 and 7 days later. Skaters only (the drop cost this feeds
prices skaters). Both leagues use the simulator's player universe (its projection table), so the
pools are ranked over the same players.

Real ownership is rebuilt from the log: drafted, added, claimed and traded for put a player on a
team, cut takes him off, in league-day order (the 6:00 AM Eastern turnover the importer assigns).
"""

import argparse
import logging
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

import paths

log = logging.getLogger("wire_depletion")

SEASON = "2024-25"
LEAGUE_ID = 12088
WINDOW = 30
TOP = 10
LAGS = (1, 3, 7)
ON = {"drafted", "added", "claimed", "traded for", "imported"}
OUT_DIR = paths.REPORTS_DIR / "wire_depletion"


def future_points(season, scoring="points-league") -> tuple:
    """({day: {player: real points over the next WINDOW days}}, the season's days, the universe)."""
    import inputs
    import simlayer

    data = inputs.load_season(season)
    actuals = data["actuals"].copy()
    actuals["fp"] = simlayer.load_scoreset(scoring).score_columns(actuals, prefix="target_")
    actuals["game_date"] = pd.to_datetime(actuals["game_date"])
    daily = actuals.groupby(["game_date", "player_id"])["fp"].sum().unstack(fill_value=0.0)
    days = pd.date_range(daily.index.min(), daily.index.max())
    daily = daily.reindex(days, fill_value=0.0)
    # Points from the day after through WINDOW days later.
    ahead = daily[::-1].rolling(WINDOW, min_periods=1).sum()[::-1].shift(-1).fillna(0.0)
    universe = set(data["projections"]["player_id"].astype(int))
    return ahead, list(days), universe


def survival(free_by_day: dict, ahead, days) -> pd.DataFrame:
    """Rows (day, rank, still free after each lag) for the top TOP free agents each day."""
    rows = []
    index = {d: i for i, d in enumerate(days)}
    for day, free in free_by_day.items():
        if day not in index or day not in ahead.index:
            continue
        values = ahead.loc[day]
        pool = [p for p in free if p in values.index]
        top = values[pool].sort_values(ascending=False).head(TOP)
        for rank, (p, pts) in enumerate(top.items(), start=1):
            row = {"day": day, "rank": rank, "player_id": int(p), "points_ahead": float(pts)}
            for lag in LAGS:
                later = days[index[day] + lag] if index[day] + lag < len(days) else None
                row[f"free_{lag}d"] = (p in free_by_day[later]) if later in free_by_day else np.nan
            rows.append(row)
    return pd.DataFrame(rows)


def real(season=SEASON):
    ahead, days, universe = future_points(season)
    log_path = paths.PROJECT_ROOT.parent / "ModelFeatures" / "data" / "features" / \
        f"fleaflicker_transactions_{LEAGUE_ID}.parquet"
    t = pd.read_parquet(log_path)
    t = t[(t["season"] == season) & t["player_id"].notna()].sort_values("time_utc")
    t["player_id"] = t["player_id"].astype(int)
    by_day = {d: g for d, g in t.groupby(t["league_day"].dt.normalize())}
    owned, free_by_day = set(), {}
    # Ownership at the start of each NHL day: every event of earlier league days applied.
    for day in pd.date_range(t["league_day"].min().normalize(), days[-1]):
        if day >= days[0]:
            free_by_day[day] = universe - owned
        for _, e in (by_day.get(day, pd.DataFrame()).iterrows()):
            if e["action"] in ON:
                owned.add(e["player_id"])
            elif e["action"] == "cut":
                owned.discard(e["player_id"])
    log.info("12088 %s: %d owned at the end, %d days", season, len(owned), len(free_by_day))
    out = survival(free_by_day, ahead, days)
    path = paths.ensure(OUT_DIR) / f"real_{LEAGUE_ID}_{season}.parquet"
    out.to_parquet(path)
    print(f"-> {path}")


def sim(replication, season=SEASON):
    from types import SimpleNamespace

    import engine as engine_module
    import ladder
    import oneseat

    free_by_day = {}
    original = engine_module.Season._play_day

    def recording(self, day, week, schedule, history):
        free_by_day[pd.Timestamp(day)] = set(self.state.pool)
        return original(self, day, week, schedule, history)

    engine_module.Season._play_day = recording
    args = SimpleNamespace(strategy=None, candidate=None, set=[], opponents="8", opponent_sd=None,
                           season=season, prior_season=None, league="league",
                           weights="points-league", workers=1, ros_tag=None)
    ctx, shipped, _, base_layout, _, _ = oneseat.setup(args)
    result = ladder.run_one(ctx.config, ctx.calendar, ctx.data, ctx.eligibility, ctx.scoreset,
                            base_layout, replication, False, 200, shipped)
    ahead, days, universe = future_points(season)
    free_by_day = {d: f & universe for d, f in free_by_day.items()}
    out = survival(free_by_day, ahead, days)
    path = paths.ensure(OUT_DIR) / f"sim_r{replication:03d}_{season}.parquet"
    out.to_parquet(path)
    tx = result["transactions"]
    print(f"{len(tx)} transactions; -> {path}")


def report(season=SEASON):
    frames = {"12088 (real)": pd.read_parquet(OUT_DIR / f"real_{LEAGUE_ID}_{season}.parquet")}
    sims = sorted(OUT_DIR.glob(f"sim_r*_{season}.parquet"))
    if sims:
        frames[f"simulator ({len(sims)} league(s))"] = pd.concat(pd.read_parquet(p) for p in sims)
    buckets = {"1": [1], "2-3": [2, 3], "4-5": [4, 5], "6-10": list(range(6, 11))}
    for name, f in frames.items():
        print(f"\n{name}: share of the day's top free agents (by real points over the next "
              f"{WINDOW} days) still free after")
        rows = []
        for label, ranks in buckets.items():
            g = f[f["rank"].isin(ranks)]
            rows.append({"rank": label, "pts ahead": g["points_ahead"].mean(),
                         **{f"{lag}d": g[f"free_{lag}d"].mean() for lag in LAGS}})
        print(pd.DataFrame(rows).to_string(index=False, float_format=lambda v: f"{v:.2f}"))
        months = f.assign(month=f["day"].dt.to_period("M"))
        top3 = months[months["rank"] <= 3].groupby("month")[[f"free_{lag}d" for lag in LAGS]].mean()
        print("top 3 by month:\n" + top3.to_string(float_format=lambda v: f"{v:.2f}"))


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--real", action="store_true")
    p.add_argument("--sim", action="store_true")
    p.add_argument("--replication", type=int, default=0)
    p.add_argument("--report", action="store_true")
    p.add_argument("--season", default=SEASON)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        stream=sys.stderr)
    for noisy in ("inputs", "draft", "draftroom", "engine", "simlayer", "ladder", "schedule"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.real:
        real(args.season)
    if args.sim:
        sim(args.replication, args.season)
    if args.report:
        report(args.season)


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Goalie workload: how many of his team's remaining games a goalie will start.

A goalie's in-season value has been `P(start) x the league-average line`, with P(start) the
latest per-game estimate -- the 50% prior before a goalie has history. So a 60-start starter and
a 25-start backup rate alike whenever their latest P(start) does, and before the season they all
do (the 2026-09-29 week plan rented Joey Daccord for Igor Shesterkin). This projects the one
goalie quantity that separates them and that the removed goalie branch found predictable:
**his share of his team's remaining starts**. It does not project how well he plays -- per-start
points (R2 -0.8%), save% (r = +0.026) and the decision (AUC 0.559) were measured and dropped,
and the line per start stays the league average.

Step 1 of plans/goalie-workload-ros.md: measure the candidates before building anything.

    python goalie_workload.py --measure --season 2024-25          # fits k, compares candidates
    python goalie_workload.py --measure --season 2025-26 --k 15   # the holdout, once
    python goalie_workload.py --build --season 2024-25            # every game day's shares
    python goalie_workload.py --build --season 2026-27 --date 2026-10-07   # one day (live)

**Measured (2026-09-27), MAE of remaining starts / the team's starter found, mean of 7 checkpoints:**

                          2024-25 (k fitted)   2025-26 (holdout, k = 15)
    a  current            16.16  / 0.625       16.89 / 0.551
    b  consensus           6.59  / 0.817        6.31 / 0.722
    c  to-date            12.06  / 0.750       10.43 / 0.727
    d  blend, k = 15       5.78  / 0.862        5.66 / 0.740
    e  blend, team         5.74  / 0.862        5.74 / 0.740

The blend is built (e is within noise of it and adds a step): consensus carries the weight of
about 15 team games, so after a month his own starts lead. `--build` writes
reports/goalie_ros_{season}.parquet: each goalie's projected start share per day, dated like the
skater rest-of-season rows (built from games before the date, knowable at its lock). Readers
turn it into points per team game with their own league-average line (engine.goalie_line).

At checkpoints through the season (opening day, then weeks 2, 4, 8, 12, 16, 20), for every goalie
with a consensus projection or a start that season, each candidate projects his remaining starts
as a share of his current team's remaining games, as of the checkpoint:

    a  current     the latest fitted P(start) before the checkpoint, else the 50% prior
    b  consensus   the preseason sources' games started / 82 (GS missing: games x the sources'
                   GS/GP ratio), averaged over sources; 0 for a goalie none projects
    c  to-date     his starts / his team's games so far, else the 50% prior
    d  blend       (k x consensus + starts so far) / (k + team games so far): consensus early,
                   his own starts as they accumulate; k fitted on 2024-25
    e  blend, team the blend rescaled so his team's goalies' shares sum to one

Scored against what happened: MAE of remaining starts, and for each team whether the goalie a
candidate ranks first started the most of its remaining games ("starter found").
"""

import argparse
import logging
import sys

import numpy as np
import pandas as pd

import paths

log = logging.getLogger("goalie_workload")

CHECK_WEEKS = (0, 1, 3, 7, 11, 15, 19)     # opening day, then weeks 2, 4, 8, 12, 16, 20
PRIOR = 0.5                                 # the P(start) prior a goalie with no history gets
SEASON_GAMES = 82
K_GRID = (0, 2, 5, 10, 15, 20, 30, 40, 60, 82, 120, 200)
K = 15                                      # fitted on 2024-25, held out on 2025-26 (above)
CANDIDATES = ("a_current", "b_consensus", "c_to_date", "d_blend", "e_blend_team")


# ---------- inputs ----------

def consensus_share(season: str) -> pd.DataFrame:
    """Each goalie's preseason projected share of 82 starts, and his projected team."""
    x = pd.read_parquet(paths.FEATURES_DIR / f"external_projections_{season}.parquet")
    g = x[x["is_goalie"].astype(bool)].copy()
    both = g.dropna(subset=["games", "games_started"])
    ratio = both["games_started"].sum() / both["games"].sum()
    g["gs"] = g["games_started"].fillna(g["games"] * ratio)
    g = g.dropna(subset=["gs"])
    team = g.dropna(subset=["team_id"]).groupby("player_id")["team_id"].agg(lambda t: t.mode().iloc[0])
    out = g.groupby("player_id")["gs"].mean().clip(upper=SEASON_GAMES).to_frame("consensus_gs")
    out["consensus_share"] = out["consensus_gs"] / SEASON_GAMES
    out["consensus_team"] = team
    log.info("%s consensus: %d goalies, %d sources, GS/GP ratio %.3f", season, len(out),
             g["source"].nunique(), ratio)
    return out


def load(season: str):
    starts = pd.read_parquet(paths.FEATURES_DIR / f"goalie_starts_{season}.parquet")
    starts["game_date"] = pd.to_datetime(starts["game_date"])
    starts["is_starter"] = starts["is_starter"].astype(bool)
    pstart = pd.read_parquet(paths.REPORTS_DIR / f"goalie_pstart_{season}.parquet")
    pstart["game_date"] = pd.to_datetime(pstart["game_date"])
    return starts, pstart, consensus_share(season)


# ---------- the measurement frame ----------

def frame(season: str) -> pd.DataFrame:
    """One row per (checkpoint, goalie): the inputs each candidate needs, and the outcome."""
    starts, pstart, consensus = load(season)
    team_games = starts[["game_id", "game_date", "team_id"]].drop_duplicates()
    starters = starts[starts["is_starter"]]
    first = starts["game_date"].min()
    population = sorted(set(consensus.index.astype(int)) | set(starters["player_id"].astype(int)))
    rows = []
    for week in CHECK_WEEKS:
        t = first + pd.Timedelta(days=7 * week)
        before, after = starts[starts["game_date"] < t], starts[starts["game_date"] >= t]
        games_before = team_games[team_games["game_date"] < t].groupby("team_id").size()
        games_after = team_games[team_games["game_date"] >= t].groupby("team_id").size()
        # His team as of t: the last game he dressed for; else the consensus sources' team.
        dressed = before.sort_values("game_date").groupby("player_id")["team_id"].last()
        started_before = before[before["is_starter"]].groupby("player_id").size()
        started_after = after[after["is_starter"]].groupby("player_id").size()
        latest_p = (pstart[pstart["game_date"] < t].sort_values("game_date")
                    .groupby("player_id")["p_start"].last())
        for p in population:
            team = dressed.get(p)
            if team is None and p in consensus.index:
                team = consensus.at[p, "consensus_team"]
            team = None if team is None or pd.isna(team) else int(team)
            remaining = games_after.get(team, np.nan) if team is not None else np.nan
            rows.append({"week": week, "checkpoint": t, "player_id": p, "team_id": team,
                         "team_games_before": int(games_before.get(team, 0)) if team is not None else 0,
                         "team_games_after": remaining,
                         "starts_before": int(started_before.get(p, 0)),
                         "starts_after": int(started_after.get(p, 0)),
                         "latest_p_start": latest_p.get(p, np.nan),
                         "consensus_share": consensus["consensus_share"].get(p, 0.0)})
    out = pd.DataFrame(rows)
    # A goalie with no team yet (a call-up nobody projected) plays against the typical schedule.
    out["team_games_after"] = out.groupby("week")["team_games_after"].transform(
        lambda r: r.fillna(r.median()))
    return out


def predict(f: pd.DataFrame, k: float) -> pd.DataFrame:
    """Each candidate's projected share of his team's remaining games, and remaining starts."""
    f = f.copy()
    f["a_current"] = f["latest_p_start"].fillna(PRIOR)
    f["b_consensus"] = f["consensus_share"]
    known = f["team_games_before"] > 0
    f["c_to_date"] = np.where(known, f["starts_before"] / f["team_games_before"].clip(lower=1), PRIOR)
    f["d_blend"] = ((k * f["consensus_share"] + f["starts_before"])
                    / (k + f["team_games_before"]).replace(0, np.nan)).fillna(f["consensus_share"])
    team_sum = f.groupby(["week", "team_id"])["d_blend"].transform("sum")
    f["e_blend_team"] = np.where(f["team_id"].notna() & (team_sum > 0), f["d_blend"] / team_sum,
                                 f["d_blend"])
    for c in CANDIDATES:
        f[c] = f[c].clip(0.0, 1.0)
        f[c + "_starts"] = f[c] * f["team_games_after"]
    return f


def score(f: pd.DataFrame) -> pd.DataFrame:
    """Per checkpoint and candidate: MAE of remaining starts, and the share of teams whose
    first-ranked goalie started the most of their remaining games."""
    rows = []
    for week, g in f.groupby("week"):
        with_team = g[g["team_id"].notna()]
        top_actual = with_team.loc[with_team.groupby("team_id")["starts_after"].idxmax()]
        top_actual = top_actual[top_actual["starts_after"] > 0].set_index("team_id")["player_id"]
        for c in CANDIDATES:
            mae = float((g[c + "_starts"] - g["starts_after"]).abs().mean())
            ranked = with_team.sort_values([c, "starts_before"], ascending=False).groupby("team_id").head(1)
            ranked = ranked.set_index("team_id")["player_id"]
            found = float((ranked.reindex(top_actual.index) == top_actual).mean())
            rows.append({"week": week, "candidate": c, "mae": mae, "starter_found": found,
                         "goalies": len(g)})
    return pd.DataFrame(rows)


def fit_k(f: pd.DataFrame) -> tuple:
    """The k that minimizes the blend's MAE of remaining starts, over all checkpoints."""
    results = {k: float(score(predict(f, k)).query("candidate == 'd_blend'")["mae"].mean())
               for k in K_GRID}
    best = min(results, key=results.get)
    return best, results


def measure(season: str, k: float | None) -> None:
    f = frame(season)
    if k is None:
        k, curve = fit_k(f)
        log.info("k fitted on %s: %s (blend MAE by k: %s)", season, k,
                 {kk: round(v, 2) for kk, v in curve.items()})
    s = score(predict(f, k))
    print(f"\n{season}, k = {k}: MAE of remaining starts (lower is better)")
    print(s.pivot(index="week", columns="candidate", values="mae").round(2).to_string())
    print(f"\n{season}: starter found -- the goalie ranked first started the most of his team's rest")
    print(s.pivot(index="week", columns="candidate", values="starter_found").round(3).to_string())
    print("\nmean over checkpoints:")
    print(s.groupby("candidate")[["mae", "starter_found"]].mean().round(3).to_string())


def shares(season: str, days, k: float = K) -> pd.DataFrame:
    """The blend's projected start share for every goalie on each of `days`, as of that day's
    lock: the preseason consensus, and starts and team games before the day. A goalie is anyone
    the consensus projects or who has dressed before the day; a call-up who has not has no row,
    and readers fall back to his P(start) as before."""
    starts = pd.read_parquet(paths.FEATURES_DIR / f"goalie_starts_{season}.parquet")
    starts["game_date"] = pd.to_datetime(starts["game_date"])
    starts["is_starter"] = starts["is_starter"].astype(bool)
    consensus = consensus_share(season)
    team_games = starts[["game_id", "game_date", "team_id"]].drop_duplicates()
    rows = []
    for day in sorted(pd.to_datetime(pd.Series(list(days))).unique()):
        before = starts[starts["game_date"] < day]
        dressed = before.sort_values("game_date").groupby("player_id")["team_id"].last()
        started = before[before["is_starter"]].groupby("player_id").size()
        games = team_games[team_games["game_date"] < day].groupby("team_id").size()
        for p in sorted(set(consensus.index.astype(int)) | set(dressed.index.astype(int))):
            team = dressed.get(p)
            if team is None and p in consensus.index:
                team = consensus.at[p, "consensus_team"]
            team = None if team is None or pd.isna(team) else int(team)
            played = int(games.get(team, 0)) if team is not None else 0
            prior = float(consensus["consensus_share"].get(p, 0.0))
            n = int(started.get(p, 0))
            share = (k * prior + n) / (k + played) if k + played > 0 else prior
            rows.append({"game_date": day, "player_id": p, "team_id": team,
                         "start_share": min(max(share, 0.0), 1.0), "consensus_share": prior,
                         "starts_before": n, "team_games_before": played, "k": k})
    return pd.DataFrame(rows)


def build(season: str, date: str | None) -> None:
    """Write (or, with --date, update one day of) reports/goalie_ros_{season}.parquet."""
    out = paths.ensure(paths.REPORTS_DIR) / f"goalie_ros_{season}.parquet"
    if date is None:
        starts = pd.read_parquet(paths.FEATURES_DIR / f"goalie_starts_{season}.parquet")
        table = shares(season, pd.to_datetime(starts["game_date"]).unique())
    else:
        day = pd.Timestamp(date)
        table = shares(season, [day])
        if out.exists():
            kept = pd.read_parquet(out)
            table = pd.concat([kept[kept["game_date"] != day], table], ignore_index=True)
    table.sort_values(["game_date", "player_id"]).to_parquet(out, index=False)
    log.info("%s: %d rows over %d day(s), %d goalies -> %s", season, len(table),
             table["game_date"].nunique(), table["player_id"].nunique(), out.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--date", default=None, help="--build: one day only (the live run)")
    parser.add_argument("--season", default="2024-25")
    parser.add_argument("--k", type=float, default=None, help="blend weight; fitted if omitted")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    if args.measure:
        measure(args.season, args.k)
    elif args.build:
        build(args.season, args.date)
    else:
        parser.error("nothing to do: --measure or --build")


if __name__ == "__main__":
    main()

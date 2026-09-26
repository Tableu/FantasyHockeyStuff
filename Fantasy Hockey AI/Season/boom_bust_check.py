#!/usr/bin/env python
"""Step 4 of the boom/bust plan: do the odds come true? Graded against where players were
really taken (ADP) and what they really scored.

    python boom_bust_check.py --season 2025-26 --draft-date 2025-10-07
    python boom_bust_check.py --season 2024-25 --draft-date 2024-10-04

Reads `boom_bust.py`'s saved draws and par curve for the season (every fit behind them was
trained on earlier seasons only). **ADP enters here and only here**: the mean of the platforms'
ADP (Yahoo, Fantrax) ranks the players, and the first teams x rounds of them are "drafted", each
at his rank's pick. For each of them:

    predicted   boom / bust at his ADP pick, from the draws and the model's par curve
    happened    his actual VOR against the same thresholds: actual points minus the lowest actual
                replacement among his positions, a position's replacement being the mean actual
                points of the three best-projected players ADP left undrafted (the model's rule,
                on the real season)

Baselines. **Round average**: every player in a round gets the model's mean odds for that round
-- does the per-player part add anything? **Other season**: the observed rate per round in the
other checked season -- the historical base rate a manager could have used.

Prints the tables (Markdown) and saves each season's graded rows to
reports/boom_bust_check_<season>_<scoring>.parquet; docs/boom-bust-check.md holds the write-up.
"""

import argparse
import logging
from dataclasses import replace

import numpy as np
import pandas as pd

import boom_bust as bb
import league as league_module
import paths
import simlayer
from decisionlayer import draft as draft_module
from decisionlayer import load_strategy

import games_played
import season_spread

log = logging.getLogger("boom_bust_check")

ADP_PLATFORMS = ("yahoo", "fantrax")
DEPTH = bb.REPLACEMENT_DEPTH


def adp_picks(season: str, picks_total: int) -> pd.Series:
    """player_id -> pick (1..picks_total) by the mean ADP across the platforms that list him."""
    tables = []
    for platform in ADP_PLATFORMS:
        path = paths.fantasy_adp(platform, season)
        if path.exists():
            tables.append(pd.read_parquet(path).set_index("player_id")["adp"].rename(platform))
    if not tables:
        raise SystemExit(f"no ADP for {season}: run ModelFeatures/build_fantasy_positions.py "
                         f"--platform Yahoo --season {season} --adp-only (and Fantrax)")
    adp = pd.concat(tables, axis=1).mean(axis=1).sort_values()
    ranked = adp.head(picks_total)
    return pd.Series(np.arange(1, len(ranked) + 1), index=ranked.index.astype(int))


def actual_points(season: str, weights: dict) -> pd.Series:
    totals, *_ = games_played.load_inputs(paths.FEATURES_DIR)
    now = totals[totals["season"] == season]
    sk = now[~now["is_goalie"].astype(bool)].set_index("player_id")
    pts_sk = sum(weights["skaters"].get(s, 0.0) * sk[s].fillna(0) for s in weights["skaters"])
    g = season_spread.goalie_fps(now, weights["goalies"]).set_index("player_id")
    pts_g = g["fps"] * g["starts"]
    return pd.concat([pts_sk, pts_g]).groupby(level=0).sum()


def actual_vor(points: pd.Series, values: pd.Series, eligibility: dict, drafted) -> pd.Series:
    drafted = set(drafted)
    by_value = [p for p in values.sort_values(ascending=False).index]
    rep = {}
    for pos in draft_module.POSITIONS:
        left = [p for p in by_value if p not in drafted and pos in eligibility.get(p, ())]
        rep[pos] = float(points.reindex(left[:DEPTH]).fillna(0).mean()) if left else 0.0
    out = {}
    for p in drafted:
        slots = [s for s in eligibility.get(p, ()) if s in rep]
        floor = min(rep[s] for s in slots) if slots else 0.0
        out[p] = float(points.get(p, 0.0)) - floor
    return pd.Series(out), rep


def auc(score: np.ndarray, label: np.ndarray) -> float:
    """P(a random positive scores above a random negative), ties half."""
    pos, neg = score[label == 1], score[label == 0]
    if not len(pos) or not len(neg):
        return float("nan")
    ranks = pd.Series(np.r_[pos, neg]).rank().to_numpy()
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def grade(season, draft_date, scoring="points-league", rules="league", eligibility_platform="fleaflicker",
          margin=None):
    config = replace(league_module.load(rules), eligibility_platform=eligibility_platform)
    scoreset = simlayer.load_scoreset(scoring)
    weights = __import__("json").loads(paths.scoreset(scoring).read_text())
    strategy = load_strategy(None)
    margin = margin or config.teams
    _, _, values, eligibility = bb.board_inputs(season, bb.previous(season), config, scoreset,
                                                strategy, pd.Timestamp(draft_date))
    stem = paths.REPORTS_DIR / f"boom_bust_{season}_{scoring}"
    vor = np.load(f"{stem}_vor_draws.npy")
    index = pd.read_csv(f"{stem}_players.csv")["player_id"].tolist()
    par = pd.read_csv(f"{stem}_par.csv")["par"].to_numpy()
    sim = pd.read_parquet(f"{stem}.parquet")
    col = {p: j for j, p in enumerate(index)}

    picks = adp_picks(season, config.teams * config.roster_size)
    points = actual_points(season, weights)
    act_vor, rep = actual_vor(points, values, eligibility, picks.index)

    rows = []
    for p, pick in picks.items():
        if p not in col:
            continue
        boom, bust = bb.odds_at(vor[:, col[p]], pick, par, margin)
        hi = par[min(pick + margin, len(par)) - 1]
        lo = par[max(pick - margin, 1) - 1]
        rows.append({"player_id": p, "pick": pick, "round": (pick - 1) // config.teams + 1,
                     "p_boom": boom, "p_bust": bust,
                     "boom": float(act_vor[p] >= lo), "bust": float(act_vor[p] < hi),
                     "actual_vor": act_vor[p], "actual_points": float(points.get(p, 0.0)),
                     "p10": sim.loc[p, "p10"], "p90": sim.loc[p, "p90"]})
    d = pd.DataFrame(rows).set_index("player_id")
    for k in ("boom", "bust"):
        d[f"p_{k}_round"] = d.groupby("round")[f"p_{k}"].transform("mean")
    log.info("%s: %d ADP picks, %d with draws; actual replacement %s", season, len(picks), len(d),
             {k: round(v, 1) for k, v in rep.items()})

    # The realized par: actual VOR by ADP pick, smoothed the same way.
    realized = pd.Series(act_vor.reindex(picks.index).to_numpy(), index=picks.to_numpy()).sort_index()
    real_par = bb._non_increasing(realized.rolling(7, center=True, min_periods=1).mean().to_numpy())
    return d, par, real_par


def reliability(d: pd.DataFrame, kind: str, bins: int = 5) -> pd.DataFrame:
    cut = pd.qcut(d[f"p_{kind}"], bins, duplicates="drop")
    return d.groupby(cut, observed=True).agg(players=(kind, "size"),
                                             predicted=(f"p_{kind}", "mean"),
                                             observed=(kind, "mean"))


def report(season, d, par, real_par, other=None) -> str:
    lines = [f"## {season}", ""]
    lines.append(f"{len(d)} players ADP drafts (top {len(par)} by mean Yahoo/Fantrax ADP) with draws.")
    lines.append("")
    lines.append("| | mean predicted | observed | Brier model | Brier round avg | Brier other season | AUC model | AUC round avg |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for k in ("boom", "bust"):
        y = d[k].to_numpy()
        brier = lambda p: float(np.mean((p - y) ** 2))
        other_b = "-"
        if other is not None:
            rate = other.groupby("round")[k].mean()
            base = d["round"].map(rate).fillna(other[k].mean()).to_numpy()
            other_b = f"{brier(base):.4f}"
        lines.append(f"| {k} | {d[f'p_{k}'].mean():.3f} | {y.mean():.3f} | {brier(d[f'p_{k}'].to_numpy()):.4f} | "
                     f"{brier(d[f'p_{k}_round'].to_numpy()):.4f} | {other_b} | "
                     f"{auc(d[f'p_{k}'].to_numpy(), y):.3f} | {auc(d[f'p_{k}_round'].to_numpy(), y):.3f} |")
    lines.append("")
    for k in ("boom", "bust"):
        lines.append(f"Reliability, {k} (quintiles of the predicted odds):")
        lines.append("")
        lines.append("| predicted range | players | predicted | observed |")
        lines.append("|---|---|---|---|")
        for idx, r in reliability(d, k).iterrows():
            lines.append(f"| {idx} | {int(r.players)} | {r.predicted:.3f} | {r.observed:.3f} |")
        lines.append("")
    inside = ((d["actual_points"] >= d["p10"]) & (d["actual_points"] <= d["p90"])).mean()
    below = (d["actual_points"] < d["p10"]).mean()
    lines.append(f"Season points inside the 10-90 range: {inside:.3f} (below {below:.3f}, above {1 - inside - below:.3f}).")
    lines.append("")
    by_round = [(r, par[(r - 1) * 14:r * 14].mean(), real_par[(r - 1) * 14:r * 14].mean())
                for r in range(1, len(par) // 14 + 1)]
    lines.append("Par by round, simulated vs realized (actual VOR by ADP pick, smoothed):")
    lines.append("")
    lines.append("| round | " + " | ".join(str(r) for r, _, _ in by_round) + " |")
    lines.append("|---|" + "---|" * len(by_round))
    lines.append("| simulated | " + " | ".join(f"{a:.0f}" for _, a, _ in by_round) + " |")
    lines.append("| realized | " + " | ".join(f"{b:.0f}" for _, _, b in by_round) + " |")
    lines.append("")
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--season", action="append", required=True)
    parser.add_argument("--draft-date", action="append", required=True)
    parser.add_argument("--scoring", default="points-league")
    args = parser.parse_args()
    graded = {s: grade(s, dd, args.scoring) for s, dd in zip(args.season, args.draft_date)}
    parts = []
    for s, (d, par, real) in graded.items():
        others = [g[0] for t, g in graded.items() if t != s]
        parts.append(report(s, d, par, real, others[0] if others else None))
        d.to_parquet(paths.REPORTS_DIR / f"boom_bust_check_{s}_{args.scoring}.parquet")
    text = "\n".join(parts)
    print(text)


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Compare two league formats on the same season -- what a format change actually does.

    python compare.py --a league-12team-simple --b league --weights points-league

Format is data in this folder precisely so that it can be varied, and a format change moves more
than one thing at once. The 14-team composite build changes three:

    team count      12 -> 14     a deeper league, so a shallower free-agent pool
    active slots    12 -> 14     more slots to fill against the same ~7-8 startable players a night
    composite slots  0 -> 2      F and F/D accept several positions, so a spare skater is not
                                 stranded behind a full C slot

Those push in opposite directions -- a shallower pool makes acquisitions worse, more empty slots
makes any extra body more valuable, and composite slots reduce the emptiness that made bodies
valuable in the first place. Which dominates is not predictable from the rules, which is the whole
reason to run it.

**Points per week are NOT comparable across formats** and the script refuses to subtract them: 14
active slots score more than 12 by construction, so the difference is mostly the slot count. What
transfers is the *gap between rungs* within a format, and the per-slot-night rates.
"""

import argparse
import json

import numpy as np
import pandas as pd

import paths

SEASON = "2025-26"


def parse_args():
    parser = argparse.ArgumentParser(description="Compare two league formats")
    parser.add_argument("--a", default="league-12team-simple", help="Baseline config stem")
    parser.add_argument("--b", default="league", help="Comparison config stem")
    parser.add_argument("--season", default=SEASON)
    parser.add_argument("--weights", default="points-league")
    return parser.parse_args()


def load(stem, season, scoreset):
    path = paths.ladder_report(season, stem)
    if not path.exists():
        raise SystemExit(f"{path} not found -- run `python ladder.py --league {stem}`")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if scoreset not in payload["results"]:
        raise SystemExit(f"{path.name} has no {scoreset} results (has "
                         f"{sorted(payload['results'])})")
    seats = pd.DataFrame(payload["results"][scoreset]["by_seat"])
    seats["pts_wk"] = seats["points"] / seats["weeks"]
    seats["win_rate"] = seats["matchup_wins"] / seats["weeks"]
    return payload, seats


def gaps(seats):
    """{(hi, lo): (mean, se)} for each rung pair, paired inside each league instance."""
    pivot = seats.pivot_table(index="replication", columns="rung", values="pts_wk")
    out = {}
    for hi, lo in ((2, 1), (3, 2), (4, 2), (4, 3)):
        if hi in pivot and lo in pivot:
            delta = (pivot[hi] - pivot[lo]).dropna()
            se = delta.std() / np.sqrt(len(delta)) if len(delta) > 1 else float("nan")
            out[(hi, lo)] = (float(delta.mean()), float(se))
    return out


def per_rung(seats):
    g = seats.groupby("rung").agg(
        n=("pts_wk", "size"), pts=("pts_wk", "mean"), sd=("pts_wk", "std"),
        win=("win_rate", "mean"), started=("slot_nights_productive", "mean"),
        offered=("slot_nights_offered", "mean"),
        # NOT `empty`: that is a pandas attribute, so `row.empty` returns False rather than the
        # column and prints as 0.0. Named `empty_nights` so attribute access cannot shadow it.
        empty_nights=("empty_slot_nights", "mean"),
        wasted=("wasted_slot_nights", "mean"), eff=("decision_efficiency", "mean"),
        moves=("moves_spent", "mean"))
    g["se"] = g["sd"] / np.sqrt(g["n"])
    # The rate that IS comparable across formats: points earned per slot-night actually started.
    g["pts_per_start"] = seats.groupby("rung").apply(
        lambda d: (d.points / d.slot_nights_productive).mean(), include_groups=False)
    g["fill"] = g["started"] / g["offered"]
    return g


def main():
    args = parse_args()
    payload_a, a = load(args.a, args.season, args.weights)
    payload_b, b = load(args.b, args.season, args.weights)

    print(f"scoring: {args.weights}   season: {args.season}\n")
    for label, stem, payload, seats in (("A", args.a, payload_a, a), ("B", args.b, payload_b, b)):
        print(f"{label}: {stem}   ({payload['replications']} rotations, "
              f"{seats.groupby('rung').size().to_dict()} seats per rung)")
    print()

    ga, gb = per_rung(a), per_rung(b)
    print("per rung, within each format")
    header = (f"{'rung':>4} | {'A pts/wk':>16} {'A win':>6} {'A fill':>7} {'A pts/start':>11}"
              f" | {'B pts/wk':>16} {'B win':>6} {'B fill':>7} {'B pts/start':>11}")
    print(header)
    print("-" * len(header))
    for rung in sorted(set(ga.index) & set(gb.index)):
        ra, rb = ga.loc[rung], gb.loc[rung]
        print(f"{rung:>4} | {ra.pts:>9.1f} +/-{ra.se:>4.1f} {ra.win:>6.3f} {ra.fill:>7.3f}"
              f" {ra.pts_per_start:>11.3f} | {rb.pts:>9.1f} +/-{rb.se:>4.1f} {rb.win:>6.3f}"
              f" {rb.fill:>7.3f} {rb.pts_per_start:>11.3f}")

    print("\nrung gaps, paired inside each league instance (the comparable quantity)")
    names = {(2, 1): "attention (2-1)", (3, 2): "naive streaming (3-2)",
             (4, 2): "full system vs no moves (4-2)", (4, 3): "MODELLING (4-3)"}
    da, db = gaps(a), gaps(b)
    print(f"{'':>32} {'A':>18} {'B':>18}   change")
    for key, name in names.items():
        if key in da and key in db:
            ma, sa = da[key]
            mb, sb = db[key]
            sig = lambda m, s: "clear" if abs(m) > 2 * s else "noise"
            print(f"{name:>32} {ma:>+8.2f} +/-{sa:>4.2f} {sig(ma,sa):>5}"
                  f" {mb:>+8.2f} +/-{sb:>4.2f} {sig(mb,sb):>5}   {mb - ma:>+7.2f}")

    print("\nslot occupancy (per team-season)")
    print(f"{'rung':>4} | {'A offered':>9} {'A empty':>8} {'A wasted':>9}"
          f" | {'B offered':>9} {'B empty':>8} {'B wasted':>9}")
    for rung in sorted(set(ga.index) & set(gb.index)):
        ra, rb = ga.loc[rung], gb.loc[rung]
        print(f"{rung:>4} | {ra.offered:>9.0f} {ra.empty_nights:>8.1f} {ra.wasted:>9.1f}"
              f" | {rb.offered:>9.0f} {rb.empty_nights:>8.1f} {rb.wasted:>9.1f}")


if __name__ == "__main__":
    main()

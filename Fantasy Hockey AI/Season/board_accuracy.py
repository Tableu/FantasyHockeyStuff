#!/usr/bin/env python
"""How well each draft-day board ranked the season that followed -- no ladder in the way.

    python board_accuracy.py --season 2025-26 --prior-season 2024-25

**The headline is VOR-rank accuracy**: a draft sorts on value over replacement, which compares
positions on one scale, so a board can rank every position well and still take goalies too early.
Each board's VOR (its own replacement levels, from a league drafted by that board) is ranked
against *actual* VOR (actual points less the replacement levels of a league drafted by actual
points), over the players drafted by either. Raw-value rank by position, MAE and bias follow.

Candidates, all knowable before the opener unless marked:

    consensus         the external sources alone: 3+ sources, else last season's total, else the
                      thin consensus (`draft.consensus_board`) -- what a real draft can use
    consensus_min1    the same with no minimum: every covered player on the consensus
    consensus_fitted  consensus x a per-position factor fitted on THIS season's actuals --
                      in-sample here, an upper bound on what any cross-position scale can buy
    last_season       last season's fantasy points (the board every non-VOR seat drafts from)
    own_model         our model's opening-week rows -- a reference only; no real draft has them

Writes docs/board-accuracy-<season>.md.
"""

import argparse
import logging
import sys

import numpy as np
import pandas as pd

import inputs
import ladder
import league as league_module
import paths
import simlayer
from decisionlayer import draft as draft_module
from decisionlayer import load_strategy

log = logging.getLogger("board-accuracy")
BOARDS = ("consensus", "consensus_min1", "consensus_fitted", "last_season", "own_model")
NOTES = {"consensus_fitted": "in-sample", "own_model": "not at a real draft"}


def spearman(a: pd.Series, b: pd.Series) -> float:
    if len(a) < 10:
        return float("nan")
    return float(np.corrcoef(a.rank(), b.rank())[0, 1])


def sides(season) -> pd.Series:
    skaters = inputs.load_actuals(season)[["player_id", "position"]]
    goalies = inputs.load_goalie_starts(season)[["player_id"]].assign(position="G")
    table = pd.concat([skaters, goalies]).drop_duplicates("player_id")
    side = table["position"].map(lambda p: p if p in ("D", "G") else "F")
    return pd.Series(side.to_numpy(), index=table["player_id"].astype(int).to_numpy())


def load(season, prior_season, strategy):
    data = inputs.load_season(season)
    external = inputs.load_external_projections(season, data["projections"]["game_date"].min(),
                                                strategy.undated_sources)
    universe = pd.concat([data["projections"][["player_id", "position"]],
                          data["goalie_candidates"][["player_id", "position"]]]
                         ).drop_duplicates("player_id")
    return data, external, universe


def boards(data, external, prior_season, scoreset, strategy, config, eligibility, side):
    """{board: season values}, and the actual season's points."""
    last = draft_module.prior_season_board(inputs.load_actuals(prior_season),
                                           inputs.load_goalie_starts(prior_season), scoreset)
    last.index = last.index.astype(int)
    actual = draft_module.prior_season_board(data["actuals"], data["goalie_starts"], scoreset)
    actual.index = actual.index.astype(int)
    k = strategy.vor_min_sources
    out = {"last_season": last}
    out["consensus"] = draft_module.values_for("consensus", scoreset, last, external=external,
                                               min_sources=k)
    out["consensus_min1"] = draft_module.values_for("consensus", scoreset, last,
                                                    external=external, min_sources=1)
    # The factor per position that makes the consensus's drafted players at that position total
    # what they actually scored -- fitted on the season being graded.
    drafted = draft_module.simulate_draft(out["consensus"][[p in eligibility
                                                            for p in out["consensus"].index]],
                                          config, eligibility)
    pool = side.reindex(drafted).fillna("F").value_counts().to_dict()
    scale = draft_module.fit_position_scale(out["consensus"], actual, side, pool)
    out["consensus_fitted"] = draft_module.values_for("consensus", scoreset, last,
                                                      external=external, min_sources=k,
                                                      scale=scale, sides=side)
    out["own_model"] = draft_module.values_for(
        "own_model", scoreset, last, ros=data["ros"],
        prior_goalie_lines=inputs.load_goalie_starts(prior_season),
        opening_days=strategy.opening_days, team_openers=ladder.team_openers(data))
    return out, actual, scale


def vor_of(values, config, eligibility):
    """VOR for every eligible player: a player the board does not list is valued at zero."""
    full = values.reindex(list(eligibility)).fillna(0.0).sort_values(ascending=False)
    return draft_module.vor_board(full, config, eligibility)


def evaluate(season, prior_season, scoreset, strategy, league_name, loaded):
    data, external, universe = loaded
    config = league_module.load(league_name)
    eligibility = inputs.load_eligibility(config, universe)
    side = sides(season)
    candidates, actual, scale = boards(data, external, prior_season, scoreset, strategy, config,
                                       eligibility, side)
    drafted_n = config.teams * config.roster_size

    true_vor = vor_of(actual, config, eligibility)
    vors = {b: vor_of(candidates[b], config, eligibility) for b in BOARDS}
    pool = set(true_vor.head(drafted_n).index)
    for b in BOARDS:
        pool |= set(vors[b].head(drafted_n).index)
    pool = sorted(pool)
    truth_vor = true_vor.reindex(pool).fillna(true_vor.min())
    truth = actual.reindex(pool).fillna(0.0)
    s = side.reindex(pool).fillna("F")

    rows = []
    for b in BOARDS:
        value = candidates[b].reindex(pool).fillna(0.0)
        err = value - truth
        row = {"board": b + (f" ({NOTES[b]})" if b in NOTES else ""),
               "VOR rank": spearman(vors[b].reindex(pool).fillna(vors[b].min()), truth_vor),
               "value rank": spearman(value, truth)}
        for g in ("F", "D", "G"):
            mask = (s == g).to_numpy()
            row[f"{g} rank"] = spearman(value[mask], truth[mask])
        for g in ("F", "D", "G"):
            mask = (s == g).to_numpy()
            row[f"{g} bias"] = float(err[mask].mean())
        row["MAE"] = float(err.abs().mean())
        rows.append(row)
    return pd.DataFrame(rows), len(pool), scale


def to_markdown(frame: pd.DataFrame) -> str:
    cols = list(frame.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in frame.iterrows():
        cells = []
        for c, v in zip(cols, r):
            if isinstance(v, float):
                cells.append(f"{v:+.1f}" if "bias" in c else f"{v:.1f}" if c == "MAE"
                             else f"{v:.3f}")
            else:
                cells.append(str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--prior-season", default="2024-25")
    parser.add_argument("--weights", action="append", default=None)
    parser.add_argument("--league", action="append", default=None,
                        help="League config(s) (default: league and league-12team-simple)")
    parser.add_argument("--strategy", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    strategy = load_strategy(args.strategy)
    loaded = load(args.season, args.prior_season, strategy)

    sections = [f"# Draft-day boards against the season: {args.season}\n",
                "Generated by `board_accuracy.py`; see its docstring for the boards. **VOR rank** "
                "is the headline: Spearman of each board's value over replacement against actual "
                "VOR, over every player either drafts. The rank columns are Spearman of raw values "
                "with actual season points by position; bias and MAE are season points (bias > 0: "
                f"the board expected more). Minimum sources {strategy.vor_min_sources}; undated "
                f"sources {strategy.undated_sources}.\n"]
    for league_name in args.league or ["league", "league-12team-simple"]:
        for name in args.weights or ["points-league", "banger-league"]:
            scoreset = simlayer.load_scoreset(name)
            table, n, scale = evaluate(args.season, args.prior_season, scoreset, strategy,
                                       league_name, loaded)
            fitted = ", ".join(f"{k} x{v:.3f}" for k, v in sorted(scale.items()))
            sections.append(f"## {league_name}, {scoreset.name} ({n} players)\n\n"
                            f"Fitted scale (in-sample): {fitted}\n\n{to_markdown(table)}\n")
            print(f"\n== {league_name} {scoreset.name} ({n}) fitted {fitted}\n"
                  f"{table.round(3).to_string(index=False)}")

    out = paths.ensure(paths.DOCS_DIR) / f"board-accuracy-{args.season}.md"
    out.write_text("\n".join(sections), encoding="utf-8")
    print(f"-> {out}")


if __name__ == "__main__":
    main()

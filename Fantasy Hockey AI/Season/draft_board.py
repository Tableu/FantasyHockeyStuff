#!/usr/bin/env python
"""The live draft board: value over replacement from the external sources' consensus alone.

    python draft_board.py --season 2026-27 --league league --weights points-league

What the ladder's VOR seats draft from (`vor_values: consensus`), built for a season that has not
started: the consensus line where three or more sources cover a player, last season's total where
fewer do (a rookie with one or two sources keeps their thin consensus), scored with the league's
file; replacement levels from a simulated league draft on the same board, with the real pick
rule; each player measured against the lowest replacement level among his positions.

No model of ours is in it -- our rest-of-season rows need the season's own games -- and no ADP
either: ADP is printed beside the board only to answer "will he last to my next pick", which is
what it forecasts. Every row says what its value rests on, so a fallback is never mistaken for a
projection.

Sources must be published before `--draft-date` (default today). Writes
reports/draft_board_<season>_<league>_<scoring>.csv, and .md with the top of the board.
"""

import argparse
import datetime as dt
import logging
import sys

import pandas as pd

import inputs
import league as league_module
import paths
import simlayer
from decisionlayer import draft as draft_module
from decisionlayer import load_strategy

log = logging.getLogger("draft-board")
POSITION_CODES = {"LW": "L", "RW": "R"}      # Reference.Players carries a few platform-style codes


def previous(season: str) -> str:
    first = int(season[:4]) - 1
    return f"{first}-{str(first + 1)[2:]}"


def build(season, prior_season, league_name, scoring, draft_date, strategy):
    config = league_module.load(league_name)
    scoreset = simlayer.load_scoreset(scoring)
    external = inputs.load_external_projections(season, draft_date, strategy.undated_sources)
    last = draft_module.prior_season_board(inputs.load_actuals(prior_season),
                                           inputs.load_goalie_starts(prior_season), scoreset)
    last.index = last.index.astype(int)
    k = strategy.vor_min_sources
    values = draft_module.values_for("consensus", scoreset, last, external=external,
                                     min_sources=k)

    players = pd.read_parquet(paths.players()).set_index("player_id")
    teams = pd.read_parquet(paths.teams()).set_index("team_id")["team"]
    universe = (players.loc[players.index.intersection(values.index), ["position"]]
                .rename_axis("player_id").reset_index())
    universe["position"] = universe["position"].replace(POSITION_CODES)
    eligibility = inputs.load_eligibility(config, universe)
    eligible = values[[p in eligibility for p in values.index]]
    levels = draft_module.replacement_levels(eligible, config, eligibility)
    vor = draft_module.vor_board(eligible, config, eligibility)

    consensus = draft_module.consensus_values(external, scoreset).set_index("player_id")
    sources = consensus["sources"].reindex(vor.index).fillna(0).astype(int)
    basis = pd.Series("consensus", index=vor.index)
    thin = sources < k
    basis[thin & vor.index.isin(last.index)] = "last season (thin)"
    basis[thin & ~vor.index.isin(last.index)] = "thin consensus"
    basis[sources == 0] = "last season (no source)"
    team_id = (external.groupby("player_id")["team_id"]
               .agg(lambda s: s.mode().iloc[0] if s.notna().any() else pd.NA))

    def against(p):
        slots = [s for s in eligibility[p] if s in levels]
        return min(slots, key=lambda s: levels[s])

    board = pd.DataFrame({
        "rank": range(1, len(vor) + 1),
        "player": players["name"].reindex(vor.index).to_numpy(),
        "team": team_id.reindex(vor.index).map(teams).to_numpy(),
        "positions": ["/".join(sorted(eligibility[p], key="C LW RW D G".split().index))
                      for p in vor.index],
        "value": eligible.reindex(vor.index).round(1).to_numpy(),
        "vor": vor.round(1).to_numpy(),
        "vs": [against(p) for p in vor.index],
        "sources": sources.to_numpy(),
        "basis": basis.to_numpy(),
    }, index=vor.index)
    board.index.name = "player_id"
    for platform in ("yahoo", "espn"):
        path = paths.fantasy_adp(platform, season)
        if path.exists():
            adp = pd.read_parquet(path).set_index("player_id")["adp"]
            board[f"adp_{platform}"] = adp.reindex(board.index).to_numpy()
    log.info("%s %s %s: %d players, replacement %s; %s", season, league_name, scoring, len(board),
             {s: round(v, 1) for s, v in levels.items()}, board["basis"].value_counts().to_dict())
    return board, levels, config


def to_markdown(board: pd.DataFrame, top: int) -> str:
    cols = [c for c in board.columns]
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in board.head(top).iterrows():
        out.append("| " + " | ".join("" if pd.isna(v) else f"{v:g}" if isinstance(v, float)
                                     else str(v) for v in r) + " |")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--season", default="2026-27")
    parser.add_argument("--prior-season", default=None, help="Default: the season before")
    parser.add_argument("--league", default="league")
    parser.add_argument("--weights", action="append", default=None)
    parser.add_argument("--draft-date", default=dt.date.today().isoformat(),
                        help="Every source must be published before this (default today)")
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--top", type=int, default=300, help="Rows in the Markdown board")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s",
                        stream=sys.stderr)
    strategy = load_strategy(args.strategy)
    prior = args.prior_season or previous(args.season)
    for scoring in args.weights or ["points-league"]:
        board, levels, config = build(args.season, prior, args.league, scoring,
                                      pd.Timestamp(args.draft_date), strategy)
        paths.ensure(paths.REPORTS_DIR)
        csv = paths.draft_board(args.season, args.league, scoring, "csv")
        board.to_csv(csv)
        md = paths.draft_board(args.season, args.league, scoring, "md")
        md.write_text(
            f"# Draft board: {args.season}, {args.league}, {scoring}\n\n"
            f"Generated by `draft_board.py` on {args.draft_date}. Value over replacement from the "
            f"external sources' consensus ({strategy.vor_min_sources}+ sources; `basis` says when "
            f"a row falls back). Replacement levels: "
            f"{', '.join(f'{s} {v:.0f}' for s, v in levels.items())}. {config.teams} teams, "
            f"{config.roster_size} rounds. ADP is beside the board, not in it.\n\n"
            + to_markdown(board, args.top) + "\n", encoding="utf-8")
        print(f"-> {csv}\n-> {md}")
        print(board.head(25).to_string())


if __name__ == "__main__":
    main()

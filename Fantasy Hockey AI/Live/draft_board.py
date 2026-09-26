#!/usr/bin/env python
"""The live draft board: value over replacement from the external sources' consensus alone.

    python draft_board.py --season 2026-27 --league league --weights points-league

What the ladder's VOR seats draft from (`vor_values: consensus`), built for a season that has not
started: the consensus line where `draft.min_sources` or more sources cover a player, last
season's total where fewer do (a rookie too thinly covered keeps his thin consensus), scored with the league's
file; replacement levels from a simulated league draft on the same board, with the real pick
rule; each player measured against the lowest replacement level among his positions.

No model of ours is in it -- our rest-of-season rows need the season's own games -- and no ADP
either: ADP is printed beside the board only to answer "will he last to my next pick", which is
what it forecasts. Every row says what its value rests on, so a fallback is never mistaken for a
projection.

`team` is the team a player is signed with, from his NHL page (pipeline/import_player_teams.py),
where the database has it; FA when his page has no team and marks him inactive (unsigned); else
the projections' most common team. `team_source` says which (NHL, FA, projections).

With `--playoffs START END` (the fantasy playoff weeks) it also counts each player's team's
off-night games and playoff games, as the aggregate workbook's Schedule Info sheet does
(`schedule_counts`).

Sources must be published before `--draft-date` (default today). Writes
reports/<league>/draft_board_<season>_<scoring>.csv, and .md with the top of the board.
"""

import argparse
import datetime as dt
import logging
import sys

import pandas as pd

import seasonlayer  # noqa: F401 -- puts Season/ on sys.path; see seasonlayer.py
import leagues
import livepaths
import inputs
import league as league_module
import paths
import simlayer
from decisionlayer import draft as draft_module
from decisionlayer import load_strategy

log = logging.getLogger("draft-board")
POSITION_CODES = {"LW": "L", "RW": "R"}      # Reference.Players carries a few platform-style codes
# A skater's peripheral categories: the scored stats that are not scoring (goals, assists and the
# power-play and short-handed points built from them). `periph_pct` is their share of his points.
PERIPHERALS = ("hits", "blocks", "shots", "pim")
OFF_NIGHT_MAX_GAMES = 8     # the aggregate workbook's off night: a date with 8 or fewer NHL games
# The aggregate workbook's TIER groups (its VorpAll sheets), as its letters, in the order it lists
# a multi-position player's tiers, and its default Tier Gap Z-Score (Settings!C24).
TIER_GROUPS = (("C", "C"), ("LW", "L"), ("RW", "R"), ("D", "D"), ("G", "G"))
TIER_GAP_Z = 1.0


def previous(season: str) -> str:
    first = int(season[:4]) - 1
    return f"{first}-{str(first + 1)[2:]}"


def schedule_counts(season, playoff_start, playoff_end) -> pd.DataFrame:
    """Per team, the aggregate workbook's two schedule columns (its Schedule Info sheet):

    - `off`: games on an off night -- a date with at most OFF_NIGHT_MAX_GAMES NHL games -- from the
      start of the season through the last day of the fantasy playoffs (its OffNights, "<= 8" and
      "<= playoff end");
    - `pog`: games from the first through the last day of the fantasy playoffs, inclusive (its
      PlayoffGames "All" column, which the Player Values sheets read as POG).

    Indexed by team abbreviation, the way the board names teams."""
    games = pd.read_parquet(paths.schedule(season))
    games["game_date"] = pd.to_datetime(games["game_date"]).dt.normalize()
    per_day = games.groupby("game_date").size()
    games["off_night"] = games["game_date"].map(per_day) <= OFF_NIGHT_MAX_GAMES
    start, end = pd.Timestamp(playoff_start), pd.Timestamp(playoff_end)
    rows = pd.concat([games.rename(columns={"home_team_id": "team_id"}),
                      games.rename(columns={"away_team_id": "team_id"})])[
        ["team_id", "game_date", "off_night"]]
    through = rows[rows["game_date"] <= end]
    counts = pd.DataFrame({
        "off": through[through["off_night"]].groupby("team_id").size(),
        "pog": through[through["game_date"] >= start].groupby("team_id").size(),
    }).reindex(rows["team_id"].unique()).fillna(0).astype(int)
    teams = pd.read_parquet(paths.teams()).set_index("team_id")["team"]
    counts.index = counts.index.map(teams)
    return counts


def status_labels(status: pd.DataFrame) -> pd.Series:
    """Per player, his current injury report as the draft window shows it: IR (out and on the
    league's IR), OUT, SUSP, DTD, GTD (active but a game-time decision); none when active."""
    status = status.set_index("player_id")
    label = status["status"].where(status["status"] != "ACTIVE")
    label = label.mask(label.isna() & status["gtd"], "GTD")
    return label.mask(status["status"].eq("OUT") & status["ir_eligible"], "IR").dropna()


def peripheral_share(board, scoreset) -> pd.Series:
    """Per skater, the percentage of his points (from his stat line, under this league's scoring)
    that come from PERIPHERALS; blank for goalies and for a skater with no line. The line is the
    one his value came from, so the points match the value to rounding."""
    weights = scoreset.skaters
    points = sum(board[s].fillna(0) * w for s, w in weights.items() if s in board)
    periph = sum(board[s].fillna(0) * weights[s] for s in PERIPHERALS if s in weights and s in board)
    share = (100 * periph / points).where(points > 0)
    return share.where(board["positions"].ne("G") & board["goals"].notna()).round(0)


def tiers(board, z=TIER_GAP_Z) -> pd.Series:
    """The aggregate workbook's TIER column: per position group, the group's values best first;
    a gap between neighbours bigger than the group's mean gap + `z` standard deviations starts a
    new tier. The mean is over the n - 1 gaps, the deviation divides by n - 1 too, as the
    workbook's formulas do. A multi-position player gets each group's tier, "C2 L1"."""
    positions = board["positions"].str.split("/")
    labels = pd.Series([[] for _ in board.index], index=board.index)
    for slot, letter in TIER_GROUPS:
        values = board.loc[positions.map(lambda p: slot in p), "value"].dropna()
        values = values.sort_values(ascending=False, kind="stable")
        gaps = -values.diff().iloc[1:].to_numpy()
        if len(gaps):
            mean = gaps.mean()
            threshold = mean + z * ((((gaps - mean) ** 2).sum() / len(gaps)) ** 0.5)
            breaks = (gaps > threshold).cumsum()
        tier = [1] + [1 + int(b) for b in breaks] if len(gaps) else [1] * len(values)
        for pid, t in zip(values.index, tier):
            labels[pid].append(f"{letter}{t}")
    return labels.map(" ".join)


def stat_lines(board, external, prior_season, scoreset) -> pd.DataFrame:
    """Every stat the league scores, as a season line per board player, plus games played: the
    consensus line for a player valued on it, last season's totals for one valued on last season
    (the line his value came from). Skater stats are blank for goalies and goalie stats for
    skaters."""
    skater_stats, goalie_stats = list(scoreset.skaters), list(scoreset.goalies)
    consensus = draft_module.consensus_lines(external)
    consensus["player_id"] = consensus["player_id"].astype(int)
    consensus = consensus.set_index("player_id")

    actuals = inputs.load_actuals(prior_season)
    played = actuals[actuals["target_played"].astype(bool)]
    last_skaters = played.groupby("player_id")[[f"target_{c}" for c in skater_stats]].sum()
    last_skaters.columns = skater_stats
    last_skaters["games"] = played.groupby("player_id").size()
    starts = inputs.load_goalie_starts(prior_season)
    starts = starts[starts["appeared"].astype(bool)]
    last_goalies = starts.groupby("player_id")[goalie_stats].sum()
    last_goalies["games"] = starts.groupby("player_id").size()
    last = pd.concat([last_skaters, last_goalies]).groupby(level=0).sum(min_count=1)
    last.index = last.index.astype(int)

    columns = ["games"] + skater_stats + goalie_stats
    from_last = board["basis"].str.startswith("last season")
    lines = pd.concat([consensus.reindex(board.index[~from_last.to_numpy()]),
                       last.reindex(board.index[from_last.to_numpy()])]).reindex(board.index)
    lines = lines.reindex(columns=columns)
    goalie = board["positions"].eq("G").to_numpy()
    lines.loc[goalie, skater_stats] = float("nan")
    lines.loc[~goalie, goalie_stats] = float("nan")
    return lines.round(1).rename(columns={"games": "gp"})


def build(season, prior_season, league_name, scoring, draft_date, strategy, eligibility_platform=None,
          playoffs=None, config=None, scoreset=None, tier_gap_z=TIER_GAP_Z):
    """The board, the replacement levels, the league config and the eligibility map. Positions come
    from `eligibility_platform` when given (the league's own platform -- Fleaflicker for league
    12090), else from the league config's. With `playoffs` (first day, last day of the fantasy
    playoffs) the board also has `off` and `pog`, the player's team's `schedule_counts`. `config` and
    `scoreset`, when given, stand in for the named league and scoring files (the draft window's
    own roster and scoring settings). `tier_gap_z` is the TIER column's gap size (`tiers`)."""
    from dataclasses import replace

    config = config if config is not None else league_module.load(league_name)
    if eligibility_platform:
        config = replace(config, eligibility_platform=eligibility_platform.lower(),
                         eligibility_season=season)
    scoreset = scoreset if scoreset is not None else simlayer.load_scoreset(scoring)
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
    # The team he is signed with (his NHL page, pipeline/import_player_teams.py) where known;
    # the projections' most common team otherwise, which is only as current as the sources.
    team_source = pd.Series("projections", index=team_id.index)
    unsigned = pd.Index([])
    if livepaths.player_teams().exists():
        signed = pd.read_parquet(livepaths.player_teams())
        signed = signed[signed["season"] == season].set_index("player_id")["team_id"]
        team_id = team_id.reindex(team_id.index.union(signed.index))
        team_source = team_source.reindex(team_id.index).fillna("projections")
        team_id.loc[signed.index] = signed
        team_source.loc[signed.index] = "NHL"
        # No team on his page and not active there: unsigned (FA), not the projections' team.
        players_active = pd.read_parquet(paths.players()).set_index("player_id")["active"]
        unsigned = players_active.index[~players_active].difference(signed.index)

    def against(p):
        slots = [s for s in eligibility[p] if s in levels]
        return min(slots, key=lambda s: levels[s])

    board = pd.DataFrame({
        "rank": range(1, len(vor) + 1),
        "player": players["name"].reindex(vor.index).to_numpy(),
        "team": team_id.reindex(vor.index).map(teams).where(~vor.index.isin(unsigned), "FA").to_numpy(),
        "team_source": team_source.reindex(vor.index).fillna("projections")
                       .where(~vor.index.isin(unsigned), "FA").to_numpy(),
        "positions": ["/".join(sorted(eligibility[p], key="C LW RW D G".split().index))
                      for p in vor.index],
        "value": eligible.reindex(vor.index).round(1).to_numpy(),
        "vor": vor.round(1).to_numpy(),
        "vs": [against(p) for p in vor.index],
        "sources": sources.to_numpy(),
        "basis": basis.to_numpy(),
    }, index=vor.index)
    board.index.name = "player_id"
    board["tier"] = tiers(board, tier_gap_z)
    if livepaths.injury_risk().exists():
        # Dobber's Band-Aid Boys tier (Certified, Trainee, Goalie), blank when not listed. Shown,
        # never used: the value is the projections' either way.
        risk = pd.read_parquet(livepaths.injury_risk())
        risk = risk[risk["season"] == season].groupby("player_id")["tier"].agg("/".join)
        board["injury"] = board.index.map(risk)
    if livepaths.injury_status().exists():
        # Today's merged injury reports (Fleaflicker, ESPN, Daily Faceoff), as of the last
        # snapshot. Shown, never used: the value is the full season's either way.
        board["status"] = board.index.map(status_labels(pd.read_parquet(livepaths.injury_status())))
    if playoffs is not None:
        counts = schedule_counts(season, *playoffs)
        board["off"] = board["team"].map(counts["off"]).astype("Int64")
        board["pog"] = board["team"].map(counts["pog"]).astype("Int64")
    stats = stat_lines(board, external, prior_season, scoreset)
    board = board.join(stats)
    board["periph_pct"] = peripheral_share(board, scoreset)
    for path in sorted(paths.FEATURES_DIR.glob(f"fantasy_adp_*_{season}.parquet")):
        platform = path.name[len("fantasy_adp_"):-len(f"_{season}.parquet")]
        adp = pd.read_parquet(path).set_index("player_id")["adp"]
        board[f"adp_{platform}"] = adp.reindex(board.index).to_numpy()
    log.info("%s %s %s (%s positions): %d players, replacement %s; %s", season, league_name,
             scoring, config.eligibility_platform, len(board),
             {s: round(v, 1) for s, v in levels.items()}, board["basis"].value_counts().to_dict())
    return board, levels, config, eligibility


def to_markdown(board: pd.DataFrame, top: int) -> str:
    cols = [c for c in board.columns]
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in board.head(top).iterrows():
        out.append("| " + " | ".join("" if pd.isna(v) else f"{v:g}" if isinstance(v, float)
                                     else str(v) for v in r) + " |")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--league", default=leagues.DEFAULT_LEAGUE,
                        help="A league in Settings/leagues/ (default %(default)s): its rules, scoring "
                             "and positions platform")
    parser.add_argument("--season", default=None, help="Default: the league's")
    parser.add_argument("--prior-season", default=None, help="Default: the season before")
    parser.add_argument("--weights", action="append", default=None,
                        help="Scoring file(s) instead of the league's; repeatable")
    parser.add_argument("--draft-date", default=dt.date.today().isoformat(),
                        help="Every source must be published before this (default today)")
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--eligibility", default=None,
                        help="Position platform, e.g. fleaflicker (default: the league's)")
    parser.add_argument("--playoffs", nargs=2, metavar=("START", "END"), default=None,
                        help="First and last day of the fantasy playoffs: adds off-night and "
                             "playoff games (e.g. 2027-03-15 2027-04-04 for league 12090)")
    parser.add_argument("--top", type=int, default=300, help="Rows in the Markdown board")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s",
                        stream=sys.stderr)
    league = leagues.load(args.league)
    season = args.season or league.season
    strategy = load_strategy(args.strategy or league.strategy)
    prior = args.prior_season or previous(season)
    for scoring in args.weights or [league.scoring]:
        board, levels, config, _ = build(season, prior, league.rules, scoring,
                                         pd.Timestamp(args.draft_date), strategy,
                                         args.eligibility or league.eligibility_platform,
                                         args.playoffs)
        livepaths.ensure(livepaths.league_reports(league.name))
        csv = livepaths.draft_board(season, league.name, scoring, "csv")
        board.to_csv(csv)
        md = livepaths.draft_board(season, league.name, scoring, "md")
        md.write_text(
            f"# Draft board: {season}, {league.name}, {scoring}\n\n"
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

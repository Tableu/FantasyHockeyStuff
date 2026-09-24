"""Draft policy: what a manager values on draft day, and which player he takes with a pick.

The draft *event* -- seat order, the snake, applying picks to league state -- belongs to whatever
runs the draft (`Season/draftroom.py` in the backtest). This module is only the choice, so the same
rule serves a backtest and a live draft.

**The board is last season's fantasy points under this league's own scoring**, which is what a
manager actually had on draft day. It is a weak board on purpose, and every ladder rung drafts from
it so the ladder measures in-season strategy with one variable moved at a time. The obvious
alternative is unusable for a backtest: real ADP is in the database, but for 2026-27 only, and a
2026-27 draft board encodes how players performed in 2025-26 -- the season being replayed. Public
ADP is close to useless as a *value* signal in this format anyway, because hits and blocks lift
defencemen and bottom-six grinders well above their standard-league value.

**The VOR board (section 9 step 2) has its own values** (`values_for`, by strategy
draft.vor_values): the external sources' consensus (`consensus_board`, over the preseason sheets
in the database) -- the only values a real draft has, since our own model's opening-week rows
(`preseason_values`, kept as a backtest reference) need the season's own games. The sheets were
published before the season, so they are clean for a 2025-26 replay; `Season/inputs.
load_external_projections` refuses any that were not.

**Positional need is enforced, not hoped for.** Best-available alone will happily leave a team with
one goalie and no legal lineup on a night his backup sits, and that is a harness bug dressed up as a
strategy result. Once a team's remaining picks equal the slots it still has to cover, it drafts for
need -- which is what every real autodraft does.
"""

import logging

import numpy as np
import pandas as pd

import slots as slots_module

log = logging.getLogger("draft")


def prior_season_board(actuals: pd.DataFrame, goalie_lines: pd.DataFrame, scoreset,
                       min_games=10) -> pd.Series:
    """Total fantasy points last season, skaters and goalies on one list.

    Goalies are ranked on the same scale as skaters, so goalie scarcity is priced by the draft
    rather than by a rule. A player with fewer than `min_games` is kept but not promoted -- the
    board is a season total, so a short sample self-penalizes without needing a filter.
    """
    played = actuals[actuals["target_played"].astype(bool)]
    skater_points = pd.Series(scoreset.score_columns(played, prefix="target_"),
                              index=played.index).groupby(played["player_id"]).sum()

    started = goalie_lines[goalie_lines["appeared"].astype(bool)]
    goalie_points = pd.Series(scoreset.score_columns(started, side="goalies"),
                              index=started.index).groupby(started["player_id"]).sum()

    board = pd.concat([skater_points, goalie_points]).groupby(level=0).sum()
    log.info("draft board: %d players from last season (%d skaters, %d goalies), "
             "top value %.0f", len(board), skater_points.size, goalie_points.size, board.max())
    return board.sort_values(ascending=False)


def prior_season_rate(actuals: pd.DataFrame, goalie_lines: pd.DataFrame, scoreset,
                      shrink_games: float) -> pd.Series:
    """Last season's fantasy points **per game played** -- a different thing from the board.

    The draft board is a season total, because a draft values the whole season and a player who
    missed half of it is worth less. A projection prior has to be a *rate*, because it is compared
    against a rate. Conflating the two puts the prior on a scale about eighty times too large,
    which swamps the season-to-date term it is supposed to stabilize and makes a streamer rank
    candidates by how healthy they were last season.
    """
    played = actuals[actuals["target_played"].astype(bool)]
    skater = pd.Series(scoreset.score_columns(played, prefix="target_"), index=played.index)
    skater_rate = skater.groupby(played["player_id"]).mean()

    started = goalie_lines[goalie_lines["is_starter"].astype(bool)]
    goalie = pd.Series(scoreset.score_columns(started, side="goalies"), index=started.index)
    # Per *team game*, not per start: a goalie who starts half his team's games is worth half a
    # starter to a fantasy roster, and that is the quantity a lineup decision compares.
    appearances = goalie.groupby(started["player_id"]).agg(["sum", "count"])
    goalie_rate = appearances["sum"] / appearances["count"]

    rate = pd.concat([skater_rate, goalie_rate]).groupby(level=0).mean()

    # Shrink by games played toward the league mean. Without this a player who appeared once and
    # had a big night carries a higher prior than the scoring leader, and the streamer chases him.
    games = pd.concat([played.groupby("player_id").size(),
                       started.groupby("player_id").size()]).groupby(level=0).sum()
    league_mean = float((rate * games).sum() / games.sum())
    k = shrink_games                     # strategy: priors.prior_rate_shrink_games
    shrunk = ((rate * games + league_mean * k) / (games + k)).rename("prior_rate")
    log.info("prior rate: %d players, league mean %.2f, shrunk range %.2f-%.2f "
             "(raw max %.2f before shrinkage)",
             len(shrunk), league_mean, shrunk.min(), shrunk.max(), rate.max())
    return shrunk


def prior_season_team_game_rate(actuals: pd.DataFrame, goalie_lines: pd.DataFrame,
                                scoreset, shrink_games: float) -> pd.Series:
    """Last season's fantasy points **per team game**, availability included -- the fallback rate.

    The same units as a carried projection (`lambda x p_plays`), so it can stand in for one: a
    player the projections have not reached yet (a call-up, a player back from a long injury) is
    valued on what he did last season rather than priced at zero. `prior_season_rate` is per game
    *played*; this multiplies it by the share of his team's games he played -- dressed games for a
    skater, starts over dressed games for a goalie -- because a forward value is about games that
    will happen, including the ones he misses. Rookies have no row and stay unknown.
    """
    rate = prior_season_rate(actuals, goalie_lines, scoreset, shrink_games)
    skater_share = actuals["target_played"].astype(bool).groupby(actuals["player_id"]).mean()
    goalie_share = goalie_lines["is_starter"].astype(bool).groupby(goalie_lines["player_id"]).mean()
    share = pd.concat([skater_share, goalie_share]).groupby(level=0).max()
    forward = (rate * share.reindex(rate.index).fillna(0.0)).rename("prior_team_game_rate")
    log.info("prior team-game rate: %d players, median %.2f", len(forward), forward.median())
    return forward


def _unfillable(roster, config, eligibility) -> int:
    """How many active slots this roster still cannot fill at once.

    Counted as the gap in a maximum matching rather than per position, because with composite slots
    "how many centres do I still need" has no answer -- a centre fills C, F and F/D, and which of
    them he should count against depends on who else is on the roster. The matching answers the only
    question that matters: how large a legal lineup this roster admits.
    """
    return config.active - slots_module.matching_size(
        roster, config.slot_order(), eligibility, config.accepts)


def _improves(roster, player_id, config, eligibility, current_gap) -> bool:
    """Whether adding this player lets the roster fill one more active slot."""
    return _unfillable(list(roster) + [player_id], config, eligibility) < current_gap


def choose_pick(roster, pool, ranked, config, eligibility, picks_left):
    """The player to take with this pick, and whether positional need forced it.

    `pool` is who is still available and `ranked` maps a player to his place on this manager's
    board (lower is better). Best available by the board, unless the bench slack is gone.
    """
    gap = _unfillable(roster, config, eligibility)
    if gap >= picks_left:
        # The bench slack is gone: every remaining pick has to make the lineup bigger. Walk the
        # board in order and take the first player who does, so the team drafts for need at the
        # latest possible moment rather than reaching early.
        for candidate in sorted(pool, key=lambda p: ranked[p]):
            if _improves(roster, candidate, config, eligibility, gap):
                return candidate, True
    return min(pool, key=lambda p: ranked[p]), False


# ---------------------------------------------------------------------------------------------
# Value over replacement (section 9, step 2)
# ---------------------------------------------------------------------------------------------

POSITIONS = ("C", "LW", "RW", "D", "G")


def preseason_values(ros: pd.DataFrame, prior_board: pd.Series, prior_goalie_lines: pd.DataFrame,
                     scoreset, opening_days: int, team_openers=None) -> pd.Series:
    """Our own model's view on draft day, season totals -- the `own_model` board, a backtest
    reference only: it cannot be built before a real season starts (see `values_for`).

    **Skaters:** projected points from each player's first rest-of-season row from the holdout
    build (the caller loads them through `inputs.load_ros`, which refuses anything trained on the
    season). A row dated d is built from games before d, so only a row dated on or before his
    team's first game (`team_openers`) is draft-day knowledge; a skater whose first row comes later
    -- scratched on opening night, a call-up -- has seen part of the season and keeps last season's
    total instead. Without `team_openers`, the first `opening_days` of the season stand in.

    **Goalies:** last season's starts x the league-average points per start. The standing goalie
    treatment -- P(start) x league average, no goalie *quality* modelled, because per-start quality
    did not project (R2 -0.8%).

    **Everyone else** keeps last season's total.
    """
    values = prior_board.astype(float).copy()
    values.index = values.index.astype(int)

    started = prior_goalie_lines[prior_goalie_lines["is_starter"].astype(bool)]
    line = float(pd.Series(scoreset.score_columns(started, side="goalies")).mean())
    goalies = started.groupby("player_id").size().astype(float) * line
    goalies.index = goalies.index.astype(int)

    skaters = own_model_skaters(ros, scoreset, opening_days, team_openers)

    values = pd.concat([values.drop(goalies.index.union(skaters.index), errors="ignore"),
                        goalies, skaters])
    values = values[~values.index.duplicated(keep="last")]
    log.info("preseason values: %d skaters projected, %d goalies at %.2f a start, %d from last "
             "season's total", len(skaters), len(goalies), line,
             len(values) - len(skaters) - len(goalies))
    return values.sort_values(ascending=False)


def own_model_skaters(ros: pd.DataFrame, scoreset, opening_days: int,
                      team_openers=None) -> pd.Series:
    """Season points from each skater's first rest-of-season row, if it predates the season.

    `team_openers` maps a team to its first game; a row qualifies only if it is dated on or before
    it. 35 of 682 skaters' first 2025-26 rows came one or two team games in, before this.
    """
    table = ros.copy()
    table["game_date"] = pd.to_datetime(table["game_date"])
    opening = table[table["game_date"] < table["game_date"].min() + pd.Timedelta(days=opening_days)]
    first = opening.sort_values("game_date").groupby("player_id").head(1)
    if team_openers is not None:
        opener = first["team_id"].map(team_openers)
        first = first[opener.notna() & (first["game_date"] <= opener)]
    return pd.Series(scoreset.score_columns(first, prefix="proj_"),
                     index=first["player_id"].astype(int).to_numpy())


def consensus_lines(external: pd.DataFrame) -> pd.DataFrame:
    """One season stat line per player: per stat, the mean over the sources that project it.

    Equal weights. A stat a source leaves NULL is missing, not zero -- Laidlaw projects no PIM and
    Lineup Experts no PPP, and averaging their blanks in as zeros would mark every player they
    cover down. A goalie source that gives GAA and SV% but not goals against or saves (Scott
    Cullen) still contributes them, derived: GA = GAA x GP, saves = GA x SV% / (1 - SV%).
    `sources` is how many sources project the player at all.
    """
    table = external.copy()
    goalie = table["is_goalie"].astype(bool)
    derived_ga = table["gaa"] * table["games"]
    table.loc[goalie, "goals_against"] = table.loc[goalie, "goals_against"].fillna(derived_ga[goalie])
    sv = table["save_pct"].where(table["save_pct"] < 1)
    derived_saves = table["goals_against"] * sv / (1 - sv)
    table.loc[goalie, "saves"] = table.loc[goalie, "saves"].fillna(derived_saves[goalie])

    stats = [c for c in table.columns
             if c not in ("source", "published_on", "player_id", "team_id", "is_goalie")]
    keys = ["player_id", "is_goalie"]
    lines = table.groupby(keys)[stats].mean()   # the mean skips NaN: missing, not zero
    lines["sources"] = table.groupby(keys)["source"].nunique()
    return lines.reset_index()


def consensus_values(external: pd.DataFrame, scoreset) -> pd.DataFrame:
    """Season fantasy points from the consensus line, under this league's scoring.

    Returns player_id, is_goalie, sources, value -- season totals, the units the board uses, so
    replacement levels and VOR are computed exactly as before.
    """
    lines = consensus_lines(external)
    goalie = lines["is_goalie"].astype(bool).to_numpy()
    value = pd.Series(0.0, index=lines.index)
    value[~goalie] = scoreset.score_columns(lines[~goalie], side="skaters")
    value[goalie] = scoreset.score_columns(lines[goalie], side="goalies")
    out = lines[["player_id", "is_goalie", "sources"]].assign(value=value)
    out["player_id"] = out["player_id"].astype(int)
    return out


def consensus_board(external: pd.DataFrame, prior_board: pd.Series, scoreset,
                    min_sources: int = 3, scale=None, sides=None) -> pd.Series:
    """The draft board from the external sources alone -- no model of ours anywhere in it.

    A player covered by `min_sources` or more sources gets the consensus line's points. A thinly
    covered one falls back to last season's total if he has one, so one optimistic sheet cannot
    carry a player up the board, and otherwise keeps his thin consensus (a rookie one or two
    sources project). A player no source covers keeps last season's total.

    `scale` ({"F": x, "D": x, "G": x}, with `sides` mapping a player to F/D/G) multiplies each
    position's consensus values. It leaves the order within a position alone and moves only the
    order across positions, which is what VOR compares on one scale.
    """
    consensus = consensus_values(external, scoreset)
    value = pd.Series(consensus["value"].to_numpy(), index=consensus["player_id"].to_numpy())
    if scale is not None:
        side = pd.Series(sides).reindex(value.index)
        side[side.isna()] = np.where(consensus["is_goalie"].to_numpy()[side.isna().to_numpy()],
                                     "G", "F")
        value = value * side.map(scale).fillna(1.0).to_numpy()
    sources = pd.Series(consensus["sources"].to_numpy(), index=value.index)
    last = prior_board.astype(float).copy()
    last.index = last.index.astype(int)
    thin = sources < min_sources
    fallback = [p for p in value.index[thin.to_numpy()] if p in last.index]
    use = value.drop(fallback)
    board = pd.concat([last.drop(use.index, errors="ignore"), use])
    log.info("consensus board: %d players from %d+ sources, %d thin with last season's total, "
             "%d thin on their thin consensus, %d on last season alone%s",
             int((~thin).sum()), min_sources, len(fallback), int(thin.sum()) - len(fallback),
             len(board) - len(use), f"; scaled {scale}" if scale else "")
    return board.sort_values(ascending=False)


def fit_position_scale(values: pd.Series, actual: pd.Series, sides, pool: dict) -> dict:
    """Per position, actual points over board points across each position's top `pool[side]`
    by the board: the factor that makes a position's board total match what it produced.
    Fitted on a season's actuals, so honest only for a later season's draft."""
    side = pd.Series(sides).reindex(values.index)
    out = {}
    for s, n in pool.items():
        top = values[(side == s).to_numpy()].sort_values(ascending=False).head(n)
        out[s] = float(actual.reindex(top.index).fillna(0.0).sum() / top.sum())
    return out


VOR_VALUE_KINDS = ("own_model", "consensus")


def values_for(kind: str, scoreset, prior_board, *, external=None, min_sources: int = 3,
               scale=None, sides=None, ros=None, prior_goalie_lines=None, opening_days=None,
               team_openers=None) -> pd.Series:
    """The VOR board's season values under `kind` (strategy: draft.vor_values).

    `consensus` is the external sources alone (`consensus_board`), and is what a real draft can
    use. `own_model` is our rest-of-season model's opening-week rows (`preseason_values`), kept as
    a backtest reference: those rows are built from the season's own games table -- its candidate
    universe, opening-night lineups, injury flags at the lockout -- so for a season that has not
    started they do not exist.
    """
    if kind == "own_model":
        return preseason_values(ros, prior_board, prior_goalie_lines, scoreset, opening_days,
                                team_openers)
    if kind == "consensus":
        if external is None:
            raise ValueError("vor_values 'consensus' needs the external projections")
        return consensus_board(external, prior_board, scoreset, min_sources, scale, sides)
    raise ValueError(f"vor_values {kind!r}; use one of {VOR_VALUE_KINDS}")


def simulate_draft(board: pd.Series, config, eligibility) -> list:
    """Every team drafting from `board` with the real pick rule: who ends up rostered."""
    ranked = {p: i for i, p in enumerate(board.index)}
    pool = set(board.index)
    rosters = [[] for _ in range(config.teams)]
    order = list(range(config.teams))
    for round_number in range(config.roster_size):
        for seat in (order if round_number % 2 == 0 else order[::-1]):
            picks_left = config.roster_size - len(rosters[seat])
            available = [p for p in board.index if p in pool]
            choice, _ = choose_pick(rosters[seat], available, ranked, config, eligibility,
                                    picks_left)
            rosters[seat].append(choice)
            pool.discard(choice)
    return [p for roster in rosters for p in roster]


def replacement_levels(values: pd.Series, config, eligibility) -> dict:
    """The best undrafted value at each position, when the whole league drafts by `values`.

    Replacement comes from the league itself rather than from a rule of thumb: run the snake
    draft with this board in every seat (positional need included) and see who is left. That is
    what a manager can pick up for nothing on opening day.
    """
    drafted = set(simulate_draft(values, config, eligibility))
    left = values[[p not in drafted for p in values.index]]
    levels = {}
    for position in POSITIONS:
        eligible = [p for p in left.index if position in eligibility.get(p, ())]
        levels[position] = float(left[eligible].max()) if eligible else 0.0
    return levels


def vor_board(values: pd.Series, config, eligibility) -> pd.Series:
    """Value over replacement: preseason value minus the lowest replacement level among the
    positions the player can fill.

    The lowest, so a flexible player is credited for the scarce slot he can cover -- a C/LW is
    measured against whichever of centre or wing is thinner. Only players with eligibility are
    kept: anyone else cannot be rostered.
    """
    values = values[[p in eligibility for p in values.index]]
    levels = replacement_levels(values, config, eligibility)
    floor = {p: min(levels[x] for x in eligibility[p] if x in levels) for p in values.index
             if any(x in levels for x in eligibility[p])}
    vor = pd.Series({p: values[p] - floor[p] for p in floor}).sort_values(ascending=False)
    log.info("VOR board: replacement %s", {k: round(v, 1) for k, v in levels.items()})
    return vor

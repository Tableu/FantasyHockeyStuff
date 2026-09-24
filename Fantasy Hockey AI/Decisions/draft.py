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

**Positional need is enforced, not hoped for.** Best-available alone will happily leave a team with
one goalie and no legal lineup on a night his backup sits, and that is a harness bug dressed up as a
strategy result. Once a team's remaining picks equal the slots it still has to cover, it drafts for
need -- which is what every real autodraft does.
"""

import logging

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


def prior_season_rate(actuals: pd.DataFrame, goalie_lines: pd.DataFrame, scoreset) -> pd.Series:
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
    k = 20.0
    shrunk = ((rate * games + league_mean * k) / (games + k)).rename("prior_rate")
    log.info("prior rate: %d players, league mean %.2f, shrunk range %.2f-%.2f "
             "(raw max %.2f before shrinkage)",
             len(shrunk), league_mean, shrunk.min(), shrunk.max(), rate.max())
    return shrunk


def prior_season_team_game_rate(actuals: pd.DataFrame, goalie_lines: pd.DataFrame,
                                scoreset) -> pd.Series:
    """Last season's fantasy points **per team game**, availability included -- the fallback rate.

    The same units as a carried projection (`lambda x p_plays`), so it can stand in for one: a
    player the projections have not reached yet (a call-up, a player back from a long injury) is
    valued on what he did last season rather than priced at zero. `prior_season_rate` is per game
    *played*; this multiplies it by the share of his team's games he played -- dressed games for a
    skater, starts over dressed games for a goalie -- because a forward value is about games that
    will happen, including the ones he misses. Rookies have no row and stay unknown.
    """
    rate = prior_season_rate(actuals, goalie_lines, scoreset)
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
                     scoreset, opening_days=7) -> pd.Series:
    """What each player is worth over the coming season, as known on draft day. Season totals.

    **Skaters:** projected points from each player's first rest-of-season row, if it is dated in
    opening week -- the same rows the engine seeds its opening-week rates from. They come from the
    holdout build (the caller loads them through `inputs.load_ros`, which refuses anything trained
    on the season), and a row dated d is built from games before d, so an opening-week row is last
    season plus the preseason. Its window runs to the end of the season (~80 team games).

    **Goalies:** last season's starts x last season's league-average points per start. The
    standing goalie treatment -- P(start) x league average, no goalie *quality* modelled, because
    per-start quality did not project (R2 -0.8%).

    **Everyone else** keeps last season's total, which is the board every rung used before.
    """
    values = prior_board.astype(float).copy()
    values.index = values.index.astype(int)

    started = prior_goalie_lines[prior_goalie_lines["is_starter"].astype(bool)]
    line = float(pd.Series(scoreset.score_columns(started, side="goalies")).mean())
    goalies = started.groupby("player_id").size().astype(float) * line
    goalies.index = goalies.index.astype(int)

    table = ros.copy()
    table["game_date"] = pd.to_datetime(table["game_date"])
    opening = table[table["game_date"] < table["game_date"].min() + pd.Timedelta(days=opening_days)]
    first = opening.sort_values("game_date").groupby("player_id").head(1)
    skaters = pd.Series(scoreset.score_columns(first, prefix="proj_"),
                        index=first["player_id"].astype(int).to_numpy())

    values = pd.concat([values.drop(goalies.index.union(skaters.index), errors="ignore"),
                        goalies, skaters])
    values = values[~values.index.duplicated(keep="last")]
    log.info("preseason values: %d skaters projected, %d goalies at %.2f a start, %d from last "
             "season's total", len(skaters), len(goalies), line,
             len(values) - len(skaters) - len(goalies))
    return values.sort_values(ascending=False)


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

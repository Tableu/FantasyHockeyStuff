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

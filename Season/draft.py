"""The draft, identical for every rung, so the ladder measures in-season strategy.

Section 16 has rung 1 autodrafting and rung 2 drafting by ADP, but section 10 allows one shared
ranking, and that is the right call here for two reasons. A controlled comparison wants one
variable moved at a time, and every rung drafting the same board makes the in-season difference
the only difference. And the obvious board is unusable: real ADP is now in the database, but for
2026-27 only, and a 2026-27 draft board encodes how players performed in 2025-26 -- the season
this replays. Drafting from it would hand the whole field a season of hindsight.

So the board is **last season's fantasy points under this league's own scoring**, which is what a
manager actually had on draft day. It is a weak board on purpose; section 7 notes that public ADP
is close to useless in this format anyway, because hits and blocks lift defencemen and bottom-six
grinders well above their standard-league value.

Seat order rotates across replications so no rung inherits the first pick.

**Positional need is enforced, not hoped for.** Best-available alone will happily leave a team
with one goalie and no legal lineup on a night his backup sits, and that is a harness bug dressed
up as a strategy result. Once a team's remaining picks equal the slots it still has to cover, it
drafts for need -- which is what every real autodraft does.
"""

import logging
from collections import Counter

import pandas as pd

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


def seat_order(config, replication=0) -> list:
    """Which rung sits in which seat, rotated by replication.

    Three clones of each rung, interleaved rather than blocked, so that a rung's three seats do
    not all draft early or all draft late.
    """
    seats = list(range(config.teams))
    shift = replication % config.teams
    return seats[shift:] + seats[:shift]


def _unfillable(roster, config, eligibility) -> int:
    """How many active slots this roster still cannot fill at once.

    Counted as the gap in a maximum matching rather than per position, because with composite slots
    "how many centres do I still need" has no answer -- a centre fills C, F and F/D, and which of
    them he should count against depends on who else is on the roster. The matching answers the only
    question that matters: how large a legal lineup this roster admits.
    """
    import slots as slots_module
    return config.active - slots_module.matching_size(
        roster, config.slot_order(), eligibility, config.accepts)


def _improves(roster, player_id, config, eligibility, current_gap) -> bool:
    """Whether adding this player lets the roster fill one more active slot."""
    return _unfillable(list(roster) + [player_id], config, eligibility) < current_gap


def run(state, config, board: pd.Series, eligibility: dict, replication=0) -> None:
    """Snake draft until every roster is full.

    A single shared board means every team wants the same player, so the snake order is the only
    thing separating the seats -- which is the point: it isolates draft position as the one
    pre-season difference between two clones of the same rung.
    """
    order = seat_order(config, replication)
    rounds = config.roster_size
    available = [p for p in board.index if p in state.pool]
    ranked = {p: i for i, p in enumerate(available)}
    forced_picks = 0

    for round_number in range(rounds):
        seats = order if round_number % 2 == 0 else list(reversed(order))
        for seat in seats:
            team = state.teams[seat]
            picks_left = rounds - len(team.roster)
            gap = _unfillable(team.roster, config, eligibility)

            pool = [p for p in available if p in state.pool]
            if not pool:
                break

            choice = None
            if gap >= picks_left:
                # The bench slack is gone: every remaining pick has to make the lineup bigger.
                # Walk the board in order and take the first player who does, so the team drafts
                # for need at the latest possible moment rather than reaching early.
                for candidate in sorted(pool, key=lambda p: ranked[p]):
                    if _improves(team.roster, candidate, config, eligibility, gap):
                        choice = candidate
                        forced_picks += 1
                        break
            if choice is None:
                choice = min(pool, key=lambda p: ranked[p])
            state.draft(seat, choice)

    state.assert_legal()
    if forced_picks:
        log.info("draft: %d of %d picks were forced by positional need",
                 forced_picks, rounds * config.teams)
    log.info("draft complete: %d players over %d rounds, %d free agents remain",
             sum(len(t.roster) for t in state.teams), rounds, len(state.pool))


def verify_rosters_fieldable(state, config, eligibility) -> None:
    """Every team can fill every active slot, ignoring who plays on a given night.

    Cheap, and it catches the failure that would otherwise appear as one rung mysteriously
    leaving slots empty all season.
    """
    import slots as slots_module

    order = config.slot_order()
    for team in state.teams:
        values = {p: 1.0 for p in team.roster}
        lineup = slots_module.assign(order, values, eligibility, config.accepts)
        if lineup.unfilled:
            empty = Counter(order[j] for j in lineup.unfilled)
            raise AssertionError(
                f"team {team.team} cannot field a legal lineup: {dict(empty)} unfillable from "
                f"{len(team.roster)} players. The draft's positional-need rule failed.")

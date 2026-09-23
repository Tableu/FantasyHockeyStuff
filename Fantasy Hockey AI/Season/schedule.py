"""The season's calendar, derived from the schedule and never assumed.

A head-to-head league scores by the week, so the week is the unit this whole layer turns on:
weekly totals decide matchups, the seven-move budget resets on the week boundary, and a
streamer's whole edge is games-remaining-before-that-reset. Getting the calendar wrong
therefore does not produce a slightly wrong answer, it produces a plausible one.

Three things about a real NHL season make "26 weeks, Monday to Sunday" not good enough:

  * **There is a two-week hole.** 2025-26 plays no games between 2026-02-09 and 2026-02-22.
    A calendar built by counting weeks forward from opening night silently pairs managers in
    a matchup with no games in it.
  * **The first and last weeks are partial** -- 26 and 31 games against a normal 50-57.
  * **Teams do not play the same number of games in a week**, which is the entire premise of
    section 15. Games per team per week is an output of this module, not a constant.

So weeks are the Monday-to-Sunday spans that actually contain games, numbered in order, and a
span with no games is not a week at all.
"""

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

log = logging.getLogger("schedule")

WEEK_ANCHORS = {"MON": "W-SUN", "SUN": "W-SAT"}


@dataclass(frozen=True)
class Week:
    number: int
    start: pd.Timestamp
    end: pd.Timestamp
    games: int

    def contains(self, day) -> bool:
        return self.start <= pd.Timestamp(day) <= self.end


class Calendar:
    """Game days and matchup weeks for one season."""

    def __init__(self, schedule: pd.DataFrame, week_starts_on="MON"):
        if week_starts_on not in WEEK_ANCHORS:
            raise ValueError(f"week_starts_on must be one of {sorted(WEEK_ANCHORS)}")
        self.schedule = schedule[["game_id", "game_date", "team_id"]].copy()
        self.schedule["game_date"] = pd.to_datetime(self.schedule["game_date"])
        self.week_starts_on = week_starts_on

        games = self.schedule.drop_duplicates("game_id")[["game_id", "game_date"]]
        period = games["game_date"].dt.to_period(WEEK_ANCHORS[week_starts_on])
        counts = games.groupby(period)["game_id"].nunique().sort_index()

        self.weeks = [Week(number=i, start=p.start_time.normalize(),
                           end=p.end_time.normalize(), games=int(n))
                      for i, (p, n) in enumerate(counts.items(), start=1)]
        self.days = sorted(games["game_date"].unique())

        # Team-games per week, the opportunity term every rung spends its attention on.
        team_period = self.schedule["game_date"].dt.to_period(WEEK_ANCHORS[week_starts_on])
        self._team_games = (self.schedule.assign(_p=team_period)
                            .groupby(["_p", "team_id"])["game_id"].nunique())
        self._week_of_period = {week_period: week.number
                                for week_period, week in zip(counts.index, self.weeks)}
        self._period_of_week = {w.number: p for p, w in zip(counts.index, self.weeks)}
        self._memo = {}

    def __len__(self):
        return len(self.weeks)

    @property
    def n_games(self) -> int:
        return self.schedule["game_id"].nunique()

    def week_of(self, day) -> int | None:
        """The matchup week a date falls in, or None if it falls in the hole."""
        period = pd.Timestamp(day).to_period(WEEK_ANCHORS[self.week_starts_on])
        return self._week_of_period.get(period)

    def days_in(self, week: int) -> list:
        target = self.weeks[week - 1]
        return [d for d in self.days if target.start <= d <= target.end]

    def team_games_in(self, week: int) -> pd.Series:
        """Games per team in a week -- unequal by design, and the point of section 15."""
        return self._team_games.loc[self._period_of_week[week]]

    def games_remaining(self, team_id: int, day, week: int | None = None) -> int:
        """A team's games from `day` (inclusive) to the end of that matchup week.

        This is what a move is worth: the seven-move budget does not carry over, so a player
        acquired on Tuesday pays out over however many games his team has left before Sunday
        and not one game more.
        """
        week = week if week is not None else self.week_of(day)
        if week is None:
            return 0
        end = self.weeks[week - 1].end
        rows = self.schedule
        return int(rows[(rows["team_id"] == team_id)
                        & (rows["game_date"] >= pd.Timestamp(day))
                        & (rows["game_date"] <= end)]["game_id"].nunique())

    def games_through(self, team_id: int, day, weeks_ahead=1) -> int:
        """A team's games from `day` to the end of the matchup week `weeks_ahead` later.

        The drop side of a streaming decision needs this. An acquisition is a rental -- it can be
        re-evaluated next week -- but a **drop is permanent**, so pricing it over the current week
        alone says a star with no games left tonight is worth nothing, and any warm body with one
        game beats him. Over a two-week window he is worth what he actually is. `weeks_ahead=None`
        runs to the end of the season. A team plays at most once a day, so this is the number of
        `team_days`, which is cached: a manager asks it of every free agent every day.
        """
        return len(self.team_days(team_id, day, weeks_ahead))

    def team_days(self, team_id: int, day, weeks_ahead=1) -> list:
        """The dates a team plays from `day` through the end of the week `weeks_ahead` later.

        The same window as `games_through`, returned as the nights themselves, because pricing a
        swap on the roster means solving the lineup on each of those nights.
        """
        key = (team_id, pd.Timestamp(day), weeks_ahead)
        if key not in self._memo:
            week = self.week_of(day)
            if week is None:
                self._memo[key] = []
            else:
                if weeks_ahead is None:
                    end = self.weeks[-1].end
                else:
                    end = self.weeks[min(week - 1 + weeks_ahead, len(self.weeks) - 1)].end
                rows = self.schedule
                dates = rows[(rows["team_id"] == team_id)
                             & (rows["game_date"] >= pd.Timestamp(day))
                             & (rows["game_date"] <= end)].drop_duplicates("game_id")["game_date"]
                self._memo[key] = sorted(dates)
        return list(self._memo[key])

    def gaps(self, min_days=7) -> list:
        """Stretches of `min_days` or more with no games, so the hole is reported not hidden."""
        days = pd.to_datetime(pd.Series(self.days))
        deltas = days.diff().dt.days
        return [(days[i - 1].date(), days[i].date(), int(deltas[i]) - 1)
                for i in range(1, len(days)) if deltas[i] - 1 >= min_days]

    def verify(self) -> dict:
        """Every game in exactly one week, and no empty week. Both are silent failures."""
        games = self.schedule.drop_duplicates("game_id")
        assigned = games["game_date"].map(self.week_of)
        unassigned = int(assigned.isna().sum())
        if unassigned:
            raise AssertionError(f"{unassigned} games fall in no matchup week")
        if sum(w.games for w in self.weeks) != self.n_games:
            raise AssertionError("week game counts do not sum to the schedule")
        empty = [w.number for w in self.weeks if w.games == 0]
        if empty:
            raise AssertionError(f"weeks {empty} contain no games and should not exist")

        per_week = [w.games for w in self.weeks]
        return {
            "games": self.n_games,
            "game_days": len(self.days),
            "weeks": len(self.weeks),
            "first_day": str(pd.Timestamp(self.days[0]).date()),
            "last_day": str(pd.Timestamp(self.days[-1]).date()),
            "games_per_week_min": min(per_week),
            "games_per_week_max": max(per_week),
            "gaps": [{"after": str(a), "before": str(b), "days": n} for a, b, n in self.gaps()],
        }


def from_candidates(candidates: pd.DataFrame, week_starts_on="MON") -> Calendar:
    """Build the calendar from the projection candidate universe.

    The universe covers every team-game it has projections for, so the schedule comes out of it
    without a second source -- the same recovery `Projections/ros.py:team_schedule` does. Using
    the *universe* rather than the database's game list is deliberate: a game with no candidate
    rows has no projections for anyone, so no manager in the field could act on it, and
    including it would let the rungs that ignore projections collect points the others cannot.
    """
    return Calendar(candidates, week_starts_on=week_starts_on)


# Note on the module name: this file is `schedule.py` and not `calendar.py` because the folder
# sits on sys.path under the flat-module convention the sibling folders use, and a local
# `calendar` shadows the standard library module that `zoneinfo` -- and therefore pandas --
# imports. The failure is a circular-import AttributeError from inside pandas, which points
# nowhere near the real cause.

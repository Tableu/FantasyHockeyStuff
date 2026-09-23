"""What a manager is allowed to see -- the structural leakage guard for section 6.

Section 2 learned this the hard way and then enforced it in code rather than in prose. The same
discipline applies here, and the failure mode is worse: a manager who can see tonight's outcomes
does not merely score well, it produces a season-level number that looks like a strategy finding.

So a manager never receives the engine's tables. It receives a `SlateView`, which is built from
projections, the schedule, today's availability flag and the simulated history up to *yesterday*
-- and which physically does not hold:

    target_*                 what the skaters actually did
    the goalie start line    who actually started, how many saves, the decision
    label_starting_goalie    who actually started, again
    outcome draws            the engine's sampled season (phase 3)

`build` takes the outcome tables only to assert their columns are absent from what it hands back.
That is deliberate: the check lives next to the construction, so adding a field to the view
cannot quietly smuggle an outcome through.

The one judgement call worth naming: **a manager sees today's `injured_at_lockout` but never the
length of the spell.** That is close to what a real manager reads off a morning injury report,
and the build log notes only the first game of a spell is genuinely ambiguous at lockout time.
Knowing a player is out *tonight* is fair; knowing he is out for six weeks would not be.
"""

import logging

import pandas as pd

log = logging.getLogger("view")

# Any column whose presence in a view would be leakage. Checked, not trusted.
FORBIDDEN_PREFIXES = ("target_", "label_")
FORBIDDEN_COLUMNS = frozenset({
    "saves", "goals_against", "shots_against", "wins", "losses", "ot_losses", "shutouts",
    "decision", "is_starter", "pulled", "appeared", "team_score", "opp_score",
    "goalies_used", "last_period_type", "points_scored", "actual_points",
})


class LeakageError(RuntimeError):
    """Raised when something that knows the future reached a manager."""


def assert_clean(frame: pd.DataFrame, what: str) -> pd.DataFrame:
    bad = [c for c in frame.columns
           if c in FORBIDDEN_COLUMNS or c.startswith(FORBIDDEN_PREFIXES)]
    if bad:
        raise LeakageError(f"{what} carries outcome column(s) {bad}; a manager must not see "
                           f"what happened. See view.py.")
    return frame


class SlateView:
    """One night, as a manager can know it at the lock."""

    def __init__(self, day, week, config, calendar, projections, goalie_projections,
                 unavailable, playing_tonight, nhl_team, history, state, team_index,
                 opponent_index, my_week_points, opponent_week_points,
                 decision_points=None, rate_estimate=None):
        self.day = pd.Timestamp(day)
        self.week = week
        self.config = config
        self.calendar = calendar
        self.projections = assert_clean(projections, "skater projections")
        self.goalie_projections = assert_clean(goalie_projections, "goalie projections")
        self.unavailable = unavailable            # set of player_ids out tonight
        self.playing_tonight = playing_tonight    # set of player_ids whose team plays
        self.nhl_team = nhl_team                  # player_id -> his NHL team id today
        self.history = history                    # naive season-to-date, through yesterday
        self.team_index = team_index
        self.opponent_index = opponent_index
        self.my_week_points = my_week_points
        self.opponent_week_points = opponent_week_points
        # {player_id: array of sampled fantasy points for tonight}. Drawn by the engine on a
        # random stream that never resolves a night -- see engine.decision_draws. Empty unless
        # the run asked for decision sims, so rungs 1-3 are unaffected by its presence.
        self.decision_points = decision_points or {}
        # {player_id: most recently projected fantasy points per game}. Carried forward by the
        # engine so a transaction can value a player whose team is dark tonight. Never a
        # future projection -- see engine.latest_rate.
        self.rate_estimate = rate_estimate or {}
        self._state = state

    # ---------- the manager's own holdings ----------

    @property
    def roster(self) -> list:
        return list(self._state.teams[self.team_index].roster)

    @property
    def ir(self) -> list:
        return list(self._state.teams[self.team_index].ir)

    @property
    def moves_left(self) -> int:
        return self._state.teams[self.team_index].moves_left

    @property
    def waiver_priority(self) -> int:
        return self._state.teams[self.team_index].waiver_priority

    def free_agents(self) -> set:
        return set(self._state.free_agents())

    def on_waivers(self, player_id) -> bool:
        return self._state.on_waivers(player_id, self.day)

    def eligibility(self, player_id) -> frozenset:
        return self._state.eligibility.get(player_id, frozenset())

    # ---------- tonight ----------

    def available(self, player_id) -> bool:
        """He dresses tonight as far as anyone can know: his team plays and he is not injured."""
        return player_id in self.playing_tonight and player_id not in self.unavailable

    def startable(self, players) -> list:
        return [p for p in players if self.available(p)]

    def ir_eligible(self) -> set:
        """On this harness, IR eligibility is an injury spell today. The live snapshot job would
        replace this with the platform's own designation, which is the thing it carries."""
        return {p for p in self.roster if p in self.unavailable}

    def opponent_roster(self) -> list:
        """The opposing manager's holdings.

        Public information in every fantasy platform, so a manager is entitled to it -- and rung 4
        needs it, because P(win the week) is a statement about a specific opponent rather than
        about a points total. His *lineup* is not knowable in advance; rung 4 projects it.
        """
        if self.opponent_index is None:
            return []
        return list(self._state.teams[self.opponent_index].roster)

    # ---------- the schedule, which costs nothing to know ----------

    def games_remaining(self, player_id) -> int:
        """That player's team's games from today to the end of the matchup week.

        This is what a move is worth: the budget does not carry over, so an add pays out over the
        games left before Sunday and not one game more.
        """
        team_id = self.nhl_team.get(player_id)
        if team_id is None:
            return 0
        return self.calendar.games_remaining(team_id, self.day, self.week)

    def games_through(self, player_id, weeks_ahead=1) -> int:
        """His team's games from today through the end of the week `weeks_ahead` later."""
        team_id = self.nhl_team.get(player_id)
        if team_id is None:
            return 0
        return self.calendar.games_through(team_id, self.day, weeks_ahead)

    def moments(self, scoreset, players=None) -> dict:
        """{player_id: (mean, sd)} of tonight's fantasy points, for the candidates asked about.

        Skaters come from the sampled draws when the run has them, so the mean and the spread are
        the calibrated layer's own -- which is the point of having built it. Goalies are not sampled
        yet (phase 3), so they come from the closed-form Bernoulli-times-line mixture instead, which
        is the standing `P(start) x league average` treatment with its variance written out.

        The cross-player copula is deliberately NOT in these numbers: they are per-player marginals,
        and correlation between two of my own players raises the variance of my TOTAL without
        changing either marginal. It is worth about 6% on a random roster's variance and it moves
        every candidate in nearly the same direction, so leaving it out of a per-player ranking
        costs little. It is not left out of the total -- see `week_distribution`.
        """
        wanted = set(players) if players is not None else None
        out = {}
        for player_id, samples in self.decision_points.items():
            if wanted is None or player_id in wanted:
                out[player_id] = (float(samples.mean()), float(samples.std()))
        if len(self.goalie_projections):
            weights = scoreset.weights("goalies")
            for row in self.goalie_projections.itertuples():
                player_id = int(row.player_id)
                if wanted is not None and player_id not in wanted:
                    continue
                p = float(getattr(row, self.p_start_column))
                mu, sigma = float(row.expected_line), float(row.line_sd)
                mean = p * mu
                var = max(p * (sigma ** 2 + mu ** 2) - mean ** 2, 0.0)
                out[player_id] = (mean, var ** 0.5)
        return out

    # Which P(start) column this manager is allowed to read. Rung 3 gets the naive share; rung 4
    # gets the fitted model. Set by the manager, so the two cannot silently read the same thing.
    p_start_column = "p_start_naive"

    def projected_rate(self, player_id, default=0.0) -> float:
        """His latest projected points per game, whether or not his team plays tonight."""
        return float(self.rate_estimate.get(int(player_id), default))

    # ---------- what the projections say ----------

    def projected_points(self, scoreset) -> dict:
        """Tonight's projected fantasy points per candidate, skaters and goalies together.

        Goalies are `p_start x the league-average line`, which is the standing treatment: per-start
        goalie quality measured as unprojectable, so what varies between two goalies tonight is the
        chance either starts, not how well he would play.
        """
        points = {}
        if len(self.projections):
            values = scoreset.score_columns(self.projections, prefix="lambda_")
            plays = self.projections["p_plays"].to_numpy("float64")
            for player_id, value in zip(self.projections["player_id"], values * plays):
                points[int(player_id)] = float(value)
        if len(self.goalie_projections):
            for row in self.goalie_projections.itertuples():
                points[int(row.player_id)] = float(
                    getattr(row, self.p_start_column) * row.expected_line)
        return points

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

import numpy as np
import pandas as pd

from decisionlayer import slots as slots_module

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
                 decision_points=None, rate_estimate=None, ros_estimate=None, injured=None,
                 goalie_draw_column=None, future_draws=None, phase="regular", alive=True,
                 on_bye=False, week_weight_mode="flat"):
        self.day = pd.Timestamp(day)
        self.week = week
        self.config = config
        self.calendar = calendar
        self.projections = assert_clean(projections, "skater projections")
        self.goalie_projections = assert_clean(goalie_projections, "goalie projections")
        self.unavailable = unavailable            # set of player_ids out tonight
        # Out as of his team's latest lockout report, carried across dark nights. IR reads this,
        # not `unavailable`: on a night his team is idle an injured player is not "out tonight",
        # but he is not healthy either (see engine.status_by_day).
        self.injured = unavailable if injured is None else injured
        self.playing_tonight = playing_tonight    # set of player_ids whose team plays
        # player_id -> his NHL team as of his latest appearance, NOT tonight's slate only: a
        # player whose club is idle tonight still has games this week (see engine.latest_team).
        self.nhl_team = nhl_team
        self.history = history                    # naive season-to-date, through yesterday
        self.team_index = team_index
        self.opponent_index = opponent_index
        self.my_week_points = my_week_points
        self.opponent_week_points = opponent_week_points
        # {player_id: array of sampled fantasy points for tonight}. Drawn by the engine on a
        # random stream that never resolves a night -- see engine.decision_draws. Empty unless
        # the run asked for decision sims, so rungs 1-3 are unaffected by its presence.
        self.decision_points = decision_points or {}
        # Which P(start) column the goalie draws in `decision_points` were made with, or None if
        # no goalie was drawn tonight.
        self.goalie_draw_column = goalie_draw_column
        # A callable returning {date: {player_id: points per sim}} for the rest of the week, drawn
        # from each team's latest knowable slate (engine.future_draws). Lazy: only a manager that
        # reads sampled week totals pays for it.
        self._future_draws = future_draws
        # The season's shape, public on any platform: regular season or playoffs, whether this
        # team is still in the bracket, and whether this week is its bye. `night_weight` turns
        # these into what a night's points are worth to it (1 in the regular season).
        self.phase = phase
        self.alive = alive
        self.on_bye = on_bye
        self.week_weight_mode = week_weight_mode
        # P(win this week), set by a manager that can compute it; an even match otherwise.
        self.p_advance = 0.5
        # {player_id: most recently projected fantasy points per game}. Carried forward by the
        # engine so a transaction can value a player whose team is dark tonight. Never a
        # future projection -- see engine.latest_rate.
        self.rate_estimate = rate_estimate or {}
        # {player_id: rest-of-season points per team game}, from the latest rest-of-season row at
        # or before today, out of a build that held this season out. Empty unless the run has it.
        self.ros_estimate = ros_estimate or {}
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
        return {p for p in self.roster if p in self.injured}

    def waiver_clears(self, player_id):
        """The date a player on waivers can be awarded to a claim (None if he is not on them).
        A claim entered today pays out only from then, which is what a rental claim has to price."""
        return self._state.waived.get(player_id)

    def healthy_on_ir(self) -> list:
        """IR players whose latest report says healthy. The league makes these come off today."""
        return [p for p in self.ir if p not in self.injured]

    def roster_room(self) -> int:
        """Open roster spots (IR excluded): what an activation or a drop-less add can use."""
        return self.config.roster_size - len(self.roster)

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

    @property
    def weighted(self) -> bool:
        """Whether nights carry weights other than 1 today."""
        return self.phase == "playoffs" and self.week_weight_mode == "p_advance"

    def night_weight(self, night) -> float:
        """What a night's points are worth to this team, from today.

        1 in the regular season and under the flat mode. In the playoffs: this week counts in full,
        or not at all on a bye; a later week counts only if the team gets there -- P(win this week)
        for next week (1 after a bye), halved for each round after that, an unknown opponent at
        even odds. An eliminated team's nights are worth nothing."""
        if not self.weighted:
            return 1.0
        if not self.alive:
            return 0.0
        ahead = (self.calendar.week_of(night) or self.week) - self.week
        if ahead <= 0:
            return 0.0 if self.on_bye else 1.0
        reach_next = 1.0 if self.on_bye else self.p_advance
        return reach_next * 0.5 ** (ahead - 1)

    def _weighted_count(self, nights) -> float:
        if not self.weighted:
            return len(nights)
        return float(sum(self.night_weight(n) for n in nights))

    def games_remaining(self, player_id) -> int:
        """That player's team's games from today to the end of the matchup week.

        This is what a move is worth: the budget does not carry over, so an add pays out over the
        games left before Sunday and not one game more.
        """
        team_id = self.nhl_team.get(player_id)
        if team_id is None:
            return 0
        if not self.weighted:
            return self.calendar.games_remaining(team_id, self.day, self.week)
        return self._weighted_count(self.calendar.team_days(team_id, self.day, 0))

    def games_through(self, player_id, weeks_ahead=1) -> int:
        """His team's games from today through the end of the week `weeks_ahead` later."""
        team_id = self.nhl_team.get(player_id)
        if team_id is None:
            return 0
        if not self.weighted:
            return self.calendar.games_through(team_id, self.day, weeks_ahead)
        return self._weighted_count(self.calendar.team_days(team_id, self.day, weeks_ahead))

    def nights_through(self, player_id, weeks_ahead=1) -> list:
        """The dates his team plays from today through the end of the week `weeks_ahead` later."""
        team_id = self.nhl_team.get(player_id)
        if team_id is None:
            return []
        return self.calendar.team_days(team_id, self.day, weeks_ahead)

    def moments(self, scoreset, players=None) -> dict:
        """{player_id: (mean, sd)} of tonight's fantasy points, for the candidates asked about.

        Skaters come from the sampled draws when the run has them, so the mean and the spread are
        the calibrated layer's own -- which is the point of having built it. Goalies do too, when
        they were drawn with the P(start) column this manager reads (`Simulation/goalies.py`: the
        line built from the opposing skaters' draw). Otherwise -- rung 3's naive share, a game
        whose skaters were not drawn, or no simulator -- they come from the closed-form
        Bernoulli-times-line mixture, the standing `P(start) x league average` treatment with its
        variance written out.

        The cross-player copula is deliberately NOT in these numbers: they are per-player marginals,
        and correlation between two of my own players raises the variance of my TOTAL without
        changing either marginal. It is worth about 6% on a random roster's variance and it moves
        every candidate in nearly the same direction, so leaving it out of a per-player ranking
        costs little. Rung 4's matchup z adds these per-player variances (`FullSystem.
        _week_projection`), so it does leave the correlation out of the total -- and with it the
        goalie-skater links the sampler draws. Reading sampled joint totals there is its own change.
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
                if (player_id in self.decision_points
                        and self.p_start_column == self.goalie_draw_column):
                    continue                               # drawn: keep the sampled moments
                p = float(getattr(row, self.p_start_column))
                mu, sigma = float(row.expected_line), float(row.line_sd)
                mean = p * mu
                var = max(p * (sigma ** 2 + mu ** 2) - mean ** 2, 0.0)
                out[player_id] = (mean, var ** 0.5)
        return out

    def week_totals(self, roster, tonight=None):
        """Per-sim points a roster still scores this week, or None if the run draws nothing.

        Tonight from tonight's draws, the rest of the week from `future_draws`, all on the same
        sims -- so two rosters' totals keep every link between them (a goalie and the skaters he
        faces). Each night only a legal lineup scores: the best by expected points, solved exactly,
        so a bench body adds nothing. `tonight` overrides tonight's lineup with a given one.
        """
        if not self.decision_points or self._future_draws is None:
            return None
        sims = len(next(iter(self.decision_points.values())))
        slot_order, accepts = self.config.slot_order(), self.config.accepts
        eligibility = self._state.eligibility
        roster = list(roster)

        def night_total(points, candidates, lineup=None):
            drawn = {p: points[p] for p in candidates if p in points}
            if not drawn:
                return np.zeros(sims)
            if lineup is None:
                means = {p: float(v.mean()) for p, v in drawn.items()}
                lineup = slots_module.assign(slot_order, means, eligibility, accepts)
            started = [p for p in lineup.assigned.values() if p in drawn]
            return np.sum([drawn[p] for p in started], axis=0) if started else np.zeros(sims)

        total = night_total(self.decision_points,
                            [p for p in roster if self.available(p)], tonight)
        for points in self._future_draws().values():
            total = total + night_total(points, roster)
        return total

    # Which P(start) column this manager is allowed to read. Rung 3 gets the naive share; rung 4
    # gets the fitted model. Set by the manager, so the two cannot silently read the same thing.
    p_start_column = "p_start_naive"

    def projected_rate(self, player_id, default=0.0):
        """His latest projected points per game, whether or not his team plays tonight.

        `default=None` returns None for a player nobody has projected yet, so a caller can tell
        "unknown" from "worth zero" -- they are not the same, and pricing the first as the second
        is how a star whose club had not opened yet became the cheapest drop on a roster.
        """
        value = self.rate_estimate.get(int(player_id))
        return default if value is None else float(value)

    def ros_rate(self, player_id, default=None):
        """His rest-of-season points per team game (availability included), or `default`."""
        value = self.ros_estimate.get(int(player_id))
        return default if value is None else float(value)

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

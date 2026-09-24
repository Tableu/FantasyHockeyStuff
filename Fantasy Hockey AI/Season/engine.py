"""The day loop: step the schedule, lock a lineup, resolve the night, accumulate the week.

This is the environment every decision model in sections 7 and 8 will eventually be evaluated
against, and in phase 1 it runs one deterministic replay: **outcomes are the real 2025-26 lines**,
not samples. That is the build order's "single-strategy backtest first", and it has a property
multi-replication does not -- the projections were built from the real season's history, so they
are consistent with the outcomes they are scored against, and there is no Monte Carlo noise to
argue about when rung 4 and rung 3 come out close.

Two boundaries are enforced rather than trusted:

  * a manager receives a `SlateView` and nothing else (`view.py`), so it cannot see tonight;
  * every roster mutation goes through `state.LeagueState`, which raises on an illegal state.

Code arrives from two siblings, each through one module: `Simulation/` supplies the scoring and
the sampler (`simlayer.py`), and `Decisions/` supplies every policy -- the managers, the lineup
solver, the draft rule, rung 3's estimator (`decisionlayer.py`). Everything else is a file.
"""

import logging
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

import paths
import view as view_module

import simlayer                                 # Simulation's code; see its docstring
from decisionlayer import estimators as estimators_module
from decisionlayer import slots as slots_module

log = logging.getLogger("engine")

# The league-average goalie line, in fantasy points per start. Measured, not assumed: this is what
# `build_goalie_starts.py` reports for the season being replayed, and the standing treatment is
# `P(start) x this` because per-start goalie quality is not projectable (R2 -0.8%).
# The league-average goalie line is NOT a constant: it is a property of the scoring file. Under
# points-league a start is worth 4.39 with a spread of 4.03; a banger league prices saves at 0.20
# instead of 0.25 and pays nothing for a loss, which moves both. Hard-coding either number makes
# every goalie decision wrong in exactly the formats the ladder is run against to find out whether
# a verdict travels. So it is measured from the same export the harness resolves nights with,
# under whichever scoreset is in play.
#
# The spread matters out of proportion to the mean, which is why it is carried at all: goalies are
# not sampled until phase 3, so rung 4 gets their variance in closed form from the
# Bernoulli-times-line mixture, Var = p(sigma^2 + mu^2) - (p mu)^2. A skater floors at 0.00 and a
# pulled goalie does not.
def goalie_line(goalie_starts, scoreset):
    """(mean, sd) of one start's fantasy points under this scoring, from the realized export."""
    started = goalie_starts[goalie_starts["is_starter"].astype(bool)]
    points = scoreset.score_columns(started, side="goalies")
    return float(points.mean()), float(points.std())


def round_robin(teams: int) -> list:
    """The circle method: `teams - 1` rounds in which everybody plays once.

    Repeated to fill the regular season, so a 12-team league plays two full cycles over 22 weeks
    -- which is why `regular_season_weeks` defaults to 22 rather than to however many weeks the
    NHL schedule happens to have.
    """
    if teams % 2:
        raise ValueError("round robin needs an even number of teams")
    order = list(range(teams))
    rounds = []
    for _ in range(teams - 1):
        pairs = [(order[i], order[teams - 1 - i]) for i in range(teams // 2)]
        rounds.append(pairs)
        order = [order[0]] + [order[-1]] + order[1:-1]
    return rounds


def matchup_schedule(config, weeks: int) -> dict:
    """{week: [(home, away), ...]} over the regular season, cycling the round robin."""
    cycle = round_robin(config.teams)
    return {week: cycle[(week - 1) % len(cycle)] for week in range(1, weeks + 1)}


class Outcomes:
    """What every player actually did, by date -- the engine's half of the world.

    Kept apart from anything a manager touches. A skater's night comes from `target_*`; a goalie's
    from the per-start export, which carries a row for every *dressed* goalie so a backup who sat
    scores the zero he really scored.
    """

    def __init__(self, actuals, goalie_starts, scoreset):
        skaters = actuals.copy()
        skaters["points"] = scoreset.score_columns(skaters, prefix="target_")
        skaters.loc[~skaters["target_played"].astype(bool), "points"] = 0.0
        skaters["played"] = skaters["target_played"].astype(bool)

        goalies = goalie_starts.copy()
        goalies["points"] = scoreset.score_columns(goalies, side="goalies")
        goalies["played"] = goalies["appeared"].astype(bool)

        columns = ["game_date", "player_id", "team_id", "points", "played"]
        both = pd.concat([skaters[columns], goalies[columns]], ignore_index=True)
        # A traded player can legitimately appear in two clubs' rows on one date; the row where he
        # actually played is the real one (see ModelFeatures' team-id join finding).
        both = both.sort_values("played", ascending=False).drop_duplicates(
            ["game_date", "player_id"], keep="first")

        self.points = {(d, int(p)): float(v) for d, p, v
                       in zip(both["game_date"], both["player_id"], both["points"])}
        self.played = {(d, int(p)) for d, p, y
                       in zip(both["game_date"], both["player_id"], both["played"]) if y}

    def score(self, day, player_id) -> float:
        return self.points.get((pd.Timestamp(day), int(player_id)), 0.0)


class Season:
    """One full simulated season for one field of managers."""

    def __init__(self, config, calendar, data, eligibility, scoreset, field,
                 replication=0, log_every_week=False, decision_sims=0,
                 decision_seed=90210):
        self.config = config
        self.calendar = calendar
        self.data = data
        self.eligibility = eligibility
        self.scoreset = scoreset
        self.field = field
        # One strategy for the whole field: the naive goalie start share below is part of the
        # view every seat shares, so its prior cannot differ by seat.
        strategies = {m.strategy for m in field}
        if len(strategies) != 1:
            raise ValueError(f"the field carries {len(strategies)} different strategies")
        self.strategy = field[0].strategy
        self.replication = replication
        self.log_every_week = log_every_week
        self.slot_order = config.slot_order()
        self.accepts = config.accepts

        # Decision draws, on a SEPARATE random stream from anything that resolves a night. In
        # phase 1 outcomes are the real season so no seed can collide -- but rung 4 samples to
        # decide and phase 3 will sample to resolve, and if those two ever share a stream the
        # manager is choosing the players who are about to score. Keeping them apart from the
        # start costs nothing; discovering it later invalidates every number.
        self.decision_sims = int(decision_sims)
        self.simulator = None
        if self.decision_sims:
            self.simulator = simlayer.build_simulator(data["season"], seed=decision_seed)
            log.info("decision draws: %d sims a slate, seed %d (independent of outcomes)",
                     self.decision_sims, decision_seed)

        self.outcomes = Outcomes(data["actuals"], data["goalie_starts"], scoreset)
        self.goalie_line_mean, self.goalie_line_sd = goalie_line(data["goalie_starts"], scoreset)
        log.info("goalie line under %s: %.2f points a start, sd %.2f",
                 scoreset.name, self.goalie_line_mean, self.goalie_line_sd)
        self._index_inputs()
        self.weekly = defaultdict(lambda: defaultdict(float))
        # team -> [occupied, productive, offered]. Three counts rather than one ratio, because a
        # slot occupied by a player who did not play and a slot left empty are different errors
        # and only rung 1 makes the first one.
        self.slot_fill = defaultdict(lambda: [0, 0, 0])
        # Section 16's decision-level metric: realized points against the best legal lineup the
        # same roster could have started, known only in hindsight. The ratio separates decision
        # error from roster quality -- a manager holding a weak roster can still be slotting it
        # perfectly, and a season total cannot tell the two apart.
        self.hindsight = defaultdict(float)
        self.results = []

    # ---------- indexing, once, because it is reused every night ----------

    def _index_inputs(self):
        projections = self.data["projections"]
        self.proj_by_day = {d: f for d, f in projections.groupby("game_date")}
        self.nhl_team_by_day = {
            d: dict(zip(f["player_id"].astype(int), f["team_id"]))
            for d, f in projections.groupby("game_date")}

        goalies = self.data["goalie_candidates"]
        self.goalies_by_day = {d: f for d, f in goalies.groupby("game_date")}
        for day, frame in self.goalies_by_day.items():
            self.nhl_team_by_day.setdefault(day, {}).update(
                dict(zip(frame["player_id"].astype(int), frame["team_id"])))

        unavailable = self.data["availability"]
        unavailable = unavailable[unavailable["injured_at_lockout"]]
        by_game = defaultdict(set)
        for game_id, player_id in zip(unavailable["game_id"], unavailable["player_id"]):
            by_game[game_id].add(int(player_id))
        game_days = (pd.concat([projections[["game_id", "game_date"]],
                               goalies[["game_id", "game_date"]]])
                     .drop_duplicates("game_id"))
        self.unavailable_by_day = defaultdict(set)
        for game_id, day in zip(game_days["game_id"], game_days["game_date"]):
            self.unavailable_by_day[day] |= by_game.get(game_id, set())

        # Every lockout's status, injured or not, for the players it lists. The flag only exists on
        # nights a player's team plays, so on a dark night an injured player looked healthy: every
        # rung activated him off IR and re-stashed him at his team's next game -- 95% of stashes
        # were followed by one of these flaps (rung 5: 346 stashes, 341 flaps a replication). The
        # engine carries each player's latest status forward instead (`self.injured`), which is
        # what a manager knows on a dark night: the last report.
        status = self.data["availability"]
        day_of_game = dict(zip(game_days["game_id"], game_days["game_date"]))
        self.status_by_day = defaultdict(dict)
        for game_id, player_id, hurt in zip(status["game_id"], status["player_id"],
                                            status["injured_at_lockout"]):
            day = day_of_game.get(game_id)
            if day is not None:
                self.status_by_day[day][int(player_id)] = bool(hurt)
        self.injured_status = {}
        self.injured = set()

        # Naive goalie start share, which is all rung 3 has: appearances over team games to date.
        self.goalie_starts_to_date = defaultdict(float)
        self.goalie_games_to_date = defaultdict(float)

        # Each player's most recently projected points per game, carried forward as the season
        # runs. A transaction is about games that have not been played yet, so it needs a rate that
        # survives a night his team is idle -- valuing him at tonight's projection makes every
        # player on a dark team worth exactly zero and therefore the first man dropped.
        #
        # It has to be the LATEST projection, never a future one. The lambda table holds a row for
        # every game of the season, but a row two weeks out was built from rolling features as of
        # that game, which include results that have not happened today. Reading it would be
        # leakage of precisely the kind section 2 exists to prevent.
        self.latest_rate = {}

        # Each player's NHL team as of his latest appearance, carried forward like the rate. The
        # view used to take the team map from tonight's slate alone, so a rostered player whose
        # club was idle today had no team, zero games in any window, and a forward value of zero
        # -- which made him the cheapest drop on the roster. Every transacting rung dropped
        # players for no reason but a dark night (measured on rung 5: dropped players showed 0.34
        # games in a window where they really played ~6). Seeded from opening-week rosters, which
        # are public before the first puck drop; anyone who first appears later is unknown until
        # he does.
        self.latest_team = {}
        opening = min(self.nhl_team_by_day) if self.nhl_team_by_day else None
        if opening is not None:
            first_week = [d for d in sorted(self.nhl_team_by_day)
                          if d <= opening + pd.Timedelta(days=6)]
            for day in first_week:
                for player_id, team_id in self.nhl_team_by_day[day].items():
                    self.latest_team.setdefault(player_id, team_id)

        # Rest-of-season points per team game, from the holdout build, by the date of the row. A row
        # dated d is built from games before d, so it is knowable at d's lock -- the same footing
        # as the per-game projections. Carried forward like `latest_rate`, never read ahead.
        self.ros_by_day = {}
        self.latest_ros = {}
        ros = self.data.get("ros")
        if ros is not None and len(ros):
            points = self.scoreset.score_columns(ros, prefix="proj_")
            games = ros["window_team_games"].to_numpy("float64")
            per_game = np.divide(points, games, out=np.zeros_like(points), where=games > 0)
            frame = pd.DataFrame({"game_date": ros["game_date"].to_numpy(),
                                  "player_id": ros["player_id"].astype(int).to_numpy(),
                                  "rate": per_game})
            for day, rows in frame.groupby("game_date"):
                self.ros_by_day[pd.Timestamp(day)] = dict(zip(rows["player_id"], rows["rate"]))

        # Opening-week seed for both rates, for the same reason `latest_team` is seeded: both fill
        # only once a player's team has played, so on the first nights a star whose club had not
        # opened yet had no rate, priced at zero, and was the first man dropped (rung 5 dropped 33
        # such players a replication at -59 rest-of-season points each). A player's first row in
        # opening week is built before his first game, from last season and the preseason, so it
        # is knowable at the first lock. Skaters only: goalie rates are recomputed nightly from
        # start shares, and a player who first appears after opening week stays unknown -- which
        # the add/drop rule treats as unknown, never as zero.
        if opening is not None:
            for day in first_week:
                frame = self.proj_by_day.get(day)
                if frame is not None and len(frame):
                    values = self.scoreset.score_columns(frame, prefix="lambda_")
                    plays = frame["p_plays"].to_numpy("float64")
                    for player_id, value in zip(frame["player_id"].astype(int), values * plays):
                        self.latest_rate.setdefault(int(player_id), float(value))
                for player_id, value in self.ros_by_day.get(pd.Timestamp(day), {}).items():
                    self.latest_ros.setdefault(int(player_id), float(value))

        # The fitted P(start), if it has been built. Rung 4 reads it; nothing else may.
        self.p_start_model = {}
        pstart = self.data.get("p_start")
        if pstart is not None and len(pstart):
            for day, frame in pstart.groupby("game_date"):
                self.p_start_model[day] = dict(zip(frame["player_id"].astype(int),
                                                   frame["p_start"].astype(float)))

    def player_pool(self) -> list:
        skaters = self.data["projections"]["player_id"].astype(int).unique().tolist()
        goalies = self.data["goalie_candidates"]["player_id"].astype(int).unique().tolist()
        return sorted(set(skaters) | set(goalies))

    # ---------- the manager's-eye view ----------

    def _goalie_projections(self, day, history):
        """Two P(start) estimates per goalie: the naive one and the fitted one.

        Rung 3 may only read `p_start_naive`, a start share off a box score. Rung 4 reads
        `p_start_model` from `Projections/goalie_starts.py`, fitted on 2023-24 and 2024-25 and held
        out of the simulated season. That column is where the goalie half of the modelling stack
        earns its place or does not, and keeping both on one frame makes the comparison a column
        swap rather than a rebuild.
        """
        frame = self.goalies_by_day.get(day)
        if frame is None or not len(frame):
            return pd.DataFrame(columns=["player_id", "p_start", "p_start_naive",
                                         "p_start_model", "expected_line", "line_sd"])
        modelled = self.p_start_model.get(day, {})
        rows = []
        for player_id, team_id in zip(frame["player_id"].astype(int), frame["team_id"]):
            games = self.goalie_games_to_date.get(player_id, 0.0)
            starts = self.goalie_starts_to_date.get(player_id, 0.0)
            # Shrunk toward a tandem even split. Deliberately NOT "who started last game", which
            # carries an AUC of 0.520 over all candidates and inverts among the healthy ones.
            prior_games = self.strategy.goalie_start_share_prior_games
            naive = ((starts + prior_games * self.strategy.goalie_start_share_prior)
                     / (games + prior_games))
            rows.append({"player_id": player_id,
                         "p_start_naive": naive,
                         "p_start_model": float(modelled.get(player_id, naive)),
                         "p_start": naive,
                         "expected_line": self.goalie_line_mean,
                         "line_sd": self.goalie_line_sd})
        return pd.DataFrame(rows)

    def decision_draws(self, day):
        """Per-candidate fantasy-point samples for tonight, drawn once and shared.

        Section 6 says projections are deterministic given the lockout snapshot, so inference
        belongs outside the loop. The same argument applies to the draws: every rung-4 clone on a
        given night faces the same slate, so one draw serves all of them. Returns
        {player_id: array of sims}.
        """
        if not self.simulator:
            return {}
        cached = getattr(self, "_draw_cache", None)
        if cached is not None and cached[0] == day:
            return cached[1]
        frame = self.proj_by_day.get(day)
        if frame is None or not len(frame):
            self._draw_cache = (day, {})
            return {}
        draws = self.simulator.draw(frame.reset_index(drop=True), self.decision_sims)
        points = self.scoreset.score_draws(draws)                     # [rows, sims]
        out = {int(p): points[i] for i, p in enumerate(draws.keys["player_id"])}
        self._draw_cache = (day, out)
        return out

    def _view_for(self, team_index, day, week, opponent, history, goalie_projections):
        projections = self.proj_by_day.get(day, self.data["projections"].iloc[:0])
        keep = [c for c in projections.columns if not c.startswith("target_")]
        playing = set(projections["player_id"].astype(int))
        goalie_frame = self.goalies_by_day.get(day)
        if goalie_frame is not None:
            playing |= set(goalie_frame["player_id"].astype(int))
        return view_module.SlateView(
            day=day, week=week, config=self.config, calendar=self.calendar,
            projections=projections[keep],
            goalie_projections=goalie_projections,
            unavailable=self.unavailable_by_day.get(day, set()),
            injured=self.injured,
            playing_tonight=playing,
            nhl_team=self.latest_team,
            history=history, state=self.state, team_index=team_index,
            opponent_index=opponent,
            my_week_points=self.weekly[week][team_index],
            opponent_week_points=self.weekly[week][opponent] if opponent is not None else 0.0,
            decision_points=self.decision_draws(day),
            rate_estimate=self.latest_rate,
            ros_estimate=self.latest_ros)

    # ---------- the loop ----------

    def run(self, prior_board: dict, prior_rate: dict, prior_forward: dict | None = None,
            boards: dict | None = None) -> dict:
        import draftroom
        import state as state_module

        # Last season's rate per team game, for anyone the opening-week projections did not reach.
        # setdefault, so it never overrides a projection; a player with neither (a rookie) stays
        # unknown, which the add/drop rule refuses to price as zero.
        for player_id, value in (prior_forward or {}).items():
            self.latest_rate.setdefault(int(player_id), float(value))

        self.state = state_module.LeagueState(self.config, self.player_pool(), self.eligibility)
        board = pd.Series(prior_board).sort_values(ascending=False)
        seat_boards = {seat: pd.Series(b).sort_values(ascending=False)
                       for seat, b in (boards or {}).items()}
        draftroom.run(self.state, self.config, board, self.eligibility, self.replication,
                      boards=seat_boards, block=len({m.rung for m in self.field}))
        draftroom.verify_rosters_fieldable(self.state, self.config, self.eligibility)

        schedule = matchup_schedule(self.config, self.config.regular_season_weeks)
        # The board is a season TOTAL and the rate is per game played. They are different
        # quantities and conflating them puts the prior ~80x too high (see Decisions/draft.py).
        prior_rate = {int(p): float(v) for p, v in prior_rate.items()}
        # Before a game is played the prior IS the history, and every player's dress share is the
        # league's -- nobody has shown anything yet.
        history = estimators_module.NaiveHistory(dict(prior_rate), {}, 1.0)
        actuals = self.data["actuals"]
        seen = []
        current_week = None

        for day in self.calendar.days:
            week = self.calendar.week_of(day)
            if week is None or week > self.config.regular_season_weeks:
                continue
            if week != current_week:
                if current_week is not None:
                    self._settle_week(current_week, schedule.get(current_week, []))
                self.state.start_week(week)
                current_week = week
                # Refresh the naive rate once a week, from everything played so far. Weekly rather
                # than nightly because that is how often a manager would actually recompute it,
                # and because it keeps the cost off the day loop.
                if seen:
                    history = estimators_module.naive_history(
                        actuals[actuals["game_date"].isin(seen)], prior_rate, self.scoreset)

            opponents = self._opponents_for(schedule.get(week, []))
            self.latest_ros.update(self.ros_by_day.get(pd.Timestamp(day), {}))
            self.latest_team.update(self.nhl_team_by_day.get(day, {}))
            goalie_projections = self._goalie_projections(day, history)
            # Tonight's lockout report updates the players it lists; everyone else keeps his last.
            self.injured_status.update(self.status_by_day.get(day, {}))
            self.injured = {p for p, hurt in self.injured_status.items() if hurt}

            # A claim whose drop has left the roster asks its manager again, with today's view.
            def redrop(team_index, player_id, _day=day, _week=week):
                view = self._view_for(team_index, _day, _week, opponents.get(team_index),
                                      history, goalie_projections)
                return self.field[team_index].claim_drop(view, player_id)

            self.state.process_waivers(day, redrop=redrop)
            for manager in self.field:
                v = self._view_for(manager.team_index, day, week,
                                   opponents.get(manager.team_index), history,
                                   goalie_projections)
                manager.manage_ir(v)
                manager.transactions(v)
                # The league's rule, not a strategy: a healthy player may not sit on IR. An
                # activation on a full roster forces a drop, and a manager has to make it today.
                self.state.assert_ir_resolved(manager.team_index, self.injured)
            self.state.assert_legal()

            for manager in self.field:
                v = self._view_for(manager.team_index, day, week,
                                   opponents.get(manager.team_index), history,
                                   goalie_projections)
                lineup = manager.set_lineup(v)
                self._resolve(manager.team_index, day, week, lineup, v)

            self._track_goalie_starts(day)
            self._carry_rates(day, goalie_projections)
            seen.append(day)

        if current_week is not None:
            self._settle_week(current_week, schedule.get(current_week, []))
        return self._report()

    def _opponents_for(self, pairs) -> dict:
        out = {}
        for home, away in pairs:
            out[home], out[away] = away, home
        return out

    def _resolve(self, team_index, day, week, lineup, v) -> None:
        """Score tonight's started players. The only place outcomes enter."""
        if not slots_module.is_legal(lineup, self.slot_order, self.eligibility, self.accepts):
            raise AssertionError(f"team {team_index} started an illegal lineup on {day}")
        held = set(self.state.teams[team_index].roster)
        productive = 0
        for player_id in lineup.started:
            if player_id not in held:
                raise AssertionError(f"team {team_index} started {player_id} on {day} without "
                                     f"holding him")
            self.weekly[week][team_index] += self.outcomes.score(day, player_id)
            if (pd.Timestamp(day), int(player_id)) in self.outcomes.played:
                productive += 1
        # The hindsight-optimal lineup: the same assignment problem, solved with what actually
        # happened as the values. Anything a manager leaves on the table shows up here.
        realized = {p: self.outcomes.score(day, p) for p in v.startable(v.roster)}
        if realized:
            best = slots_module.assign(self.slot_order, realized, self.eligibility,
                                       self.accepts)
            self.hindsight[team_index] += slots_module.total_value(best, realized)

        offered = min(len(v.startable(v.roster)), len(self.slot_order))
        self.slot_fill[team_index][0] += lineup.filled
        self.slot_fill[team_index][1] += productive
        self.slot_fill[team_index][2] += offered

    def _carry_rates(self, day, goalie_projections) -> None:
        """Update the running rate estimate from tonight's slate, after the night has been set."""
        frame = self.proj_by_day.get(day)
        if frame is not None and len(frame):
            values = self.scoreset.score_columns(frame, prefix="lambda_")
            plays = frame["p_plays"].to_numpy("float64")
            for player_id, value in zip(frame["player_id"].astype(int), values * plays):
                self.latest_rate[int(player_id)] = float(value)
        if len(goalie_projections):
            for row in goalie_projections.itertuples():
                # Stored at the naive share; a manager rescales by whichever P(start) it may read.
                self.latest_rate[int(row.player_id)] = float(
                    row.p_start_naive * row.expected_line)

    def _track_goalie_starts(self, day) -> None:
        frame = self.goalies_by_day.get(day)
        if frame is None:
            return
        started = self.data["goalie_starts"]
        for player_id in frame["player_id"].astype(int):
            self.goalie_games_to_date[player_id] += 1.0
        todays = started[started["game_date"] == day]
        for player_id, is_starter in zip(todays["player_id"].astype(int), todays["is_starter"]):
            if is_starter:
                self.goalie_starts_to_date[player_id] += 1.0

    def _settle_week(self, week, pairs) -> None:
        for home, away in pairs:
            mine, theirs = self.weekly[week][home], self.weekly[week][away]
            if mine > theirs:
                self.state.teams[home].matchup_wins += 1.0
            elif theirs > mine:
                self.state.teams[away].matchup_wins += 1.0
            else:
                share = self.config.tie_share()          # the league's tie rule
                self.state.teams[home].matchup_wins += share
                self.state.teams[away].matchup_wins += share
            self.results.append({"week": week, "home": home, "away": away,
                                 "home_points": mine, "away_points": theirs})
        for team in self.state.teams:
            team.weekly_points[week] = self.weekly[week][team.team]
        if self.log_every_week:
            log.info("week %2d settled: mean %.1f points, spread %.1f-%.1f", week,
                     np.mean([self.weekly[week][t] for t in range(self.config.teams)]),
                     min(self.weekly[week].values()), max(self.weekly[week].values()))

    def _report(self) -> dict:
        rows = []
        for manager in self.field:
            team = self.state.teams[manager.team_index]
            occupied, productive, offered = self.slot_fill[manager.team_index]
            weeks = [w for w in self.weekly if self.weekly[w].get(manager.team_index) is not None]
            rows.append({
                "seat": manager.team_index,
                "rung": manager.rung,
                "strategy": manager.name,
                "matchup_wins": team.matchup_wins,
                "weeks": len(weeks),
                "points": float(sum(team.weekly_points.values())),
                "slot_nights_occupied": occupied,
                "slot_nights_productive": productive,
                "slot_nights_offered": offered,
                # Of the games a manager could have started, how many he actually got. This is the
                # section 15 metric, and it is the one that separates rungs 1 and 2.
                "games_started_rate": productive / offered if offered else 0.0,
                "empty_slot_nights": max(0, offered - occupied),
                "wasted_slot_nights": occupied - productive,
                "hindsight_points": self.hindsight[manager.team_index],
                "decision_efficiency": (float(sum(team.weekly_points.values()))
                                        / self.hindsight[manager.team_index]
                                        if self.hindsight[manager.team_index] else 0.0),
                "moves_spent": sum(1 for t in self.state.transactions
                                   if t["team"] == manager.team_index),
                # Activations on a full roster, each of which forced a (free) drop.
                "forced_drops": len(manager.ir_log),
                # Waiver claims: entered, won, and lost for a recorded reason (state.failed_claims).
                "claims_submitted": self.state.claims_submitted.get(manager.team_index, 0),
                "claims_awarded": sum(1 for t in self.state.transactions
                                      if t["team"] == manager.team_index and t["kind"] == "claim"),
                "claims_failed": sum(1 for f in self.state.failed_claims
                                     if f["team"] == manager.team_index),
                **self._move_quality(manager.team_index),
            })
        return {"teams": pd.DataFrame(rows), "matchups": pd.DataFrame(self.results),
                "transactions": pd.DataFrame(self.state.transactions)}

    # The window a move is judged over, in matchup weeks after the current one. Fixed and shared by
    # every rung, so hit rates compare -- it is an accounting convention, not any manager's horizon.
    MOVE_ACCOUNTING_WEEKS = 1

    def _move_quality(self, team_index) -> dict:
        """Did this team's moves pay? Realized points of the player added minus the player dropped.

        Counted over the move day through the end of next week, whether or not either man was
        started -- it grades the choice of player, not the lineup around him. A move with no drop
        is graded against zero. This is the decision-level metric for transactions: a rung can lose
        points by picking badly or by moving too often, and a hit rate separates the two.
        """
        gains, rentals, drop_tail = [], [], []
        for move in self.state.transactions:
            if move["team"] != team_index:
                continue
            week = self.calendar.week_of(move["date"])
            if week is None:
                continue
            # A rental (section 10's stream) is graded over its own week only. Grading it through
            # next week would charge it for the dropped streamer's post-week games -- games the
            # manager meant to buy back from the pool, which is the whole premise of a stream.
            rental = move["kind"] == "rental"
            last = week if rental else min(week + self.MOVE_ACCOUNTING_WEEKS,
                                           len(self.calendar.weeks))
            end = self.calendar.weeks[last - 1].end
            days = [d for d in self.calendar.days if move["date"] <= d <= end]
            added = sum(self.outcomes.score(d, move["player_id"]) for d in days)
            dropped = (sum(self.outcomes.score(d, move["dropped"]) for d in days)
                       if move["dropped"] is not None else 0.0)
            (rentals if rental else gains).append(added - dropped)
            if rental and move["dropped"] is not None:
                # What the dropped streamer went on to score next week: should be ~replacement.
                nxt = self.calendar.weeks[min(week, len(self.calendar.weeks) - 1)]
                drop_tail.append(sum(self.outcomes.score(d, move["dropped"])
                                     for d in self.calendar.days
                                     if nxt.start <= d <= nxt.end and week < len(self.calendar.weeks)))

        def summary(values):
            if not values:
                return float("nan"), float("nan")
            return float(np.mean([g > 0 for g in values])), float(np.mean(values))

        hit, per = summary(gains)
        rhit, rper = summary(rentals)
        return {"move_hit_rate": hit, "realized_gain_per_move": per,
                "rentals": len(rentals), "rental_hit_rate": rhit, "rental_gain": rper,
                "rental_drop_next_week": float(np.mean(drop_tail)) if drop_tail else float("nan")}

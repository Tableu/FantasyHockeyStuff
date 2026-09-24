"""Goalie lines, drawn from the game the skaters already drew.

Goalie quality is not modelled here -- the standing finding is that it cannot be at this grain
(per-start R^2 -0.8%, save% r = +0.026) -- so the basis is P(start) x a league-average line. What
this adds over the closed-form version of that is structure: a goalie's line is almost entirely
the OPPOSING skaters' line, and it is taken from their draw rather than sampled beside it.

    starter        one per team-game, categorical on P(start) within the team-game
    shots against  the opponent's sampled skater shots, less empty-net goals
    goals against  the opponent's sampled skater goals, less empty-net goals
    saves          shots against - goals against  (>= 0, since a skater's goals are his shots)
    decision       own skater goals against the opponent's. A tie is a shootout (shootout goals
                   are not skater goals), a coin flip with the loser taking an OT loss; a one-goal
                   game went to overtime with a fitted probability, likewise an OT loss
    pull           P(pulled | team goals against), fitted; a pulled starter is charged a fitted
                   share of the goals against and saves, and keeps the decision at a fitted rate.
                   The reliever's line is not drawn
    shutout        no goals against, not pulled, and a win -- as the NHL export codes it

So a goalie's win moves with his own skaters' goals and his goals against are the opposing
skaters' goals, with no correlation fitted for either. Every number that structure cannot supply
comes from `goalie_fit.py`, fitted on the seasons BEFORE the one drawn.

No `paths` import: Season reaches this module through `simlayer.py`, where `paths` is Season's.
"""

import json

import numpy as np
import pandas as pd

from sampler import Draws

GOALIE_COUNTS = ["wins", "losses", "ot_losses", "shutouts", "saves", "goals_against"]


class GoalieFit:
    def __init__(self, payload: dict):
        self.payload = payload
        self.en_by_margin = {int(k): float(v) for k, v in payload["empty_net_by_margin"].items()}
        self.p_ot = float(payload["p_ot_one_goal"])
        self.pull_by_ga = np.asarray(payload["pull_by_ga"], dtype="float64")
        self.share_ga = float(payload["pulled_share_ga"])
        self.share_saves = float(payload["pulled_share_saves"])
        self.keeps_win = float(payload["pulled_decision"]["team_win"])
        self.keeps_loss = float(payload["pulled_decision"]["team_loss"])

    @classmethod
    def load(cls, path):
        return cls(json.loads(open(path, encoding="utf-8").read()))

    def en_rate(self, margin: np.ndarray) -> np.ndarray:
        """Per-goal empty-net probability for the goals beyond the first, by final margin."""
        rate = np.zeros(margin.shape)
        rate[margin == 2] = self.en_by_margin[2]
        rate[margin == 3] = self.en_by_margin[3]
        rate[margin >= 4] = self.en_by_margin[4]
        return rate


def _team_sums(skaters: Draws, team_key: pd.Series, index: dict, n_team_games: int):
    """Per team-game, per sim: the skaters' total shots and goals. Skater rows from games no
    goalie is being drawn for are ignored."""
    mapped = team_key.map(index)
    keep = mapped.notna().to_numpy()
    rows = mapped[keep].astype(int).to_numpy()
    shots = np.zeros((n_team_games, skaters.n_sims), dtype="int32")
    goals = np.zeros((n_team_games, skaters.n_sims), dtype="int32")
    np.add.at(shots, rows, skaters["shots"][keep])
    np.add.at(goals, rows, skaters["goals"][keep])
    return shots, goals


def draw_goalies(skaters: Draws, goalies: pd.DataFrame, fit: GoalieFit, rng,
                 p_column: str = "p_start") -> Draws:
    """Goalie `Draws` on the same sims as `skaters`, one row per row of `goalies` (game_id,
    team_id, player_id, `p_column`). Every team-game with a goalie row must have skater rows for
    both sides, or its goalie line is refused rather than invented."""
    goalies = goalies.reset_index(drop=True)
    sims = skaters.n_sims
    s_keys = skaters.keys
    s_team = s_keys["game_id"].astype(str) + ":" + s_keys["team_id"].astype(str)
    g_team = goalies["game_id"].astype(str) + ":" + goalies["team_id"].astype(str)

    # Team-games of the games goalies are drawn for, each with its opponent in the same game.
    # Both sides must have skaters drawn: a goalie line is the opponent's draw, and with none
    # drawn it would be a free shutout.
    games_wanted = set(goalies["game_id"])
    skater_sides = (s_keys.loc[s_keys["game_id"].isin(games_wanted), ["game_id", "team_id"]]
                    .drop_duplicates())
    per_game = skater_sides.groupby("game_id")["team_id"].nunique().reindex(
        sorted(games_wanted), fill_value=0)
    if (per_game != 2).any():
        bad = per_game[per_game != 2].index.tolist()[:5]
        raise ValueError(f"games without both teams' skaters drawn: {bad}")
    teams = skater_sides.reset_index(drop=True)
    stray = set(zip(goalies["game_id"], goalies["team_id"])) - set(zip(teams["game_id"], teams["team_id"]))
    if stray:
        raise ValueError(f"goalie rows for teams with no skaters in their game: {sorted(stray)[:5]}")
    teams["key"] = teams["game_id"].astype(str) + ":" + teams["team_id"].astype(str)
    index = {k: i for i, k in enumerate(teams["key"])}
    n = len(teams)
    other = teams.groupby("game_id")["key"].transform(lambda k: k.iloc[::-1].to_numpy())
    opp = other.map(index).to_numpy()
    game = pd.factorize(teams["game_id"])[0]
    first_in_game = ~teams["game_id"].duplicated().to_numpy()

    shots, goals = _team_sums(skaters, s_team, index, n)
    opp_shots, opp_goals = shots[opp], goals[opp]

    # The result, decided once per game so both sides agree: one coin for a shootout, one for OT.
    coin = rng.random((game.max() + 1, sims))[game]
    ot_draw = rng.random((game.max() + 1, sims))[game]
    diff = goals - opp_goals
    tie = diff == 0
    first = first_in_game[:, None]
    win = (diff > 0) | (tie & ((coin < 0.5) == first))
    extra = tie | ((np.abs(diff) == 1) & (ot_draw < fit.p_ot))     # the loser takes an OT loss

    # Empty-net goals are the WINNER's, scored on the loser's empty net, so they come off the
    # loser's goals against. Only goals beyond the first-goal margin can be empty-netters.
    margin = np.abs(diff)
    en = rng.binomial(np.maximum(margin - 1, 0), fit.en_rate(margin))
    en_against = np.where(~win & ~tie, en, 0)                        # this team lost: EN on us
    ga_team = opp_goals - en_against
    saves_team = np.maximum(opp_shots - en_against - ga_team, 0)

    # Pull, on the team's goals against, then the pulled starter's share of the line.
    pull_p = fit.pull_by_ga[np.minimum(ga_team, len(fit.pull_by_ga) - 1)]
    pulled = rng.random((n, sims)) < pull_p
    starter_ga = np.where(pulled, rng.binomial(ga_team, fit.share_ga), ga_team)
    starter_saves = np.where(pulled, rng.binomial(saves_team, fit.share_saves), saves_team)
    keeps = np.where(pulled, rng.random((n, sims)) < np.where(win, fit.keeps_win, fit.keeps_loss),
                     True)
    w = win & keeps
    otl = ~win & extra & keeps
    lo = ~win & ~extra & keeps
    shutout = win & ~pulled & (starter_ga == 0)

    # One starter per team-game: categorical on P(start), normalized within the team-game.
    g_index = g_team.map(index).to_numpy()
    p = goalies[p_column].to_numpy("float64").clip(0.0, None)
    totals = np.bincount(g_index, weights=p, minlength=n)
    order = np.argsort(g_index, kind="stable")
    cum = np.empty(len(goalies))
    running = pd.Series(p[order]).groupby(g_index[order]).cumsum().to_numpy()
    cum[order] = running
    cum = cum / np.where(totals[g_index] > 0, totals[g_index], 1.0)
    prev = cum - p / np.where(totals[g_index] > 0, totals[g_index], 1.0)
    u = rng.random((n, sims))[g_index]
    started = (prev[:, None] < u) & (u <= cum[:, None]) & (totals[g_index] > 0)[:, None]

    def per_row(values):
        return np.where(started, values[g_index], 0).astype(np.int16)

    counts = {"wins": per_row(w.astype(np.int16)), "losses": per_row(lo.astype(np.int16)),
              "ot_losses": per_row(otl.astype(np.int16)),
              "shutouts": per_row(shutout.astype(np.int16)),
              "saves": per_row(starter_saves), "goals_against": per_row(starter_ga),
              "pulled": per_row(pulled.astype(np.int16))}
    keys = goalies[[c for c in ("season_id", "game_id", "game_date", "team_id", "player_id")
                    if c in goalies.columns]].copy()
    keys["position"] = "G"
    return Draws(keys, counts, started)

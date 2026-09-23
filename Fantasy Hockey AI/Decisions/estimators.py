"""Projections a manager computes for himself, as opposed to the ones the modelling stack hands him.

Only one lives here so far: rung 3's box-score estimator. It is decision logic rather than
harness logic because it is a *policy's* view of the world -- what a manager with no models would
compute from a box score. What it is allowed to read is still the harness's job: the caller hands
it the actuals through yesterday and nothing later, which is where the leakage boundary sits.
"""

import pandas as pd


class NaiveHistory:
    """Rung 3's whole projection: two numbers a manager could read off a box score.

    `rate[p]`  fantasy points per game **he dressed for**, shrunk toward last season
    `dress[p]` share of his team's games he has actually dressed for, shrunk toward the league rate

    The second one is not optional, and leaving it out was the single largest bug in this harness.
    Section 3 established that P(plays) multiplies everything -- a projection is worth zero if he
    is a healthy scratch -- and a streamer that ranks acquisitions by *his team's* games remaining
    rather than by his own expected appearances buys thirteenth forwards on four-game weeks. It
    then starts them, so it converts moves into empty and wasted slot-nights and loses to a manager
    who simply never transacts. The naive form of P(plays) costs one extra counter to carry.

    Still no model: no features, no learning, nothing the projection stack produces. Section 16
    wants rung 3 to isolate how much of the edge is the schedule rather than the modelling, and a
    games-played share is arithmetic on a box score.
    """

    def __init__(self, rate: dict, dress: dict, league_dress: float):
        self.rate = rate
        self.dress = dress
        self.league_dress = league_dress

    def value(self, player_id) -> float:
        """Expected fantasy points from one of his team's games -- the rate a slot earns."""
        return (self.rate.get(player_id, 0.0)
                * self.dress.get(player_id, self.league_dress))

    def get(self, player_id, default=0.0) -> float:
        """So a `NaiveHistory` can stand in wherever a plain rate dict was expected."""
        return self.value(player_id) if player_id in self.rate else default


def naive_history(actuals_to_date: pd.DataFrame, prior_season: dict, scoreset,
                  shrink_games=10.0, shrink_dress=10.0) -> NaiveHistory:
    """Season-to-date rate and dress share, both shrunk -- rung 3's projection.

    Shrinkage only exists to stop a one-game sample outranking a season:
    `(points + k * prior_rate) / (games + k)`, and the same form for the dress share.
    """
    prior = {int(p): float(v) for p, v in prior_season.items()}
    if not len(actuals_to_date):
        return NaiveHistory(prior, {}, 1.0)

    played = actuals_to_date[actuals_to_date["target_played"].astype(bool)]
    points = pd.Series(scoreset.score_columns(played, prefix="target_"), index=played.index)
    scored = points.groupby(played["player_id"]).agg(["sum", "count"])

    # Dressed over *offered*: rows in the candidate universe are his team's games, so the ratio is
    # his share of appearances. Counted this way round on purpose -- the rest-of-season work found
    # that weighting availability by games played rather than attempts makes a player passed over
    # forty times look like a player with no evidence.
    offered = actuals_to_date.groupby("player_id")["target_played"].agg(["sum", "count"])
    league_dress = float(offered["sum"].sum() / max(offered["count"].sum(), 1))

    rate, dress = dict(prior), {}
    for player_id, row in scored.iterrows():
        base = prior.get(int(player_id), 0.0)
        rate[int(player_id)] = float((row["sum"] + shrink_games * base)
                                     / (row["count"] + shrink_games))
    for player_id, row in offered.iterrows():
        dress[int(player_id)] = float((row["sum"] + shrink_dress * league_dress)
                                      / (row["count"] + shrink_dress))
    return NaiveHistory(rate, dress, league_dress)

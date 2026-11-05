#!/usr/bin/env python
"""Does a predictor change predict better? Scored on outcomes, with no league in the way.

    python predictor_accuracy.py goalie_line

Predictor changes are decided here; a league A/B only checks for harm. Squared error, not MAE: the
plans use expected values, and on skewed fantasy scoring MAE rewards a prediction for running low.
`paired` is the shared comparison: whole weeks are resampled, so a night's correlated results and a
slow drift through the season don't pass for independent evidence. To score another predictor, give
it a frame of week, actual, current, candidate and add a subcommand.

goalie_line: the flat line of points per start, "history" (every earlier season's mean) against
"season" (inputs.SeasonGoalieLine). Each start is scored against the line known that morning and
six days earlier, the far end of a week plan; each week's line against the rest of the season's mean.
"""

import argparse
import logging

import numpy as np
import pandas as pd

import corelayer  # noqa: F401 -- puts Core/ on sys.path; see corelayer.py
import inputs
import paths
import simlayer

SEASONS = {"2024-25": "choose", "2025-26": "confirm", "2026-27": "live, so far"}
LEAGUES = ("beagles", "espn-la")
RESAMPLES = 2000
MIN_REMAINING_STARTS = 200   # rest-of-season means below this are mostly noise


def resample_weights(weeks: int, seed: int = 0) -> np.ndarray:
    """How many times each week appears in each resample."""
    return np.random.default_rng(seed).multinomial(weeks, np.full(weeks, 1 / weeks), RESAMPLES)


def paired(frame: pd.DataFrame) -> dict:
    """Current against candidate, on the same outcomes. mse_change < 0: the candidate is better."""
    error_a, error_b = frame["current"] - frame["actual"], frame["candidate"] - frame["actual"]
    by_week = pd.DataFrame({"week": frame["week"], "n": 1.0,
                            "change": error_b ** 2 - error_a ** 2}).groupby("week").sum()
    weights = resample_weights(len(by_week))
    draws = weights @ by_week["change"].to_numpy() / (weights @ by_week["n"].to_numpy())
    return {"n": len(frame), "bias_current": error_a.mean(), "bias_candidate": error_b.mean(),
            "mse_current": (error_a ** 2).mean(), "mse_candidate": (error_b ** 2).mean(),
            "mse_change": by_week["change"].sum() / by_week["n"].sum(),
            "low": np.percentile(draws, 2.5), "high": np.percentile(draws, 97.5)}


def goalie_line_ros(starts: pd.DataFrame, current: float, prior: float, prior_starts: float) -> dict:
    """Each week's line against the mean of all the starts still to come. The resampling recomputes
    both from the chosen weeks, since a week's starts feed the later line and the earlier targets."""
    weekly = starts.groupby("week")["actual"].agg(["sum", "count"]).sort_index()
    points, counts = weekly["sum"].to_numpy("float64"), weekly["count"].to_numpy("float64")

    def errors(mult):
        """Root mean square error of (history, season) over the weeks with enough starts left."""
        s, n = points * mult, counts * mult
        done_s, done_n = np.concatenate([[0], s.cumsum()[:-1]]), np.concatenate([[0], n.cumsum()[:-1]])
        left_s, left_n = s.sum() - done_s, n.sum() - done_n
        keep = left_n >= MIN_REMAINING_STARTS
        target = left_s[keep] / left_n[keep]
        line = (prior_starts * prior + done_s[keep]) / (prior_starts + done_n[keep])
        return (np.sqrt(((current - target) ** 2).mean()), np.sqrt(((line - target) ** 2).mean()),
                int(keep.sum()))

    rmse_current, rmse_candidate, weeks = errors(np.ones(len(weekly)))
    if not weeks:
        return {}
    draws = []
    for mult in resample_weights(len(weekly)):
        history_rmse, season_rmse, kept = errors(mult)
        draws.append(season_rmse - history_rmse if kept else np.nan)
    return {"weeks": weeks, "rmse_current": rmse_current, "rmse_candidate": rmse_candidate,
            "low": np.nanpercentile(draws, 2.5), "high": np.nanpercentile(draws, 97.5)}


def goalie_line_frame(season: str, league: str):
    """One row per start, with the line each side held that morning and six days earlier."""
    scoreset = simlayer.load_scoreset(league)
    history, starts = inputs.load_goalie_history(season), inputs.load_goalie_starts(season)
    current = inputs.goalie_line(history, scoreset)[0]
    line = inputs.SeasonGoalieLine(history, starts, scoreset)
    started = starts[starts["is_starter"].astype(bool)]
    frame = pd.DataFrame({"day": started["game_date"].to_numpy(),
                          "player_id": started["player_id"].to_numpy(),
                          "actual": scoreset.score_columns(started, side="goalies")})
    first = frame["day"].min()
    frame["week"] = (frame["day"] - first).dt.days // 7
    for offset, name in ((0, "season_now"), (6, "season_6")):
        by_day = {d: line.at(d - pd.Timedelta(days=offset)) for d in frame["day"].unique()}
        frame[name] = frame["day"].map(by_day)
    frame["current"] = current
    return frame, current, line


def show(label: str, r: dict) -> None:
    print(f"  {label:<22}{r['n']:>6}  bias {r['bias_current']:+.3f} -> {r['bias_candidate']:+.3f}   "
          f"MSE {r['mse_current']:.3f} -> {r['mse_candidate']:.3f}   "
          f"change {r['mse_change']:+.4f} [{r['low']:+.4f}, {r['high']:+.4f}]")


def score_goalie_line() -> None:
    for season, use in SEASONS.items():
        if not paths.goalie_starts(season).exists():
            print(f"\n{season}: no goalie starts file")
            continue
        for league in LEAGUES:
            frame, current, line = goalie_line_frame(season, league)
            print(f"\n{season} ({use}), {league}: history {current:.3f}, last season {line.prior:.3f}, "
                  f"actual {frame['actual'].mean():.3f}, sd {frame['actual'].std():.2f}")
            as_of = lambda column: frame.assign(candidate=frame[column])
            show("per start, that day", paired(as_of("season_now")))
            show("per start, 6 days out", paired(as_of("season_6")))
            today = as_of("season_now")
            thirds = pd.cut(today["week"], 3, labels=["early", "middle", "late"])
            for name in ("early", "middle", "late"):
                show(f"  {name} season", paired(today[thirds == name]))
            # A slice, not a forecast: the line is league-wide, but the goalies people roster are
            # the busy ones, and the line could be right on average and wrong for them.
            busy = today["player_id"].value_counts().head(32).index
            show("  top 32 by starts", paired(today[today["player_id"].isin(busy)]))
            ros = goalie_line_ros(frame, current, line.prior, line.prior_starts)
            if ros:
                print(f"  rest of season, {ros['weeks']} weeks: RMSE {ros['rmse_current']:.3f} -> "
                      f"{ros['rmse_candidate']:.3f}, change "
                      f"{ros['rmse_candidate'] - ros['rmse_current']:+.3f} "
                      f"[{ros['low']:+.3f}, {ros['high']:+.3f}]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("predictor", choices=["goalie_line"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    score_goalie_line()

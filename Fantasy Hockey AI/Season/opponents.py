#!/usr/bin/env python
"""Realistic opponents (T2): real managers' pickup patterns, replayed by rung 8.

    python opponents.py --summary                 # 12088's measured behaviour, the targets
    python opponents.py --validate --replications 8   # simulated opponents against those targets

The sister league 12088 (Fleaflicker, same 14-team format and rules as 12090, different managers)
publishes every transaction. `ModelFeatures/build_fleaflicker_transactions.py` crawls it into
`fleaflicker_transactions_12088.parquet`. Here each real team-season becomes a **profile**: for
every week of its season, the weekdays it picked someone up (an add or a waiver claim) and
whether each was a goalie. An opponent seat (`Decisions/managers.Opponent`) is handed one profile,
drawn by (replication, seat), and replays its timing; which player it takes is its own noisy
box-score judgement.

Weeks line up as the simulator numbers them: `schedule.from_candidates` on that season's games,
so a thin opening week (2024-25's Prague games) folds into the next exactly as the engine folds
it. Pickups before week 1 are dropped -- pre-season moves are free and the simulation starts at
the opener.

**Calibration.** Activity, its spread across managers, the second-half fade, at-cap and idle
weeks, goalie share and day-of-week timing come with the replay. One parameter is fitted: the
valuation noise `noise_sd`, so that simulated pickups score about what 12088's did (points per
game the added player scored over the next 14 days). News lag -- how late real managers react to
injuries -- is not modelled yet; opponents see injuries the moment the engine does.
"""

import argparse
import logging
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

import league as league_module
import paths
import schedule as schedule_module

log = logging.getLogger("opponents")

LEAGUE_ID = 12088
SEASONS = ("2023-24", "2024-25", "2025-26")
PICKUPS = ("added", "claimed")
SEED = 881208
# Fitted 2026-09-29 on 2024-25 (2 drafts a setting): simulated pickups scored 3.27 / 3.24 / 3.15
# points per game over the next 14 days at 0.25 / 0.5 / 1.0, against 3.13 for 12088's real ones.
NOISE_SD = 1.0


@dataclass(frozen=True)
class OpponentField:
    """What `ladder.run_one` needs to set every rung-8 seat up: the profiles and the noise."""

    profiles: tuple        # of dicts: key, weeks {week: ((weekday, is_goalie), ...)}
    noise_sd: float

    def attach(self, manager, replication) -> None:
        rng = np.random.default_rng([SEED, int(replication), int(manager.team_index)])
        profile = self.profiles[int(rng.integers(len(self.profiles)))]
        manager.attach(profile, self.noise_sd, (SEED, int(replication), int(manager.team_index)))


def transactions(league_id=LEAGUE_ID) -> pd.DataFrame:
    path = paths.FEATURES_DIR / f"fleaflicker_transactions_{league_id}.parquet"
    if not path.exists():
        raise SystemExit(f"{path} is missing -- run ModelFeatures/build_fleaflicker_transactions.py "
                         f"--league {league_id}")
    frame = pd.read_parquet(path)
    frame["league_day"] = pd.to_datetime(frame["league_day"])
    return frame


def season_calendar(season, config):
    """The simulator's week numbering for a season, from its games."""
    games = pd.read_parquet(paths.base_table(season), columns=["game_id", "game_date", "team_id"])
    games["game_date"] = pd.to_datetime(games["game_date"])
    return schedule_module.from_candidates(games.drop_duplicates(["game_id", "team_id"]),
                                           config.week_starts_on, config.min_first_week_games)


def pickups(config, league_id=LEAGUE_ID, seasons=SEASONS) -> pd.DataFrame:
    """Every in-season pickup, with the simulator's week number."""
    frame = transactions(league_id)
    frame = frame[frame["action"].isin(PICKUPS) & frame["season"].isin(seasons)].copy()
    out = []
    for season, rows in frame.groupby("season"):
        calendar = season_calendar(season, config)
        first = min(calendar.days)
        rows = rows[rows["league_day"] >= first].copy()
        rows["week"] = [calendar.week_of(d) or _week_after(calendar, d) for d in rows["league_day"]]
        out.append(rows)
    frame = pd.concat(out, ignore_index=True)
    frame["weekday"] = frame["league_day"].dt.weekday
    frame["goalie"] = frame["position"].eq("G")
    return frame


def _week_after(calendar, day):
    """A day the NHL calendar has no games on (an all-star break) still belongs to a week."""
    later = [d for d in calendar.days if d >= day]
    return calendar.week_of(later[0]) if later else None


def profiles(config, league_id=LEAGUE_ID, seasons=SEASONS) -> tuple:
    frame = pickups(config, league_id, seasons)
    teams = transactions(league_id)
    teams = teams[teams["season"].isin(seasons)][["season", "team_id"]].drop_duplicates()
    out = []
    for (season, team), _ in teams.groupby(["season", "team_id"]):
        rows = frame[(frame["season"] == season) & (frame["team_id"] == team)]
        weeks = {int(w): tuple(sorted(zip(g["weekday"].astype(int), g["goalie"].astype(bool))))
                 for w, g in rows.dropna(subset=["week"]).groupby("week")}
        out.append({"key": f"{season}/{team}", "weeks": weeks})
    return tuple(out)


def weekly_counts(frame, teams_weeks) -> pd.Series:
    """Pickups per team-week, zero-filled over every (team, week) in `teams_weeks`."""
    counts = frame.groupby(["unit", "week"]).size()
    return counts.reindex(teams_weeks, fill_value=0)


def describe(counts: pd.Series, frame: pd.DataFrame, cap: int, mid_week: int) -> dict:
    by_unit = counts.groupby(level=0).mean()
    early = counts[counts.index.get_level_values(1) <= mid_week].mean()
    late = counts[counts.index.get_level_values(1) > mid_week].mean()
    days = frame["weekday"].value_counts(normalize=True).reindex(range(7), fill_value=0)
    return {"team_weeks": int(len(counts)),
            "pickups_per_week": round(float(counts.mean()), 2),
            "median": float(counts.median()),
            "at_cap": round(float((counts >= cap).mean()), 3),
            "zero": round(float((counts == 0).mean()), 3),
            "team_mean_min": round(float(by_unit.min()), 2),
            "team_mean_max": round(float(by_unit.max()), 2),
            "first_half": round(float(early), 2), "second_half": round(float(late), 2),
            "goalie_share": round(float(frame["goalie"].mean()), 3),
            "monday": round(float(days[0]), 3), "sunday": round(float(days[6]), 3)}


def real_summary(config, weeks) -> dict:
    frame = pickups(config)
    frame = frame[frame["week"].between(1, weeks)]
    frame["unit"] = frame["season"] + "/" + frame["team_id"].astype(str)
    units = transactions()
    units = units[units["season"].isin(SEASONS)]
    units = sorted(set(units["season"] + "/" + units["team_id"].astype(str)))
    index = pd.MultiIndex.from_product([units, range(1, weeks + 1)], names=["unit", "week"])
    return describe(weekly_counts(frame, index), frame, config.moves_per_week, weeks // 2)


def pickup_quality(frame, actuals_points, days=14) -> float:
    """Mean fantasy points per game the added player scored over the next `days` days (games he
    played only) -- how good the pickups were, whoever made them."""
    out = []
    for season, rows in frame.groupby("season"):
        pts = actuals_points[season]
        by_player = {p: g for p, g in pts.groupby("player_id")}
        for player, day in zip(rows["player_id"], rows["day"]):
            games = by_player.get(int(player)) if pd.notna(player) else None
            if games is None:
                continue
            window = games[(games["game_date"] >= day)
                           & (games["game_date"] < day + pd.Timedelta(days=days))]
            if len(window):
                out.append(float(window["points"].mean()))
    return float(np.mean(out)) if out else float("nan")


def played_points(season, scoreset) -> pd.DataFrame:
    """Fantasy points in every game a player dressed for, skaters and goalies -- as the engine
    scores a night."""
    import inputs

    skaters = inputs.load_actuals(season)
    skaters = skaters[skaters["target_played"].astype(bool)].copy()
    skaters["points"] = scoreset.score_columns(skaters, prefix="target_")
    goalies = inputs.load_goalie_starts(season)
    goalies = goalies[goalies["appeared"].astype(bool)].copy()
    goalies["points"] = scoreset.score_columns(goalies, side="goalies")
    columns = ["player_id", "game_date", "points"]
    both = pd.concat([skaters[columns], goalies[columns]], ignore_index=True)
    both["player_id"] = both["player_id"].astype(int)
    return both


def simulate(ctx, strategy, noise_sd, replications, workers=None):
    """The realistic field -- our shipped system in one seat, rung 8 in the other thirteen --
    returning the opponents' pickups, one row each."""
    from types import SimpleNamespace

    import ladder
    import oneseat
    import opponents as module      # not __main__'s copy: worker processes unpickle it by name

    ctx.data["opponent_field"] = module.OpponentField(profiles(ctx.config), noise_sd)
    layout = oneseat.OneSeat(oneseat.SHIPPED, (8,))
    args = SimpleNamespace(replications=replications, workers=workers, verbose_weeks=False,
                           decision_sims=200)
    runs = ladder.run_replications(args, ctx.config, ctx.calendar, ctx.data, ctx.eligibility,
                                   ctx.scoreset, layout, strategy)
    rows = []
    for r, run in enumerate(runs):
        mine = layout.test_seat(ctx.config, r)
        moves = run["transactions"]
        moves = moves[moves["team"] != mine]
        rows.append(moves.assign(replication=r))
    frame = pd.concat(rows, ignore_index=True)
    frame["unit"] = frame["replication"].astype(str) + "/" + frame["team"].astype(str)
    frame["day"] = pd.to_datetime(frame["date"])
    frame["weekday"] = frame["day"].dt.weekday
    frame["goalie"] = [("G" in ctx.eligibility.get(p, ())) for p in frame["player_id"]]
    frame["season"] = ctx.season
    units = [f"{r}/{s}" for r in range(replications) for s in range(ctx.config.teams)
             if s != layout.test_seat(ctx.config, r)]
    return frame, units, runs


def validate(args):
    import tune
    from decisionlayer import load_strategy

    strategy = load_strategy(args.strategy)
    ctx = tune.Context(args.season, tune.previous(args.season), args.league, args.weights,
                       strategy, args.workers)
    config = ctx.config
    weeks = config.regular_season_weeks_in(ctx.calendar) + config.playoff_weeks
    points = {args.season: played_points(args.season, ctx.scoreset)}

    real = pickups(config, seasons=(args.season,))
    real = real[real["week"].between(1, weeks)].rename(columns={"league_day": "day"})
    real["unit"] = real["team_id"].astype(str)
    units = sorted(transactions().query("season == @args.season")["team_id"].astype(str).unique())
    index = pd.MultiIndex.from_product([units, range(1, weeks + 1)], names=["unit", "week"])
    target = describe(weekly_counts(real, index), real, config.moves_per_week, weeks // 2)
    target["pickup_pts_per_game"] = round(pickup_quality(real, points), 3)
    table = [{"source": f"12088 {args.season}", **target}]

    for sd in args.noise:
        sim, sim_units, _ = simulate(ctx, strategy, sd, args.replications, args.workers)
        sim = sim[sim["week"].between(1, weeks)]
        index = pd.MultiIndex.from_product([sim_units, range(1, weeks + 1)],
                                           names=["unit", "week"])
        got = describe(weekly_counts(sim, index), sim, config.moves_per_week, weeks // 2)
        got["pickup_pts_per_game"] = round(pickup_quality(sim, points), 3)
        table.append({"source": f"rung 8, noise_sd {sd:g}", **got})
        log.info("noise %g: %s", sd, got)
    frame = pd.DataFrame(table).set_index("source").T
    print(frame.to_string())
    return frame


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--league", default="league")
    p.add_argument("--summary", action="store_true")
    p.add_argument("--validate", action="store_true")
    p.add_argument("--season", default="2024-25")
    p.add_argument("--weights", default="points-league")
    p.add_argument("--strategy", default="strategy-beagles")
    p.add_argument("--noise", type=float, nargs="+", default=[NOISE_SD])
    p.add_argument("--replications", type=int, default=4)
    p.add_argument("--workers", type=int, default=None)
    return p.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        stream=sys.stderr)
    for noisy in ("inputs", "draft", "draftroom", "engine", "simlayer", "ladder", "schedule"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    config = league_module.load(args.league)
    weeks = 24 + config.playoff_weeks
    if args.summary:
        print(real_summary(config, weeks))
        ps = profiles(config)
        print(f"{len(ps)} team-season profiles")
    if args.validate:
        validate(args)


if __name__ == "__main__":
    main()

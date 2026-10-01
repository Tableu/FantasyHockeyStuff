#!/usr/bin/env python
"""Realistic opponents (T2): real managers' weekly activity, our machinery, their own opinions.

    python opponents.py --summary                 # 12088's measured behaviour, the targets
    python opponents.py --validate --replications 8   # simulated opponents against those targets

The sister league 12088 (Fleaflicker, same 14-team format and rules as 12090, different managers)
publishes every transaction. `ModelFeatures/build_fleaflicker_transactions.py` crawls it into
`fleaflicker_transactions_12088.parquet`. Here each real team-season becomes a **profile**: for
every week of its season, the weekdays it picked someone up (an add or a waiver claim) and
whether each was a goalie. An opponent seat (`Decisions/managers.Opponent`, rung 8) is handed one
profile, drawn by (replication, seat), and may spend each week only as many moves as that manager
made pickups; within that budget it runs our orchestrator (rung 7) on `strategy.json`'s add/drop
and streaming blocks, reading every projection through its own persistent per-player error.

Weeks line up as the simulator numbers them: `schedule.from_candidates` on that season's games,
so a thin opening week (2024-25's Prague games) folds into the next exactly as the engine folds
it. Pickups before week 1 are dropped -- pre-season moves are free and the simulation starts at
the opener.

**Calibration.** The weekly budget carries activity, its spread across managers and the fade
after January. The profile's goalie pickups are replayed on their weekdays (a likely starter
streamed, `managers.Opponent._stream_goalie`, since 2026-09-30: the orchestrator alone took 15%
goalies against 26%); the rest of the days and players are the orchestrator's own.
One parameter is fitted: the error `sd`, so the opponents score what 12088's managers did per NHL
game day (Fleaflicker's standings; its matchups are not the simulator's weeks, so points per game
day is the comparable unit). Pickup and cut quality -- points per game the added or dropped
player scored over the next 14 days -- are reported beside it as a check. News lag is measured
(Season/README) but not modelled; opponents see injuries the moment the engine does.

History (2026-09-29): a first rung 8 replayed real managers' pickup DAYS and chose the player on
a noisy box score. It matched activity and pickup quality but topped out at 28.2 points per game
day against the real 30.2 -- with our projections, a gate, full-source drafts or the confirmed
starting goalie -- and its noise made it cut better players than it added.
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
# 12088's regular-season points for per team-week, from Fleaflicker's FetchLeagueStandings
# (2026-09-29): mean and sd over the fourteen teams. Its scoring is 12090's exactly
# (FetchLeagueRules), so it is the strength a realistic opponent has to match.
REAL_POINTS_PER_WEEK = {"2023-24": (206.8, 18.9), "2024-25": (209.9, 15.8),
                        "2025-26": (208.7, 13.5)}
# Per NHL game day, the comparable number: Fleaflicker's matchups are not the simulator's weeks
# (2024-25: 21 regular matchups over Oct 4 - Mar 16, a 10-day first and a 14-day 4 Nations one,
# against the simulator's 24 weeks to Mar 30). 209.9 x 21 matchups / 146 game days.
REAL_POINTS_PER_GAME_DAY = {"2024-25": 30.19}
# The error on the projections each opponent reads, fitted to REAL_POINTS_PER_GAME_DAY on
# 2024-25 (2 drafts a setting, 2026-09-29). Any error only weakens them -- sd 0.5 scored 27.2
# points per game day against 29.3 at 0 on strategy.json's blocks -- so the fit is 0: a real
# 12088 manager plays about as well as our projections read straight.
SD = 0.0
# The add/drop and streaming blocks every opponent runs: strategy.json's, plus these. On
# strategy.json's alone (2 streaming spots, no goalie rentals) the opponents spent 3.7 of a
# 4.5-pickup real budget, hit the cap in 20% of weeks against 44%, took 1.7% goalies against 26%
# and scored 29.3; with these -- espn-la's live choices, frozen here so editing that league's file
# cannot move the opponents -- 4.65 pickups, 46% at the cap, 15% goalies, 29.8 against the real
# 30.2. Real managers stream hard.
OPPONENT_STRATEGY = "strategy"
OPPONENT_OVERRIDES = {"adddrop": {"tail": "cost"},
                      "streaming": {"mode": "week", "goalies": True, "spots": 99}}


@dataclass(frozen=True)
class OpponentField:
    """What `ladder.run_one` needs to set every rung-8 seat up: the profiles and the noise."""

    profiles: tuple        # of dicts: key, weeks {week: ((weekday, is_goalie), ...)}
    sd: float
    adddrop: object        # the opponents' own blocks (OPPONENT_STRATEGY)
    streaming: object

    def describe(self) -> str:
        return f"sd{self.sd:g}"

    def attach(self, manager, replication) -> None:
        import dataclasses

        seed = (SEED, int(replication), int(manager.team_index))
        profile = self.profiles[int(np.random.default_rng(list(seed)).integers(len(self.profiles)))]
        own = dataclasses.replace(manager.strategy, adddrop=self.adddrop, streaming=self.streaming)
        manager.attach(profile, self.sd, seed, own)


def field(config, sd=SD, strategy_name=OPPONENT_STRATEGY) -> OpponentField:
    """The opponents for `config`'s league, as `ladder.run_one` expects them in
    data["opponent_field"]."""
    from decisionlayer import load_strategy

    import opponents as module      # not __main__'s copy: worker processes unpickle it by name

    import dataclasses

    own = load_strategy(strategy_name)
    if strategy_name == OPPONENT_STRATEGY:
        own = dataclasses.replace(
            own, adddrop=dataclasses.replace(own.adddrop, **OPPONENT_OVERRIDES["adddrop"]),
            streaming=dataclasses.replace(own.streaming, **OPPONENT_OVERRIDES["streaming"]))
    return module.OpponentField(profiles(config), float(sd), own.adddrop, own.streaming)


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


def simulate(ctx, strategy, sd, replications, workers=None, opponent_strategy=OPPONENT_STRATEGY):
    """The realistic field -- our shipped system in one seat, rung 8 in the other thirteen --
    returning the opponents' pickups, one row each."""
    from types import SimpleNamespace

    import ladder
    import oneseat

    ctx.data["opponent_field"] = field(ctx.config, sd, opponent_strategy)
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


def test_seat(ctx, runs) -> pd.Series:
    """Our seat's regular-season points per week, one per draft."""
    import oneseat

    layout = oneseat.OneSeat(oneseat.SHIPPED, (8,))
    out = []
    for r, run in enumerate(runs):
        teams = run["teams"]
        teams = teams[teams["seat"] == layout.test_seat(ctx.config, r)]
        out.append(teams["points"] / teams["weeks"])
    return pd.concat(out, ignore_index=True)


def team_strength(ctx, runs) -> pd.Series:
    """Each opponent seat's regular-season points per week (the test seat left out)."""
    import oneseat

    layout = oneseat.OneSeat(oneseat.SHIPPED, (8,))
    out = []
    for r, run in enumerate(runs):
        teams = run["teams"]
        teams = teams[teams["seat"] != layout.test_seat(ctx.config, r)]
        out.append(teams["points"] / teams["weeks"])
    return pd.concat(out, ignore_index=True)


def validate(args):
    import tune
    from decisionlayer import load_strategy

    import field as field_module

    strategy = load_strategy(args.strategy)
    field_config = field_module.with_overrides(field_module.load(), None, args.opponent_sources)
    ctx = tune.Context(args.season, tune.previous(args.season), args.league, args.weights,
                       strategy, args.workers, field_config=field_config)
    log.info("field: %s", field_config.describe())
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
    cuts = transactions().query("season == @args.season and action == 'cut'").copy()
    cuts = cuts[cuts["league_day"] >= min(ctx.calendar.days)].rename(columns={"league_day": "day"})
    target["cut_pts_per_game"] = round(pickup_quality(cuts, points), 3)
    target["team_pts_per_week"], target["team_pts_sd"] = REAL_POINTS_PER_WEEK[args.season]
    regular = config.regular_season_weeks_in(ctx.calendar)
    sim_days = sum(len(ctx.calendar.days_in(w)) for w in range(1, regular + 1))
    target["team_pts_per_game_day"] = REAL_POINTS_PER_GAME_DAY.get(args.season)
    target["test_seat_pts_per_week"] = None
    table = [{"source": f"12088 {args.season}", **target}]

    for sd in args.sd:
        sim, sim_units, runs = simulate(ctx, strategy, sd, args.replications, args.workers,
                                        args.opponent_strategy)
        sim = sim[sim["week"].between(1, weeks)]
        index = pd.MultiIndex.from_product([sim_units, range(1, weeks + 1)],
                                           names=["unit", "week"])
        got = describe(weekly_counts(sim, index), sim, config.moves_per_week, weeks // 2)
        got["pickup_pts_per_game"] = round(pickup_quality(sim, points), 3)
        dropped = sim.dropna(subset=["dropped"]).drop(columns="player_id").rename(
            columns={"dropped": "player_id"})
        got["cut_pts_per_game"] = round(pickup_quality(dropped, points), 3)
        strength = team_strength(ctx, runs)
        got["team_pts_per_week"] = round(float(strength.mean()), 1)
        got["team_pts_sd"] = round(float(strength.std()), 1)
        got["team_pts_per_game_day"] = round(float(strength.mean()) * regular / sim_days, 2)
        got["test_seat_pts_per_week"] = round(float(test_seat(ctx, runs).mean()), 1)
        table.append({"source": f"rung 8, sd {sd:g}", **got})
        log.info("sd %g: %s", sd, got)
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
    p.add_argument("--opponent-sources", default=None,
                   help="Sources each opponent drafts from, e.g. 10 or 1-3 (field.json default)")
    p.add_argument("--opponent-strategy", default=OPPONENT_STRATEGY,
                   help="Strategy file whose add/drop and streaming blocks the opponents run")
    p.add_argument("--sd", type=float, nargs="+", default=[SD],
                   help="The error on the projections each opponent reads (one run each)")
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

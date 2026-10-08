"""The live runner: the shipped manager (rung 7, `managers.Orchestrated`) on the real league.

The ladder runs a manager against a `SlateView` built from a replayed season. This builds the same
view from today instead and hands it to the same manager, so live play and the backtests share
every decision rule:

    league      a LeagueSnapshot: the 14 rosters, IR, this week's moves, waivers, the matchup
                -- from the platform (Fleaflicker, ESPN) after the draft, or from a JSON file (`--league-file`),
                which is how the runner is exercised before rosters exist (`make_fake_league`)
    tonight     Projections/reports/live/tonight_{date}.parquet (project_tonight.py): skater
                p_plays and lambdas, goalie P(start) with the Daily Faceoff overrides
    status      ModelFeatures/data/live/tonight_{date}_status.parquet: every reported player's
                merged injury status (Live.PlayerStatus), for IR -- a rostered player can be
                out while his team is idle tonight
    rates       each player's latest projected points per game (the newest tonight file that has
                him), seeded from the preseason consensus board; rest-of-season points per team
                game from that board until an in-season rest-of-season build exists

The manager acts on a copy of the league state, and the plan is the difference between that copy
and the snapshot: IR moves, adds and drops, claims, and tonight's lineup. Nothing is sent to
Fleaflicker -- its API is read-only; the plan is a file the user acts on.

**Per-game lock.** Fleaflicker locks each player at his own game's start. Players whose game has
started keep the slot the snapshot says they hold, and the lineup is re-solved over the remaining
slots and players -- `slots.assign` has no pinning, so the locked slots are removed instead.
"""

import copy
import dataclasses
import datetime as dt
import json
import logging
import statistics
from pathlib import Path

import numpy as np
import pandas as pd

import seasonlayer  # noqa: F401 -- puts Season/ on sys.path; see seasonlayer.py
import livepaths
import draft_board
import choices as choices_module
import engine as engine_module
import inputs
import league as league_module
import paths
import schedule as schedule_module
import simlayer
import state as state_module
import sync_league_settings
import view as view_module
from decisionlayer import adddrop as adddrop_module
from decisionlayer import draft as draft_module
from decisionlayer import estimators as estimators_module
from decisionlayer import load_strategy
from decisionlayer import managers as managers_module
from decisionlayer import slots as slots_module
from decisionlayer import valuation as valuation_module
from decisionlayer import weekplan as weekplan_module

log = logging.getLogger("live")

TONIGHT_DIR = paths.PROJECTIONS_REPORTS / "live"
STATUS_DIR = paths.FEATURES_DIR.parent / "live"
INJURED = ("OUT", "SUSP")
DECISION_SIMS = 400
FREE_AGENTS_SHOWN = 60
OPTIONS_PRICED = 25        # free agents the options price on the roster (the rule itself prices 10)
OPTIONS_SHOWN = 15         # pickups listed, each with its best drop
# Week plans shown beside the one made, each built around a different pickup (weekplan.run):
# ten plans in all, where that many are worth a move.
WEEK_ALTERNATIVES = 9
# A logged move's kind as the plan names it (managers.repair_roster, adddrop, streaming).
MOVE_KINDS = {"repair": "Repair", "add": "Upgrade", "claim": "Claim", "rental": "Rental",
              "rental claim": "Rental claim"}
PLATFORM_NAMES = {"fleaflicker": "Fleaflicker", "espn": "ESPN"}     # as the plan footer names them
# In a league whose adds made after the day's first puck count from the next day (rules.add_effective
# "next_day_after_first_game": ESPN's FIRSTGAME_SCORINGPERIOD -- the user, 2026-10-04), such a move
# is priced from tomorrow for both sides: the add cannot play tonight, so its drop is best made once
# the dropped player's game tonight is over, and the plan window says so. Where adds count at once
# ("immediate": Fleaflicker), the player counts from his next game (closed_tonight).
# The local hour by which the nightly job (pipeline/run_nightly_ingest.cmd, 4:00) has loaded last
# night's games and projections; a plan after it on older ones says so (LiveRunner.freshness).
NIGHTLY_DONE_HOUR = 6
# Points a move recommended TODAY must clear its bar by, in the live plan alone (LiveRunner): a
# move costs a person's effort and hands a player to the field, which no backtest charges for.
LIVE_MIN_GAIN = 0.5


def season_of(day: dt.date) -> str:
    start = day.year if day.month >= 7 else day.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def _note(x):
    """A report's note, or None when there is none (a missing note arrives as NaN)."""
    return None if x is None or (isinstance(x, float) and pd.isna(x)) else x


# ---------- the league snapshot ----------

@dataclasses.dataclass
class LeagueSnapshot:
    """The real league at one moment, in our player ids. `teams[i]` = {"name", "roster", "ir",
    "moves_used", "unmatched"} -- `unmatched` the rostered (not IR) platform ids with no PlayerID,
    whose roster spots are full though the plan cannot see who holds them; `lineup` = my current {slot label: [player ids]} (what Fleaflicker shows now, for
    the per-game lock); `waivers` = {player id: date he clears}."""
    teams: list
    me: int
    opponent: int | None = None
    my_week_points: float = 0.0
    opponent_week_points: float = 0.0
    lineup: dict = dataclasses.field(default_factory=dict)
    waivers: dict = dataclasses.field(default_factory=dict)
    source: str = "file"
    # Whether a move made now counts toward no week's limit, as the platform says (Fleaflicker:
    # before its first period begins); None when it cannot say, and the calendar decides.
    moves_free: bool | None = None
    # This week's move limit as the platform shows it (Fleaflicker's team page), and the week's
    # length in days for prorating the league's weekly limit; None when it cannot say.
    move_limit: int | None = None
    week_days: int | None = None
    # The last day (ISO) of the platform's period that a move made now counts toward, where it can
    # say (Fleaflicker): on Sunday night that is still the old week until 6:00 AM Eastern Monday,
    # while the plan is for Monday (LiveRunner._state's rollover).
    period_end: str | None = None
    # When the platform's week turns over, as the plan says it ("6:00 AM Eastern"), where the
    # adapter knows (Fleaflicker.week_turns_over).
    turns_over: str | None = None

    @classmethod
    def load(cls, path: Path) -> "LeagueSnapshot":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        teams = [{"name": t["name"], "roster": [int(p) for p in t["roster"]],
                  "ir": [int(p) for p in t.get("ir", [])], "moves_used": int(t.get("moves_used", 0)),
                  "unmatched": list(t.get("unmatched", []))}
                 for t in raw["teams"]]
        return cls(teams=teams, me=int(raw["me"]), opponent=raw.get("opponent"),
                   my_week_points=float(raw.get("my_week_points", 0.0)),
                   opponent_week_points=float(raw.get("opponent_week_points", 0.0)),
                   lineup={k: [int(p) for p in v] for k, v in raw.get("lineup", {}).items()},
                   waivers={int(k): v for k, v in raw.get("waivers", {}).items()},
                   move_limit=raw.get("move_limit"), week_days=raw.get("week_days"),
                   period_end=raw.get("period_end"), turns_over=raw.get("turns_over"),
                   moves_free=raw.get("moves_free"),
                   source=raw.get("source", f"file {Path(path).name}"))

    def dump(self, path: Path) -> Path:
        """Write the snapshot as `load` reads it back (planpass keeps each league's last read, so a
        re-plan on the user's choices needs no platform read)."""
        Path(path).write_text(json.dumps(dataclasses.asdict(self), default=str), encoding="utf-8")
        return Path(path)


    @classmethod
    def from_platform(cls, adapter, my_team_id: int, day: dt.date,
                      now: dt.datetime | None = None) -> "LeagueSnapshot":
        """The league as its platform shows it now (platforms/): every roster and IR, my current
        lineup slots, moves used this week, the matchup. Platform ids become our PlayerIDs through
        `platforms.PlayerIds`; a player with none is logged and left out -- never guessed. `now`
        (UTC) is when the moves would be made: the period they count toward is the platform's at
        that instant, which can differ from `day`'s (before 6:00 AM on a Fleaflicker week's first
        day, the week before)."""
        import platforms

        when = None if now is None else now.replace(tzinfo=dt.timezone.utc)

        ids = platforms.PlayerIds(adapter.platform)
        teams, me, unmatched = [], None, []
        for index, team in enumerate(adapter.rosters()):
            roster, missing = ids.resolve(team.roster)
            ir, missing_ir = ids.resolve(team.ir)
            unmatched += missing + missing_ir
            if team.team_id == my_team_id:
                me = index
                lineup = {label: ids.resolve(players)[0] for label, players in team.lineup.items()
                          if label not in ("BN", "IR")}
            teams.append({"name": team.name, "team_id": team.team_id, "roster": roster, "ir": ir,
                          "moves_used": adapter.moves_used(team.team_id, day, when=when),
                          "unmatched": missing})
        if me is None:
            raise SystemExit(f"team {my_team_id} is not in this league's rosters")
        if unmatched:
            log.warning("%d rostered player(s) with no PlayerID, left out: %s", len(unmatched), unmatched)
        matchup = adapter.matchup(my_team_id, day)
        opponent = None
        if matchup is not None and matchup.opponent_id is not None:
            opponent = next((i for i, t in enumerate(teams) if t["team_id"] == matchup.opponent_id), None)
        # Who is on waivers and until when, where the platform says (ESPN); otherwise none.
        waivers = {}
        if hasattr(adapter, "waivers"):
            for external_id, clears in adapter.waivers().items():
                player_id = ids.get(external_id)
                if player_id is not None:
                    waivers[player_id] = clears
        moves_free = (adapter.before_first_period(when)
                      if when is not None and hasattr(adapter, "before_first_period") else None)
        week_days = adapter.week_days(day, when=when) if hasattr(adapter, "week_days") else None
        period = adapter.period_at(when) if when is not None and hasattr(adapter, "period_at") else None
        period_end = period[2].isoformat() if period is not None else None
        # The platform's own count for my team (Fleaflicker's team page), over ours from the
        # transaction list: on 2026-10-01 they agreed on 4 used, and the page alone knew the
        # limit was 6, not the rules page's 7.
        move_limit = None
        shown = adapter.acquisitions(my_team_id) if hasattr(adapter, "acquisitions") else None
        if shown is not None:
            used, move_limit = shown
            if used != teams[me]["moves_used"]:
                log.warning("%s shows %d move(s) used this week; the transaction list counts %d -- "
                            "using %d", adapter.platform, used, teams[me]["moves_used"], used)
                teams[me]["moves_used"] = used
        return cls(teams=teams, me=me, opponent=opponent, moves_free=moves_free,
                   move_limit=move_limit, week_days=week_days, period_end=period_end,
                   turns_over=getattr(adapter, "week_turns_over", None),
                   my_week_points=matchup.points if matchup else 0.0,
                   opponent_week_points=matchup.opponent_points if matchup else 0.0,
                   lineup=lineup, waivers=waivers,
                   source=f"{adapter.platform} league {adapter.league_id}"
                          + (f" (season {adapter.season})" if adapter.season else ""))


def make_fake_league(board: pd.DataFrame, config, eligibility, me: int, opponent: int, seed: int = 0,
                     my_name: str = "My team") -> dict:
    """A made-up league for exercising the runner before the draft: every seat drafts the
    consensus VOR board with the real pick rule (`draft.simulate_draft`), lightly shuffled so the
    rosters are not a perfect snake of the board."""
    rng = np.random.default_rng(seed)
    values = board["vor"].astype(float)
    noisy = (values + rng.normal(0, values.std() * 0.15, len(values))).sort_values(ascending=False)
    picks = draft_module.simulate_draft(noisy, config, eligibility)
    per_team = config.roster_size
    teams = []
    for seat in range(config.teams):
        roster = picks[seat * per_team:(seat + 1) * per_team]
        teams.append({"name": my_name if seat == me else f"Team {seat + 1}",
                      "roster": [int(p) for p in roster], "ir": [], "moves_used": 0})
    return {"source": f"fake league (seed {seed}): simulated draft on the 2026-27 consensus board",
            "me": me, "opponent": opponent, "my_week_points": 0.0, "opponent_week_points": 0.0,
            "teams": teams, "lineup": {}, "waivers": {}}


# ---------- tonight's inputs ----------

# Tonight's rows as project_tonight.py writes them; a day with no games has none.
TONIGHT_COLUMNS = {
    "season_id": "int64", "game_id": "int64", "game_date": "datetime64[ns]", "team_id": "int64",
    "player_id": "int64", "position": "object", "variant": "object", "copy_index": "int64",
    "p_plays": "float64", "toi": "float64", "ev_toi": "float64", "pp_toi": "float64",
    "lambda_shots": "float64", "lambda_hits": "float64", "lambda_blocks": "float64",
    "lambda_assists": "float64", "lambda_goals": "float64", "lambda_pim": "float64",
    "pp_point_share": "float64", "sh_point_share": "float64", "p_plays_model": "float64",
    "questionable": "object", "note": "object", "injured_at_lockout": "bool", "kind": "object",
    "p_start": "float64", "p_start_model": "float64", "nhl_game_id": "int64",
    "start_time_utc": "datetime64[ns]", "lineup_source": "object"}


def no_games_rows() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=t) for c, t in TONIGHT_COLUMNS.items()})


def tonight_rows(day: dt.date) -> pd.DataFrame:
    path = TONIGHT_DIR / f"tonight_{day.isoformat()}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing -- run ModelFeatures/build_tonight.py and "
                                f"Projections/project_tonight.py for {day}")
    return pd.read_parquet(path)


def reported_status(day: dt.date) -> pd.DataFrame:
    path = STATUS_DIR / f"tonight_{day.isoformat()}_status.parquet"
    if not path.exists():
        return pd.DataFrame(columns=["player_id", "status", "team_id", "game_time_decision"])
    return pd.read_parquet(path)


GOALIE_LINE = ("wins", "losses", "ot_losses", "shutouts", "goals_against", "saves")


def season_lines(season: str) -> pd.DataFrame:
    """Each player's stat line this season so far (index player_id, draft_board.STAT_HEADINGS
    keys), empty before the season's first game: skaters from season_stats_{season}.parquet
    (ModelFeatures/build_season_stats.py), goalies summed from goalie_starts_{season}.parquet --
    both rebuilt by the nightly job."""
    frames = []
    skaters = paths.FEATURES_DIR / f"season_stats_{season}.parquet"
    goalie_path = paths.goalie_starts(season)
    goalies = pd.DataFrame()
    if goalie_path.exists():
        starts = pd.read_parquet(goalie_path)
        starts = starts[starts["appeared"].astype(bool)]
        if len(starts):
            goalies = (starts.groupby("player_id")[list(GOALIE_LINE)].sum()
                       .assign(gp=starts.groupby("player_id").size()))
    if skaters.exists():
        table = pd.read_parquet(skaters).set_index("player_id")
        frames.append(table.drop(index=goalies.index, errors="ignore"))
    frames.append(goalies)
    lines = pd.concat([f for f in frames if len(f)]) if any(len(f) for f in frames) else pd.DataFrame()
    lines.index = lines.index.astype(int) if len(lines) else lines.index
    return lines[lines["gp"] > 0] if len(lines) else lines


def latest_rates(day: dt.date, scoreset, expected_line: float) -> dict:
    """{player id: projected points per game} from the newest tonight file that has him, on or
    before `day` -- the live `latest_rate`: carried across nights his team is idle."""
    rates = {}
    season_start = dt.date(day.year if day.month >= 7 else day.year - 1, 7, 1).isoformat()
    for path in sorted(TONIGHT_DIR.glob("tonight_*.parquet")):
        # This season's nights only: a replayed past date (verification runs) is not "latest".
        if not season_start <= path.stem[len("tonight_"):] <= day.isoformat():
            continue
        rows = pd.read_parquet(path)
        skaters = rows[rows["kind"] == "skater"]
        if len(skaters):
            points = scoreset.score_columns(skaters, prefix="lambda_") * skaters["p_plays"].to_numpy("float64")
            rates.update(dict(zip(skaters["player_id"].astype(int), points)))
        goalies = rows[rows["kind"] == "goalie"]
        rates.update({int(p): float(s) * expected_line
                      for p, s in zip(goalies["player_id"], goalies["p_start"])})
    return rates


def latest_spreads(day: dt.date, weights: dict, theta: dict) -> tuple:
    """({player: sd of his points per team game}, {player: peripheral share}) from the newest
    tonight file that has him, on or before `day` (valuation.line_spread) -- carried like the rate."""
    sd, share = {}, {}
    season_start = dt.date(day.year if day.month >= 7 else day.year - 1, 7, 1).isoformat()
    for path in sorted(TONIGHT_DIR.glob("tonight_*.parquet")):
        if not season_start <= path.stem[len("tonight_"):] <= day.isoformat():
            continue
        rows = pd.read_parquet(path)
        skaters = rows[rows["kind"] == "skater"]
        if len(skaters):
            spread, periph = valuation_module.line_spread(skaters, weights, theta)
            ids = skaters["player_id"].astype(int)
            sd.update(zip(ids, spread.astype(float)))
            share.update(zip(ids, periph.astype(float)))
    return sd, share


class LiveRunner:
    """Everything that does not change between the passes of one day: league rules, strategy,
    the preseason board, the calendar, the simulator."""

    def __init__(self, day: dt.date, league, sim_season=None):
        """`league` is a registry league (leagues.load): its rules, scoring, strategy and positions."""
        self.day = day
        self.league = league
        self.plans_dir = livepaths.league_reports(league.name) / "plans"
        self.season = season_of(day)
        strategy = load_strategy(league.strategy)
        # The live plan only: a move recommended today clears its bar by LIVE_MIN_GAIN. The
        # strategy files are the backtests' too, where 0.5 measured neutral (branch screen, 2024-25:
        # +0.12 +/- 0.27 pts/wk); live it spares the +0.03 rentals a person would make by hand.
        self.strategy = dataclasses.replace(strategy, streaming=dataclasses.replace(
            strategy.streaming, min_gain=max(strategy.streaming.min_gain, LIVE_MIN_GAIN)))
        self.scoreset = simlayer.load_scoreset(league.scoring)
        self.board, _, self.config, self.eligibility = draft_board.build(
            self.season, draft_board.previous(self.season), league.rules, league.scoring,
            pd.Timestamp(day), self.strategy, eligibility_platform=league.eligibility_platform)
        self.slot_order = self.config.slot_order()
        scored = set(self.scoreset.skaters) | set(self.scoreset.goalies)
        self.stat_keys = [k for k in draft_board.STAT_HEADINGS
                          if k in self.board.columns and (k == "gp" or k in scored)]

        games = pd.read_parquet(paths.schedule(self.season)).reset_index(drop=True)
        games["game_id"] = games.index
        schedule = pd.concat([games.rename(columns={"home_team_id": "team_id"})[["game_id", "game_date", "team_id"]],
                              games.rename(columns={"away_team_id": "team_id"})[["game_id", "game_date", "team_id"]]])
        self.calendar = schedule_module.from_candidates(schedule, self.config.week_starts_on,
                                                        self.config.min_first_week_games)
        self.regular_weeks = self.config.regular_season_weeks_in(self.calendar)
        self.calendar.last_week = self.regular_weeks + self.config.playoff_weeks
        team_games = schedule.groupby("team_id")["game_id"].nunique()
        self.game_days = set(pd.to_datetime(games["game_date"]).dt.date)

        history = inputs.load_goalie_history(self.season)
        self.goalie_line_mean, self.goalie_line_sd = engine_module.goalie_line(history, self.scoreset)

        # The board's season totals as a rate per team game: the rest-of-season seed (skaters, as
        # the engine's `ros` is) and the per-game seed for everyone nothing has projected yet.
        # An abbreviation can have two ids (UTA is 18 and 65; the schedule plays 18): take the one
        # this season's schedule uses, or every Utah player looks like he has no games.
        teams = pd.read_parquet(paths.teams())
        scheduled = set(team_games.index)
        teams = teams.assign(plays=teams["team_id"].isin(scheduled)).sort_values("plays")
        abbreviation_to_id = dict(zip(teams["team"], teams["team_id"]))
        self.board_team = {int(p): abbreviation_to_id.get(t) for p, t in zip(self.board.index, self.board["team"])}
        per_game = {}
        for player_id, value, team in zip(self.board.index.astype(int), self.board["value"], self.board["team"]):
            games_left = team_games.get(abbreviation_to_id.get(team), 82)
            per_game[player_id] = float(value) / max(int(games_left), 1)
        self.seed_rate = per_game
        goalies = {int(p) for p, pos in zip(self.board.index, self.board["positions"]) if pos == "G"}
        self.ros_seed = {p: v for p, v in per_game.items() if p not in goalies}

        self.sim_season = sim_season or self._latest_sim_season()
        # Seeded by the date, so two passes over the same reports give the same plan: a
        # recommendation that flips between passes for no reason but the random stream is noise.
        self.seed = int(day.strftime("%Y%m%d"))
        self.simulator = simlayer.build_simulator(self.sim_season, seed=self.seed)
        self.goalie_fit = simlayer.load_goalie_fit(self.sim_season)
        dispersion = paths.dispersion_path(self.sim_season)
        self.theta = ({k: v["theta"] for k, v in json.loads(dispersion.read_text(encoding="utf-8"))
                       ["categories"].items()} if dispersion.exists() else {})
        # Peripheral share from the consensus season line, for anyone no tonight file has yet.
        w = self.scoreset.weights("skaters")
        stat = lambda k: self.board[k].fillna(0.0) if k in self.board.columns else 0.0
        scoring = sum(w.get(k, 0.0) * stat(k) for k in ("goals", "assists", "ppp", "shp"))
        periph = sum(w.get(k, 0.0) * stat(k) for k in valuation_module.PERIPHERAL_STATS)
        total = scoring + periph
        self.board_periph = {int(p): float(x) for p, x in (periph / total.where(total > 0)).dropna().items()}

    def _latest_sim_season(self) -> str:
        """The newest season with a fitted sampler (dispersion + correlations + goalie fit) no later
        than this one -- 2026-27's are fitted once it has games to fit on."""
        start = int(self.season[:4])
        for year in range(start, start - 4, -1):
            candidate = f"{year}-{(year + 1) % 100:02d}"
            if all(p.exists() for p in (paths.dispersion_path(candidate), paths.correlations_path(candidate),
                                        paths.goalie_fit_path(candidate))):
                if candidate != self.season:
                    log.warning("no %s sampler fit yet -- using %s's", self.season, candidate)
                return candidate
        raise FileNotFoundError("no fitted sampler for any recent season")

    # ---------- one pass ----------

    def plan(self, snapshot: LeagueSnapshot, now: dt.datetime) -> dict:
        """Run the shipped manager for my team on a copy of the league and return the plan."""
        day = pd.Timestamp(self.day)
        # No games today: no lineup to set, but the roster, IR moves and pickups still stand.
        rows = tonight_rows(self.day) if self.day in self.game_days else no_games_rows()
        skaters = rows[rows["kind"] == "skater"].copy()
        goalies = rows[rows["kind"] == "goalie"].copy()
        status = reported_status(self.day)

        injured = set(status.loc[status["status"].isin(INJURED), "player_id"].astype(int))
        # Who the league's IR still takes besides them (rules.ir_eligible: Fleaflicker 12090 takes
        # day-to-day players): they may stay on IR -- never forced off with a drop -- until their
        # report clears. They are not out: a DTD player is valued and started as before.
        ir_ok = set(status.loc[status["status"].isin(self.config.rules["ir_eligible"]), "player_id"].astype(int))
        unavailable = injured | set(rows.loc[rows["injured_at_lockout"].fillna(False).astype(bool), "player_id"].astype(int))
        playing = set(rows["player_id"].astype(int))
        nhl_team = {p: t for p, t in self.board_team.items() if t is not None}
        nhl_team.update({int(p): int(t) for p, t in zip(status["player_id"], status["team_id"]) if pd.notna(t)})
        nhl_team.update({int(p): int(t) for p, t in zip(rows["player_id"], rows["team_id"])})

        goalie_projections = pd.DataFrame({
            "player_id": goalies["player_id"].astype(int).to_numpy(),
            "p_start": goalies["p_start"].to_numpy(), "p_start_naive": goalies["p_start"].to_numpy(),
            "p_start_model": goalies["p_start"].to_numpy(),
            "expected_line": self.goalie_line_mean, "line_sd": self.goalie_line_sd})
        rate_estimate = dict(self.seed_rate)
        # Skaters' rest-of-season rate from the rest-of-season model (skater_ros), with the
        # preseason board scaled to the model's level for anyone it has not projected; goalies'
        # from their projected share of their team's remaining starts x the league line.
        skaters_ros, ros_note = self.skater_ros()
        ros_estimate = {**skaters_ros, **self.goalie_ros()}
        returns = {**self.expected_returns(injured, status, nhl_team),
                   **self.return_overrides(injured)}
        _, periph = latest_spreads(self.day, self.scoreset.weights("skaters"), self.theta)
        self.periph = {**self.board_periph, **periph}
        rate_estimate.update(latest_rates(self.day, self.scoreset, self.goalie_line_mean))
        decision_points = self._draws(skaters, goalies)

        state = self._state(snapshot, day)
        # What the user chose in the plan window (Live/choices.py): who upgrades may drop (the
        # Upgrade tab), and the Week tab's day by day OK-to-drop lists and pinned moves. None or
        # empty: the model chooses.
        self.choices = choices_module.load(self.league.name, self.day)
        self.upgrade_drops, self.day_drops, self.pinned = choices_module.for_planner(self.choices)
        week = self.calendar.week_from(day)
        phase = "playoffs" if week and week > self.regular_weeks else "regular"

        # Who cannot play tonight for us if picked up now: the platform locks each player at his
        # game's start, so a free agent whose team is already playing counts from his next game;
        # so does one whose claim clears today at a known time after his puck drop.
        puck = pd.to_datetime(rows["start_time_utc"])
        puck_by_team = puck.groupby(rows["team_id"].astype(int)).min().to_dict() if len(rows) else {}
        started = {t for t, at in puck_by_team.items() if pd.notna(at) and at <= pd.Timestamp(now)}
        closed_tonight = {p for p, t in nhl_team.items() if t in started}
        for p, clears in snapshot.waivers.items():
            clears, at = pd.Timestamp(clears), puck_by_team.get(nhl_team.get(p))
            if (at is not None and pd.notna(at) and clears.normalize() == day
                    and clears != clears.normalize() and clears > at):
                closed_tonight.add(int(p))

        # After the day's first puck, in a league that holds adds until tomorrow, today's adds take
        # effect tomorrow.
        moves_from = (day + pd.Timedelta(days=1)
                      if self.config.rules["add_effective"] == "next_day_after_first_game" and started
                      else None)
        self.adds_locked = moves_from is not None

        def view(state=state, **changes):
            fields = dict(
                day=day, week=week, config=self.config, calendar=self.calendar,
                projections=skaters[[c for c in skaters.columns if not c.startswith(("target_", "label_"))]],
                goalie_projections=goalie_projections, unavailable=unavailable, injured=injured,
                playing_tonight=playing, nhl_team=nhl_team,
                history=estimators_module.NaiveHistory(dict(self.seed_rate), {}, 1.0),
                state=state, team_index=snapshot.me, opponent_index=snapshot.opponent,
                my_week_points=snapshot.my_week_points, opponent_week_points=snapshot.opponent_week_points,
                decision_points=decision_points, goalie_draw_column="p_start_model", future_draws=None,
                phase=phase, alive=True, on_bye=False,
                week_weight_mode=self.strategy.playoff_week_weight,
                rate_estimate=rate_estimate, ros_estimate=ros_estimate, returns=returns,
                closed_tonight=closed_tonight, board_estimate=self.ros_seed,
                ros_games=self.ros_games, board_scale=self.board_scale)
            v = view_module.SlateView(**{**fields, **changes})
            v.goalie_absence = self.strategy.adddrop.goalie_absence   # valuation.player_value
            v.upgrade_drops = self.upgrade_drops      # adddrop
            v.ir_ok = ir_ok                           # view.healthy_on_ir
            v.day_drops, v.pinned = self.day_drops, self.pinned   # streaming.spots, weekplan
            v.moves_from = moves_from                 # adddrop, streaming.run, weekplan
            return v

        before = copy.deepcopy(state.teams[snapshot.me].__dict__)
        # The league as it stands, kept apart: the window shows the roster and tonight's lineup
        # before any move, and the moves as recommendations.
        state_now = copy.deepcopy(state)
        manager = managers_module.Orchestrated(snapshot.me, self.config, self.scoreset, self.strategy)
        manager.plan.week_alternatives = WEEK_ALTERNATIVES
        # The week plan apart from the upgrades (the user, 2026-10-07; orchestrator.DailyPlan
        # week_view_on): planned on the roster and moves before today's upgrades.
        manager.plan.week_view_on = lambda before_upgrades: view(state=before_upgrades)
        manager.transactions(view())
        next_week, next_plans = self._next_week_plans(manager, state, week, view, skaters, goalie_projections)
        problems = (([ros_note] if ros_note else []) + self.freshness()
                    + sync_league_settings.recent(self.league.name, self.day))
        if self.rollover:
            left, cap = self.rollover["leftover"], self.rollover["new_cap"]
            platform = PLATFORM_NAMES.get(self.league.platform, "The platform")
            turns_over = snapshot.turns_over or "the start of its new week"
            problems.append(
                f"{platform}'s week turns over at {turns_over}: a move made "
                f"before then counts against last week's {left} leftover move(s), free for this week "
                "-- make today's moves tonight, at most that many -- and later ones against this "
                f"week's {cap}" if left else
                f"{platform}'s week turns over at {turns_over} and last week "
                f"has no moves left: make today's moves after {turns_over}; they "
                f"count against this week's {cap}")
        # The upgrades' drop list carried into a new matchup week is easy to forget (it overrides
        # who may be dropped until cleared): say when it was set. The Week tab's lists are by day
        # and lapse with them.
        if self.upgrade_drops is not None:
            set_on = choices_module.saved_on(self.choices)
            set_week = self.calendar.week_from(pd.Timestamp(set_on)) if set_on else None
            if week is not None and set_week is not None and set_week < week:
                problems.append(f"the upgrades' OK-to-drop list ({len(self.upgrade_drops)} player(s)) "
                                f"was set on {set_on:%a %b %d} in week {set_week} and is still active "
                                "-- upgrades drop only them; clear or change it on the Upgrade tab")
        hidden = (list(snapshot.teams[snapshot.me].get("unmatched", []))
                  + [p for p in snapshot.teams[snapshot.me]["roster"] if p not in self.eligibility])
        if hidden:
            problems.append(f"{len(hidden)} rostered player(s) the plan cannot see ({', '.join(map(str, hidden))}: "
                            "no player id or no eligibility) -- their spots count as full, but the "
                            "plan never moves or starts them; check the platform id map")
        try:
            state.assert_ir_resolved(snapshot.me, injured | ir_ok)
        except state_module.IllegalMove as error:
            problems.append(str(error))
        v = view()
        # After the first puck on ESPN today's adds play from tomorrow, and their drops wait for
        # tonight's games: tonight's lineup is solved on the roster as it stands.
        lineup, z = self._lineup(manager, view(state_now) if self.adds_locked else v, snapshot, rows, now)
        # After the plan's own lineup, so the plan is what it would be without this extra solve.
        manager_now = managers_module.Orchestrated(snapshot.me, self.config, self.scoreset, self.strategy)
        lineup_now, _ = self._lineup(manager_now, view(state_now), snapshot, rows, now)
        # The pickups to choose from: the add/drop rule's own pricing, on the roster as it stands.
        options = adddrop_module.price(view(state_now), manager_now.params, manager_now.slot_order,
                                       manager_now.accepts, manager_now._fieldable, shortlist=OPTIONS_PRICED)
        return self._describe(snapshot, state, before, manager, v, lineup, z, rows, status, problems, now,
                              state_now, lineup_now, options, next_week, next_plans)

    def _next_week_plans(self, manager, state, week, view, skaters, goalie_projections) -> tuple:
        """On the matchup week's last day, next week's streaming plans as well (the Week tab's next
        week): weekplan's team-slot plans from next week's first day, on the roster today's moves
        leave, with next week's move limit and no tonight -- every night priced on the players'
        rates, as this week's later nights are. Shown only: it runs on a copy of the league, and
        it is planned again from scratch once that week starts. Returns (next week, its plans), or
        (None, []) on any other day."""
        if (week is None or self.strategy.streaming.mode != "week" or week >= self.calendar.last_week
                or pd.Timestamp(self.day) != self.calendar.weeks[week - 1].end):
            return None, []
        coming = self.calendar.weeks[week]
        ahead = copy.deepcopy(state)
        ahead.week, ahead.free_moves = coming.number, False
        mine = ahead.teams[manager.team_index]
        # Fleaflicker prorates its weekly limit by the week's days (_week_cap); a full week is
        # the league's limit.
        days = (coming.end - coming.start).days + 1
        # After the first puck on ESPN a move takes effect tomorrow, so on the week's last day it
        # counts toward next week -- and so does the platform's count (Espn.move_period), kept.
        if not self.adds_locked:
            mine.moves_used = 0
        mine.week_cap = max(1, round(self.config.moves_per_week * days / 7))
        v = view(ahead, day=coming.start, week=coming.number,
                 projections=skaters.iloc[0:0], goalie_projections=goalie_projections.iloc[0:0],
                 unavailable=set(), playing_tonight=set(), decision_points={}, closed_tonight=set(),
                 opponent_index=None, my_week_points=0.0, opponent_week_points=0.0,
                 phase="playoffs" if coming.number > self.regular_weeks else "regular")
        v.p_start_column = manager.p_start_column
        v.moves_from = None                           # next week: no lock yet
        _, plans = weekplan_module.run(
            v, manager.plan.stream_params, manager.params.horizon_weeks, manager.params.rate_source,
            manager.slot_order, manager.accepts, manager._fieldable, z=0.0,
            alternatives=WEEK_ALTERNATIVES)
        return coming.number, plans

    def expected_returns(self, injured, status, nhl_team) -> dict:
        """{injured player: expected return date}: his reported body part's injury type group and
        its typical remaining absence, from history only (ModelFeatures/build_injury_absence.py;
        ESPN's own return dates are not used until measured -- plans/injury-absence.md step 4)."""
        table_path = paths.FEATURES_DIR / f"injury_absence_{self.season}.parquet"
        types_path = paths.FEATURES_DIR / f"injury_types_{self.season}.parquet"
        if not table_path.exists() or not types_path.exists():
            return {}
        table = pd.read_parquet(table_path)
        absence = dict(zip(table["InjuryTypeGroup"].astype(int), table["remaining"].astype(float)))
        norm = lambda text: " ".join(str(text).lower().replace("-", " ").split())
        types = pd.read_parquet(types_path)
        group_of = dict(zip(types["InjuryType"].map(norm), types["InjuryTypeGroup"].astype(int)))
        parts = (dict(zip(status["player_id"].astype(int), status["injury"]))
                 if "injury" in status.columns else {})
        type_of = {p: group_of.get(norm(parts[p]), -1) for p in injured if parts.get(p)}
        return view_module.expected_returns(injured, type_of, absence, nhl_team, self.calendar,
                                            pd.Timestamp(self.day))

    def return_overrides(self, injured) -> dict:
        """{injured player: return date} you know better than the model does, from
        Settings/returns-<league>.json ({"Neal Pionk": "2026-10-02"}; keys starting "_" are notes).
        Matched by name among today's injured players only.

        The file cleans itself on today's plan: an entry whose player is off the injury report,
        or whose date has passed while he is still out (the model's estimate then stands), is
        removed. A name that matches no player is kept and warned about -- likely a typo. Nothing
        is removed on a rehearsal of another day, or when no one at all is reported injured (a
        report that failed to load, not a league with no injuries)."""
        path = paths.SETTINGS_DIR / f"returns-{self.league.name}.json"
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = {k: v for k, v in raw.items() if not k.startswith("_")}
        names = pd.read_parquet(paths.players()).set_index("player_id")["name"]
        known = set(names)
        by_name = {}
        for p in injured:
            if p in names.index:
                by_name.setdefault(names[p], []).append(int(p))
        out, stale, day = {}, [], pd.Timestamp(self.day)
        for name, back in entries.items():
            back, ids = pd.Timestamp(back), by_name.get(name, [])
            if not ids:
                if name in known:
                    stale.append(name)
                    log.info("%s: %s is off the injury report", path.name, name)
                else:
                    log.warning("%s: no player named %r -- check the spelling", path.name, name)
            elif back < day:
                stale.append(name)
                log.warning("%s: %s's return %s has passed and he is still out -- the model's "
                            "estimate stands", path.name, name, back.date())
            elif len(ids) == 1:
                out[ids[0]] = back
            else:
                log.warning("%s: %d injured players named %s -- ignored", path.name, len(ids), name)
        if stale and injured and self.day == dt.date.today():
            for name in stale:
                raw.pop(name)
            path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
            log.info("%s: removed %s", path.name, ", ".join(stale))
        return out

    def peripheral(self, player_id):
        """His projected points' peripheral share (hits, blocks, shots, PIM), None for a goalie
        or a player nothing projects: his latest tonight line, else the consensus season line."""
        if "G" in self.eligibility.get(player_id, ()):
            return None
        share = getattr(self, "periph", self.board_periph).get(int(player_id))
        return None if share is None else round(share, 3)

    def skater_ros(self) -> tuple:
        """({skater: rest-of-season points per team game}, a note when the model is missing).

        The rest-of-season model's projection (Projections/ros_predict.py, run nightly: the newest
        `ros_projections_<season>_<date>.parquet` on or before today), its stat line scored under
        this league's scoring over his team's games remaining. Everyone it has not projected yet
        (not dressed this season) gets the preseason board, scaled by the model's level over
        the players both cover, so one comparison never mixes the two scales (the board runs
        ~15% high; scaled and unscaled tested alike, Decisions/valuation.rate). Why the model
        and not the board (2026-10-03): with the multi-season prior it was level with the
        frozen board in the realistic league (2024-25 +3.4 +/- 1.1 pts/wk at 64 drafts, 2025-26
        -0.0 +/- 1.5), and it updates with the season, which the board never does; the user's call.
        With no projection file, the board alone, as before, and the plan says so."""
        files = sorted(paths.PROJECTIONS_REPORTS.glob(f"ros_projections_{self.season}_*.parquet"))
        files = [f for f in files if f.stem.rsplit("_", 1)[-1] <= self.day.isoformat()]
        # Rate source `blend` (valuation.rate): games played behind each row, the model's level.
        self.ros_games, self.board_scale = {}, 1.0
        if not files:
            return dict(self.ros_seed), ("no rest-of-season projections for this season yet "
                                         "(Projections/ros_predict.py): moves priced on the preseason board")
        rows = pd.read_parquet(files[-1])
        games = rows["games_remaining"].to_numpy("float64")
        points = self.scoreset.score_columns(rows, prefix="proj_")
        model = {int(p): float(v / g) for p, v, g in zip(rows["player_id"], points, games) if g > 0}
        shared = [p for p in model if p in self.ros_seed]
        scale = (sum(model[p] for p in shared) / sum(self.ros_seed[p] for p in shared)
                 if shared and sum(self.ros_seed[p] for p in shared) > 0 else 1.0)
        self.ros_games = {int(p): float(g) for p, g in zip(rows["player_id"], rows["gp_std"].fillna(0.0))}
        self.board_scale = scale
        log.info("rest of season: %d skaters from %s, the rest from the board x %.3f",
                 len(model), files[-1].name, scale)
        return {**{p: v * scale for p, v in self.ros_seed.items()}, **model}, None

    def freshness(self) -> list:
        """What today's plan was built on that is older than it should be, for its problems line,
        once the 4:00 nightly job should have run (NIGHTLY_DONE_HOUR): the newest game loaded (the
        nightly ingest's goalie starts) or the newest rest-of-season projections behind the last
        game day before today. ros_predict.py dates its file by the newest game it saw, so a fresh
        run on Oct 5 writes Oct 4's file. On 2026-10-04 the PC slept through 4:00 and the plan ran
        on Oct 2's games without a word. A rehearsal of a past date is old on purpose: only today's
        plan is checked, and tomorrow's (made once today's games have all started), which checks
        the same days: tonight's games load at 4:00."""
        today = dt.date.today()
        played = [d for d in self.game_days if d < min(self.day, today)]
        if (self.day not in (today, today + dt.timedelta(days=1)) or not played
                or dt.datetime.now().hour < NIGHTLY_DONE_HOUR):
            return []
        out, last = [], max(played)
        path = paths.goalie_starts(self.season)
        loaded = (pd.to_datetime(pd.read_parquet(path, columns=["game_date"])["game_date"]).max().date()
                  if path.exists() else None)
        if loaded is None or loaded < last:
            out.append(f"stale data: games through {last} are not loaded (newest {loaded or 'none'})"
                       " -- the nightly ingest has not run (pipeline/run_nightly_ingest.cmd)")
        files = sorted(paths.PROJECTIONS_REPORTS.glob(f"ros_projections_{self.season}_*.parquet"))
        newest = files[-1].stem.rsplit("_", 1)[-1] if files else None
        if newest is not None and newest < last.isoformat():
            out.append(f"stale data: rest-of-season projections are as of {newest}, before the last "
                       f"game day {last} -- the nightly job has not run (pipeline/run_nightly_ingest.cmd)")
        return out

    def goalie_ros(self) -> dict:
        """{goalie: rest-of-season points per team game} from today's (or the latest earlier)
        goalie workload rows (Projections/goalie_workload.py, built by planpass.tonight): his
        projected share of his team's remaining starts x the league-average line. Empty when
        none are built -- goalies then fall back to their latest P(start), as before."""
        path = paths.PROJECTIONS_REPORTS / f"goalie_ros_{self.season}.parquet"
        if not path.exists():
            return {}
        rows = pd.read_parquet(path)
        rows = rows[pd.to_datetime(rows["game_date"]) <= pd.Timestamp(self.day)]
        if not len(rows):
            return {}
        rows = rows[rows["game_date"] == rows["game_date"].max()]
        return {int(p): float(s) * self.goalie_line_mean
                for p, s in zip(rows["player_id"], rows["start_share"])}

    def _state(self, snapshot, day) -> state_module.LeagueState:
        if len(snapshot.teams) != self.config.teams:
            raise ValueError(f"the snapshot has {len(snapshot.teams)} teams; the league has {self.config.teams}")
        universe = set(self.eligibility)
        state = state_module.LeagueState(self.config, universe, self.eligibility)
        state.week = self.calendar.week_from(day)
        week_cap = self._week_cap(snapshot)
        for index, team in enumerate(snapshot.teams):
            unknown = [p for p in team["roster"] + team["ir"] if p not in self.eligibility]
            if unknown:
                log.warning("%s: %d player(s) with no eligibility, ignored: %s", team["name"], len(unknown), unknown)
            holder = state.teams[index]
            holder.roster = [p for p in team["roster"] if p in self.eligibility]
            holder.ir = [p for p in team["ir"] if p in self.eligibility]
            holder.moves_used = team["moves_used"]
            # Roster spots held by players the plan cannot see -- no PlayerID, or no eligibility --
            # are still full (view.roster_room): one left out once read as an open spot.
            holder.unseen = (len(team.get("unmatched", []))
                             + sum(1 for p in team["roster"] if p not in self.eligibility))
            holder.week_cap = week_cap
            for p in holder.roster + holder.ir:
                state.owner[p] = index
                state.pool.discard(p)
        state.waived = {int(p): pd.Timestamp(d) for p, d in snapshot.waivers.items()}
        # Before the first week, a move counts toward no week's limit (Fleaflicker, 2026-09-27):
        # the platform's own first period when it says (it can start a day after our calendar's
        # week 1 -- Fleaflicker 2026-27: Tue Sep 29, 6:00 AM Eastern), else the calendar's.
        state.free_moves = (snapshot.moves_free if snapshot.moves_free is not None
                            else day < self.calendar.weeks[0].start)
        # The rollover (Fleaflicker, 2026-10-05): after Sunday's last puck the plan is for Monday,
        # but a move made now still counts toward the old week until 6:00 AM Eastern, and the
        # snapshot's counts are that week's. Planning the new week on the old week's leftover moves
        # under-budgets every later night. So: the new week's limit for everyone, nothing used; a
        # move made tonight spends the old week's leftovers, which expire anyway -- free for this
        # week while any are left (not capped at that count: the plan's problems line says it).
        self.rollover = None
        ended = pd.Timestamp(snapshot.period_end) if snapshot.period_end else None
        week = state.week
        if ended is not None and week is not None and day > ended:
            old_cap = week_cap if week_cap is not None else self.config.moves_per_week
            leftover = max(0, old_cap - snapshot.teams[snapshot.me]["moves_used"])
            coming = self.calendar.weeks[week - 1]
            new_cap = max(1, round(self.config.moves_per_week
                                   * ((coming.end - coming.start).days + 1) / 7))
            for holder in state.teams:
                holder.moves_used, holder.week_cap = 0, new_cap
            if leftover > 0:
                state.free_moves = True
            self.rollover = {"leftover": leftover, "new_cap": new_cap}
        return state

    def _week_cap(self, snapshot):
        """This week's move limit for every team: the platform's own figure when it shows one, else
        the league's weekly limit prorated by the week's days (Fleaflicker: 6 in a 6-day week, 14 in
        a 14-day one), else None (config.moves_per_week)."""
        weekly = self.config.moves_per_week
        prorated = (None if snapshot.week_days is None
                    else max(1, round(weekly * snapshot.week_days / 7)))
        if snapshot.move_limit is not None:
            if prorated is not None and snapshot.move_limit != prorated:
                log.warning("the platform's move limit this week is %d; prorating %d a week over %d "
                            "days gives %d -- using %d", snapshot.move_limit, weekly, snapshot.week_days,
                            prorated, snapshot.move_limit)
            return snapshot.move_limit
        return prorated

    def _draws(self, skaters, goalies) -> dict:
        """Tonight's per-player fantasy-point samples, skaters and goalies on the same sims, as the
        engine's decision draws are made (engine.decision_draws / _goalie_draws)."""
        if not len(skaters):
            return {}
        # Every pass starts the day's stream afresh. The runner lives all day in the plan window,
        # and a stream carried on from the last pass gave a refresh over unchanged reports a
        # different lineup than a fresh process -- the flip the date seed exists to prevent.
        self.simulator.rng = np.random.default_rng(self.seed)
        frame = skaters.reset_index(drop=True)
        draws = self.simulator.draw(frame, DECISION_SIMS)
        points = self.scoreset.score_draws(draws)
        out = {int(p): points[i] for i, p in enumerate(draws.keys["player_id"])}
        sides = draws.keys.groupby("game_id")["team_id"].nunique()
        drawn = set(sides[sides == 2].index)
        g = goalies.loc[goalies["game_id"].isin(drawn), ["game_id", "team_id", "player_id", "p_start"]]
        if len(g):
            goalie_draws = simlayer.goalies_module.draw_goalies(draws, g, self.goalie_fit, self.simulator.rng)
            gpoints = self.scoreset.score_draws(goalie_draws, side="goalies")
            out.update({int(p): gpoints[i] for i, p in enumerate(goalie_draws.keys["player_id"])})
        return out

    def _lineup(self, manager, v, snapshot, rows, now):
        """The manager's lineup, re-solved around players whose game has already started."""
        started_teams = set(rows.loc[pd.to_datetime(rows["start_time_utc"]) <= pd.Timestamp(now), "team_id"].astype(int))
        locked = {}                                     # slot index -> player, from the current lineup
        if started_teams and snapshot.lineup:
            free_slots = list(range(len(self.slot_order)))
            for label, players in snapshot.lineup.items():
                for p in players:
                    if v.nhl_team.get(p) in started_teams and p in v.roster:
                        index = next((i for i in free_slots if self.slot_order[i] == label), None)
                        if index is not None:
                            locked[index] = p
                            free_slots.remove(index)
        lineup = manager.set_lineup(v)
        z = getattr(manager, "_last_z", 0.0)
        if not locked:
            return lineup, z
        # Re-solve over the open slots with the manager's own values (mean - z * sd^2 / 2s).
        moments = v.moments(self.scoreset, v.roster)
        started = {p for p in v.roster if v.nhl_team.get(p) in started_teams}
        values = {p: manager.lineup_value(mu, sd) for p, (mu, sd) in moments.items()
                  if p in v.roster and v.available(p) and p not in started}
        open_slots = [i for i in range(len(self.slot_order)) if i not in locked]
        partial = slots_module.assign([self.slot_order[i] for i in open_slots], values, self.eligibility,
                                      self.config.accepts)
        assigned = dict(locked)
        assigned.update({open_slots[j]: p for j, p in partial.assigned.items()})
        benched = [p for p in values if p not in assigned.values()]
        return slots_module.Lineup(assigned, benched, [i for i in range(len(self.slot_order)) if i not in assigned]), z

    # ---------- the plan ----------

    def _describe(self, snapshot, state, before, manager, v, lineup, z, rows, status, problems, now,
                  state_now, lineup_now, options, next_week=None, next_plans=()) -> dict:
        me = state.teams[snapshot.me]
        names = pd.read_parquet(paths.players()).set_index("player_id")["name"]
        teams = pd.read_parquet(paths.teams()).set_index("team_id")["team"]
        name = lambda p: f"{names.get(p, p)} ({teams.get(v.nhl_team.get(p), '?')})"
        moments = v.moments(self.scoreset, list(set(me.roster) | set(before["roster"]) | set(before["ir"])))
        by_player = rows.drop_duplicates("player_id").set_index("player_id")
        state_status = status.set_index("player_id")["status"].to_dict()
        gtd = set(status.loc[status["game_time_decision"].astype(bool), "player_id"].astype(int))

        # Stats for the tables: tonight's per-game projection on the Tonight tab; the season so far
        # elsewhere, or -- before the first game -- the projected season (the draft board's line).
        so_far = season_lines(self.season)
        board = self.board

        def per_game(p):
            row = by_player.loc[p] if p in by_player.index else None
            if row is not None and row["kind"] == "skater":
                plays, points = float(row["p_plays"]), float(row["lambda_goals"] + row["lambda_assists"])
                line = {"goals": row["lambda_goals"], "assists": row["lambda_assists"],
                        "ppp": points * row["pp_point_share"], "shp": points * row["sh_point_share"],
                        "shots": row["lambda_shots"], "hits": row["lambda_hits"],
                        "blocks": row["lambda_blocks"], "pim": row["lambda_pim"]}
                return {k: round(float(line[k]) * plays, 2) for k in self.stat_keys if k in line}
            if p not in board.index or not board.loc[p, "gp"] > 0:
                return {}
            share = float(row["p_start"]) if row is not None else 1.0    # a goalie tonight: P(start)
            return {k: round(float(board.loc[p, k]) / float(board.loc[p, "gp"]) * share, 2)
                    for k in self.stat_keys if k != "gp" and pd.notna(board.loc[p, k])}

        def season(p):
            source = so_far if len(so_far) else board
            if p not in source.index:
                return {"gp": 0} if len(so_far) else {}
            return {k: int(round(float(source.loc[p, k]))) for k in self.stat_keys
                    if k in source.columns and pd.notna(source.loc[p, k])}
        self._season_stats = season

        # The rate a move was priced on: the add/drop rule's source (rest of season by default).
        source = self.strategy.adddrop.rate_source
        priced = lambda p: round(valuation_module.rate(v, p, source), 2)
        # A pickup the week mode made for next week (a won week's last day, weekplan.py).
        for_next_week = {q["incoming"] for q in manager.move_log if q.get("for_next_week")}
        moves = []
        for t in state.transactions:
            if t["team"] != snapshot.me:
                continue
            moves.append({"kind": "pickup for next week" if t["player_id"] in for_next_week else t["kind"],
                          "add": name(t["player_id"]),
                          "drop": name(t["dropped"]) if t["dropped"] is not None else None,
                          "add_rate": priced(t["player_id"]),
                          "drop_rate": priced(t["dropped"]) if t["dropped"] is not None else None,
                          "add_status": state_status.get(t["player_id"])})
        # A rental claim's drop can be a player the week plan picks up only before the claim clears
        # (a chain: rent Sanheim Wed, claim Podkolzin clearing Thu in Sanheim's spot). The platform
        # wants a drop you hold now, so name who holds that spot today -- the claim can be entered
        # with him and its drop changed once the planned pickup is made.
        # weekplan.execute records who holds that spot today (`outgoing_today`).
        logged = {q["incoming"]: q for q in manager.move_log if q.get("kind") == "rental claim"}

        def claim_row(p):
            drop = state.claim_drops.get((snapshot.me, p))
            row = {"claim": name(p), "drop": name(drop) if drop is not None else None, "note": None}
            q = logged.get(p)
            if drop is None or drop in me.roster or q is None:
                return row
            holder, when = q.get("outgoing_today"), q.get("outgoing_picked_up")
            row["drop"] = name(holder) if holder is not None and holder in me.roster else "(open spot)"
            row["note"] = (f"planned drop {name(drop)}, a pickup for "
                           f"{pd.Timestamp(when).strftime('%a') if when is not None else 'later'}: "
                           f"enter with {row['drop']}, change the drop once he is picked up")
            return row

        claims = [claim_row(p) for p, teams_ in state.pending_claims.items() if snapshot.me in teams_]
        # The Upgrade tab: the add/drop rule's own moves first (upgrades and claims -- rentals are
        # on the Week tab), then the pickups it priced on the roster as it stands, for comparison.
        choices = []
        upgrades = [q for q in manager.move_log if q["kind"] in ("add", "claim")]
        for q in upgrades:
            p, d = q["incoming"], q["outgoing"]
            gain, bar = q["predicted_gain"], q["bar"]
            choices.append({"rank": None, "kind": MOVE_KINDS[q["kind"]], "add": name(p),
                            "drop": name(d) if d is not None else None,
                            "gain": round(gain, 1), "bar": round(bar, 1), "edge": round(gain - bar, 1),
                            "clears": True, "in_plan": True, "note": "in plan",
                            "add_rate": priced(p), "drop_rate": priced(d) if d is not None else None,
                            "add_periph": self.peripheral(p),
                            "add_games": q.get("incoming_games"),
                            "drop_games": q.get("outgoing_games") if d is not None else None})
        # An upgrade is the same pricing as its row below, so it takes that row's rank instead; a
        # rented player keeps his row, priced as an upgrade.
        made = {(q["incoming"], q["outgoing"]): row for q, row in zip(upgrades, choices)}
        rented = {q["incoming"] for q in manager.move_log if q["kind"] in ("rental", "rental claim")}
        added = {q["incoming"] for q in manager.move_log}
        seen, ranked = set(), 0
        for q in options:                                # best first; each pickup with its best drop
            p, d = q["incoming"], q["outgoing"]
            if p in seen:
                continue
            seen.add(p)
            ranked += 1
            clears = q["gain"] > q["bar"]
            if (p, d) in made:                           # listed above, as the plan made it
                made[(p, d)]["rank"] = ranked
                continue
            note = ("rental in plan" if p in rented else "in plan, other drop" if p in added
                    else ("clears bar" if q["shortlisted"]
                          else f"clears bar, outside the rule's top {self.strategy.adddrop.shortlist}")
                    if clears else "below bar")
            choices.append({"rank": ranked, "kind": "Claim" if q["claim"] else "Add", "add": name(p),
                            "drop": name(d) if d is not None else None,
                            "gain": round(q["gain"], 1), "bar": round(q["bar"], 1),
                            "edge": round(q["gain"] - q["bar"], 1),
                            "clears": clears, "in_plan": False, "note": note,
                            "add_rate": priced(p), "drop_rate": priced(d) if d is not None else None,
                            "add_periph": self.peripheral(p),
                            "add_games": len(q["nights"].nights(p)),
                            "drop_games": len(q["nights"].nights(d)) if d is not None else None})
            if ranked == OPTIONS_SHOWN:
                break
        # The week mode's plan (weekplan.py): every move it would make this week, today's made above,
        # the later ones what it would do if nothing changes -- it plans again on every pass. Each
        # move is a team slot (weekplan.TeamPlans): its team and position, and the players on that
        # team who fit it, ranked -- any of them buys the same nights.
        def option_rows(options):
            return [{"add": name(o["incoming"]), "kind": MOVE_KINDS[o["kind"]],
                     "positions": "/".join(sorted(self.eligibility.get(o["incoming"], ()))),
                     "from": o["effective"].date().isoformat(), "add_rate": priced(o["incoming"]),
                     "add_periph": self.peripheral(o["incoming"]), "add_games": o["games"],
                     "status": "GTD" if o["incoming"] in gtd else state_status.get(o["incoming"]),
                     "gain": round(o["gain"], 1), "bar": round(o["bar"], 1),
                     "edge": round(o["gain"] - o["bar"], 1)} for o in options]

        def week_rows(planned):
            rows = []
            for m in planned:
                p, d = m["incoming"], m["outgoing"]
                rows.append({"day": m["day"].date().isoformat(), "from": m["effective"].date().isoformat(),
                             # The night the plan drops him for its next rental (his games stop there).
                             "until": m["released"].date().isoformat() if m.get("released") is not None else None,
                             "today": m["today"], "for_next_week": m["for_next_week"],
                             "kind": MOVE_KINDS[m["kind"]], "add": name(p),
                             "drop": name(d) if d is not None else None,
                             "add_rate": priced(p), "drop_rate": priced(d) if d is not None else None,
                             "add_periph": self.peripheral(p),
                             "add_games": m["incoming_games"],
                             "drop_games": m["outgoing_games"] if d is not None else None,
                             "gain": round(m["gain"], 1), "bar": round(m["bar"], 1),
                             "edge": round(m["gain"] - m["bar"], 1),
                             "pinned": m.get("pinned", False),       # the user's pick (choices.py)
                             "team": teams.get(m["team"]) if m.get("team") is not None else None,
                             # The pick's own positions: a slot takes any skater (or a goalie).
                             "pos": "/".join(sorted(self.eligibility.get(p, ()))) or m.get("group"),
                             "expected": None if m.get("expected") is None else round(m["expected"], 1),
                             "options": option_rows(m.get("options", [])),
                             # Other teams' near ties (TeamPlans.near_ties), display only.
                             "near": [{"add": name(n["incoming"]),
                                       "team": teams.get(n["team"]) if n.get("team") is not None else None,
                                       "positions": "/".join(sorted(self.eligibility.get(n["incoming"], ()))),
                                       "add_rate": priced(n["incoming"]), "add_games": n["games"],
                                       "delta": round(n["delta"], 1)} for n in m.get("near", [])]})
            return rows
        week_plan = week_rows(manager.plan.week_plan)
        # The Week tab's calendar (weekplan.WeekPlanner.nights_view): each night, who starts in
        # which slot on the roster the plan holds, who it holds, its open slots and moves. A
        # rental is anyone not on the roster this morning.
        mine = set(before["roster"])
        person = lambda q: {"player_id": int(q), "player": name(q)}

        def night_rows(nights):
            return [{"day": n["night"].date().isoformat(), "points": round(n["points"], 1),
                     "started": [{**person(q), "slot": slot, "pts": round(value, 2),
                                  "new": q not in mine} for slot, q, value in n["started"]],
                     "open": n["open"], "held": [person(q) for q in n["held"]],
                     "moves": [{"add": person(m["incoming"]),
                                "drop": person(m["outgoing"]) if m["outgoing"] is not None else None,
                                "kind": MOVE_KINDS["rental claim" if m["kind"] == "claim" else "rental"],
                                "pinned": m.get("pinned", False)} for m in n["moves"]]}
                    for n in nights]

        def fit_rows(fits):
            return {night.date().isoformat(): [{**person(q), "positions": "/".join(sorted(self.eligibility.get(q, ()))),
                                                "gain": round(gain, 1), "games": games,
                                                "rate": priced(q)} for q, gain, games in found]
                    for night, found in fits.items()}

        # The plan made (A) and the alternatives (B, C, ...), each opening on a different team.
        def team_plan(i, w):
            slot_list = week_rows(w["moves"])
            return {
                "nights": night_rows(w.get("nights", [])),
                **({"fits": fit_rows(w["fits"])} if "fits" in w else {}),
                "label": "ABCDEFGHIJKLMNOP"[i], "first": name(w["first"]) if w["first"] is not None else None,
                "week_gain": round(w["week_gain"], 1), "week_edge": round(w["week_edge"], 1),
                "expected": None if w.get("expected") is None else round(w["expected"], 1),
                "games": w.get("games"), "thinnest": w.get("thinnest"),
                "without": [teams.get(t, "?") for t in w.get("without", [])],
                # The plan's schedule: "Tue NYR", one per slot (a slot takes any skater, so no
                # position; the slot rows show the pick's).
                "schedule": [f"{pd.Timestamp(m['day']).strftime('%a')} {m['team'] or '?'}"
                             for m in slot_list],
                "moves": slot_list}
        week_plans = [team_plan(i, w) for i, w in enumerate(manager.plan.week_plans)]
        # Today's rentals the upgrades left no room for (the week is planned without them).
        for r, why in manager.plan.week_conflicts:
            problems.append(f"today's rental {name(r['incoming'])}"
                            + (f" for {name(r['outgoing'])}" if r["outgoing"] is not None else "")
                            + f" clashes with today's upgrades ({why}) -- not made; the week plan "
                            "is planned as if no upgrade is made, so choose between them")
        # The user's picks the plan could not put in (weekplan.WeekPlanner.seeded).
        for day_, q, why in (manager.plan.week_plans[0].get("skipped", []) if manager.plan.week_plans else []):
            problems.append(f"your pick {name(q)} on {pd.Timestamp(day_):%a %b %d} was left out: {why}")
        to_ir = [name(p) for p in me.ir if p not in before["ir"]]
        off_ir = [name(p) for p in before["ir"] if p not in me.ir]
        dropped = [name(p) for p in before["roster"] + before["ir"]
                   if p not in me.roster and p not in me.ir and not any(m["drop"] == name(p) for m in moves)]

        def slot_rows(lineup):
            slots = []
            for index, label in enumerate(self.slot_order):
                p = lineup.assigned.get(index)
                if p is None:
                    slots.append({"slot": label, "player": None})
                    continue
                mu, sd = moments.get(p, (0.0, 0.0))
                row = by_player.loc[p] if p in by_player.index else None
                slots.append({"slot": label, "player": name(p), "player_id": int(p),
                              "mean": round(mu, 2), "sd": round(sd, 2),
                              "puck_utc": str(row["start_time_utc"]) if row is not None else None,
                              "locked": bool(row is not None and pd.notna(row["start_time_utc"])
                                             and pd.Timestamp(row["start_time_utc"]) <= pd.Timestamp(now)),
                              "p_plays": round(float(row["p_plays"] if row["kind"] == "skater" else row["p_start"]), 3) if row is not None else None,
                              "flag": ("GTD" if p in gtd else state_status.get(p)) if (p in gtd or state_status.get(p) not in (None, "ACTIVE")) else None,
                              "stats": per_game(p)})
            return slots
        slots = slot_rows(lineup)
        bench = [name(p) for p in me.roster if p not in lineup.assigned.values()]

        # Before the moves (the window): the lineup over the roster as it stands, each slot with the
        # player the plan would put there after its moves; every player's recommended action.
        slots_now = slot_rows(lineup_now)
        for index, slot in enumerate(slots_now):
            after = lineup.assigned.get(index)
            slot["after_moves"] = name(after) if after is not None and after != lineup_now.assigned.get(index) else None
        action = {}
        for t in state.transactions:
            if t["team"] == snapshot.me:
                action[t["player_id"]] = t["kind"]
                if t["dropped"] is not None:
                    action[t["dropped"]] = "drop"
        action.update({p: "claim" for p, teams_ in state.pending_claims.items() if snapshot.me in teams_})
        action.update({p: "move to IR" for p in me.ir if p not in before["ir"]})
        action.update({p: "activate" for p in before["ir"] if p not in me.ir})
        action.update({p: "drop" for p in before["roster"] + before["ir"] if p not in me.roster and p not in me.ir})
        me_now = state_now.teams[snapshot.me]
        watch = [{"player": name(p), "status": ("GTD" if p in gtd else state_status.get(p)),
                  "note": _note(by_player.loc[p, "note"]) if p in by_player.index and "note" in by_player.columns else None}
                 for p in me.roster + me.ir if p in gtd or state_status.get(p) not in (None, "ACTIVE")]
        goalie_notes = rows[(rows["kind"] == "goalie") & rows["player_id"].isin(me.roster)]
        return {
            "generated_at": now.isoformat(timespec="minutes") + "Z", "game_date": self.day.isoformat(),
            "games_today": bool(len(rows)),
            "league_source": snapshot.source, "platform": PLATFORM_NAMES.get(self.league.platform, "the platform"),
            "team": snapshot.teams[snapshot.me]["name"],
            "week": v.week, "moves_left": v.moves_left, "free_moves": state.free_moves,
            "moves_used_before": before["moves_used"],
            "matchup_z": round(z, 2), "p_win": round(statistics.NormalDist().cdf(z), 3),
            "opponent": snapshot.teams[snapshot.opponent]["name"] if snapshot.opponent is not None else None,
            "ir_to": to_ir, "ir_off": off_ir, "moves": moves, "claims": claims, "other_drops": dropped,
            "options": choices, "week_plan": week_plan, "week_plans": week_plans,
            # On the week's last day, next week's plans too (_next_week_plans); else none.
            # What the user chose (Live/choices.py) as the plan applied it -- player ids.
            "choices": self.choices,
            # After the day's first puck, in a league that holds adds until tomorrow
            # (rules.add_effective): the day today's adds take effect; else None.
            "moves_from": (self.day + dt.timedelta(days=1)).isoformat() if self.adds_locked else None,
            "next_week": next_week, "next_week_plans": [team_plan(i, w) for i, w in enumerate(next_plans)],
            "stream_mode": self.strategy.streaming.mode, "horizon_weeks": self.strategy.adddrop.horizon_weeks,
            "shortlist": self.strategy.adddrop.shortlist,
            "lineup": slots, "bench": bench, "watch": watch,
            "goalies": [{"player": name(int(r.player_id)), "p_start": round(float(r.p_start), 3),
                         "note": _note(r.note)} for r in goalie_notes.itertuples()],
            "problems": problems, "sim_season": self.sim_season, "rate_source": source,
            "plan_log": manager.plan.log[-1] if manager.plan.log else {},
            # For the plan window's tables (not in the Markdown): the whole roster, the best free
            # agents, the opponent, the week's points so far.
            "my_week_points": snapshot.my_week_points, "opponent_week_points": snapshot.opponent_week_points,
            # Both as the league stands now; `plan` is the recommended action (add, drop, ...).
            "lineup_now": slots_now,
            "bench_now": [name(p) for p in me_now.roster if p not in lineup_now.assigned.values()],
            "bench_now_stats": [per_game(p) for p in me_now.roster if p not in lineup_now.assigned.values()],
            "stat_columns": [[k, draft_board.STAT_HEADINGS[k]] for k in self.stat_keys],
            "stats_basis": "season so far" if len(so_far) else "projected season",
            "roster": [self._player_row(v, p, name, priced, state_status, gtd, by_player,
                                        lineup_ids=set(lineup_now.assigned.values()), on_ir=p in me_now.ir,
                                        plan=action.get(p))
                       for p in me_now.roster + me_now.ir],
            "free_agents": [self._player_row(v, p, name, priced, state_status, gtd, by_player,
                                             waivers=state_now.on_waivers(p, v.day), plan=action.get(p))
                            for p in sorted(state_now.free_agents(), key=priced, reverse=True)[:FREE_AGENTS_SHOWN]],
            "opponent_roster": ([self._player_row(v, p, name, priced, state_status, gtd, by_player)
                                 for p in state.teams[snapshot.opponent].roster]
                                if snapshot.opponent is not None else []),
        }

    def _player_row(self, v, p, name, priced, state_status, gtd, by_player, lineup_ids=None,
                    on_ir=False, waivers=False, plan=None) -> dict:
        """One player for the plan window's tables."""
        row = by_player.loc[p] if p in by_player.index else None
        tonight = None
        if row is not None:
            tonight = float(row["p_plays"] if row["kind"] == "skater" else row["p_start"])
        return {"player_id": int(p), "player": name(p),
                "positions": "/".join(sorted(self.eligibility.get(p, ()))),
                "status": "GTD" if p in gtd else state_status.get(p) if state_status.get(p) != "ACTIVE" else None,
                "rate": priced(p), "per_game": round(v.projected_rate(p), 2),
                "peripheral": self.peripheral(p),
                # The rate times his team's games left in the fantasy season (its playoffs included).
                "ros_points": round(valuation_module.player_value(
                    v, p, None, self.strategy.adddrop.rate_source), 1),
                "games_left": v.games_remaining(p), "plays_tonight": tonight,
                "in_lineup": lineup_ids is not None and p in lineup_ids, "on_ir": on_ir,
                "on_waivers": bool(waivers), "plan": plan, "stats": self._season_stats(p)}


def render(plan: dict) -> str:
    """The plan as a Markdown file, action items first."""
    lines = [f"# Lineup plan -- {plan['team']}, {plan['game_date']}",
             f"Generated {plan['generated_at']} UTC · week {plan['week']} · vs {plan['opponent'] or '-'} · "
             f"P(win) {plan['p_win']:.0%} (z {plan['matchup_z']:+.2f}) · moves left after this plan: {plan['moves_left']}",
             f"League data: {plan['league_source']}", ""]
    if plan.get("free_moves"):
        lines[-1:-1] = [f"Before week {plan['week']}: today's moves count toward no week's limit."]
    actions = []
    actions += [f"- **IR:** move {p} to IR" for p in plan["ir_to"]]
    actions += [f"- **IR:** activate {p}" for p in plan["ir_off"]]
    for m in plan["moves"]:
        drop = f", drop {m['drop']} ({m['drop_rate']} pts/g)" if m["drop"] else ""
        hurt = f" -- reported {m['add_status']}" if m.get("add_status") not in (None, "ACTIVE") else ""
        actions.append(f"- **{m['kind'].capitalize()}:** add {m['add']} ({m['add_rate']} pts/g){hurt}{drop}")
    actions += [f"- **Waiver claim:** {c['claim']}" + (f", dropping {c['drop']}" if c["drop"] else "")
                + (f" ({c['note']})" if c.get("note") else "") for c in plan["claims"]]
    actions += [f"- **Drop:** {p}" for p in plan["other_drops"]]
    lines += ["## Moves", *(actions or ["- None today."]), ""]
    if plan.get("stream_mode") == "week":
        lines += ["## This week's streaming plan", "",
                  "Every rental the plan would make this week if nothing changes, as team slots: a day, an "
                  "NHL team and a position, which any of the players listed can fill -- they play the "
                  "same nights, so if the first is gone, take the next. Today's are the moves above; the "
                  "later ones are planned again on every run, as news and the free-agent pool change. "
                  "Gain = lineup points this week (a pickup for next week: next week's, made on the last "
                  "day of a week already won); bar = the drop cost (+ margin x sd); expected = the edge "
                  "you can expect from the slot when each player may be taken before you get there.", ""]
        if plan["week_plan"]:
            lines += ["| Day | Slot | Add | pts/g | games | Drop | Gain | Bar | Edge | Exp. | Next options | |",
                      "|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for w in plan["week_plan"]:
                day = pd.Timestamp(w["day"]).strftime("%a %b %d")
                start = "" if w["from"] == w["day"] else f" (from {pd.Timestamp(w['from']).strftime('%a')})"
                others = "; ".join(f"{o['add']} {o['edge']:+.1f}" for o in w.get("options", [])
                                   if o["add"] != w["add"])
                others = "; ".join(others.split("; ")[:3])
                expected = "" if w.get("expected") is None else f"{w['expected']:+.1f}"
                lines.append(f"| {day}{start} | {w.get('team') or ''} {w.get('pos') or ''} | {w['add']} | "
                             f"{w['add_rate']} | {w['add_games']} | {w['drop'] or '(open spot)'} | "
                             f"{w['gain']:+.1f} | {w['bar']:.1f} | {w['edge']:+.1f} | {expected} | {others} | "
                             f"{'**today, for next week**' if w.get('for_next_week') else '**today**' if w['today'] else 'planned'} |")
        else:
            lines.append("- No rentals worth a move this week.")
        lines.append("")
        if len(plan.get("week_plans", [])) > 1:
            a = plan["week_plans"][0]
            lines += [f"Plan A, above: {a['week_gain']:+.1f} lineup points this week, expected "
                      f"{a['expected']:+.1f}. The fallbacks when a team is picked over: each plan leaves out "
                      f"every earlier plan's first team (in the plan window, click a plan for its slots "
                      f"and a slot for its players):", "",
                      "| Plan | Schedule | Without | Moves | Games | Week | Exp. | vs A | Thinnest slot |",
                      "|---|---|---|---|---|---|---|---|---|"]
            for w in plan["week_plans"][1:]:
                lines.append(f"| {w['label']} | {' -> '.join(w['schedule'])} | {', '.join(w['without'])} | "
                             f"{len(w['moves'])} | {w['games']} | {w['week_gain']:+.1f} | {w['expected']:+.1f} | "
                             f"{w['expected'] - a['expected']:+.1f} | {w['thinnest']} option(s) |")
            lines.append("")
    if plan.get("options"):
        weeks = plan.get("horizon_weeks")
        window = "the rest of the season" if weeks is None else f"this week and the next {weeks}"
        lines += ["## Upgrade", "",
                  f"The plan's upgrades and claims first, then the best pickups on your roster as it "
                  f"stands, each with its best drop (rentals are in the week's streaming plan). Gain = lineup points over {window}; a move is made only when the gain clears "
                  f"the bar (its own sd x the margin, plus the claim premium for a claim). Ranked by "
                  f"edge = gain - bar, as the rule ranks them. The rule prices only its top "
                  f"{plan.get('shortlist', 10)} free agents by their own value over the window; a pickup "
                  f"outside them is not made even when it clears.", "",
                  "| # | Move | Add | pts/g | games | Drop | pts/g | games | Gain | Bar | Edge | |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        blank = lambda x, f="": "" if x is None else format(x, f)
        for o in plan["options"]:
            mark = f"**{o['note']}**" if o["in_plan"] else o["note"]
            lines.append(f"| {blank(o['rank'])} | {o['kind']} | {o['add']} | {o['add_rate']} | {blank(o['add_games'])} | "
                         f"{o['drop'] or '(open spot)'} | {blank(o['drop_rate'])} | {blank(o['drop_games'])} | "
                         f"{blank(o['gain'], '+.1f')} | {blank(o['bar'], '.1f')} | {blank(o['edge'], '+.1f')} | {mark} |")
        lines.append("")
    if plan.get("games_today", True):
        lines += ["## Tonight's lineup", "", "| Slot | Player | Exp. pts | P(plays/starts) | Puck (UTC) | Flag |",
                  "|---|---|---|---|---|---|"]
        for s in plan["lineup"]:
            if s["player"] is None:
                lines.append(f"| {s['slot']} | *(empty)* | | | | |")
            else:
                puck = (s["puck_utc"] or "")[11:16]
                lines.append(f"| {s['slot']} | {s['player']} | {s['mean']:.2f} | {s['p_plays'] if s['p_plays'] is not None else ''} | {puck} | {s['flag'] or ''} |")
        lines += ["", f"**Bench / not playing:** {', '.join(plan['bench']) or 'none'}", ""]
    else:
        lines += ["## Tonight's lineup", "", "No NHL games today: no lineup to set.", "",
                  f"**Roster:** {', '.join(plan['bench']) or 'none'}", ""]
    if plan["goalies"]:
        lines += ["## My goalies tonight", *[f"- {g['player']}: P(start) {g['p_start']:.0%}" + (f" ({g['note']})" if g["note"] else "")
                                           for g in plan["goalies"]], ""]
    if plan["watch"]:
        lines += ["## Watch list", *[f"- {w['player']}: {w['status']}" + (f" -- {w['note']}" if w["note"] else "")
                                    for w in plan["watch"]], ""]
    if plan["problems"]:
        lines += ["## Problems", *[f"- {p}" for p in plan["problems"]], ""]
    lines += ["---", f"Rates are points per team game as the move was priced ({plan['rate_source']}). Sampler fit: {plan['sim_season']}. "
              f"Recommend-only: make these moves on {plan.get('platform', 'the platform')} yourself.", ""]
    return "\n".join(lines)

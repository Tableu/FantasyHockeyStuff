"""How often a setting changes a decision in the backtest: the realistic league's test seat
(rung 17) plays the shipped strategy, and every day, before it moves, a shadow copy of it plans
the same day once per flipped setting -- on a deep copy of the league -- and is thrown away.
A day "fires" when the shadow's roster, IR or claims after its moves differ from the real
seat's, or (week mode) when its week plan differs. A day that fires is valued: the actual points
of the players the real seat holds and the shadow does not, less the shadow's, to the end of this
week and of next. The real season is untouched by the shadows: --verify checks a shadow equal to
the shipped strategy never fires and the seat scores the same. The live side of the comparison is
Server/settings_check.py (2026-10-28: replacement_depth 'best' changes the day's moves on ~17% of
game days in both leagues).

    python fire_rate.py --league espn-la --weights espn-la --strategy strategy-espn-la \
        --season 2024-25 --replication 0 --flip streaming.keep_upgrades=false --out r0.json
"""

import argparse
import copy
import json
from pathlib import Path

import pandas as pd

import corelayer  # noqa: F401 -- puts Core/ on sys.path; see corelayer.py
import engine as engine_module
import oneseat
import tune
from decisionlayer import managers as managers_module

RECORDS = []          # one row per (day, flip) for the test seat
SHADOWS = {}          # flip label -> strategy


def decision(state, team_index):
    team = state.teams[team_index]
    claims = sorted((p, state.claim_drops.get((team_index, p)))
                    for p, who in state.pending_claims.items() if team_index in who)
    return (sorted(team.roster), sorted(team.ir), claims, team.moves_used)


def held(state, team_index):
    """Who the team will have: its roster, plus pending claims, less the claims' drops."""
    team = state.teams[team_index]
    claimed = {p for p, who in state.pending_claims.items() if team_index in who}
    drops = {state.claim_drops.get((team_index, p)) for p in claimed}
    return (set(team.roster) | claimed) - drops


SEASONS = []
_season_init = engine_module.Season.__init__


def _capture(self, *a, **k):
    _season_init(self, *a, **k)
    SEASONS.append(self)


engine_module.Season.__init__ = _capture


def value(season, day, gained, lost):
    """Actual points of the players the real seat holds and the shadow does not (`gained`) less
    the shadow's (`lost`), from `day` to the end of this week and to the end of next week --
    every game counted, started or not, as engine's move grading does."""
    day = pd.Timestamp(day)
    week = season.calendar.week_of(day)
    if week is None:
        return None
    out = {}
    for name, last in (("week", week), ("next", min(week + 1, len(season.calendar.weeks)))):
        end = season.calendar.weeks[last - 1].end
        days = [d for d in season.calendar.days if day <= d <= end]
        score = lambda ps: sum(season.outcomes.score(d, p) for d in days for p in ps)
        out[name] = score(gained) - score(lost)
    return out


def week_plan(manager):
    return [(str(m.get("day")), m.get("incoming"), m.get("outgoing"))
            for m in manager.plan.week_plan]


def shadow_view(view):
    v = copy.copy(view)
    for name, value in vars(view).items():
        if name == "_state":
            v._state = copy.deepcopy(value)
        elif isinstance(value, (dict, set, list)):
            setattr(v, name, copy.copy(value))
    return v


original = managers_module.Orchestrated.transactions


def transactions(self, view):
    if self.rung != tune.SHIPPED or not SHADOWS:
        return original(self, view)
    shared = {id(x): x for x in (self.config, self.scoreset)}
    outcomes = {}
    for label, strategy in SHADOWS.items():
        shadow = copy.deepcopy(self, dict(shared))
        shadow.strategy = strategy
        shadow.plan.stream_params = strategy.streaming
        v = shadow_view(view)
        original(shadow, v)
        outcomes[label] = (decision(v._state, self.team_index), week_plan(shadow),
                           held(v._state, self.team_index))
    original(self, view)
    real = (decision(view._state, self.team_index), week_plan(self))
    mine = held(view._state, self.team_index)
    for label, (today, plan, theirs) in outcomes.items():
        RECORDS.append({"day": str(view.day.date()), "week": view.week, "flip": label,
                        "today": today != real[0], "plan": plan != real[1],
                        "real_moves": real[0][3], "shadow_moves": today[3],
                        "gained": sorted(int(p) for p in mine - theirs),
                        "lost": sorted(int(p) for p in theirs - mine)})


managers_module.Orchestrated.transactions = transactions


def test_seat_pts(ctx, layout, replication):
    table = ctx._seats(layout, None, [replication])
    return float(table.loc[(replication, layout.test_seat(ctx.config, replication)), "pts"])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--season", default="2024-25")
    p.add_argument("--league", default="league")
    p.add_argument("--weights", default="points-league")
    p.add_argument("--strategy", required=True)
    p.add_argument("--replication", type=int, required=True)
    p.add_argument("--flip", action="append", default=[], metavar="BLOCK.KEY=VALUE")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--out", required=True)
    args = p.parse_args()
    ns = argparse.Namespace(strategy=args.strategy, candidate=None, set=[], opponents="8",
                            opponent_sd=None, season=args.season, prior_season=None,
                            league=args.league, weights=args.weights, workers=1, ros_tag=None)
    ctx, shipped, _, base_layout, _, _ = oneseat.setup(ns)
    for flip in args.flip:
        SHADOWS[flip] = oneseat.with_settings(shipped, [flip])
    if args.verify:
        SHADOWS["identity"] = shipped
    result = {"replication": args.replication, "season": args.season, "league": args.league,
              "pts": test_seat_pts(ctx, base_layout, args.replication), "records": RECORDS}
    season = SEASONS[-1]
    for r in RECORDS:
        if r["today"] and (r["gained"] or r["lost"]):
            r["value"] = value(season, r["day"], r["gained"], r["lost"])
    if args.verify:
        SHADOWS.clear()
        RECORDS_before = len(RECORDS)
        plain_pts = test_seat_pts(ctx, base_layout, args.replication)
        fired = sum(r["today"] or r["plan"] for r in RECORDS if r["flip"] == "identity")
        result["verify"] = {"identity_fired": fired, "pts_with_shadows": result["pts"],
                            "pts_without": plain_pts, "records_added": len(RECORDS) - RECORDS_before}
        print("verify:", result["verify"])
    Path(args.out).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()

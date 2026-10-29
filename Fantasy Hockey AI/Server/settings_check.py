#!/usr/bin/env python
"""How often each of a league's live choices changes its plan: today's kept read of the league
(planpass.last_read_path) planned with the league's strategy, then once with each add/drop or
streaming value that differs from strategy.json put back to strategy.json's. One line a day per
league is appended to reports/<league>/settings_check.jsonl; the plan saved for the app is
untouched. The backtest's side of the comparison counts the same thing on a shadow copy of its test
seat (2026-10-28: e.g. replacement_depth 'best' changes the day's moves on ~17% of game days).

    python settings_check.py                  # every active league with a read from today
    python settings_check.py --league espn-la
    python settings_check.py --dry-run        # printed, nothing appended

A league with no read from today is skipped: the projections and injury files are the day's own, so
an older read cannot be planned again as it was. Scheduled daily by scheduled.py ("settings_check").
"""

import argparse
import dataclasses
import datetime as dt
import json
import sys

import leagues as registry
import live
import livepaths
import planpass
from decisionlayer import load_strategy

BLOCKS = ("adddrop", "streaming")


def live_choices(strategy, default) -> list:
    """(block, key, default value) for every add/drop or streaming value the league sets its own way."""
    return [(block, f.name, getattr(getattr(default, block), f.name))
            for block in BLOCKS for f in dataclasses.fields(getattr(strategy, block))
            if getattr(getattr(strategy, block), f.name) != getattr(getattr(default, block), f.name)]


def with_value(strategy, block, key, value):
    return dataclasses.replace(strategy, **{block: dataclasses.replace(getattr(strategy, block),
                                                                       **{key: value})})


def signature(result) -> tuple:
    """Today's moves and claims, and the week plan, as comparable lists."""
    today = sorted([m.get("kind"), m.get("add"), m.get("drop")] for m in result["moves"])
    today += sorted(["claim", c.get("add"), c.get("drop")] for c in result.get("claims", []))
    plan = [[str(m.get("day")), m.get("add"), m.get("drop")] for m in result.get("week_plan", [])]
    return today, plan


def check(league) -> dict | None:
    kept = planpass.last_read_path(league)
    if not kept.exists():
        print(f"{league.name}: no read kept")
        return None
    read_at = dt.datetime.fromtimestamp(kept.stat().st_mtime)
    if read_at.date() != dt.date.today():
        print(f"{league.name}: the last read is from {read_at:%Y-%m-%d %H:%M}, not today -- skipped")
        return None
    snapshot = live.LeagueSnapshot.load(kept)
    runner = live.LiveRunner(read_at.date(), league)
    shipped = runner.strategy
    today, plan = signature(runner.plan(snapshot, read_at))
    line = {"day": read_at.date().isoformat(), "read_at": read_at.isoformat(timespec="minutes"),
            "today": today, "plan": plan, "choices": {}}
    # The league's file against strategy.json's -- not runner.strategy, which carries the live
    # plan's own min_gain floor (live.LIVE_MIN_GAIN), a rule of the live plan and not a choice.
    for block, key, value in live_choices(load_strategy(league.strategy), load_strategy()):
        runner.strategy = with_value(shipped, block, key, value)
        other_today, other_plan = signature(runner.plan(snapshot, read_at))
        line["choices"][f"{block}.{key}"] = {
            "default": value, "today_differs": other_today != today, "plan_differs": other_plan != plan,
            "today": other_today, "plan": other_plan}
        print(f"{league.name} {block}.{key} -> {value!r}: today "
              f"{'DIFFERS' if other_today != today else 'same'}, week plan "
              f"{'DIFFERS' if other_plan != plan else 'same'}")
    runner.strategy = shipped
    return line


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--league", action="append", help="A registry league (default: every active one)")
    p.add_argument("--dry-run", action="store_true", help="Print the check, append nothing")
    args = p.parse_args()
    leagues = [registry.load(name) for name in args.league] if args.league else registry.active()
    for league in leagues:
        line = check(league)
        if line is None or args.dry_run:
            continue
        out = livepaths.ensure(livepaths.league_reports(league.name)) / "settings_check.jsonl"
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, default=str) + "\n")
        print(f"{league.name}: {len(line['choices'])} choice(s) checked -> {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

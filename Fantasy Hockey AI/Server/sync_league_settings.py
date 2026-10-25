#!/usr/bin/env python
"""Detects a league's settings from its platform and writes them, so no rules file is typed:

    Settings/rosters/<league>.json    teams, slots, bench, IR, moves, waivers, locks, schedule, playoffs
    Settings/scoring/<league>.json    points per stat

What the settings MEAN is Season/league.py's (RULE_CHOICES and the rest of the rosters schema); how
a platform spells them is its adapter's `league_settings()` (Live/platforms/). Nothing in a written
file is edited by hand: what no platform reports (a backtest's merged first week, a replayed
season's shorter schedule) is the registry's `overrides` (Settings/leagues/<league>.json), laid
over the detected settings. What a league does that the schema cannot hold -- a scored stat our
projections do not carry, divisions -- is listed in the file's description and printed.

Each write is compared with the file it replaces, and every changed setting is printed: a league
that changed a rule mid-season shows up here, not in a plan that quietly used the new one.

Usage:
    python sync_league_settings.py --league <name>          # prints what it detects, and the changes
    python sync_league_settings.py --league <name> --write  # writes both files + points the registry at them
    python sync_league_settings.py --check                   # offline: saved answers -> the files in use
"""

import argparse
import datetime as dt
import json
from pathlib import Path

import seasonlayer  # noqa: F401 -- puts Season/ on sys.path; see seasonlayer.py
import leagues
import livepaths
import paths
import platforms
import league as league_module


def merged(base: dict, over: dict) -> dict:
    """`base` with `over` laid on it, nested dicts key by key."""
    out = dict(base)
    for key, value in over.items():
        out[key] = merged(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def changes(old, new, path="") -> list:
    """["schedule.regular_season_weeks: 24 -> 23", ...] between two settings dicts."""
    if isinstance(old, dict) and isinstance(new, dict):
        return [c for key in sorted(set(old) | set(new), key=str)
                for c in changes(old.get(key), new.get(key), f"{path}.{key}" if path else str(key))]
    return [] if old == new else [f"{path}: {json.dumps(old)} -> {json.dumps(new)}"]


def detect(league) -> tuple:
    """(rosters dict, scoring dict, notes) for a registry league, read from its platform."""
    adapter = platforms.for_league(league)
    if adapter is None or not hasattr(adapter, "league_settings"):
        raise SystemExit(f"{league.name}: its platform ({league.platform}) cannot report settings yet")
    return assemble(league, adapter.league_settings())


def from_fixture(league) -> dict:
    """An adapter's league_settings() on the platform answers saved in fixtures/settings/
    (<platform>-<league id>/), offline."""
    from platforms import espn, fleaflicker
    folder = livepaths.FIXTURES_DIR / "settings" / f"{league.platform}-{league.league_id}"
    read = lambda name: json.loads((folder / name).read_text(encoding="utf-8"))
    if league.platform == "espn":
        return espn.settings_from(read("mSettings.json")["settings"])
    return fleaflicker.settings_from(read("FetchLeagueRules.json"),
                                     (folder / "rules.html").read_text(encoding="utf-8"),
                                     read("FetchLeagueScoreboard.json"), read("FetchLeagueDraftBoard.json"),
                                     read("teams.json")["teams"])


def check() -> int:
    """Every readable league's saved platform answers translate to exactly the settings files it
    uses (descriptions aside): a translation change that moves a setting fails here, offline."""
    failures = 0
    for league in leagues.all_leagues():
        if not league.readable:
            continue
        rosters, scoring, _ = assemble(league, from_fixture(league))
        found = []
        for name, new in ((f"rosters/{league.rules}", rosters), (f"scoring/{league.scoring}", scoring)):
            old = json.loads((paths.SETTINGS_DIR / f"{name}.json").read_text(encoding="utf-8"))
            found += [f"{name}: {c}" for c in changes(
                {k: v for k, v in old.items() if k != "description"},
                {k: v for k, v in new.items() if k != "description"})]
        failures += bool(found)
        print(f"  {'FAIL' if found else 'PASS'}  {league.name}: "
              + ("; ".join(found) if found else "fixture translates to its settings files"))
    return 1 if failures else 0


def assemble(league, found: dict) -> tuple:
    """(rosters dict, scoring dict, notes): an adapter's league_settings() with the registry's
    overrides laid over it, checked by the harness."""
    rules = found["settings"]["rules"]
    rules["ir_eligible"] = sorted(rules["ir_eligible"], key=league_module.IR_STATUSES.index)
    source = f"{league.platform} league {league.league_id}"
    notes = found["notes"]
    rosters = {"name": league.name,
               "description": (f"Detected from {source} on {dt.date.today()} (sync_league_settings.py)"
                               + (f"; registry overrides: {json.dumps(league.overrides)}" if league.overrides else "")
                               + (". Not held by these settings: " + "; ".join(notes) if notes else "") + "."),
               **merged(found["settings"], league.overrides),
               "eligibility_platform": league.eligibility_platform,
               "eligibility_season": league.season}
    scoring = {"name": league.name, "description": f"Detected from {source} on {dt.date.today()}.",
               **found["scoring"]}
    league_module.LeagueConfig(**rosters)          # the harness accepts it, or this fails loudly
    return rosters, scoring, notes


def sync(league, write: bool) -> list:
    """Detect, compare with the files in place, and (with `write`) replace them. Returns the
    changes, descriptions aside."""
    rosters, scoring, notes = detect(league)
    targets = {paths.ROSTERS_DIR / f"{league.name}.json": rosters,
               paths.SCORESETS_DIR / f"{league.name}.json": scoring}
    # Compared with the files the league uses now (the registry's rules and scoring), which are
    # these same files once a league has been synced.
    in_use = [paths.ROSTERS_DIR / f"{league.rules}.json", paths.SCORESETS_DIR / f"{league.scoring}.json"]
    found = []
    for (path, new), current in zip(targets.items(), in_use):
        old = json.loads(current.read_text(encoding="utf-8")) if current.exists() else {}
        found += [f"{path.parent.name}: {c}" for c in changes(
            {k: v for k, v in old.items() if k != "description"},
            {k: v for k, v in new.items() if k != "description"})]
        if write:
            path.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if write and (league.rules, league.scoring) != (league.name, league.name):
        raw = json.loads(league.path.read_text(encoding="utf-8"))
        raw["rules"], raw["scoring"] = league.name, league.name
        league.path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return found, notes, targets


def history_path(league) -> Path:
    return livepaths.league_reports(league.name) / "settings_changes.json"


def daily(league, echo=print) -> None:
    """The server's daily sync (planpass.Planner): detect and write, and log any change -- or a
    read that failed, which keeps the files in place -- to reports/<league>/settings_changes.json,
    where the plan's problems line finds it (`recent`). Never raises: a platform that is down or a
    rules page that changed must not stop the plan."""
    entry = {"at": dt.datetime.now().isoformat(timespec="seconds")}
    try:
        found, notes, _ = sync(league, write=True)
        entry["changes"] = found
        echo(f"{league.name} settings: " + ("; ".join(found) if found else "unchanged"))
    except (Exception, SystemExit) as error:          # noqa: BLE001 -- reported, the plan goes on
        entry["error"] = f"{type(error).__name__}: {error}"
        echo(f"{league.name} settings not read ({entry['error']}); the saved ones stand")
    if entry.get("changes") or entry.get("error"):
        path = history_path(league)
        log = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(log + [entry], indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def recent(league_name: str, day: dt.date, days: int = 7) -> list:
    """The plan's lines for settings changes (and failed reads) in the `days` before `day`."""
    path = livepaths.league_reports(league_name) / "settings_changes.json"
    if not path.exists():
        return []
    out = []
    for entry in json.loads(path.read_text(encoding="utf-8")):
        at = dt.datetime.fromisoformat(entry["at"])
        if not (day - dt.timedelta(days=days) <= at.date() <= day):
            continue
        if entry.get("changes"):
            out.append(f"the league's settings changed ({at:%a %b %d}): {'; '.join(entry['changes'])} "
                       "-- the plan uses the new ones")
        elif entry.get("error") and at.date() == day:
            out.append(f"the league's settings could not be read today ({entry['error']}); the plan "
                       "uses the last ones read")
    return out


def main():
    parser = argparse.ArgumentParser(description="Detect a league's settings from its platform")
    parser.add_argument("--league", help="A league in Settings/leagues/")
    parser.add_argument("--check", action="store_true",
                        help="Translate the saved platform answers (fixtures/settings/) and compare, offline")
    parser.add_argument("--write", action="store_true", help="Write the files and point the registry at them")
    args = parser.parse_args()
    if args.check:
        raise SystemExit(check())
    if not args.league:
        parser.error("--league is required (or --check)")
    league = leagues.load(args.league)
    found, notes, targets = sync(league, args.write)
    if not args.write:
        for new in targets.values():
            print(json.dumps(new, indent=2, ensure_ascii=False))
    for note in notes:
        print("   not held:", note)
    print(f"{len(found)} change(s) from the files in place" + (":" if found else ""))
    for change in found:
        print("  ", change)
    if args.write:
        print("-> " + "\n-> ".join(map(str, targets)))


if __name__ == "__main__":
    main()

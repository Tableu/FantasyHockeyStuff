#!/usr/bin/env python
"""Writes a league's rules and scoring files from what its platform reports, instead of typing them:

    Settings/rosters/<league>.json    teams, slots, bench, IR, moves, waivers, schedule, playoffs
    Settings/scoring/<league>.json    points per stat

and points the league's registry file at them. Where the league uses a rule the backtest harness
does not model (Season/league.py SUPPORTED_RULES: a daily lock, rolling waivers, a weekly move
limit), the file takes the nearest supported value and says so in its description -- the live
runner reads the real rule from the platform, and nothing is dropped without a word. The same goes
for a scored stat our projections do not carry.

Today: ESPN (platforms/espn.py rules() + scoring()).

Usage:
    python import_league_settings.py --league <name>          # prints both files
    python import_league_settings.py --league <name> --write  # writes them + the registry
"""

import argparse
import json

import seasonlayer  # noqa: F401 -- puts Season/ on sys.path; see seasonlayer.py
import leagues
import paths
import platforms
import league as league_module

UNLIMITED_MOVES = 99      # the harness needs a number; no real week comes near it


def roster_rules(league, rules: dict) -> tuple:
    """(Settings/rosters dict, [differences from the platform's own rules])."""
    differences = []
    if rules["lineup_lock"] != "DAILY":
        differences.append(f"lineup lock {rules['lineup_lock']} (the harness locks daily)")
    if rules["waivers"] != "WAIVERS_CONTINUOUS":
        differences.append(f"waivers {rules['waivers']} (the harness rolls them)")
    if rules["acquisition_limit"] is None:
        differences.append(f"no acquisition limit (written as {UNLIMITED_MOVES} a week)")
    if rules.get("max_goalies"):
        differences.append(f"at most {rules['max_goalies']} goalies rostered (not enforced by the harness)")
    rounds = next(r for r in (1, 2, 3, 4) if 2 ** r >= rules["playoff_teams"])
    slot_positions = {**{k: v for k, v in rules["slot_positions"].items()}, "G": ["G"]}
    out = {
        "name": league.name,
        "description": (f"Generated from {league.platform} league {league.league_id} "
                        f"(import_league_settings.py). Differs from the platform: "
                        + ("; ".join(differences) if differences else "nothing") + "."),
        "teams": rules["teams"],
        "active_slots": rules["slots"],
        "bench": rules["bench"],
        "ir": rules["ir"],
        "moves_per_week": rules["acquisition_limit"] or UNLIMITED_MOVES,
        "moves_carry_over": False,
        "waiver_days": max(1, round(rules["waiver_hours"] / 24)),
        "ties": "split",
        "schedule": {"type": "round_robin", "regular_season_weeks": rules["regular_season_weeks"],
                     "week_starts_on": "MON", "min_first_week_games": 20},
        "draft": {"type": "snake", "order": "lottery", "keepers": 0},
        "playoffs": {"teams": rules["playoff_teams"], "byes": 2 ** rounds - rules["playoff_teams"],
                     "rounds": rounds, "weeks_per_round": 1, "seeding": "record",
                     "tiebreak": "points_for"},
        "eligibility_platform": league.eligibility_platform,
        "eligibility_season": league.season,
        "slot_positions": slot_positions,
        "rules": {"lineup_lock": "daily", "waivers": "rolling", "ir_eligible": "injured",
                  "move_cost": {"add": 1, "claim": 1, "drop": 0, "ir_stash": 0, "ir_activate": 0}},
    }
    return out, differences


def scoring_file(league, scoring: dict) -> dict:
    unsupported = scoring["unsupported"]
    note = ("; not projected, so not scored here: "
            + ", ".join(f"{k} {v:+g}" for k, v in unsupported.items())) if unsupported else ""
    return {"name": league.name,
            "description": f"Generated from {league.platform} league {league.league_id}{note}.",
            "skaters": scoring["skaters"], "goalies": scoring["goalies"]}


def main():
    parser = argparse.ArgumentParser(description="Write a league's rules and scoring from its platform")
    parser.add_argument("--league", required=True, help="A league in Settings/leagues/")
    parser.add_argument("--write", action="store_true", help="Write the files and update the registry")
    args = parser.parse_args()
    league = leagues.load(args.league)
    adapter = platforms.for_league(league)
    if adapter is None or not hasattr(adapter, "scoring"):
        raise SystemExit(f"{league.name}: its platform ({league.platform}) cannot report settings yet")
    rosters, differences = roster_rules(league, adapter.rules())
    scoring = scoring_file(league, adapter.scoring())
    if not args.write:
        print(json.dumps(rosters, indent=2)); print(json.dumps(scoring, indent=2))
        return
    rosters_path = paths.ROSTERS_DIR / f"{league.name}.json"
    scoring_path = paths.SCORESETS_DIR / f"{league.name}.json"
    rosters_path.write_text(json.dumps(rosters, indent=2) + "\n", encoding="utf-8")
    scoring_path.write_text(json.dumps(scoring, indent=2) + "\n", encoding="utf-8")
    league_module.load(league.name)          # the harness accepts it, or this fails loudly
    raw = json.loads(league.path.read_text(encoding="utf-8"))
    raw["rules"], raw["scoring"] = league.name, league.name
    league.path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"-> {rosters_path}\n-> {scoring_path}\n-> {league.path} (rules, scoring)")
    for d in differences:
        print("   differs:", d)


if __name__ == "__main__":
    main()

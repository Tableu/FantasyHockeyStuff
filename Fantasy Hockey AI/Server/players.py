"""The phone app's Players screen (plan app-redesign.md, step 2): every free agent in the newest
plan -- his season so far in the league's scoring, ROS, whether he is on waivers -- and the nights
he plays the rest of this week (plan A's workbench), for the app's sorts and filters.

    players_view(plan, scoreset, workbench, photo, games_on)
                                              {day, columns, days: [{date, games}], rows}
"""

import roster


def players_view(plan: dict, scoreset, workbench: dict | None, photo=lambda player_id, team: None,
                 games_on=lambda date: None) -> dict:
    """`workbench`: plan A's (suggestions.plans_shown), for each player's nights left this week;
    `photo(player_id, team)`: his headshot's URL; `games_on(date)`: the NHL's games that night."""
    bench = workbench or {}
    nights = {int(k): p.get("nights", []) for k, p in (bench.get("players") or {}).items()}
    days = [{"date": d, "games": games_on(d)} for d in bench.get("nights", [])]
    rows = []
    for r in plan.get("free_agents", []):
        goalie = r["positions"] == "G"
        short, team = roster.split_name(r["player"])
        rows.append({"player_id": r["player_id"], "player": r["player"], "name": short, "team": team,
                     "positions": r["positions"], "goalie": goalie, "photo": photo(r["player_id"], team),
                     "flag": r.get("status"), "waivers": bool(r.get("on_waivers")),
                     "rate": r.get("rate"), "ros": r.get("ros_points"), "games_left": r.get("games_left"),
                     "nights": nights.get(int(r["player_id"]), []),
                     "season": roster.season(r.get("stats"), scoreset.goalies if goalie else scoreset.skaters, goalie)})
    columns = plan.get("stat_columns", [])
    return {"day": plan.get("game_date"), "days": days,
            "columns": {"skater": [c for c in columns if c[0] not in roster.GOALIE_KEYS and c[0] != "gp"],
                        "goalie": [c for c in columns if c[0] in roster.GOALIE_KEYS]},
            "rows": rows}

"""dailyfaceoff espn.csv -- mixed skaters + goalies, one row per player (Pos == 'G' for
goalies). No shorthanded-points or faceoff-win-percentage columns are provided (FOL isn't
in the sheet at all), so those stay NULL for this source. Goalie games played comes from
'GS' (games started) since 'GP' is blank on goalie rows.

Previously imported under the source name "All Points League" and read from a since-renamed
file (all_pts_league.csv, no longer present in Sheets/) -- same site, same column layout,
just renamed for consistency with the workbook's own "Dailyfaceoff" tab.
"""

import csv
from pathlib import Path

FILENAME = "dailyfaceoff espn.csv"


def _to_int(value):
    return int(round(float(value))) if value not in (None, "") else None


def _to_float(value):
    return float(value) if value not in (None, "") else None


def rows(sheets_dir: Path):
    with open(sheets_dir / FILENAME, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            is_goalie = r["Pos"] == "G"
            if is_goalie:
                gs, ga, sv = _to_int(r["GS"]), _to_int(r["GA"]), _to_int(r["SV"])
                stats = {
                    "GamesPlayed": gs,
                    "Wins": _to_int(r["W"]),
                    "Losses": _to_int(r["L"]),
                    "OvertimeLosses": _to_int(r["T/O"]),
                    "Shutouts": _to_int(r["SO"]),
                    "SavePercentage": _to_float(r["SV%"]),
                    "GoalsAgainstAverage": _to_float(r["GAA"]),
                    "GamesStarted": gs,
                    "GoalsAgainst": ga,
                    "Saves": sv,
                    # not in the sheet directly -- the only source of truth available.
                    "ShotsAgainst": (ga + sv) if ga is not None and sv is not None else None,
                }
            else:
                stats = {
                    "GamesPlayed": _to_int(r["GP"]),
                    "Goals": _to_int(r["G"]),
                    "Assists": _to_int(r["A"]),
                    "Points": _to_int(r["PTS"]),
                    "PowerPlayPoints": _to_int(r["PPP"]),
                    "Shots": _to_int(r["SOG"]),
                    "Hits": _to_int(r["HIT"]),
                    "Blocks": _to_int(r["BLK"]),
                    "PenaltyMinutes": _to_int(r["PIM"]),
                    "AverageTOIMinutes": _to_float(r["ATOI"]),
                    "PlusMinus": _to_int(r["(+/-)"]),
                    "PowerPlayGoals": _to_int(r["PPG"]),
                    "PowerPlayAssists": _to_int(r["PPA"]),
                    "FaceoffWins": _to_int(r["FOW"]),
                }
            yield {
                "raw_name": r["Player"],
                "team_raw": r["Team"],
                "is_goalie": is_goalie,
                "stats": stats,
            }

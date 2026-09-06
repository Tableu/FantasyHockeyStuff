"""dailyfaceoff espn.csv -- mixed skaters + goalies, one row per player (Pos == 'G' for
goalies). No shorthanded-points or faceoff-win-percentage columns are provided (FOL isn't
in the sheet at all), so those stay NULL for this source. Goalie games played comes from
'GS' (games started) since 'GP' is blank on goalie rows.

Every counting stat is read as a float, not rounded to an int -- this source genuinely
projects several of them (and goalie Shutouts) to fractional precision (e.g. A=29.9,
SOG=156.5, HIT=210.3, SO=1.1), and Projections.SkaterProjections/GoalieProjections' matching
columns are DECIMAL for exactly this reason. Rounding here would silently throw that
precision away before it ever reached the database.

Previously imported under the source name "All Points League" and read from a since-renamed
file (all_pts_league.csv, no longer present in Sheets/) -- same site, same column layout,
just renamed for consistency with the workbook's own "Dailyfaceoff" tab.
"""

import csv
from pathlib import Path

FILENAME = "dailyfaceoff espn.csv"


def _to_float(value):
    return float(value) if value not in (None, "") else None


def rows(sheets_dir: Path):
    with open(sheets_dir / FILENAME, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            is_goalie = r["Pos"] == "G"
            if is_goalie:
                gs, ga, sv = _to_float(r["GS"]), _to_float(r["GA"]), _to_float(r["SV"])
                stats = {
                    "GamesPlayed": gs,
                    "Wins": _to_float(r["W"]),
                    "Losses": _to_float(r["L"]),
                    "OvertimeLosses": _to_float(r["T/O"]),
                    "Shutouts": _to_float(r["SO"]),
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
                    "GamesPlayed": _to_float(r["GP"]),
                    "Goals": _to_float(r["G"]),
                    "Assists": _to_float(r["A"]),
                    "Points": _to_float(r["PTS"]),
                    "PowerPlayPoints": _to_float(r["PPP"]),
                    "Shots": _to_float(r["SOG"]),
                    "Hits": _to_float(r["HIT"]),
                    "Blocks": _to_float(r["BLK"]),
                    "PenaltyMinutes": _to_float(r["PIM"]),
                    "AverageTOIMinutes": _to_float(r["ATOI"]),
                    "PlusMinus": _to_float(r["(+/-)"]),
                    "PowerPlayGoals": _to_float(r["PPG"]),
                    "PowerPlayAssists": _to_float(r["PPA"]),
                    "FaceoffWins": _to_float(r["FOW"]),
                }
            yield {
                "raw_name": r["Player"],
                "team_raw": r["Team"],
                "is_goalie": is_goalie,
                "stats": stats,
            }

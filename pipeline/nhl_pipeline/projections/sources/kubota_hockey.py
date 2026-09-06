"""Kubota Hockey 2026-27 Projections -- skaters only. Every counting stat is projected to
fractional precision (e.g. GamesPlayed=84.0, Goals=40.31), so passed through as-is rather
than rounded, matching Dom/Apples & Ginos. The sheet's own "Fantasy Points" and "VORP"
columns are Kubota's derived rankings, not raw stat categories, so they aren't imported;
SHG/SHA (shorthanded goals/assists) have no matching column in SkaterProjections either --
only the combined ShortHandedPoints does.
"""

import csv
from pathlib import Path

FILENAME = "kubota_hockey_2026_2027.csv"


def _to_float(value):
    return float(value) if value not in (None, "") else None


def rows(sheets_dir: Path):
    with open(sheets_dir / FILENAME, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            fow, fol = _to_float(r["FOW"]), _to_float(r["FOL"])
            faceoff_pct = round(fow / (fow + fol), 4) if fow and (fow + fol) else None
            yield {
                "raw_name": r["Player"],
                "team_raw": r["Team"],
                "is_goalie": False,
                "stats": {
                    "GamesPlayed": _to_float(r["GP"]),
                    "Goals": _to_float(r["G"]),
                    "Assists": _to_float(r["A"]),
                    "Points": _to_float(r["PTS"]),
                    "Shots": _to_float(r["SOG"]),
                    "PenaltyMinutes": _to_float(r["PIM"]),
                    "PlusMinus": _to_float(r["+/-"]),
                    "PowerPlayGoals": _to_float(r["PPG"]),
                    "PowerPlayAssists": _to_float(r["PPA"]),
                    "PowerPlayPoints": _to_float(r["PPP"]),
                    "ShortHandedPoints": _to_float(r["SHP"]),
                    "Blocks": _to_float(r["BLK"]),
                    "Hits": _to_float(r["HIT"]),
                    "FaceoffLosses": fol,
                    "FaceoffWins": fow,
                    "FaceoffWinPct": faceoff_pct,
                },
            }

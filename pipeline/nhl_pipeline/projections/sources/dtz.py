"""Free Version DtZ NHL Fantasy Projections -- skaters only (no goalie rows)."""

import csv
from pathlib import Path

FILENAME = "DtZ 2026-2027 NHL Fantasy Projections - Skater Projections.csv"


def _to_int(value):
    return int(round(float(value))) if value not in (None, "") else None


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
                    "GamesPlayed": _to_int(r["GP"]),
                    "Goals": _to_int(r["Goals"]),
                    "Assists": _to_int(r["Assists"]),
                    "Points": _to_int(r["Points"]),
                    "PowerPlayPoints": _to_int(r["PP Points"]),
                    "ShortHandedPoints": _to_int(r["SHP"]),
                    "Shots": _to_int(r["SOG"]),
                    "Hits": _to_int(r["Hit"]),
                    "Blocks": _to_int(r["BLK"]),
                    "PenaltyMinutes": _to_int(r["PIM"]),
                    "AverageTOIMinutes": _to_float(r["Total TOI"]),
                    "FaceoffWinPct": faceoff_pct,
                    "PlusMinus": _to_int(r["+/-"]),
                    "PowerPlayGoals": _to_int(r["PPG"]),
                    "PowerPlayAssists": _to_int(r["PPA"]),
                    "FaceoffWins": _to_int(fow),
                    "FaceoffLosses": _to_int(fol),
                },
            }

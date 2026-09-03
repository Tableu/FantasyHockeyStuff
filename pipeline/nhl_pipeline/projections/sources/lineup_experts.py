"""Lineup Experts Hockey Fantasy Draft Cheat Sheet -- mixed skaters + goalies, one row per
player (Position == 'G' for goalies). No TOI, power-play/shorthanded-point, or shutout
columns are provided, so those stay NULL for this source.
"""

import csv
from pathlib import Path

FILENAME = "Lineup Experts Hockey Fantasy Draft Cheat Sheet.csv"


def _to_int(value):
    return int(round(float(value))) if value not in (None, "") else None


def _to_float(value):
    return float(value) if value not in (None, "") else None


def rows(sheets_dir: Path):
    with open(sheets_dir / FILENAME, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            is_goalie = r["Position"] == "G"
            if is_goalie:
                stats = {
                    "GamesPlayed": _to_int(r["GP"]),
                    "Wins": _to_int(r["W"]),
                    "Losses": _to_int(r["L"]),
                    "OvertimeLosses": _to_int(r["OTL"]),
                    "SavePercentage": _to_float(r["SV%"]),
                    "GoalsAgainstAverage": _to_float(r["GAA"]),
                }
            else:
                fow, fol = _to_float(r["FOW"]), _to_float(r["FOL"])
                stats = {
                    "GamesPlayed": _to_int(r["GP"]),
                    "Goals": _to_int(r["G"]),
                    "Assists": _to_int(r["AST"]),
                    "Points": _to_int(r["PTS"]),
                    "Shots": _to_int(r["SOG"]),
                    "Hits": _to_int(r["HIT"]),
                    "Blocks": _to_int(r["BLK"]),
                    "PenaltyMinutes": _to_int(r["PIM"]),
                    "FaceoffWinPct": round(fow / (fow + fol), 4) if fow and (fow + fol) else None,
                }
            yield {
                "raw_name": r["Player"],
                "team_raw": r["Team"],
                "is_goalie": is_goalie,
                "stats": stats,
            }

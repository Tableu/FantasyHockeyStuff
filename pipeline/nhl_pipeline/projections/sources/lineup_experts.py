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


def _to_signed_int(value):
    """'+/-' values are exported with a leading straight quote (e.g. "'-13", "'17") -- a
    spreadsheet text-format marker, not part of the number -- that plain _to_int/_to_float
    can't parse."""
    if value in (None, ""):
        return None
    return _to_int(value.lstrip("'"))


def rows(sheets_dir: Path):
    with open(sheets_dir / FILENAME, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            is_goalie = r["Position"] == "G"
            if is_goalie:
                ga, sv = _to_int(r["GA"]), _to_int(r["SV"])
                stats = {
                    "GamesPlayed": _to_int(r["GP"]),
                    "Wins": _to_int(r["W"]),
                    "Losses": _to_int(r["L"]),
                    "OvertimeLosses": _to_int(r["OTL"]),
                    "SavePercentage": _to_float(r["SV%"]),
                    "GoalsAgainstAverage": _to_float(r["GAA"]),
                    "GoalsAgainst": ga,
                    "Saves": sv,
                    # not in the sheet directly -- the only source of truth available.
                    "ShotsAgainst": (ga + sv) if ga is not None and sv is not None else None,
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
                    "PlusMinus": _to_signed_int(r["'+/-"]),
                    "FaceoffWins": _to_int(fow),
                    "FaceoffLosses": _to_int(fol),
                }
            yield {
                "raw_name": r["Player"],
                "team_raw": r["Team"],
                "is_goalie": is_goalie,
                "stats": stats,
            }

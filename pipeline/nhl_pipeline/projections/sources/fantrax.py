"""Fantrax 2026-27 Fantasy Projections workbook -- 'The List' is the one sheet with raw
season-total counting stats per player; the other 11 sheets (Team Comparison, per-position
F/C/LW/RW/D/G, ADP, Player Data, Standard Deviations, Settings) are all fantasy-scoring/
ranking views (FP, VORP, ADP) derived from it, not separate raw data.

Read positionally, not by header name: 'GP' appears twice in the header row (once for the
skater block, once for the goalie block), so a name-keyed dict would silently collide.
"""

from pathlib import Path

import openpyxl

FILENAME = "doms 2026-27-Fantasy-Projections-Fantrax.xlsx"
SHEET = "The List"


def _round(value):
    return round(value) if value is not None else None


def rows(sheets_dir: Path):
    wb = openpyxl.load_workbook(sheets_dir / FILENAME, data_only=True)
    ws = wb[SHEET]
    for r in ws.iter_rows(min_row=2, values_only=True):
        name = r[1]
        if not name:
            continue
        is_goalie = r[3] == "G"
        if is_goalie:
            stats = {
                "GamesPlayed": _round(r[35]),
                "Wins": _round(r[36]),
                "Losses": _round(r[37]),
                "OvertimeLosses": _round(r[38]),
                "Shutouts": _round(r[39]),
                "SavePercentage": r[42],
                "GoalsAgainstAverage": r[43],
            }
        else:
            stats = {
                "GamesPlayed": _round(r[16]),
                "AverageTOIMinutes": r[17],
                "Goals": _round(r[18]),
                "Assists": _round(r[19]),
                "Points": _round(r[20]),
                "Shots": _round(r[21]),
                "PowerPlayPoints": _round(r[23]),
                "ShortHandedPoints": _round(r[25]),
                "Blocks": _round(r[26]),
                "Hits": _round(r[27]),
                "PenaltyMinutes": _round(r[29]),
                "FaceoffWinPct": r[33],
            }
        yield {
            "raw_name": name,
            "team_raw": r[5],
            "is_goalie": is_goalie,
            "stats": stats,
        }

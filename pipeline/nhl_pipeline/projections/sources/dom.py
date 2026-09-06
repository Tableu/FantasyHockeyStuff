"""Fantrax 2026-27 Fantasy Projections workbook -- 'The List' is the one sheet with raw
season-total counting stats per player; the other 11 sheets (Team Comparison, per-position
F/C/LW/RW/D/G, ADP, Player Data, Standard Deviations, Settings) are all fantasy-scoring/
ranking views (FP, VORP, ADP) derived from it, not separate raw data.

Read positionally, not by header name: 'GP' appears twice in the header row (once for the
skater block, once for the goalie block), so a name-keyed dict would silently collide.

SHG (shorthanded goals) and GWG (game-winning goals) have no matching column in
SkaterProjections -- only the combined ShortHandedPoints does -- so those two are the only
skater-block columns this deliberately leaves out; every other counting stat the sheet
provides is imported.

Every counting stat is passed through as-is, not rounded -- this source projects everything
to long decimal precision (e.g. Goals=21.99, GamesPlayed=83.2625, PenaltyMinutes=33.9), and
Projections.SkaterProjections/GoalieProjections' matching columns are DECIMAL for exactly
this reason. Rounding here would silently throw that precision away before it ever reached
the database.
"""

from pathlib import Path

import openpyxl

FILENAME = "doms 2026-27-Fantasy-Projections-Fantrax.xlsx"
SHEET = "The List"


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
                "GamesPlayed": r[35],
                "Wins": r[36],
                "Losses": r[37],
                "OvertimeLosses": r[38],
                "Shutouts": r[39],
                "Saves": r[40],
                "GoalsAgainst": r[41],
                "SavePercentage": r[42],
                "GoalsAgainstAverage": r[43],
            }
        else:
            stats = {
                "GamesPlayed": r[16],
                "AverageTOIMinutes": r[17],
                "Goals": r[18],
                "Assists": r[19],
                "Points": r[20],
                "Shots": r[21],
                "PowerPlayGoals": r[22],
                "PowerPlayPoints": r[23],
                "ShortHandedPoints": r[25],
                "Blocks": r[26],
                "Hits": r[27],
                "PlusMinus": r[28],
                "PenaltyMinutes": r[29],
                "FaceoffWins": r[31],
                "FaceoffLosses": r[32],
                "FaceoffWinPct": r[33],
            }
        yield {
            "raw_name": name,
            "team_raw": r[5],
            "is_goalie": is_goalie,
            "stats": stats,
        }

"""Scott Cullen Projections -- skaters only, season-total counting stats. No faceoffs, TOI,
or PP/SH goal-assist split (only combined PPP), so those stay NULL for this source.
"""

import csv
from pathlib import Path

FILENAME = "Scott Cullen Projections.csv"


def _to_int(value):
    return int(round(float(value))) if value not in (None, "") else None


def rows(sheets_dir: Path):
    with open(sheets_dir / FILENAME, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["RANK"] == "RANK":
                # paginated export repeats its header row every ~82 rows.
                continue
            yield {
                "raw_name": r["PLAYER"],
                "team_raw": r["TEAM"],
                "is_goalie": False,
                "stats": {
                    "GamesPlayed": _to_int(r["GP"]),
                    "Goals": _to_int(r["G"]),
                    "Assists": _to_int(r["A"]),
                    "Points": _to_int(r["PTS"]),
                    "PlusMinus": _to_int(r["+/-"]),
                    "PowerPlayPoints": _to_int(r["PPP"]),
                    "PenaltyMinutes": _to_int(r["PIM"]),
                    "Hits": _to_int(r["HITS"]),
                    "Blocks": _to_int(r["BLOCKS"]),
                    "Shots": _to_int(r["SOG"]),
                },
            }

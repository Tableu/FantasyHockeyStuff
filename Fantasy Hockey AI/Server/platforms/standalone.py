"""No platform: a draft board built from the pick order alone (the draft tools' --standalone)."""


class Standalone:
    """A draft source with no platform behind it: the board is the pick order, picks are recorded
    by hand (the tools force --manual), and there are no playoff weeks or platform ids."""

    platform = "standalone"
    platform_name = None

    def __init__(self, teams: int, slot: int, rounds: int, order: str = "snake", my_name: str = "My team"):
        self.board = standalone_board(teams, slot, rounds, order, my_name)

    def draft_board(self) -> dict:
        return self.board

    def playoff_window(self, weeks: int):
        return None


def standalone_board(teams: int, slot: int, rounds: int, order: str = "snake",
                     my_name: str = "My team") -> dict:
    """A draft board for a league the tools cannot read (ESPN, Yahoo, a room with no API), in the
    shape Fleaflicker's FetchLeagueDraftBoard returns, so everything downstream -- picks_from,
    team_id_for, manual_cells, the redraw -- runs unchanged. Seat `slot` (1-based) is mine; the
    others are "Team N". `order` is "snake" (reverses every round) or "linear"."""
    if not 1 <= slot <= teams:
        raise SystemExit(f"--slot must be between 1 and {teams}")
    seats = [{"id": seat, "name": my_name if seat == slot else f"Team {seat}"}
             for seat in range(1, teams + 1)]
    rows, overall = [], 0
    for round_number in range(1, rounds + 1):
        seats_this_round = seats if order == "linear" or round_number % 2 == 1 else seats[::-1]
        cells = []
        for team in seats_this_round:
            overall += 1
            cells.append({"slot": {"overall": overall, "round": round_number}, "team": team})
        rows.append({"cells": cells})
    return {"draftOrder": seats, "rows": rows}

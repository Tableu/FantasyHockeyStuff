"""GET https://www.nhl.com/scores/htmlreports/{season}/TH|TV{gametype}{gameno}.HTM --
the NHL's static "TOI Shift Report" pages (TH = home team, TV = visitor), used as a
fallback when the shiftcharts JSON endpoint returns an empty data[].

That happens for whole stretches of a season: verified 2026-09-18, every game from
2024021235 through 2024021291 (the last 57 of 2024-25) returns {"data": [], "total": 0}
while both HTML reports return 200 with the full shift tables. Parsed output was checked
against a game the JSON still covers (2024021234): 766 shift rows from each source, an
exact match on (playerId, period, startTime, endTime). The same markup is served back to
at least 2010-11 and for playoff games.

Two quirks of the page shape the parsing:
  * Each player's section holds the shift table AND a per-period summary table whose rows
    also start with a digit. Only the shift rows carry "MM:SS / MM:SS" (elapsed / time
    remaining) in the start and end cells, which is what separates them.
  * Overtime is labelled "OT", not "4". Shootout rows ("SO") are dropped, because the
    JSON feed omits them too -- e.g. 2024021155 went to a shootout yet its shift chart
    stops at period 4 -- and matching it keeps both sources interchangeable downstream.

The report identifies players by sweater number and "LAST, FIRST" only, so the caller
supplies the play-by-play's rosterSpots[] to resolve (teamId, sweaterNumber) -> playerId.
get_shift_chart_payload() returns the same shape as the JSON endpoint so that field_map,
ingest.shifts and the RAW_SHIFT_CHARTS row need no knowledge of which source was used.
"""

import html as html_module
import re

from nhl_pipeline.api.field_map import parse_clock
from nhl_pipeline.http_client import get_text

BASE = "https://www.nhl.com/scores/htmlreports"

_SHIFT_TYPE_CODE = 517  # the JSON feed's code for a shift row (505 = goal marker)

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_ELAPSED_OF_TOTAL = re.compile(r"^(\d+:\d\d)\s*/\s*\d+:\d\d$")
_CLOCK = re.compile(r"^\d+:\d\d$")
_PLAYER_HEADING = re.compile(r"^(\d+)\s+(.+)$")
_MULTI_OT = re.compile(r"^(?:(\d+)OT|OT(\d+))$")


def report_url(nhl_game_id: int, home: bool) -> str:
    """2024021291 -> .../20242025/TH021291.HTM: the first four digits are the season's
    starting year, the last six are the game type + number used as the file name."""
    game_id = str(nhl_game_id)
    start_year = int(game_id[:4])
    season = f"{start_year}{start_year + 1}"
    return f"{BASE}/{season}/{'TH' if home else 'TV'}{game_id[4:]}.HTM"


def _cell_text(raw: str) -> str:
    return html_module.unescape(_TAG.sub(" ", raw)).replace("\xa0", " ").strip()


def _period_number(label: str):
    """Numeric labels pass through; 'OT' is period 4 and 'NOT'/'OTN' the Nth overtime.
    'SO' returns None (dropped, see the module docstring); anything else raises rather
    than silently discarding ice time."""
    label = label.strip().upper()
    if label.isdigit():
        return int(label)
    if label == "OT":
        return 4
    if label == "SO":
        return None
    match = _MULTI_OT.match(label)
    if match:
        return 3 + int(match.group(1) or match.group(2))
    raise ValueError(f"Unrecognised period label in shift report: {label!r}")


def parse_report(report_html: str) -> list:
    """One dict per shift row, in document order:
    {sweater_number, player_name, shift_number, period, start_seconds, end_seconds,
     duration_seconds}."""
    body = report_html[report_html.lower().find("<body"):] or report_html
    rows = []
    player = None

    for match in _ROW.finditer(body):
        row_html = match.group(1)
        cells = [_cell_text(c) for c in _CELL.findall(row_html)]

        if "playerHeading" in row_html:
            heading = _PLAYER_HEADING.match(next((c for c in cells if c), ""))
            player = (int(heading.group(1)), heading.group(2).strip()) if heading else None
            continue

        if player is None or len(cells) < 6 or not cells[0].isdigit():
            continue

        start, end = _ELAPSED_OF_TOTAL.match(cells[2]), _ELAPSED_OF_TOTAL.match(cells[3])
        if not (start and end):
            continue  # a summary-table row, not a shift

        period = _period_number(cells[1])
        if period is None:
            continue

        rows.append({
            "sweater_number": player[0],
            "player_name": player[1],
            "shift_number": int(cells[0]),
            "period": period,
            "start_seconds": parse_clock(start.group(1)),
            "end_seconds": parse_clock(end.group(1)),
            "duration_seconds": parse_clock(cells[4]) if _CLOCK.match(cells[4]) else None,
        })

    return rows


def _player_id_by_sweater(roster_spots: list) -> dict:
    return {
        (spot["teamId"], int(spot["sweaterNumber"])): spot["playerId"]
        for spot in roster_spots
        if spot.get("sweaterNumber") is not None
    }


def _as_clock(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def get_shift_chart_payload(nhl_game_id: int, roster_spots: list, home_nhl_team_id: int,
                            away_nhl_team_id: int) -> dict:
    """Both reports, parsed and resolved into the shiftcharts endpoint's payload shape."""
    id_by_sweater = _player_id_by_sweater(roster_spots)
    data = []
    unresolved = set()

    for home, nhl_team_id in ((True, home_nhl_team_id), (False, away_nhl_team_id)):
        for row in parse_report(get_text(report_url(nhl_game_id, home))):
            player_id = id_by_sweater.get((nhl_team_id, row["sweater_number"]))
            if player_id is None:
                unresolved.add((nhl_team_id, row["sweater_number"], row["player_name"]))
                continue
            data.append({
                "gameId": nhl_game_id,
                "playerId": player_id,
                "teamId": nhl_team_id,
                "period": row["period"],
                "shiftNumber": row["shift_number"],
                "startTime": _as_clock(row["start_seconds"]),
                "endTime": _as_clock(row["end_seconds"]),
                "duration": _as_clock(row["duration_seconds"]) if row["duration_seconds"] is not None else None,
                "typeCode": _SHIFT_TYPE_CODE,
                "detailCode": 0,
                "eventDescription": None,
            })

    if unresolved:
        # A missing sweater number would silently drop a player's entire game, so fail the
        # stage instead: the roster is from the same game's play-by-play and should be complete.
        detail = ", ".join(f"team {t} #{n} {name}" for t, n, name in sorted(unresolved))
        raise ValueError(f"Shift report players not in rosterSpots for game {nhl_game_id}: {detail}")

    return {"data": data, "total": len(data), "source": "HTML_SHIFT_REPORT"}

"""The Crome aggregate workbook -- one file carrying many sources' raw projections, one sheet each.

`ProjectionSheets/2025-26/Crome Aggregate Projections 2025-26.xlsx` is last season's copy of a
community blending sheet. Its own tabs blend and rank; the ones read here are the per-source tabs
it imports into, which all share one layout, the one Crome's importer writes:

    row 1    A: "363 PLAYERS\\n363 ON MASTER"   B: "346 MATCHED\\n17 FIXED\\n0 NO MATCH"
             C onward: the source's own column headers
    row 2+   A: Crome's matched master name, or "NO MATCH"
             B: a matched flag
             C onward: the source's raw row -- C is always the source's own spelling of the name,
             whatever its header says (Steve Laidlaw's has none)

So one reader covers every source, driven by header NAMES from row 1, never by position: a source
that adds or drops a column still reads correctly. Where a sheet repeats a header (Dom's has a
skater GP and a goalie GP), a skater row reads the first occurrence and a goalie row the last.

Blank cells, spreadsheet errors ("#ERROR!") and non-numbers ("Undrafted") read as NULL, never 0:
a source that does not project a stat must not look like it projects zero.
"""

import datetime as dt
import re
from pathlib import Path

import openpyxl

FILENAME = "Crome Aggregate Projections 2025-26.xlsx"

# sheet -> (SourceName, the name Crome's SourceCheck tab dates it under, description, overrides).
# SourceName matches the 2026-27 source where there is one, so the two seasons line up by name.
SHEETS = {
    "Apples & Ginos - Blake": ("Apples & Ginos - Blake", "Apples & Ginos - Blake", {}),
    "Apples & Ginos - Nate": ("Apples & Ginos - Nate", "Apples & Ginos - Nate", {}),
    "Bangers Fantasy Hockey": ("Bangers Fantasy Hockey", "Bangers Fantasy Hockey", {}),
    "Dailyfaceoff": ("Dailyfaceoff", "Dailyfaceoff", {"goalie_gp_from_gs": True}),
    "DatsyukToZetterberg": ("DtZ", "DatsyukToZetterberg", {}),
    "KUBOTA": ("Kubota Hockey", "KUBOTA", {}),
    "LineupExperts": ("Lineup Experts", "LineupExperts", {"team_pos": "TEAM-POS"}),
    "Scott Cullen": ("Scott Cullen", "Scott Cullen", {}),
    "Steve Laidlaw": ("Steve Laidlaw", "Steve Laidlaw", {}),
    "Yahoo  Fantrax": ("Yahoo / Fantrax", "Yahoo / Fantrax", {}),
    # Dom's sheet, pasted into the workbook's first free import slot. Its "#ERROR!" header sits
    # where Dom's layout has plus-minus (between HIT and PIM, as in the 2026-27 file).
    "Import 1": ("Dom", "Import 1", {"extra_headers": {"#ERROR!": "PlusMinus"}}),
}

SKATER_HEADERS = {
    "GP": "GamesPlayed", "G": "Goals", "A": "Assists", "P": "Points", "PTS": "Points",
    "PPP": "PowerPlayPoints", "SHP": "ShortHandedPoints", "SOG": "Shots", "HIT": "Hits",
    "BLK": "Blocks", "PIM": "PenaltyMinutes", "ATOI": "AverageTOIMinutes",
    "TOI": "AverageTOIMinutes", "+/-": "PlusMinus", "PPG": "PowerPlayGoals",
    "PPA": "PowerPlayAssists", "FOW": "FaceoffWins", "FOL": "FaceoffLosses",
    "FO%": "FaceoffWinPct",
}
GOALIE_HEADERS = {
    "GP": "GamesPlayed", "GS": "GamesStarted", "W": "Wins", "L": "Losses",
    "OTL": "OvertimeLosses", "GA": "GoalsAgainst", "GAA": "GoalsAgainstAverage",
    "SA": "ShotsAgainst", "SV": "Saves", "SV%": "SavePercentage", "SO": "Shutouts",
    "SHO": "Shutouts",
}
TEAM_HEADERS = ("TEAM", "Team")
POSITION_HEADERS = ("POS", "Pos", "Position", "Proj Pos")
GOALIE_ONLY = {"W", "GAA", "SV%", "SV", "GS", "GA", "SO", "SHO"}


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None                    # "#ERROR!", "Undrafted", "N/A", ...


def _positions(raw) -> list:
    """'C,LW' / 'C/LW' / 'C1' (Apples & Ginos' line slot) -> ['C', 'LW']."""
    if raw is None:
        return []
    codes = []
    for part in re.split(r"[,/ ]+", str(raw).strip().upper()):
        part = re.sub(r"\d+$", "", part)
        if part in ("W",):
            codes += ["LW", "RW"]
        elif part:
            codes.append(part)
    return codes


def open_workbook(sheets_dir: Path):
    return openpyxl.load_workbook(sheets_dir / FILENAME, read_only=True, data_only=True)


def summary(workbook, sheet: str) -> dict:
    """The sheet's own row-1 counts, to check a read against: players, matched, fixed, no match."""
    first = next(workbook[sheet].iter_rows(max_row=1, values_only=True))
    text = f"{first[0]} {first[1]}"
    grab = lambda label: int(m.group(1)) if (m := re.search(rf"(\d+) {label}", text)) else None
    return {"players": grab("PLAYERS"), "matched": grab("MATCHED"), "fixed": grab("FIXED"),
            "no_match": grab("NO MATCH")}


def published_dates(workbook) -> dict:
    """{SourceCheck source name: date} from the tab's 'last update' row. A source SourceCheck
    leaves blank takes the date of the ChangeLog entry that added it ("Added Bangers Fantasy
    Hockey", 12 Sep 2025); one with neither stays undated (Dom's sheet)."""
    rows = list(workbook["SourceCheck"].iter_rows(max_row=2, values_only=True))
    dates, names = rows[0], rows[1]
    out, blank = {}, []
    for date, name in zip(dates, names):
        if not name or name in ("PLAYER", "MASTER", "TOTAL"):
            continue
        if isinstance(date, dt.datetime):
            out[str(name)] = date.date()
        else:
            blank.append(str(name))
    for row in workbook["ChangeLog"].iter_rows(max_col=2, values_only=True):
        when, text = (tuple(row) + (None, None))[:2]
        if not isinstance(when, dt.datetime) or not text:
            continue
        for name in blank:
            if f"added {name}".lower() in str(text).lower():
                out.setdefault(name, when.date())
    return out


def rows(workbook, sheet: str):
    """Yield {"raw_name", "crome_name", "team_raw", "position_codes", "is_goalie", "stats"}."""
    _, _, overrides = SHEETS[sheet]
    ws = workbook[sheet]
    it = ws.iter_rows(values_only=True)
    header = next(it)
    first, last = {}, {}
    for i, h in enumerate(header):
        if i < 2 or h is None:
            continue
        name = str(h).strip()
        first.setdefault(name, i)
        last[name] = i
    extra = overrides.get("extra_headers", {})

    def column(names, index_map):
        for n in names:
            if n in index_map:
                return index_map[n]
        return None

    team_col = column(TEAM_HEADERS, first)
    pos_col = column(POSITION_HEADERS, first)
    team_pos_col = first.get(overrides.get("team_pos")) if overrides.get("team_pos") else None

    for r in it:
        if not r or len(r) < 3 or not r[2]:
            continue
        raw_name = str(r[2]).strip()
        crome = str(r[0]).strip() if r[0] else None
        crome_name = None if crome in (None, "NO MATCH") else crome
        cell = lambda i: r[i] if i is not None and i < len(r) else None

        team_raw, pos_raw = cell(team_col), cell(pos_col)
        if team_pos_col is not None and cell(team_pos_col):
            team_part, _, pos_part = str(cell(team_pos_col)).partition(" - ")
            team_raw, pos_raw = team_part.strip(), pos_part.strip()
        if team_raw in ("N/A", "FA", ""):
            team_raw = None
        codes = _positions(pos_raw)

        if codes:
            is_goalie = "G" in codes
        else:                          # no position column: goalie if a goalie-only stat is set
            is_goalie = any(_number(cell(last[h])) is not None for h in GOALIE_ONLY if h in last)

        headers = GOALIE_HEADERS if is_goalie else SKATER_HEADERS
        index_map = last if is_goalie else first
        stats = {}
        for h, target in headers.items():
            value = _number(cell(index_map.get(h)))
            if value is not None and target not in stats:
                stats[target] = value
        if not is_goalie:
            for h, target in extra.items():
                value = _number(cell(first.get(h)))
                if value is not None:
                    stats.setdefault(target, value)
        if is_goalie and overrides.get("goalie_gp_from_gs") and "GamesPlayed" not in stats:
            if "GamesStarted" in stats:
                stats["GamesPlayed"] = stats["GamesStarted"]
        if is_goalie and "ShotsAgainst" not in stats and {"Saves", "GoalsAgainst"} <= set(stats):
            stats["ShotsAgainst"] = stats["Saves"] + stats["GoalsAgainst"]
        if not stats:
            continue                   # a name with nothing projected (a placeholder row)
        yield {"raw_name": raw_name, "crome_name": crome_name, "team_raw": team_raw,
               "position_codes": [c for c in codes if c in ("C", "LW", "RW", "D", "G")],
               "is_goalie": is_goalie, "stats": stats}


# ---------- the platform tabs ----------

PLATFORMS = ("Yahoo", "Fantrax", "ESPN", "Fleaflicker")
POSITION_CODES = {"C", "LW", "RW", "D", "G"}


def positions(workbook):
    """Yield (platform, name, team, [codes]) from the Positions tab: one row per player, a column
    per platform. Names are Crome's master names. A code outside C/LW/RW/D/G is dropped."""
    it = workbook["Positions"].iter_rows(values_only=True)
    header = next(it)
    cols = {p: header.index(p) for p in PLATFORMS if p in header}
    for r in it:
        if not r or not r[0]:
            continue
        for platform, i in cols.items():
            codes = [c for c in _positions(r[i] if i < len(r) else None) if c in POSITION_CODES]
            if codes:
                yield platform, str(r[0]).strip(), r[1], codes


def adp(workbook, platform: str):
    """(as-of date, [(raw name, fixed name, team, position codes, ADP)]) from ADPYahoo / ADPFantrax.
    The raw table sits to the right: 'PLAYER FIXED', then the platform's own name, team,
    position and pick."""
    ws = workbook[f"ADP{platform}"]
    it = ws.iter_rows(values_only=True)
    header = next(it)
    as_of = next((v.date() for v in header if isinstance(v, dt.datetime)), None)
    fixed = header.index("PLAYER FIXED")
    name_i, team_i, pos_i, adp_i = fixed + 1, fixed + 2, fixed + 3, fixed + 4
    out = []
    for r in it:
        if not r or len(r) <= adp_i or not r[name_i]:
            continue
        value = _number(r[adp_i])
        if value is None:
            continue
        out.append((str(r[name_i]).strip(), str(r[fixed]).strip() if r[fixed] else None,
                    r[team_i], [c for c in _positions(r[pos_i]) if c in POSITION_CODES], value))
    return as_of, out


# ---------- names the resolver cannot match, confirmed by hand ----------

# Spellings in this workbook that match no Reference.Players name, each checked against the
# player's team and 2025-26 games (2026-09-24). Each names one player only, so it is stored as an
# alias -- per projection source, and per platform -- and a fresh database resolves it the same
# way. Deliberately NOT here: prospects with no NHL games (Sasha Pastujov, Michal Kunc, Ilya
# Nazarov, Semyon Demidov, Aiden Celebrini -- Macklin's brother), which stay unresolved rather
# than be matched to someone else.
CONFIRMED_ALIASES = {
    "Mack Celebrini": 563, "Dmitry Voronkov": 171, "Vasili Podkolzin": 292,
    "Emil Martinsen Lilleberg": 738, "Fyodor Svechkov": 297, "Christopher Tanev": 699,
    "Zachary Jones": 2076, "Phillip Tomasino": 688, "Max Comtois": 4070, "Nikita Okhotyuk": 1887,
    "Benoit-Olivier Groulx": 995, "Joe Labate": 828,
    "John St. Ivany": 110, "Georgi Merkulov": 896, "Boko Imama": 899, "Nicolas Daws": 816,
    "Axel Sandin Pellikka": 221,
}

# Names two real players share, each settled by evidence for the sources where it failed:
# "Elias Pettersson" and "Sebastian Aho" on Steve Laidlaw's list, which has no team or position
# column -- every other sheet lists Pettersson as Vancouver's centre (not the 2004 defenceman), and
# Laidlaw's Aho line (32 G, 76 P, 24 blocks) is Carolina's centre, not the Islanders' defenceman;
# "Matt Murray" is Seattle's goalie (he played 16 games for them), not Nashville's; "Colin White"
# is the Sharks' 1997 centre (3 games for them), not the retired 1977 defenceman. NEVER a blanket alias: the platforms' alias table is shared across seasons, and a
# blanket "Matt Murray" would misroute the other goalie in the next live 2026-27 import. So:
SOURCE_ALIASES = {        # stored, but only for that one 2025-26 projection source
    ("Steve Laidlaw", "Elias Pettersson"): 55,
    ("Steve Laidlaw", "Sebastian Aho"): 240,
    ("Yahoo / Fantrax", "Matt Murray"): 806,
    ("Yahoo / Fantrax", "Colin White"): 4063,
    ("Lineup Experts", "Colin White"): 4063,
}
PLATFORM_RUN_ALIASES = {  # used during the platform import only, never written as an alias
    "Matt Murray": 806,
    "Colin White": 4063,
}

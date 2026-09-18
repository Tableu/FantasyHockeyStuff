"""Prototype: derive opening line combinations from Game.Shifts for one game.

Usage: python lines_from_shifts.py <NHLGameID> [window_seconds]
"""
import sys
from collections import defaultdict
from itertools import combinations

sys.path.insert(0, r"C:\Users\Brian\Documents\Fantasy Hockey Stuff\pipeline")
from nhl_pipeline import db  # noqa: E402

nhl_game_id = int(sys.argv[1])
WINDOW = int(sys.argv[2]) if len(sys.argv) > 2 else 600  # opening seconds of period 1 to use

conn = db.connect()
cur = conn.cursor()
cur.execute(
    """
    SELECT s.TeamID, t.Abbreviation, s.PlayerID, p.FullName, p.PositionCode,
           s.PeriodNumber, s.ShiftStartSeconds, s.ShiftEndSeconds
    FROM Game.Shifts s
    JOIN Game.Games g ON g.GameID = s.GameID
    JOIN Reference.Teams t ON t.TeamID = s.TeamID
    JOIN Reference.Players p ON p.PlayerID = s.PlayerID
    WHERE g.NHLGameID = ?
    """,
    nhl_game_id,
)
rows = cur.fetchall()

# per-second on-ice sets, keyed by absolute game second (period offsets of 1200s)
on_ice = defaultdict(lambda: defaultdict(set))  # sec -> team -> {player}
info = {}
for team, abbrev, pid, name, pos, per, st, en in rows:
    info[pid] = (team, abbrev, name, pos)
    base = (per - 1) * 1200
    for sec in range(base + st, base + en):
        on_ice[sec][team].add(pid)

teams = sorted({r[0] for r in rows})
abbrev = {r[0]: r[1] for r in rows}


def skaters(team, sec):
    return {p for p in on_ice[sec][team] if info[p][3] != "G"}


def strength(sec):
    return {t: len(skaters(t, sec)) for t in teams}


def shared_seconds(team, secs, position_filter):
    pair = defaultdict(int)
    toi = defaultdict(int)
    for sec in secs:
        ps = [p for p in skaters(team, sec) if position_filter(info[p][3])]
        for p in ps:
            toi[p] += 1
        for a, b in combinations(sorted(ps), 2):
            pair[(a, b)] += 1
    return pair, toi


def cluster(pair, toi, size):
    units = []
    assigned = {}
    for (a, b), secs in sorted(pair.items(), key=lambda kv: -kv[1]):
        ua, ub = assigned.get(a), assigned.get(b)
        if ua is None and ub is None:
            units.append({a, b}); assigned[a] = assigned[b] = len(units) - 1
        elif ua is not None and ub is None and len(units[ua]) < size:
            units[ua].add(b); assigned[b] = ua
        elif ub is not None and ua is None and len(units[ub]) < size:
            units[ub].add(a); assigned[a] = ub
    for p in toi:
        if p not in assigned:
            units.append({p})
    return sorted(units, key=lambda u: -sum(toi[p] for p in u))


def fmt(unit, toi):
    return " - ".join(f"{info[p][2]}" for p in sorted(unit, key=lambda p: -toi[p]))


ev_secs = [s for s in range(WINDOW) if all(n == 5 for n in strength(s).values())]
print(f"Game {nhl_game_id}: {len(ev_secs)}s of 5v5 in first {WINDOW}s of P1\n")

for team in teams:
    print(f"=== {abbrev[team]} ===")
    pair, toi = shared_seconds(team, ev_secs, lambda pos: pos in ("C", "L", "R"))
    for i, u in enumerate(cluster(pair, toi, 3), 1):
        print(f"  F{i}: {fmt(u, toi)}")
    pair, toi = shared_seconds(team, ev_secs, lambda pos: pos == "D")
    for i, u in enumerate(cluster(pair, toi, 2), 1):
        print(f"  D{i}: {fmt(u, toi)}")

    other = [t for t in teams if t != team][0]
    pp_secs = [s for s in range(3600) if len(skaters(team, s)) > len(skaters(other, s)) >= 3]
    if pp_secs:
        pair, toi = shared_seconds(team, pp_secs, lambda pos: pos != "G")
        for i, u in enumerate(cluster(pair, toi, 5)[:2], 1):
            print(f"  PP{i}: {fmt(u, toi)}  ({sum(toi[p] for p in u)//len(u)}s avg)")
    else:
        print("  (no power play time)")
    print()

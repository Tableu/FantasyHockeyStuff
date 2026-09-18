"""Lineups.GameLineups -- a team's opening deployment for one game, derived from Game.Shifts.

Nobody archives pre-game line charts point-in-time, so for history the actual opening
deployment is the closest thing to what Daily Faceoff publishes before puck drop (see
docs/fantasy-ai/data-sources.md for the validation against beat-reporter lines and the
noise-injection plan that makes it usable as a lockout-time training feature).

Method (ported from docs/fantasy-ai/lines_from_shifts.py):
- Per-second on-ice sets per team from shift intervals. The strength state each second comes
  from the play-event StrengthCode timeline (the same segments calc/strength_toi.py uses for
  PP/SH TOI) rather than from the on-ice counts: shift charts overlap by a few seconds at
  every line change, so counting skaters would read each change as a momentary 6-on-5.
- Forward trios / defence pairs: seconds each pair of same-position skaters shares at 5v5 in
  the opening window of period 1 (the first 600 s of clock, stretched to the first 300 s of
  5v5 if early penalties ate the window), then greedy merging of the most-shared pairs into
  units of three / two. Membership is stable across window sizes (checked at 300/600/1200 s);
  only the ORDER moves, so units are ranked by their summed period-1 EV seconds -- never
  full-game TOI, which would encode in-game injuries and benchings into what is meant to be
  a lockout-time signal.
- PP / PK units: same clustering (five / four) over every second the team is up / down a
  skater, top two by ice time. Below 60 s of that strength the units are unknowable rather
  than absent -- PowerPlayUnit/PenaltyKillUnit stay NULL and TeamHad* is 0 so a consumer can
  tell the two apart (and fall back to the previous game's units).
- Dressed comes from the boxscore rows (Stats.PlayerGameStats) so the backup goalie counts;
  the starter is whoever is in net at 0:00 of period 1.
"""

import logging
from collections import defaultdict
from itertools import combinations

from nhl_pipeline import db
from nhl_pipeline.calc import situation_resolver, strength_toi

log = logging.getLogger("calc.lineups")

PERIOD_SECONDS = 1200
SHOOTOUT_PERIOD = 5
OPENING_WINDOW_SECONDS = 600
MIN_OPENING_EV_SECONDS = 300
MIN_SPECIAL_TEAMS_SECONDS = 60
MAX_FORWARD_LINES = 4
MAX_DEFENSE_PAIRS = 3
MAX_SPECIAL_TEAMS_UNITS = 2
FORWARD_POSITIONS = ("C", "L", "R")


def _cluster(pair_seconds: dict, member_seconds: dict, size: int) -> list:
    """Greedy: walk pairs by shared seconds, merging into units of at most `size`. Every
    skater who took a shift ends up in some unit (singletons included). Units come back
    sorted by summed member_seconds, descending."""
    units, assigned = [], {}
    for (a, b), _ in sorted(pair_seconds.items(), key=lambda kv: -kv[1]):
        ua, ub = assigned.get(a), assigned.get(b)
        if ua is None and ub is None:
            units.append({a, b})
            assigned[a] = assigned[b] = len(units) - 1
        elif ua is not None and ub is None and len(units[ua]) < size:
            units[ua].add(b)
            assigned[b] = ua
        elif ub is not None and ua is None and len(units[ub]) < size:
            units[ub].add(a)
            assigned[a] = ub
    for p in member_seconds:
        if p not in assigned:
            units.append({p})
    return sorted(units, key=lambda u: -sum(member_seconds[p] for p in u))


def _shared_seconds(on_ice: list, seconds, eligible) -> tuple:
    """(pair -> shared seconds, player -> seconds) over the given seconds, restricted to
    `eligible` players."""
    pair, toi = defaultdict(int), defaultdict(int)
    for sec in seconds:
        present = sorted(p for p in on_ice[sec] if p in eligible)
        for p in present:
            toi[p] += 1
        for a, b in combinations(present, 2):
            pair[(a, b)] += 1
    return pair, toi


def _rank_units(units: list, max_units: int) -> dict:
    """{player: rank} for the first max_units units with at least two members."""
    ranks = {}
    rank = 0
    for unit in units:
        if len(unit) < 2:
            continue
        rank += 1
        if rank > max_units:
            break
        for p in unit:
            ranks[p] = rank
    return ranks


def strength_timeline(plays: list, code_map: dict, home_team_id: int, away_team_id: int, total_seconds: int) -> dict:
    """{"5v5": [bool per second], "pp": {team_id: [bool per second]}} from the play-event
    strength segments (strength_toi._build_period_segments). A period with no plays (never
    seen) is treated as 5v5 throughout."""
    segments_by_period = strength_toi._build_period_segments(plays)
    five_v_five = [True] * total_seconds
    pp = {home_team_id: [False] * total_seconds, away_team_id: [False] * total_seconds}
    for period, segments in segments_by_period.items():
        base = (period - 1) * PERIOD_SECONDS
        if period == SHOOTOUT_PERIOD or base >= total_seconds:
            continue
        for seg_start, seg_end, raw_code in segments:
            row = code_map.get(raw_code)
            home_pp, _ = situation_resolver.classify_strength(code_map, raw_code, True)
            away_pp, _ = situation_resolver.classify_strength(code_map, raw_code, False)
            is_5v5 = bool(row) and row[0] and row[3] and row[1] == 5 and row[2] == 5
            for sec in range(base + seg_start, min(base + seg_end, base + PERIOD_SECONDS, total_seconds)):
                five_v_five[sec] = is_5v5
                pp[home_team_id][sec] = home_pp
                pp[away_team_id][sec] = away_pp
    return {"5v5": five_v_five, "pp": pp}


def derive_lineups(shifts: list, positions: dict, team_ids: list, strength: dict) -> dict:
    """shifts: rows with TeamID, PlayerID, PeriodNumber, ShiftStartSeconds, ShiftEndSeconds.
    positions: {TeamID: {PlayerID: PositionCode}} for that game (everyone who dressed).
    strength: strength_timeline() output. Returns {(TeamID, PlayerID): row dict} for every
    dressed player plus any shift-taker missing from the boxscore."""
    total_seconds = len(strength["5v5"])
    on_ice = {t: [set() for _ in range(total_seconds)] for t in team_ids}
    for s in shifts:
        if s.PeriodNumber == SHOOTOUT_PERIOD or s.TeamID not in on_ice:
            continue
        base = (s.PeriodNumber - 1) * PERIOD_SECONDS
        for sec in range(base + s.ShiftStartSeconds, min(base + s.ShiftEndSeconds, total_seconds)):
            on_ice[s.TeamID][sec].add(s.PlayerID)

    position = {p: pos for by_player in positions.values() for p, pos in by_player.items()}

    def is_goalie(p):
        return position.get(p) == "G"

    ev_seconds = [sec for sec in range(total_seconds) if strength["5v5"][sec]]
    period1_ev = [sec for sec in ev_seconds if sec < PERIOD_SECONDS]
    opening = [sec for sec in period1_ev if sec < OPENING_WINDOW_SECONDS]
    if len(opening) < MIN_OPENING_EV_SECONDS:
        opening = period1_ev[:MIN_OPENING_EV_SECONDS]

    rows = {}
    for team in team_ids:
        other = next(t for t in team_ids if t != team)
        team_players = {p for secs in on_ice[team] for p in secs} | set(positions.get(team, {}))
        forwards = {p for p in team_players if position.get(p) in FORWARD_POSITIONS}
        defence = {p for p in team_players if position.get(p) == "D"}
        skater_set = forwards | defence

        pair, toi = _shared_seconds(on_ice[team], opening, forwards)
        f_units = _cluster(pair, toi, 3)
        pair, toi = _shared_seconds(on_ice[team], opening, defence)
        d_units = _cluster(pair, toi, 2)
        # Rank by period-1 EV seconds, not opening-window seconds (see module docstring).
        _, p1_ev = _shared_seconds(on_ice[team], period1_ev, skater_set)
        f_units.sort(key=lambda u: -sum(p1_ev.get(p, 0) for p in u))
        d_units.sort(key=lambda u: -sum(p1_ev.get(p, 0) for p in u))
        lines = _rank_units(f_units, MAX_FORWARD_LINES)
        pairs = _rank_units(d_units, MAX_DEFENSE_PAIRS)

        pp_seconds = [sec for sec in range(total_seconds) if strength["pp"][team][sec]]
        sh_seconds = [sec for sec in range(total_seconds) if strength["pp"][other][sec]]
        pp_units, pk_units = {}, {}
        if len(pp_seconds) >= MIN_SPECIAL_TEAMS_SECONDS:
            pair, toi = _shared_seconds(on_ice[team], pp_seconds, skater_set)
            pp_units = _rank_units(_cluster(pair, toi, 5), MAX_SPECIAL_TEAMS_UNITS)
        if len(sh_seconds) >= MIN_SPECIAL_TEAMS_SECONDS:
            pair, toi = _shared_seconds(on_ice[team], sh_seconds, skater_set)
            pk_units = _rank_units(_cluster(pair, toi, 4), MAX_SPECIAL_TEAMS_UNITS)

        _, ev_toi = _shared_seconds(on_ice[team], ev_seconds, team_players)
        _, pp_toi = _shared_seconds(on_ice[team], pp_seconds, team_players)
        _, sh_toi = _shared_seconds(on_ice[team], sh_seconds, team_players)
        starting_goalie = next((p for p in on_ice[team][0] if is_goalie(p)), None) if total_seconds else None

        for p in team_players:
            rows[(team, p)] = {
                "PositionCode": position.get(p) or "?",
                "Dressed": True,
                "IsStartingGoalie": p == starting_goalie,
                "ForwardLine": lines.get(p),
                "DefensePair": pairs.get(p),
                "PowerPlayUnit": pp_units.get(p),
                "PenaltyKillUnit": pk_units.get(p),
                "Period1EVSeconds": p1_ev.get(p, 0),
                "EVSeconds": ev_toi.get(p, 0),
                "PPSeconds": pp_toi.get(p, 0),
                "SHSeconds": sh_toi.get(p, 0),
                "TeamHadPowerPlay": len(pp_seconds) >= MIN_SPECIAL_TEAMS_SECONDS,
                "TeamHadPenaltyKill": len(sh_seconds) >= MIN_SPECIAL_TEAMS_SECONDS,
            }
    return rows


def compute_and_store(cursor, game_id: int, code_map: dict | None = None) -> None:
    cursor.execute("SELECT HomeTeamID, AwayTeamID FROM Game.Games WHERE GameID = ?", game_id)
    game = cursor.fetchone()
    team_ids = [game.HomeTeamID, game.AwayTeamID]
    if code_map is None:
        code_map = situation_resolver.load_situation_code_map(cursor)

    cursor.execute(
        "SELECT TeamID, PlayerID, PeriodNumber, ShiftStartSeconds, ShiftEndSeconds FROM Game.Shifts WHERE GameID = ?",
        game_id,
    )
    shifts = cursor.fetchall()
    if not shifts:
        log.warning("Game %s has no shifts -- no lineups derived", game_id)
        return

    # The game's own position for each dressed player (a player's Reference.Players position
    # is their current one); the boxscore rows also define who dressed, backup goalie included.
    cursor.execute(
        "SELECT s.PlayerID, s.TeamID, COALESCE(s.PositionCode, p.PositionCode) AS PositionCode "
        "FROM Stats.PlayerGameStats s JOIN Reference.Players p ON p.PlayerID = s.PlayerID WHERE s.GameID = ?",
        game_id,
    )
    positions: dict = {t: {} for t in team_ids}
    for r in cursor.fetchall():
        positions.setdefault(r.TeamID, {})[r.PlayerID] = r.PositionCode
    # A shift-taker missing from the boxscore (shouldn't happen, but a partial boxscore
    # ingest would do it) still needs a position for the clustering.
    missing = {(s.TeamID, s.PlayerID) for s in shifts} - {(t, p) for t, by_player in positions.items() for p in by_player}
    if missing:
        ids = {p for _, p in missing}
        cursor.execute(
            f"SELECT PlayerID, PositionCode FROM Reference.Players WHERE PlayerID IN ({','.join('?' * len(ids))})",
            *ids,
        )
        fallback = {r.PlayerID: r.PositionCode for r in cursor.fetchall()}
        for team, p in missing:
            positions.setdefault(team, {})[p] = fallback.get(p)

    cursor.execute(
        "SELECT PeriodNumber, PeriodTimeSeconds, StrengthCode FROM Game.Plays "
        "WHERE GameID = ? ORDER BY PeriodNumber, PeriodTimeSeconds, PlayID",
        game_id,
    )
    plays = cursor.fetchall()
    total_seconds = max((s.PeriodNumber for s in shifts if s.PeriodNumber != SHOOTOUT_PERIOD), default=3) * PERIOD_SECONDS
    strength = strength_timeline(plays, code_map, game.HomeTeamID, game.AwayTeamID, total_seconds)

    rows = derive_lineups(shifts, positions, team_ids, strength)

    db.delete_where(cursor, "Lineups.GameLineups", {"GameID": game_id})
    for (team, p), r in rows.items():
        db.upsert(
            cursor, "Lineups.GameLineups",
            {"GameID": game_id, "TeamID": team, "PlayerID": p},
            r,
        )

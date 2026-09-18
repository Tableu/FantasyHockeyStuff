"""Injuries.Spells -- the NHL Injury Viz database (api.nhl_injury_viz), every regular-season
injury spell from 2000-01 on, resolved to our SeasonID/TeamID/PlayerID and to calendar dates.

The source publishes absences as team game numbers ("missed team games 39-40"), so each
(team, season) first goes through ingest.team_schedules, which upserts that team's whole
schedule into Reference.Teams/Reference.Schedule (the by-product that gives the DB every
past season's schedule, and the defunct ATL/PHX/ARI franchises) and hands back the
game-number -> date/NHLGameID map. Team labels are the source's own city names ("NY
Islanders", "Phoenix" vs. "Arizona"), mapped statically below to the abbreviation the NHL
schedule endpoint knows -- team_resolver can't do this since defunct teams aren't in
Reference.Teams until this import puts them there.

Player names span 25 seasons, so most aren't in Reference.Players (which only holds who has
dressed in an ingested game, plus draft picks). Resolution is two-tier: nhl_pipeline.
name_resolver first (aliases, then local normalized-name matching with the source's F/D/G
as a same-name tiebreaker); a name with no position-compatible local candidate then goes to
NHL player search -- exact name first (nickname swaps and hyphen/space differences allowed),
else last name + first initial for the NHL's diminutives ('Olie' Kolzig, 'Ziggy' Palffy) and
transliterations ('Baertschi') -- keeping only players who actually played NHL games through
the injury's season, in the same position group, with the injury's team as a final tiebreaker.
The source's own "Erik Gustafsson (2)"-style numbering becomes a real player this way, and
every loose match is logged for audit. A unique hit is inserted into Reference.Players under
its permanent NHLPlayerID (so a later season backfill upserts onto the same row) and aliased;
anything still ambiguous lands in Injuries.UnresolvedPlayerNames for a one-time manual alias,
and its spells are written with PlayerID NULL until then. Re-running the import replaces each season's spells wholesale
(same as the Fantasy imports) so upstream rebuilds that drop/change rows don't leave
leftovers; aliases persist across runs so the NHL search only ever happens once per name.
"""

import logging
from collections import defaultdict

from nhl_pipeline import db, name_resolver
from nhl_pipeline.api import field_map, player_search
from nhl_pipeline.ingest import team_schedules
from nhl_pipeline.ingest.season import ensure_season

log = logging.getLogger("ingest.injury_history")

SOURCE_NAME = "NHL Injury Viz"
ALIAS_TABLE = "Injuries.PlayerNameAliases"
UNRESOLVED_TABLE = "Injuries.UnresolvedPlayerNames"

# The source's team labels -> the abbreviation the NHL club-schedule endpoint uses for that
# franchise. Relocations are already distinct labels in the source (Atlanta/Winnipeg,
# Phoenix/Arizona/Utah), so no season logic is needed here.
TEAM_ABBREV = {
    "Anaheim": "ANA", "Arizona": "ARI", "Atlanta": "ATL", "Boston": "BOS", "Buffalo": "BUF",
    "Calgary": "CGY", "Carolina": "CAR", "Chicago": "CHI", "Colorado": "COL", "Columbus": "CBJ",
    "Dallas": "DAL", "Detroit": "DET", "Edmonton": "EDM", "Florida": "FLA", "Los Angeles": "LAK",
    "Minnesota": "MIN", "Montreal": "MTL", "Nashville": "NSH", "New Jersey": "NJD",
    "NY Islanders": "NYI", "NY Rangers": "NYR", "Ottawa": "OTT", "Philadelphia": "PHI",
    "Phoenix": "PHX", "Pittsburgh": "PIT", "San Jose": "SJS", "Seattle": "SEA", "St. Louis": "STL",
    "Tampa Bay": "TBL", "Toronto": "TOR", "Utah": "UTA", "Vancouver": "VAN", "Vegas": "VGK",
    "Washington": "WSH", "Winnipeg": "WPG",
}

# Source position group -> Reference.Players.PositionCode values it covers.
POSITION_CODES = {"F": ["C", "L", "R"], "D": ["D"], "G": ["G"]}

# The NHL transliterates Germanic/Nordic letters rather than stripping the diacritic
# (Bärtschi -> Baertschi, Müller -> Mueller, Bødker -> Boedker), unlike ascii_fold.
_TRANSLITERATE = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ø": "oe", "å": "aa", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "Ø": "Oe", "Å": "Aa"})

# A player injured through the end of their final contract never plays again, so the NHL's
# lastSeasonId (last season *played*) sits a season or two before the source's row (Probert
# 2002-03 vs. 2001-02, Bourdon 2013-14 vs. 2011-12). Tolerated for exact-name matches only.
_LAST_SEASON_TOLERANCE = 2 * 10001  # two seasons in NHL season-id arithmetic (20112012 -> 20132014)


def _search(search_cache: dict, query: str) -> list:
    if query not in search_cache:
        search_cache[query] = player_search.search_player(query, limit=20)
    return search_cache[query]


def _same_name(result: dict, search_name: str) -> bool:
    """The NHL's spelling of search_name, allowing the same first-name nickname swaps
    name_resolver applies locally ('Alexander' / 'Alexandre' Kharitonov) and a hyphen the NHL
    writes as a space ('Charles-Alexis' -> 'Charles Alexis' Legault; normalize_name drops the
    hyphen without leaving a space, so compare space-free too)."""
    theirs = name_resolver.normalize_name(result.get("name") or "")
    if theirs.replace(" ", "") == name_resolver.normalize_name(search_name).replace(" ", ""):
        return True
    return bool(name_resolver.candidates(search_name, {theirs: [(0, None)]}))


def _same_last_name_and_initial(result: dict, search_name: str) -> bool:
    """Loose match for the NHL's diminutive first names the nickname table doesn't cover
    ('Olie' Kolzig for the source's 'Olaf', 'Ziggy' Palffy for 'Zigmund', 'Freddy' Modin for
    'Fredrik'): identical last name, same first initial."""
    ours = name_resolver.normalize_name(search_name).split(" ", 1)
    theirs = name_resolver.normalize_name(result.get("name") or "").split(" ", 1)
    return len(ours) == 2 and len(theirs) == 2 and ours[1] == theirs[1] and ours[0][:1] == theirs[0][:1]


def _plausible(candidates: list, f: dict, team_abbrev: str, loose: bool) -> list:
    """Narrows NHL search hits for one source row to the ones who could have missed its games."""
    # No lastSeasonId = never played an NHL game (a draft pick / minor-leaguer record -- the
    # NHL has one of these next to most common names), so they can't have missed one; a last
    # season before the injury's season rules a player out the same way (see the tolerance).
    earliest_last_season = f["nhl_season_id"] - (0 if loose else _LAST_SEASON_TOLERANCE)
    candidates = [r for r in candidates if r.get("lastSeasonId") and int(r["lastSeasonId"]) >= earliest_last_season]
    codes = POSITION_CODES.get(f["position_group"])
    if codes:
        # The NHL records a career-final position, the source the position that season, and
        # players do switch (Dandenault D -> R, Clymer D -> R). An exact-name match that only
        # fails on position is still that player; the loose path keeps position as its net.
        same_position = [r for r in candidates if r.get("positionCode") in codes]
        if same_position or loose:
            candidates = same_position
    if len(candidates) > 1:
        by_team = [r for r in candidates if team_abbrev in (r.get("teamAbbrev"), r.get("lastTeamAbbrev"))]
        if len(by_team) == 1:
            candidates = by_team
    return candidates


def _resolve_via_nhl_search(cursor, source_id: int, f: dict, team_abbrev: str, alias_map: dict,
                            player_index: dict, search_cache: dict) -> int | None:
    # search_name is already ASCII-folded (field_map): NHL search is keyed on ASCII spellings
    # ('Marc-Andre Fleury' hits, 'Marc-André Fleury' doesn't) and returns them the same way.
    search_name = f["search_name"]
    loose = False
    candidates = _plausible(
        [r for r in _search(search_cache, search_name) if _same_name(r, search_name)], f, team_abbrev, loose,
    )
    if not candidates:
        # The full-name search ranks on first name and can miss the player entirely
        # ('Fredrik Modin' returns twenty Fredriks and no Modin); searching the last name alone
        # finds them, and the tighter filters carry the disambiguation. Both query shapes per
        # spelling, since the bare last name drowns for a common one ('Mueller' returns twenty
        # other Muellers), and the loop moves on whenever nothing plausible survives.
        loose = True
        first_name, _, last_name = name_resolver.normalize_name(search_name).partition(" ")
        spellings = [last_name]
        transliterated = name_resolver.normalize_name(name_resolver.ascii_fold((f["last_name"] or "").translate(_TRANSLITERATE)))
        if transliterated and transliterated != last_name:
            spellings.append(transliterated)
        for spelling in spellings:
            for query in (f"{first_name} {spelling}", spelling):
                hits = [r for r in _search(search_cache, query) if _same_last_name_and_initial(r, f"{first_name} {spelling}")]
                candidates = _plausible(hits, f, team_abbrev, loose)
                if candidates:
                    break
            if candidates:
                break

    if len(candidates) != 1:
        log.info(
            "NHL search could not settle %r (%s, %s, %s): %d candidate(s) %s",
            f["raw_name"], f["position_group"], team_abbrev, f["season_display"], len(candidates),
            [(r.get("name"), r.get("playerId")) for r in candidates],
        )
        return None
    if loose:
        log.info("Matched %r to NHL %r (%s) on last name + initial", f["raw_name"], candidates[0].get("name"), candidates[0].get("playerId"))

    match = field_map.player_search_result_fields(candidates[0])
    full_name = " ".join(p for p in (f["first_name"], f["last_name"]) if p)
    # A player we already hold under another spelling ("Mitchell" vs. the source's "Mitch")
    # just gets aliased -- their NHL-sourced name/bio isn't overwritten with the source's.
    player_id = db.fetch_scalar(cursor, "SELECT PlayerID FROM Reference.Players WHERE NHLPlayerID = ?", match["nhl_player_id"])
    if player_id is None:
        player_id = db.upsert_get_id(
            cursor, "Reference.Players", "PlayerID",
            {"NHLPlayerID": match["nhl_player_id"]},
            {
                "FirstName": f["first_name"], "LastName": f["last_name"], "FullName": full_name,
                "PositionCode": match["position_code"],
                "HeightInches": match["height_inches"], "WeightLbs": match["weight_lbs"],
                "Active": bool(candidates[0].get("active")),
            },
        )
        player_index.setdefault(name_resolver.normalize_name(full_name), []).append((player_id, match["position_code"]))
    db.upsert(cursor, ALIAS_TABLE, {"SourceID": source_id, "RawName": f["raw_name"]}, {"PlayerID": player_id})
    cursor.execute(f"DELETE FROM {UNRESOLVED_TABLE} WHERE SourceID = ? AND RawName = ?", source_id, f["raw_name"])
    alias_map[f["raw_name"]] = player_id
    return player_id


def _resolve_player(cursor, source_id: int, f: dict, team_abbrev: str, alias_map: dict,
                    player_index: dict, search_cache: dict, counts: dict) -> int | None:
    codes = POSITION_CODES.get(f["position_group"])
    local = name_resolver.candidates(f["raw_name"], player_index)
    # Only take the local path when at least one local candidate is position-compatible.
    # resolve_player_id auto-aliases a lone candidate regardless of position, which is right
    # for the fantasy/projection sources but wrong here: the source's "Sebastian Aho (D)" must
    # not be aliased to the Carolina centre just because the Islanders defenseman hasn't
    # dressed in an ingested game yet -- that name goes to NHL search instead.
    if f["raw_name"] in alias_map or (local and (not codes or any(pos in codes for pos in local.values()))):
        player_id = name_resolver.resolve_player_id(
            cursor, ALIAS_TABLE, UNRESOLVED_TABLE, source_id, f["raw_name"], alias_map, player_index,
            position_codes=codes,
        )
        # None here is the ambiguous case name_resolver just queued for manual review.
        counts["resolved_local" if player_id is not None else "unresolved"] += 1
        return player_id

    player_id = _resolve_via_nhl_search(cursor, source_id, f, team_abbrev, alias_map, player_index, search_cache)
    if player_id is None:
        db.upsert(
            cursor, UNRESOLVED_TABLE,
            {"SourceID": source_id, "RawName": f["raw_name"]},
            {"CandidatePlayerIDs": ",".join(str(c) for c in sorted(local)) if local else None},
        )
    counts["resolved_search" if player_id is not None else "unresolved"] += 1
    return player_id


def sync_injury_history(cursor, rows: list, seasons: list | None = None, on_season_done=None) -> dict:
    """rows are api.nhl_injury_viz.read_rows() output. seasons, when given, restricts the
    import to those DisplayNames ('2025-26') -- only those seasons' spells are replaced.
    on_season_done(season_display) is called after each season is fully written (the CLI
    commits there, so a mid-run failure keeps every completed season)."""
    source_id = db.upsert_get_id(
        cursor, "Injuries.Sources", "SourceID",
        {"SourceName": SOURCE_NAME},
        {"Description": "nhlinjuryviz.blogspot.com Tableau Public workbook (public.tableau.com/workbooks/NHLinjurydatabase.twb)"},
    )

    counts = {
        "rows": len(rows), "playoffs_skipped": 0, "filtered_out": 0, "spells": 0, "undated": 0,
        "resolved_local": 0, "resolved_search": 0, "unresolved": 0, "schedule_rows": 0,
    }
    by_season: dict = defaultdict(list)
    for row in rows:
        f = field_map.injury_viz_row_fields(row)
        if f["is_playoffs"]:
            counts["playoffs_skipped"] += 1
            continue
        if seasons and f["season_display"] not in seasons:
            counts["filtered_out"] += 1
            continue
        by_season[f["nhl_season_id"]].append(f)

    player_index = name_resolver.load_player_index(cursor)
    alias_map = name_resolver.load_alias_map(cursor, ALIAS_TABLE, source_id)
    search_cache: dict = {}

    for nhl_season_id in sorted(by_season):
        fields = by_season[nhl_season_id]
        season_display = fields[0]["season_display"]
        season_id = ensure_season(cursor, {"SeasonID_NHL": nhl_season_id, "DisplayName": season_display})

        schedules: dict = {}
        for raw_team in sorted({f["raw_team"] for f in fields}):
            if raw_team not in TEAM_ABBREV:
                raise ValueError(f"Unmapped team label {raw_team!r} in season {season_display} -- add it to TEAM_ABBREV")
            abbrev = TEAM_ABBREV[raw_team]
            schedules[raw_team] = team_schedules.sync_team_season(cursor, abbrev, nhl_season_id, season_id)
            counts["schedule_rows"] += schedules[raw_team]["schedule_rows"]

        db.delete_where(cursor, "Injuries.Spells", {"SourceID": source_id, "SeasonID": season_id})

        for f in fields:
            schedule = schedules[f["raw_team"]]
            team_abbrev = TEAM_ABBREV[f["raw_team"]]
            player_id = _resolve_player(cursor, source_id, f, team_abbrev, alias_map, player_index, search_cache, counts)

            start = schedule["games"].get(f["start_game"])
            end = schedule["games"].get(f["end_game"])
            if start is None or end is None:
                counts["undated"] += 1
                log.warning(
                    "%s %s %r games %s-%s fall outside the team's %d-game schedule -- dates left NULL",
                    season_display, team_abbrev, f["raw_name"], f["start_game"], f["end_game"], len(schedule["games"]),
                )

            db.upsert(
                cursor, "Injuries.Spells",
                {
                    "SourceID": source_id, "SeasonID": season_id, "TeamID": schedule["team_id"],
                    "RawPlayerName": f["raw_name"], "StartGameNumber": f["start_game"],
                },
                {
                    "PlayerID": player_id,
                    "PositionGroup": f["position_group"],
                    "IsRetiredContract": f["is_retired_contract"],
                    "InjuryType": f["injury_type"],
                    "InjuryTypeGroup": f["injury_type_group"],
                    "GamesMissed": f["games_missed"],
                    "EndGameNumber": f["end_game"],
                    "StartDate": start["date"] if start else None,
                    "EndDate": end["date"] if end else None,
                    "StartNHLGameID": start["nhl_game_id"] if start else None,
                    "EndNHLGameID": end["nhl_game_id"] if end else None,
                    "CapHitMillions": f["cap_hit"],
                },
            )
            counts["spells"] += 1

        log.info("%s: %d spell(s) written across %d team(s)", season_display, len(fields), len(schedules))
        if on_season_done:
            on_season_done(season_display)

    return counts

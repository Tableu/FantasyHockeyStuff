"""Resolves an external source's raw player-name spelling to our PlayerID. Shared by
projections/importer.py (Projections.PlayerNameAliases/UnresolvedPlayerNames) and
ingest/fantasy_espn.py and friends (Fantasy.PlayerNameAliases/UnresolvedPlayerNames) --
same problem (a source's own name spelling vs. ours) recurs for any external source keyed
by player name rather than NHLPlayerID, so the alias/unresolved table names are passed in
rather than hardcoded.

Two-tier: an already-reviewed name goes straight through the alias table (the crosswalk
built up over time, see nhl_database_schema.sql). A name seen for the first time is
normalized (HTML-unescaped -- one source's export leaves apostrophes as "&#x27;" -- then
accents/suffixes/punctuation stripped) and matched against Reference.Players, also trying
common first-name nickname swaps (a source publishing "Alex Kerfoot" when we have "Alexander
Kerfoot", or "Nicholas Robertson" for our "Nick Robertson"). If that lands on exactly one
player, the match is auto-confirmed and recorded as a new alias so it's instant on every
future import. Anything that still doesn't resolve to exactly one player (zero matches, or
more than one -- e.g. the two NHL players named Elias Pettersson) is recorded in the
unresolved-names table for one-time manual review instead of guessed at, since a silent wrong
match would misattribute one player's data to another.
"""

import html
import re
import unicodedata

from nhl_pipeline import db

_SUFFIX_RE = re.compile(r"\s+(jr\.?|sr\.?|ii|iii|iv)$", re.IGNORECASE)
# Fantrax appends a parenthetical position code to disambiguate same-named players (e.g.
# "Elias Pettersson (D)" for the defenseman, vs. the bare name for the forward) -- stripped
# before matching since it isn't part of the name itself. When a source's raw name really is
# ambiguous between two real players in Reference.Players, stripping this doesn't lose
# anything: it still lands on more than one candidate and falls through to the same
# unresolved-name review queue as before.
_POS_SUFFIX_RE = re.compile(r"\s*\([a-z]{1,3}\)\s*$", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")

# Common first-name nicknames seen across sheets so far, each a group of interchangeable
# spellings (e.g. a sheet using "Alex Kerfoot" or "Alexandre Texier" for our "Alexander
# Kerfoot" / "Alexandre Texier"). Only the first token of a name is ever swapped, and a
# swap only auto-confirms a match if it still lands on exactly one player -- this is a
# convenience for the common case, not a substitute for the ambiguous-match safety net.
_NICKNAME_GROUPS = [
    {"alex", "alexander", "alexandre"},
    {"nick", "nicholas", "nicolas"},
    {"matt", "matthew", "matty"},
    {"josh", "joshua"},
    {"tom", "tommy", "thomas"},
    {"tony", "anthony"},
    {"cam", "cameron"},
    {"zach", "zack", "zachary"},
    {"will", "william"},
    {"yegor", "egor"},
    {"vincent", "vince", "vinnie", "vin"},
    {"jon", "jonathan"},
    {"jacob", "jake"},
    {"gabriel", "gabe", "gabby"},
    {"sam", "samuel"},
    {"dan", "danny", "daniel"},
    {"ben", "benjamin"},
    {"max", "maxim", "maxwell"},
    {"alexei", "alexey", "aleksei", "aleksey"},
    {"mike", "mikey", "michael"},
]
_NICKNAME_LOOKUP = {name: group for group in _NICKNAME_GROUPS for name in group}


def normalize_name(name: str) -> str:
    name = html.unescape(name)
    name = _POS_SUFFIX_RE.sub("", name)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = _SUFFIX_RE.sub("", name.lower().strip())
    name = _NON_ALNUM_RE.sub("", name)
    return re.sub(r"\s+", " ", name).strip()


def _name_variants(normalized_name: str) -> list:
    first, _, rest = normalized_name.partition(" ")
    group = _NICKNAME_LOOKUP.get(first)
    if not group or not rest:
        return [normalized_name]
    return [f"{variant} {rest}" for variant in group]


def load_player_index(cursor) -> dict:
    """Returns {normalized_full_name: [PlayerID, ...]}."""
    cursor.execute("SELECT PlayerID, FullName FROM Reference.Players")
    index: dict = {}
    for row in cursor.fetchall():
        index.setdefault(normalize_name(row.FullName), []).append(row.PlayerID)
    return index


def load_alias_map(cursor, alias_table: str, source_id: int) -> dict:
    cursor.execute(f"SELECT RawName, PlayerID FROM {alias_table} WHERE SourceID = ?", source_id)
    return {row.RawName: row.PlayerID for row in cursor.fetchall()}


def resolve_player_id(
    cursor, alias_table: str, unresolved_table: str, source_id: int,
    raw_name: str, alias_map: dict, player_index: dict,
) -> int | None:
    if raw_name in alias_map:
        return alias_map[raw_name]

    candidates: set = set()
    for variant in _name_variants(normalize_name(raw_name)):
        candidates.update(player_index.get(variant, []))

    if len(candidates) == 1:
        player_id = next(iter(candidates))
        db.upsert(
            cursor, alias_table,
            {"SourceID": source_id, "RawName": raw_name},
            {"PlayerID": player_id},
        )
        cursor.execute(
            f"DELETE FROM {unresolved_table} WHERE SourceID = ? AND RawName = ?",
            source_id, raw_name,
        )
        alias_map[raw_name] = player_id
        return player_id

    db.upsert(
        cursor, unresolved_table,
        {"SourceID": source_id, "RawName": raw_name},
        {"CandidatePlayerIDs": ",".join(str(c) for c in sorted(candidates)) if candidates else None},
    )
    return None

"""Best-effort team resolution for external sources, which don't agree on how they spell
team names: standard 3-letter abbreviations, full nicknames ("Avalanche"), a location instead
of a nickname ("Utah"), a nickname that's a substring of ours ("Knights" vs. our "Golden
Knights"), or a dotted/shortened style ("T.B"/"TB", "L.A"/"LA", "N.J"/"NJ", "S.J"/"SJ"). TeamID
is nullable everywhere this is used and isn't part of the player-identity join (PlayerID is),
so a spelling we can't resolve just leaves TeamID NULL rather than blocking the import.
"""

_ALIASES = {
    "L.A": "LAK", "N.J": "NJD", "S.J": "SJS", "T.B": "TBL",
    "LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL",
}


def load_team_index(cursor):
    """Returns (exact_match_index, [(TeamName_upper, TeamID), ...]) -- the pair list is used
    for the substring fallback (e.g. "KNIGHTS" in "GOLDEN KNIGHTS")."""
    cursor.execute("SELECT TeamID, Abbreviation, TeamName, Location FROM Reference.Teams")
    rows = cursor.fetchall()
    exact_index: dict = {}
    for r in rows:
        exact_index[r.Abbreviation.upper()] = r.TeamID
        exact_index[r.TeamName.upper()] = r.TeamID
        if r.Location:
            exact_index[r.Location.upper()] = r.TeamID
    name_pairs = [(r.TeamName.upper(), r.TeamID) for r in rows]
    return exact_index, name_pairs


def resolve_team_id(raw_team, exact_index: dict, team_name_pairs: list) -> int | None:
    if not raw_team:
        return None
    key = raw_team.strip().upper()
    key = _ALIASES.get(key, key)
    if key in exact_index:
        return exact_index[key]
    for team_name, team_id in team_name_pairs:
        if key in team_name:
            return team_id
    return None

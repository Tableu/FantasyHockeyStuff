"""Team-relative situation resolution -- the core nuance from the schema review: the raw
NHL situationCode (e.g. "1551") is ambiguous on its own. Reference.SituationCodeMap only
parses it into skater/goalie counts; resolving the *team-relative* SituationID (5v4 for one
team is 4v5 for the other, from the very same raw code) happens here by comparing those
counts against Reference.Situations.StrengthHome/StrengthAway, which are reused generically
as "strength of the team being scored" vs "strength of their opponent" -- not literally
tied to which team is home in a given game.
"""


def load_situation_code_map(cursor) -> dict:
    cursor.execute(
        "SELECT RawSituationCode, AwayGoalieInNet, AwaySkaters, HomeSkaters, HomeGoalieInNet "
        "FROM Reference.SituationCodeMap"
    )
    return {
        row.RawSituationCode: (row.AwayGoalieInNet, row.AwaySkaters, row.HomeSkaters, row.HomeGoalieInNet)
        for row in cursor.fetchall()
    }


def load_situations_by_strength(cursor) -> dict:
    cursor.execute(
        "SELECT SituationID, StrengthHome, StrengthAway FROM Reference.Situations "
        "WHERE StrengthHome IS NOT NULL AND StrengthAway IS NOT NULL"
    )
    return {(row.StrengthHome, row.StrengthAway): row.SituationID for row in cursor.fetchall()}


def get_all_situation_id(cursor) -> int:
    cursor.execute("SELECT SituationID FROM Reference.Situations WHERE SituationCode = 'ALL'")
    return cursor.fetchone()[0]


def resolve_situation_id(
    code_map: dict, situations_by_strength: dict, all_situation_id: int,
    raw_code, team_is_home: bool, unmapped_codes: set,
) -> int:
    row = code_map.get(raw_code)
    if row is None:
        if raw_code is not None:
            unmapped_codes.add(str(raw_code))
        return all_situation_id

    _away_goalie, away_skaters, home_skaters, _home_goalie = row
    team_skaters, opp_skaters = (home_skaters, away_skaters) if team_is_home else (away_skaters, home_skaters)

    situation_id = situations_by_strength.get((team_skaters, opp_skaters))
    if situation_id is None:
        unmapped_codes.add(f"{raw_code} (resolved {team_skaters}v{opp_skaters}, no Situations row)")
        return all_situation_id
    return situation_id


def classify_strength(code_map: dict, raw_code, team_is_home: bool) -> tuple:
    """Returns (is_power_play, is_short_handed) for the team at `team_is_home`, independent
    of whether a named Situations row exists, so goal PP/SH flags don't get lost just
    because a rare raw code isn't in the map yet.

    A pulled goalie is replaced on the ice by an extra attacker, which inflates that team's
    raw skater count by one without reflecting an actual penalty-driven advantage (e.g. raw
    code "0651" is just a normal 5-on-5 game where the away team pulled its goalie -- not a
    shorthanded situation for the home team). Comparing raw skater counts directly conflates
    "pulled goalie" with "power play/penalty kill", so each side's count is first adjusted
    back down by one when that side's goalie is out, recovering the true penalty-driven
    skater differential (which still correctly reports a PP/SH goal when a real power play
    and a goalie pull happen to coincide)."""
    row = code_map.get(raw_code)
    if row is None:
        return False, False
    away_goalie, away_skaters, home_skaters, home_goalie = row
    away_skaters -= 0 if away_goalie else 1
    home_skaters -= 0 if home_goalie else 1
    team_skaters, opp_skaters = (home_skaters, away_skaters) if team_is_home else (away_skaters, home_skaters)
    return team_skaters > opp_skaters, team_skaters < opp_skaters

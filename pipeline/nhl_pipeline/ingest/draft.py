"""Reference.Players -- adds a draft class using each pick's real, permanent NHLPlayerID,
resolved via api.player_search since api.draft's picks/{year}/all endpoint doesn't include
one. Using the real ID rather than a placeholder means a prospect who later plays a real NHL
game gets upserted onto this very same row by the normal ingest.teams_players pipeline
instead of creating a duplicate -- and is exactly why several 2026 draft picks show up as
unresolved names in Projections.UnresolvedPlayerNames until this is run (see
import_projections.py): they simply didn't have a PlayerID yet.

Team/roster affiliation (Reference.PlayerTeamHistory) is intentionally out of scope here,
same as ingest.teams_players.sync_players() -- team association is captured per-event once
the player actually appears in ingested game data.
"""

import logging

from nhl_pipeline import db
from nhl_pipeline.api import draft as api_draft
from nhl_pipeline.api import field_map, player_search

log = logging.getLogger("ingest.draft")


def _resolve_nhl_player_id(full_name: str, team_abbrev: str | None) -> int | None:
    candidates = [
        field_map.player_search_result_fields(r)
        for r in player_search.search_player(full_name)
        if (r.get("name") or "").strip().lower() == full_name.strip().lower()
    ]
    if len(candidates) == 1:
        return candidates[0]["nhl_player_id"]
    if len(candidates) > 1 and team_abbrev:
        team_matches = [c for c in candidates if c["team_abbrev"] == team_abbrev]
        if len(team_matches) == 1:
            return team_matches[0]["nhl_player_id"]
    return None


def sync_draft_class(cursor, year: int) -> dict:
    picks = api_draft.get_draft_picks(year)

    counts = {"added": 0, "unresolved": 0}
    unresolved_names = []
    for pick in picks:
        f = field_map.draft_pick_fields(pick)
        if not f["full_name"]:
            continue

        nhl_player_id = _resolve_nhl_player_id(f["full_name"], f["team_abbrev"])
        if nhl_player_id is None:
            counts["unresolved"] += 1
            unresolved_names.append(f["full_name"])
            continue

        db.upsert_get_id(
            cursor, "Reference.Players", "PlayerID",
            {"NHLPlayerID": nhl_player_id},
            {
                "FirstName": f["first_name"], "LastName": f["last_name"], "FullName": f["full_name"],
                "PositionCode": f["position_code"],
                "HeightInches": f["height_inches"], "WeightLbs": f["weight_lbs"],
            },
        )
        counts["added"] += 1

    if unresolved_names:
        log.warning("Could not resolve an NHL player ID for: %s", ", ".join(unresolved_names))

    return counts

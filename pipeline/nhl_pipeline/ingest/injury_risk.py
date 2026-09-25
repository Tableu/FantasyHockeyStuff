"""Injuries.RiskLists -- a published list of players at risk of missing games, per season
(first: Dobber's Band-Aid Boys, api.dobber_bandaid). One row per listed player: the source's own
tier and name, resolved to PlayerID through nhl_pipeline.name_resolver with the Injuries alias and
unresolved tables (shared with the injury history, keyed by SourceID). Re-importing a season
replaces that source's rows for it wholesale; a name that cannot be settled is written with
PlayerID NULL and left in Injuries.UnresolvedPlayerNames for a manual alias.
"""

import logging

from nhl_pipeline import db, name_resolver

log = logging.getLogger("ingest.injury_risk")

TABLE = "Injuries.RiskLists"
ALIAS_TABLE = "Injuries.PlayerNameAliases"
UNRESOLVED_TABLE = "Injuries.UnresolvedPlayerNames"

_DDL = """CREATE TABLE Injuries.RiskLists
(
    SourceID            INT NOT NULL,
    SeasonID            INT NOT NULL,
    RawPlayerName       VARCHAR(200) NOT NULL,
    PlayerID            BIGINT NULL,
    Tier                VARCHAR(20) NOT NULL,
    ImportedAt          DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_InjuryRiskLists PRIMARY KEY (SourceID, SeasonID, RawPlayerName),
    CONSTRAINT FK_IRL_Source FOREIGN KEY (SourceID) REFERENCES Injuries.Sources(SourceID),
    CONSTRAINT FK_IRL_Season FOREIGN KEY (SeasonID) REFERENCES Reference.Seasons(SeasonID),
    CONSTRAINT FK_IRL_Player FOREIGN KEY (PlayerID) REFERENCES Reference.Players(PlayerID)
);"""

# Spellings the resolver cannot bridge, confirmed by hand: raw name -> PlayerID.
CONFIRMED_ALIASES = {"Matt Dumba": 814}         # Reference.Players: Mathew Dumba (D, b. 1994)

# The source's goalie group is the only position it gives; the other groups mix F and D.
_POSITION_HINT = {"Goalie": ["G"]}


def ensure_table(cursor) -> None:
    """Create Injuries.RiskLists if this database predates it (nhl_database_schema.sql)."""
    cursor.execute("IF OBJECT_ID('Injuries.RiskLists', 'U') IS NULL EXEC('" +
                   " ".join(line.strip() for line in _DDL.splitlines()).replace("'", "''") + "')")


def sync_risk_list(cursor, season_id: int, source_name: str, description: str, rows: list) -> dict:
    source_id = db.upsert_get_id(cursor, "Injuries.Sources", "SourceID",
                                 {"SourceName": source_name}, {"Description": description})
    ensure_table(cursor)
    db.delete_where(cursor, TABLE, {"SourceID": source_id, "SeasonID": season_id})

    alias_map = name_resolver.load_alias_map(cursor, ALIAS_TABLE, source_id)
    player_index = name_resolver.load_player_index(cursor)
    for raw_name, player_id in CONFIRMED_ALIASES.items():
        if alias_map.get(raw_name) != player_id:
            db.upsert(cursor, ALIAS_TABLE, {"SourceID": source_id, "RawName": raw_name},
                      {"PlayerID": player_id})
            cursor.execute(f"DELETE FROM {UNRESOLVED_TABLE} WHERE SourceID = ? AND RawName = ?",
                           source_id, raw_name)
            alias_map[raw_name] = player_id
    counts = {"listed": len(rows), "resolved": 0, "unresolved": []}
    for r in rows:
        player_id = name_resolver.resolve_player_id(
            cursor, ALIAS_TABLE, UNRESOLVED_TABLE, source_id, r["name"], alias_map, player_index,
            position_codes=_POSITION_HINT.get(r["tier"]))
        db.upsert(cursor, TABLE,
                  {"SourceID": source_id, "SeasonID": season_id, "RawPlayerName": r["name"]},
                  {"PlayerID": player_id, "Tier": r["tier"]})
        if player_id is None:
            counts["unresolved"].append(r["name"])
        else:
            counts["resolved"] += 1
    log.info("%s: %d listed, %d resolved, %d unresolved %s", source_name, counts["listed"],
             counts["resolved"], len(counts["unresolved"]), counts["unresolved"])
    return counts

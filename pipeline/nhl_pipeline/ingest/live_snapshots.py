"""The Live schema -- point-in-time snapshots of injury reports, line charts and starting-goalie
reports, taken on a schedule during the season (snapshot_live.py). Nothing here is ever
overwritten: a question like "what did we know about him at 18:45 UTC?" has to stay answerable,
because that is what the live flag reads before each lineup lock and what recalibrating the
lineup noise (perturb) later compares against the lineups that actually played.

Every poll is one row in Live.SnapshotRuns (kind, source, when, how many rows it saw and
wrote, or the error). The data tables are CHANGE LOGS keyed to a run, so a 5-minute poll does
not copy the same report 100 times a day:

- Live.InjuryStatus: one row when a player's status from a source changes -- including the
  row that says he left the report (MappedStatus 'ACTIVE'). A player's status at time T is his
  latest row at or before T.
- Live.LineCharts + Live.LineChartPlayers: a team's whole chart, written again only when its
  content changes (a chart is a set; one changed line means a new set).
- Live.GoalieReports: one row per (game date, team) whenever the named goalie, the report's
  strength or its timestamp changes.

Statuses are mapped to four values (user decision, 2026-09-25): OUT and SUSP are "injured" (the
hard gate); DTD is questionable (lower odds of playing that day, handled downstream); ACTIVE
means off the report. A value this module has never seen maps to UNKNOWN with a warning.

Sources live in Injuries.Sources (the registry the name-alias tables are keyed on). Names are
resolved with nhl_pipeline.name_resolver; Fleaflicker players by their platform id first
(Fantasy.PlatformPlayerIDs), then Fleaflicker's existing Fantasy aliases.
"""

import datetime as dt
import hashlib
import logging

from nhl_pipeline import db, name_resolver

log = logging.getLogger("ingest.live_snapshots")

SOURCES = {
    "fleaflicker": ("Fleaflicker", "Fleaflicker league injury designations (OUT / IR); IR = IR-slot eligible"),
    "espn": ("ESPN injuries", "ESPN's NHL injuries endpoint: status, fantasy status, estimated return"),
    "dailyfaceoff": ("Daily Faceoff", "Daily Faceoff line charts (with injury / GTD flags) and starting goalies"),
}
FLEAFLICKER_PLATFORM = "Fleaflicker"

INJURED = ("OUT", "SUSP")

_FLEAFLICKER_STATUS = {"OUT": "OUT", "IR": "OUT", "DTD": "DTD", "Q": "DTD", "SUSP": "SUSP"}
_ESPN_STATUS = {"Out": "OUT", "Injured Reserve": "OUT", "Day-To-Day": "DTD", "Suspension": "SUSP"}
_DFO_STATUS = {"out": "OUT", "ir": "OUT", "dtd": "DTD"}

# Reference.Players spells wingers both ways (L/LW, R/RW); a hint must accept either.
_POSITION_CODES = {"c": ["C"], "lw": ["L", "LW"], "l": ["L", "LW"], "rw": ["R", "RW"],
                   "r": ["R", "RW"], "ld": ["D"], "rd": ["D"], "d": ["D"], "g": ["G"],
                   "f": ["C", "L", "LW", "R", "RW"]}

# Platforms that abbreviate teams their own way -> Reference.Teams.Abbreviation.
_TEAM_ALIASES = {"LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL", "UTAH": "UTA", "VEG": "VGK",
                 "WAS": "WSH", "MON": "MTL", "CLS": "CBJ", "NAS": "NSH", "CAL": "CGY"}

_DDL = [
    ("Live.SnapshotRuns", """CREATE TABLE Live.SnapshotRuns
(
    SnapshotRunID   BIGINT IDENTITY(1,1) NOT NULL,
    Kind            VARCHAR(20) NOT NULL,
    SourceID        INT NOT NULL,
    SnapshotAt      DATETIME2(0) NOT NULL,
    RowsSeen        INT NULL,
    RowsWritten     INT NULL,
    Error           VARCHAR(1000) NULL,
    CONSTRAINT PK_LiveSnapshotRuns PRIMARY KEY (SnapshotRunID),
    CONSTRAINT FK_LSR_Source FOREIGN KEY (SourceID) REFERENCES Injuries.Sources(SourceID)
);"""),
    ("Live.InjuryStatus", """CREATE TABLE Live.InjuryStatus
(
    SnapshotRunID       BIGINT NOT NULL,
    ExternalPlayerID    VARCHAR(50) NOT NULL,
    RawPlayerName       VARCHAR(200) NOT NULL,
    PlayerID            BIGINT NULL,
    TeamID              INT NULL,
    RawStatus           VARCHAR(50) NULL,
    MappedStatus        VARCHAR(10) NOT NULL,
    IREligible          BIT NULL,
    ReturnDate          DATE NULL,
    Detail              VARCHAR(1000) NULL,
    CONSTRAINT PK_LiveInjuryStatus PRIMARY KEY (SnapshotRunID, ExternalPlayerID),
    CONSTRAINT FK_LIS_Run FOREIGN KEY (SnapshotRunID) REFERENCES Live.SnapshotRuns(SnapshotRunID),
    CONSTRAINT FK_LIS_Player FOREIGN KEY (PlayerID) REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_LIS_Team FOREIGN KEY (TeamID) REFERENCES Reference.Teams(TeamID)
);"""),
    ("Live.LineCharts", """CREATE TABLE Live.LineCharts
(
    LineChartID         BIGINT IDENTITY(1,1) NOT NULL,
    SnapshotRunID       BIGINT NOT NULL,
    TeamID              INT NOT NULL,
    SourceLabel         VARCHAR(100) NULL,
    SourceUpdatedAt     DATETIME2(3) NULL,
    ContentHash         CHAR(64) NOT NULL,
    CONSTRAINT PK_LiveLineCharts PRIMARY KEY (LineChartID),
    CONSTRAINT FK_LLC_Run FOREIGN KEY (SnapshotRunID) REFERENCES Live.SnapshotRuns(SnapshotRunID),
    CONSTRAINT FK_LLC_Team FOREIGN KEY (TeamID) REFERENCES Reference.Teams(TeamID)
);"""),
    ("Live.LineChartPlayers", """CREATE TABLE Live.LineChartPlayers
(
    LineChartID         BIGINT NOT NULL,
    ExternalPlayerID    VARCHAR(50) NOT NULL,
    GroupIdentifier     VARCHAR(10) NOT NULL,
    RawPlayerName       VARCHAR(200) NOT NULL,
    PlayerID            BIGINT NULL,
    Position            VARCHAR(5) NULL,
    InjuryStatus        VARCHAR(20) NULL,
    GameTimeDecision    BIT NOT NULL,
    CONSTRAINT PK_LiveLineChartPlayers PRIMARY KEY (LineChartID, ExternalPlayerID, GroupIdentifier),
    CONSTRAINT FK_LLCP_Chart FOREIGN KEY (LineChartID) REFERENCES Live.LineCharts(LineChartID),
    CONSTRAINT FK_LLCP_Player FOREIGN KEY (PlayerID) REFERENCES Reference.Players(PlayerID)
);"""),
    ("Live.PlayerStatus", """CREATE TABLE Live.PlayerStatus
(
    PlayerStatusID      BIGINT IDENTITY(1,1) NOT NULL,
    ChangedAt           DATETIME2(0) NOT NULL,
    PlayerID            BIGINT NOT NULL,
    TeamID              INT NULL,
    Status              VARCHAR(10) NOT NULL,
    GameTimeDecision    BIT NOT NULL,
    IREligible          BIT NOT NULL,
    Sources             VARCHAR(300) NULL,
    CONSTRAINT PK_LivePlayerStatus PRIMARY KEY (PlayerStatusID),
    CONSTRAINT FK_LPS_Player FOREIGN KEY (PlayerID) REFERENCES Reference.Players(PlayerID),
    CONSTRAINT FK_LPS_Team FOREIGN KEY (TeamID) REFERENCES Reference.Teams(TeamID)
);"""),
    ("Live.GoalieReports", """CREATE TABLE Live.GoalieReports
(
    SnapshotRunID       BIGINT NOT NULL,
    GameDate            DATE NOT NULL,
    TeamID              INT NOT NULL,
    NHLGameID           INT NULL,
    PuckUTC             DATETIME2(0) NULL,
    ExternalPlayerID    VARCHAR(50) NULL,
    RawPlayerName       VARCHAR(200) NULL,
    PlayerID            BIGINT NULL,
    Strength            VARCHAR(30) NULL,
    NewsCreatedAt       DATETIME2(3) NULL,
    NewsSourceName      VARCHAR(200) NULL,
    NewsSourceUrl       VARCHAR(500) NULL,
    CONSTRAINT PK_LiveGoalieReports PRIMARY KEY (SnapshotRunID, GameDate, TeamID),
    CONSTRAINT FK_LGR_Run FOREIGN KEY (SnapshotRunID) REFERENCES Live.SnapshotRuns(SnapshotRunID),
    CONSTRAINT FK_LGR_Team FOREIGN KEY (TeamID) REFERENCES Reference.Teams(TeamID),
    CONSTRAINT FK_LGR_Player FOREIGN KEY (PlayerID) REFERENCES Reference.Players(PlayerID)
);"""),
]


def ensure_tables(cursor) -> None:
    """Create the Live schema and its tables if this database predates them (nhl_database_schema.sql)."""
    cursor.execute("IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'Live') EXEC('CREATE SCHEMA Live')")
    for name, ddl in _DDL:
        cursor.execute(f"IF OBJECT_ID('{name}', 'U') IS NULL EXEC('" +
                       " ".join(line.strip() for line in ddl.splitlines()).replace("'", "''") + "')")


def source_id(cursor, key: str) -> int:
    name, description = SOURCES[key]
    return db.upsert_get_id(cursor, "Injuries.Sources", "SourceID",
                            {"SourceName": name}, {"Description": description})


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def parse_utc(value):
    """An ISO timestamp from a source ('...Z' or with an offset) as naive UTC, or None."""
    if not value:
        return None
    stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return stamp


def start_run(cursor, kind: str, source: int, at: dt.datetime) -> int:
    cursor.execute("INSERT INTO Live.SnapshotRuns (Kind, SourceID, SnapshotAt) "
                   "OUTPUT inserted.SnapshotRunID VALUES (?, ?, ?)", kind, source, at)
    return cursor.fetchone()[0]


def finish_run(cursor, run_id: int, seen: int, written: int) -> None:
    cursor.execute("UPDATE Live.SnapshotRuns SET RowsSeen = ?, RowsWritten = ? WHERE SnapshotRunID = ?",
                   seen, written, run_id)


def record_failure(cursor, kind: str, source: int, at: dt.datetime, error: str) -> None:
    cursor.execute("INSERT INTO Live.SnapshotRuns (Kind, SourceID, SnapshotAt, Error) VALUES (?, ?, ?, ?)",
                   kind, source, at, error[:1000])


# ---------- teams ----------

class Teams:
    """The season's NHL clubs by every spelling the sources use: abbreviation (with the
    platforms' own variants) and full name ('Montreal Canadiens', accents folded)."""

    def __init__(self, cursor, season_id: int):
        cursor.execute("""
            SELECT DISTINCT t.TeamID, t.Abbreviation, t.Location, t.TeamName
            FROM Reference.Teams t
            JOIN Reference.Schedule s ON t.TeamID IN (s.HomeTeamID, s.AwayTeamID)
            WHERE s.SeasonID = ? AND s.GameType = '2'""", season_id)
        self.by_abbreviation, self.by_name = {}, {}
        for row in cursor.fetchall():
            self.by_abbreviation[row.Abbreviation] = row.TeamID
            self.by_name[name_resolver.normalize_name(f"{row.Location} {row.TeamName}")] = row.TeamID
        if len(self.by_abbreviation) != 32:
            log.warning("expected 32 clubs in the season's schedule, found %d", len(self.by_abbreviation))

    def from_abbreviation(self, abbreviation):
        if not abbreviation:
            return None
        code = abbreviation.upper()
        return self.by_abbreviation.get(_TEAM_ALIASES.get(code, code))

    def from_name(self, name):
        return self.by_name.get(name_resolver.normalize_name(name)) if name else None


# ---------- players ----------

class Resolver:
    """PlayerID for a source's player: its own id first when the source has a platform id map,
    then its name through the shared resolver (auto-aliasing single matches, leaving the rest
    in the unresolved table for a manual alias -- never guessed)."""

    def __init__(self, cursor, alias_source_id: int, alias_table: str, unresolved_table: str,
                 player_index: dict, id_map: dict | None = None):
        self.cursor = cursor
        self.alias_source_id = alias_source_id
        self.alias_table, self.unresolved_table = alias_table, unresolved_table
        self.alias_map = name_resolver.load_alias_map(cursor, alias_table, alias_source_id)
        self.player_index = player_index
        self.id_map = id_map or {}

    def resolve(self, external_id, name, position=None):
        if external_id is not None and external_id in self.id_map:
            return self.id_map[external_id]
        if not name:
            return None
        return name_resolver.resolve_player_id(
            self.cursor, self.alias_table, self.unresolved_table, self.alias_source_id, name,
            self.alias_map, self.player_index,
            # Daily Faceoff numbers goalie slots (g1, g2); the digit is not part of the position.
            position_codes=_POSITION_CODES.get((position or "").lower().rstrip("0123456789")))


# Spellings the resolver cannot bridge, checked by hand: source key -> {raw name: PlayerID}.
CONFIRMED_ALIASES = {
    # Vancouver's defenceman (b. 2004); Daily Faceoff adds his middle name to tell him from the centre.
    "dailyfaceoff": {"Elias Nils Pettersson": 784},
}


def injuries_resolver(cursor, source: int, player_index: dict, source_key: str | None = None) -> Resolver:
    resolver = Resolver(cursor, source, "Injuries.PlayerNameAliases", "Injuries.UnresolvedPlayerNames",
                        player_index)
    for raw_name, player_id in CONFIRMED_ALIASES.get(source_key, {}).items():
        if resolver.alias_map.get(raw_name) != player_id:
            db.upsert(cursor, resolver.alias_table, {"SourceID": source, "RawName": raw_name},
                      {"PlayerID": player_id})
            cursor.execute(f"DELETE FROM {resolver.unresolved_table} WHERE SourceID = ? AND RawName = ?",
                           source, raw_name)
            resolver.alias_map[raw_name] = player_id
    return resolver


def fleaflicker_resolver(cursor, season_id: int, player_index: dict) -> Resolver:
    platform_id = db.upsert_get_id(cursor, "Fantasy.Platforms", "FantasyPlatformID",
                                   {"PlatformName": FLEAFLICKER_PLATFORM}, None)
    cursor.execute("SELECT ExternalPlayerID, PlayerID FROM Fantasy.PlatformPlayerIDs "
                   "WHERE FantasyPlatformID = ? AND SeasonID = ?", platform_id, season_id)
    id_map = {row.ExternalPlayerID: row.PlayerID for row in cursor.fetchall()}
    return Resolver(cursor, platform_id, "Fantasy.PlayerNameAliases", "Fantasy.UnresolvedPlayerNames",
                    player_index, id_map)


# ---------- injury status ----------

def map_status(table: dict, raw, source_name: str) -> str:
    if raw in table:
        return table[raw]
    log.warning("%s: unmapped status %r -> UNKNOWN (add it to live_snapshots)", source_name, raw)
    return "UNKNOWN"


def fleaflicker_status_rows(injuries: list, teams: Teams, resolver: Resolver) -> list:
    return [{
        "external_id": r["external_id"], "name": r["name"],
        "player_id": resolver.resolve(r["external_id"], r["name"], r["position"]),
        "team_id": teams.from_abbreviation(r["team_abbreviation"]),
        "raw_status": r["type"], "mapped": map_status(_FLEAFLICKER_STATUS, r["type"], "Fleaflicker"),
        "ir_eligible": r["type"] == "IR", "return_date": None,
        "detail": r["description"],
    } for r in injuries]


def espn_status_rows(injuries: list, teams: Teams, resolver: Resolver) -> list:
    rows = []
    for r in injuries:
        if not r["external_id"]:
            log.warning("ESPN: no athlete id for %s, skipped", r["name"])
            continue
        detail = " | ".join(x for x in (r["fantasy_status"], r["body_part"], r["comment"]) if x)
        rows.append({
            "external_id": r["external_id"], "name": r["name"],
            "player_id": resolver.resolve(r["external_id"], r["name"], r["position"]),
            "team_id": teams.from_abbreviation(r["team_abbreviation"]),
            "raw_status": r["status"], "mapped": map_status(_ESPN_STATUS, r["status"], "ESPN"),
            "ir_eligible": None,
            "return_date": dt.date.fromisoformat(r["return_date"][:10]) if r["return_date"] else None,
            "detail": detail or None,
        })
    return rows


def dfo_status_rows(charts: list, resolver: Resolver) -> list:
    """Daily Faceoff's own injury flags, from the charts: a player marked out/dtd, or listed
    in the chart's IR group. One row per player (he may appear in several groups)."""
    rows = {}
    for chart in charts:
        for p in chart["players"]:
            raw = p["injury_status"] or ("ir" if p["group"] == "ir" else None)
            if raw is None or p["external_id"] in rows:
                continue
            rows[p["external_id"]] = {
                "external_id": p["external_id"], "name": p["name"],
                "player_id": p["player_id"], "team_id": chart["team_id"],
                "raw_status": raw, "mapped": map_status(_DFO_STATUS, raw, "Daily Faceoff"),
                "ir_eligible": None, "return_date": None, "detail": None,
            }
    return list(rows.values())


def _latest_status(cursor, source: int) -> dict:
    """{ExternalPlayerID: latest row} for one source."""
    cursor.execute("""
        SELECT ExternalPlayerID, RawPlayerName, PlayerID, TeamID, RawStatus, MappedStatus,
               IREligible, ReturnDate
        FROM (SELECT s.*, ROW_NUMBER() OVER (PARTITION BY s.ExternalPlayerID
                                             ORDER BY r.SnapshotAt DESC, r.SnapshotRunID DESC) AS rn
              FROM Live.InjuryStatus s JOIN Live.SnapshotRuns r ON r.SnapshotRunID = s.SnapshotRunID
              WHERE r.SourceID = ?) x
        WHERE rn = 1""", source)
    return {row.ExternalPlayerID: row for row in cursor.fetchall()}


def _status_key(raw_status, mapped, ir_eligible, return_date, team_id):
    return (raw_status, mapped, None if ir_eligible is None else bool(ir_eligible), return_date, team_id)


def write_status(cursor, run_id: int, source: int, rows: list, covered_team_ids=None) -> int:
    """Append the rows whose status changed since this source's last report, plus an ACTIVE row
    for every player who was on the report and no longer is. `covered_team_ids` limits the
    ACTIVE rows to teams this run actually read (a failed team page must not clear its players)."""
    latest = _latest_status(cursor, source)
    written = 0
    seen = set()
    for r in rows:
        seen.add(r["external_id"])
        prior = latest.get(r["external_id"])
        if prior is not None and prior.PlayerID is None and r["player_id"] is not None:
            # A name that resolves now (a debut, a new alias) fills in its earlier rows; only
            # NULLs are touched, never a recorded status.
            cursor.execute(
                "UPDATE s SET PlayerID = ? FROM Live.InjuryStatus s JOIN Live.SnapshotRuns r "
                "ON r.SnapshotRunID = s.SnapshotRunID WHERE r.SourceID = ? AND s.ExternalPlayerID = ? "
                "AND s.PlayerID IS NULL", r["player_id"], source, r["external_id"])
        key = _status_key(r["raw_status"], r["mapped"], r["ir_eligible"], r["return_date"], r["team_id"])
        if prior is not None and key == _status_key(prior.RawStatus, prior.MappedStatus, prior.IREligible,
                                                    prior.ReturnDate, prior.TeamID):
            continue
        _insert_status(cursor, run_id, r["external_id"], r["name"], r["player_id"], r["team_id"],
                       r["raw_status"], r["mapped"], r["ir_eligible"], r["return_date"], r["detail"])
        written += 1
    for external_id, prior in latest.items():
        if external_id in seen or prior.MappedStatus == "ACTIVE":
            continue
        if covered_team_ids is not None and prior.TeamID not in covered_team_ids:
            continue
        _insert_status(cursor, run_id, external_id, prior.RawPlayerName, prior.PlayerID, prior.TeamID,
                       None, "ACTIVE", False if prior.IREligible is not None else None, None, None)
        written += 1
    return written


def _insert_status(cursor, run_id, external_id, name, player_id, team_id, raw_status, mapped,
                   ir_eligible, return_date, detail):
    cursor.execute(
        "INSERT INTO Live.InjuryStatus (SnapshotRunID, ExternalPlayerID, RawPlayerName, PlayerID, TeamID, "
        "RawStatus, MappedStatus, IREligible, ReturnDate, Detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        run_id, external_id, name[:200], player_id, team_id, raw_status, mapped, ir_eligible,
        return_date, detail[:1000] if detail else None)


# ---------- line charts ----------

def chart_hash(chart: dict) -> str:
    content = sorted((p["external_id"], p["group"], p["injury_status"] or "", p["game_time_decision"])
                     for p in chart["players"])
    return hashlib.sha256(repr((chart["source_label"], content)).encode()).hexdigest()


def _latest_chart_hashes(cursor, source: int) -> dict:
    cursor.execute("""
        SELECT TeamID, ContentHash
        FROM (SELECT c.TeamID, c.ContentHash,
                     ROW_NUMBER() OVER (PARTITION BY c.TeamID ORDER BY c.LineChartID DESC) AS rn
              FROM Live.LineCharts c JOIN Live.SnapshotRuns r ON r.SnapshotRunID = c.SnapshotRunID
              WHERE r.SourceID = ?) x
        WHERE rn = 1""", source)
    return {row.TeamID: row.ContentHash for row in cursor.fetchall()}


def write_charts(cursor, run_id: int, source: int, charts: list) -> int:
    """A team's chart is written whole, and only when it differs from the last one kept."""
    latest = _latest_chart_hashes(cursor, source)
    written = 0
    for chart in charts:
        digest = chart_hash(chart)
        if latest.get(chart["team_id"]) == digest:
            continue
        cursor.execute(
            "INSERT INTO Live.LineCharts (SnapshotRunID, TeamID, SourceLabel, SourceUpdatedAt, ContentHash) "
            "OUTPUT inserted.LineChartID VALUES (?, ?, ?, ?, ?)",
            run_id, chart["team_id"], chart["source_label"], parse_utc(chart["updated_at"]), digest)
        chart_id = cursor.fetchone()[0]
        for p in chart["players"]:
            cursor.execute(
                "INSERT INTO Live.LineChartPlayers (LineChartID, ExternalPlayerID, GroupIdentifier, "
                "RawPlayerName, PlayerID, Position, InjuryStatus, GameTimeDecision) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                chart_id, p["external_id"], p["group"], p["name"][:200], p["player_id"], p["position"],
                p["injury_status"], p["game_time_decision"])
        written += 1
    return written


# ---------- goalie reports ----------

def _latest_goalie_reports(cursor, source: int, game_dates: list) -> dict:
    if not game_dates:
        return {}
    marks = ",".join("?" * len(game_dates))
    cursor.execute(f"""
        SELECT GameDate, TeamID, ExternalPlayerID, Strength, NewsCreatedAt
        FROM (SELECT g.*, ROW_NUMBER() OVER (PARTITION BY g.GameDate, g.TeamID
                                             ORDER BY r.SnapshotAt DESC, r.SnapshotRunID DESC) AS rn
              FROM Live.GoalieReports g JOIN Live.SnapshotRuns r ON r.SnapshotRunID = g.SnapshotRunID
              WHERE r.SourceID = ? AND g.GameDate IN ({marks})) x
        WHERE rn = 1""", source, *game_dates)
    return {(row.GameDate, row.TeamID): (row.ExternalPlayerID, row.Strength, row.NewsCreatedAt)
            for row in cursor.fetchall()}


def schedule_games(cursor, game_date: dt.date) -> dict:
    """{(HomeTeamID, AwayTeamID): NHLGameID} for the date."""
    cursor.execute("SELECT NHLGameID, HomeTeamID, AwayTeamID FROM Reference.Schedule WHERE GameDate = ?",
                   game_date)
    return {(row.HomeTeamID, row.AwayTeamID): row.NHLGameID for row in cursor.fetchall()}


def write_goalie_reports(cursor, run_id: int, source: int, rows: list) -> int:
    """`rows` already carry team_id, player_id, game_date and nhl_game_id."""
    latest = _latest_goalie_reports(cursor, source, sorted({r["game_date"] for r in rows}))
    written = 0
    for r in rows:
        created = parse_utc(r["news_created_at"])
        if created is not None:
            created = created.replace(microsecond=created.microsecond // 1000 * 1000)
        if latest.get((r["game_date"], r["team_id"])) == (r["external_id"], r["strength"], created):
            continue
        cursor.execute(
            "INSERT INTO Live.GoalieReports (SnapshotRunID, GameDate, TeamID, NHLGameID, PuckUTC, "
            "ExternalPlayerID, RawPlayerName, PlayerID, Strength, NewsCreatedAt, NewsSourceName, NewsSourceUrl) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            run_id, r["game_date"], r["team_id"], r["nhl_game_id"], parse_utc(r["puck_utc"]),
            r["external_id"], r["name"], r["player_id"], r["strength"], created,
            (r["news_source_name"] or None) and r["news_source_name"][:200],
            (r["news_source_url"] or None) and r["news_source_url"][:500])
        written += 1
    return written


# ---------- merged status ----------

# When two sources that list a player changed at the same poll, the more severe one wins.
_SEVERITY = {"OUT": 3, "SUSP": 3, "DTD": 2, "UNKNOWN": 1, "ACTIVE": 0}


def merge_status(source_rows: list, gtd_players: set, fleaflicker_source: int) -> dict:
    """{PlayerID: (status, team_id, gtd, ir_eligible, sources)} from each source's latest row per
    player. Only sources that have listed a player speak for him (the sources cover different
    players -- 2026-09-25: 19 of Fleaflicker's 39 OUT were not on ESPN's report), and among
    those the most recent change wins: a clearing (ACTIVE) row is news too. `source_rows` are
    (SourceID, SourceName, PlayerID, TeamID, MappedStatus, RawStatus, SnapshotAt)."""
    by_player: dict = {}
    for row in source_rows:
        by_player.setdefault(row.PlayerID, []).append(row)
    merged = {}
    for player_id, rows in by_player.items():
        best = max(rows, key=lambda r: (r.SnapshotAt, _SEVERITY.get(r.MappedStatus, 1)))
        status = "ACTIVE" if best.MappedStatus == "UNKNOWN" else best.MappedStatus
        ir_eligible = any(r.SourceID == fleaflicker_source and r.RawStatus == "IR" for r in rows)
        sources = ",".join(f"{r.SourceName}:{r.MappedStatus}" for r in sorted(rows, key=lambda r: r.SourceName))
        merged[player_id] = (status, best.TeamID, player_id in gtd_players, ir_eligible, sources[:300])
    for player_id in gtd_players - merged.keys():
        merged[player_id] = ("ACTIVE", None, True, False, "Daily Faceoff:GTD")
    return merged


def write_player_status(cursor, at: dt.datetime) -> int:
    """Recompute every reported player's merged status and append the ones that changed."""
    fleaflicker = source_id(cursor, "fleaflicker")
    cursor.execute("""
        SELECT x.SourceID, src.SourceName, x.PlayerID, x.TeamID, x.MappedStatus, x.RawStatus, x.SnapshotAt
        FROM (SELECT r.SourceID, s.PlayerID, s.TeamID, s.MappedStatus, s.RawStatus, r.SnapshotAt,
                     ROW_NUMBER() OVER (PARTITION BY r.SourceID, s.ExternalPlayerID
                                        ORDER BY r.SnapshotAt DESC, r.SnapshotRunID DESC) AS rn
              FROM Live.InjuryStatus s JOIN Live.SnapshotRuns r ON r.SnapshotRunID = s.SnapshotRunID
              WHERE r.SnapshotAt <= ?) x
        JOIN Injuries.Sources src ON src.SourceID = x.SourceID
        WHERE x.rn = 1 AND x.PlayerID IS NOT NULL""", at)
    source_rows = cursor.fetchall()
    cursor.execute("""
        SELECT DISTINCT p.PlayerID
        FROM (SELECT c.LineChartID, ROW_NUMBER() OVER (PARTITION BY c.TeamID ORDER BY c.LineChartID DESC) AS rn
              FROM Live.LineCharts c JOIN Live.SnapshotRuns r ON r.SnapshotRunID = c.SnapshotRunID
              WHERE r.SnapshotAt <= ?) c
        JOIN Live.LineChartPlayers p ON p.LineChartID = c.LineChartID
        WHERE c.rn = 1 AND p.GameTimeDecision = 1 AND p.PlayerID IS NOT NULL""", at)
    gtd = {row.PlayerID for row in cursor.fetchall()}
    merged = merge_status(source_rows, gtd, fleaflicker)

    cursor.execute("""
        SELECT PlayerID, TeamID, Status, GameTimeDecision, IREligible
        FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY PlayerID ORDER BY ChangedAt DESC, PlayerStatusID DESC) AS rn
              FROM Live.PlayerStatus) x
        WHERE rn = 1""")
    latest = {row.PlayerID: (row.Status, row.TeamID, bool(row.GameTimeDecision), bool(row.IREligible))
              for row in cursor.fetchall()}
    # A player who has left every report and every GTD list goes back to ACTIVE, once.
    for player_id, (status, team_id, gtd_flag, ir_flag) in latest.items():
        if player_id not in merged and (status != "ACTIVE" or gtd_flag or ir_flag):
            merged[player_id] = ("ACTIVE", team_id, False, False, None)

    written = 0
    for player_id, (status, team_id, gtd_flag, ir_flag, sources) in merged.items():
        prior = latest.get(player_id)
        team_id = team_id if team_id is not None else (prior[1] if prior else None)
        if prior == (status, team_id, gtd_flag, ir_flag):
            continue
        if prior is None and (status, gtd_flag, ir_flag) == ("ACTIVE", False, False):
            continue  # cleared before we ever held a status for him: nothing to record
        cursor.execute("INSERT INTO Live.PlayerStatus (ChangedAt, PlayerID, TeamID, Status, GameTimeDecision, "
                       "IREligible, Sources) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       at, player_id, team_id, status, gtd_flag, ir_flag, sources)
        written += 1
    return written
